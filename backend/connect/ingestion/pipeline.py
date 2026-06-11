"""The ingestion pipeline: fetch/extract -> dedup -> blob + row + FTS -> T0.

Three entry points (ingest_url / ingest_file / ingest_text) converge on one
``_store`` path:

1. normalize text, sha256 content_hash — exact dup returns the EXISTING
   document (created=False; the API maps that to 200).
2. 64-bit simhash vs the last 14 days — near-dup gets
   canonical_document_id + enrichment_status='skipped_dup' (still stored:
   immutable snapshot discipline).
3. raw bytes -> content-addressed blob; row insert (FTS triggers index it).
4. T0 hooks: local embedding (lazy/optional, never fatal) + deterministic
   watch matching (sets document.watch_hit).
5. Phase 0.5, HTML only: in-content link extraction from the raw blob bytes
   (document_link rows) + selective auto-follow (link_follow.py) — both run
   AFTER the parent row is committed and never raise out of the ingest.
   ``is_link_follow=True`` marks a depth-1 ingest: links are still
   extracted/stored, but never auto-followed (no recursion).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Awaitable, Callable, Sequence

import psycopg

from connect.domain.models import DiscoveredItem, Document, IngestResult
from connect.ingestion import dedup, link_follow
from connect.ingestion import links as links_mod
from connect.ingestion.blobs import BlobStore
from connect.ingestion.extract_html import Extracted, ExtractionError, extract_html
from connect.ingestion.extract_office import (
    OFFICE_MEDIA_TYPES,
    detect_office_kind,
    extract_office,
)
from connect.ingestion.extract_pdf import extract_pdf
from connect.ingestion.fetcher import Fetcher
from connect.knowledge import watches as watch_matcher
from connect.knowledge.embedder import Embedder
from connect.knowledge.vector import VectorIndex
from connect.storage import documents as doc_dao
from connect.storage import links as link_dao
from connect.storage.pg import utc_now

log = logging.getLogger(__name__)

_EMBED_CHARS = 2000  # title + lede is plenty for a 384-dim doc vector


class IngestionPipeline:
    def __init__(self, *, fetcher: Fetcher,
                 blobs: BlobStore, embedder: Embedder, vectors: VectorIndex,
                 embeddings_enabled: bool = True,
                 dedup_window_days: int = 14, simhash_max_hamming: int = 3,
                 link_follow_enabled: bool = True,
                 link_follow_max_per_doc: int = 5,
                 link_max_per_doc: int = links_mod.MAX_LINKS_PER_DOC,
                 official_domains: Sequence[str] = (),
                 enrich_fast_path: Callable[
                     [psycopg.AsyncConnection, int, int | None, bool],
                     Awaitable[None]] | None = None):
        self.fetcher = fetcher
        self.blobs = blobs
        self.embedder = embedder
        self.vectors = vectors
        self.embeddings_enabled = embeddings_enabled
        self.dedup_window_days = dedup_window_days
        self.simhash_max_hamming = simhash_max_hamming
        self.link_follow_enabled = link_follow_enabled
        self.link_follow_max_per_doc = link_follow_max_per_doc
        self.link_max_per_doc = link_max_per_doc
        self.official_domains = tuple(official_domains)
        # Phase 1: (document_id, source_id, watch_hit) -> queue a sync T1
        # enrich job for watch-hit / fact-checker docs. Wired by the
        # composition root; never fatal to the ingest.
        self.enrich_fast_path = enrich_fast_path

    # -- entry points -----------------------------------------------------------

    async def ingest_url(self, conn: psycopg.AsyncConnection, url: str,
                         source_id: int | None = None,
                         title: str | None = None, *,
                         published_at_hint: str | None = None,
                         is_link_follow: bool = False) -> IngestResult:
        """``title`` (e.g. the RSS entry headline) overrides the extracted
        page title — feed titles are editor-curated, while page <title>s can
        be generic shells (RBI's circular pages all say "Notifications").
        ``published_at_hint`` (the feed item's pubDate) likewise WINS over
        extracted metadata — PIB pages carry wrong embedded dates; the
        extracted date stays as the fallback when no hint exists."""
        result = await self.fetcher.fetch(url)
        ctype = result.content_type.lower()
        is_pdf = ("pdf" in ctype) or result.final_url.lower().endswith(".pdf")
        office_kind = None if is_pdf else detect_office_kind(
            content_type=ctype, name=result.final_url, data=result.content)
        if is_pdf:
            extracted = await asyncio.to_thread(extract_pdf, result.content)
            media_type = "pdf"
        elif office_kind is not None:
            # gov annexes: .xlsx/.xls/.docx — an extraction failure raises
            # ExtractionError, the exact failure path of a bad PDF (link
            # rows flip to 'failed', no document row).
            extracted = await asyncio.to_thread(
                extract_office, result.content, office_kind)
            media_type = OFFICE_MEDIA_TYPES[office_kind]
        else:
            extracted = await asyncio.to_thread(
                extract_html, result.content, result.final_url)
            media_type = "html"
        if title:
            extracted = replace(extracted, title=title)
        if published_at_hint:
            extracted = replace(extracted, published_at=published_at_hint)
        stored = await self._store(
            conn, extracted, raw=result.content, media_type=media_type,
            url=url, canonical_url=result.final_url, source_id=source_id)
        if stored.created and media_type == "html":
            await self._process_links(
                conn, stored.document, raw=result.content,
                base_url=result.final_url, is_link_follow=is_link_follow)
        return stored

    async def ingest_file(self, conn: psycopg.AsyncConnection,
                          filename: str, data: bytes,
                          content_type: str | None = None) -> IngestResult:
        name = filename.lower()
        ctype = (content_type or "").lower()
        office_kind = detect_office_kind(
            content_type=ctype, name=filename, data=data)
        if name.endswith(".pdf") or "pdf" in ctype:
            extracted = await asyncio.to_thread(extract_pdf, data)
            media_type = "pdf"
        elif office_kind is not None:
            extracted = await asyncio.to_thread(extract_office, data,
                                                office_kind)
            media_type = OFFICE_MEDIA_TYPES[office_kind]
        elif name.endswith((".html", ".htm")) or "html" in ctype:
            extracted = await asyncio.to_thread(extract_html, data)
            media_type = "html"
        else:
            text = data.decode("utf-8", errors="replace").strip()
            if not text:
                raise ExtractionError("file contains no text")
            extracted = Extracted(text=text, title=filename)
            media_type = "text"
        if extracted.title is None:
            extracted = Extracted(
                text=extracted.text, title=filename, author=extracted.author,
                published_at=extracted.published_at, language=extracted.language)
        stored = await self._store(conn, extracted, raw=data,
                                   media_type=media_type)
        if stored.created and media_type == "html":
            # No base URL for an upload: only absolute links are extractable.
            await self._process_links(
                conn, stored.document, raw=data, base_url=None,
                is_link_follow=False)
        return stored

    async def ingest_prefetched(self, conn: psycopg.AsyncConnection,
                                item: DiscoveredItem, *,
                                source_id: int | None = None) -> IngestResult:
        """Adapter-supplied full content (tweets, telegram posts): the item
        URL is never fetched — x.com would block it and the payload already
        carries text, author, timestamp and out-links. The provider payload
        (``item.raw``) becomes the raw blob; ``item.link_urls`` become
        document_link rows (classified official/file), then auto-followed
        under the normal policy/caps — these are depth-0 documents."""
        text = (item.content_text or "").strip()
        if not text:
            raise ExtractionError("prefetched item has no content_text")
        extracted = Extracted(
            text=text, title=item.title or None, author=item.author,
            published_at=item.published_at)
        raw = item.raw if item.raw is not None else text.encode("utf-8")
        stored = await self._store(
            conn, extracted, raw=raw, media_type=item.media_type or "text",
            url=item.url, canonical_url=item.url, source_id=source_id)
        if stored.created and item.link_urls:
            await self._process_link_urls(conn, stored.document,
                                          item.link_urls)
        return stored

    async def ingest_text(self, conn: psycopg.AsyncConnection, text: str,
                          title: str | None = None,
                          source_id: int | None = None) -> IngestResult:
        cleaned = text.strip()
        if not cleaned:
            raise ExtractionError("empty text")
        if not title:
            title = cleaned.splitlines()[0][:120]
        extracted = Extracted(text=cleaned, title=title)
        return await self._store(
            conn, extracted, raw=text.encode("utf-8"), media_type="text",
            source_id=source_id)

    # -- the one store path -------------------------------------------------------

    async def _store(self, conn: psycopg.AsyncConnection,
                     extracted: Extracted, *, raw: bytes, media_type: str,
                     url: str | None = None,
                     canonical_url: str | None = None,
                     source_id: int | None = None) -> IngestResult:
        normalized = dedup.normalize_text(extracted.text)
        chash = dedup.content_hash(normalized)

        existing = await doc_dao.get_by_hash(conn, chash)
        if existing is not None:
            return IngestResult(document=existing, created=False)

        fingerprint = dedup.simhash64(normalized)
        canonical_id = await dedup.find_near_duplicate(
            conn, fingerprint,
            window_days=self.dedup_window_days,
            max_hamming=self.simhash_max_hamming)
        status = "skipped_dup" if canonical_id is not None else "pending"

        blob_path = self.blobs.put(raw)
        doc_id = await doc_dao.insert(conn, {
            "source_id": source_id,
            "url": url,
            "canonical_url": canonical_url,
            "title": extracted.title,
            "author": extracted.author,
            "published_at": extracted.published_at,
            "fetched_at": utc_now(),
            "media_type": media_type,
            "language": extracted.language,
            "content_text": extracted.text,
            "content_hash": chash,
            "raw_blob_path": blob_path,
            "enrichment_status": status,
            "simhash": dedup.to_signed64(fingerprint),
            "canonical_document_id": canonical_id,
        })

        # T0 hooks — never fatal to the ingest.
        await self._embed(conn, doc_id, extracted)
        hits: list[int] = []
        try:
            hits = await watch_matcher.match_document(conn, doc_id)
        except Exception:  # noqa: BLE001
            log.exception("watch matching failed for document %s", doc_id)

        # Phase 1 fast path: watch-hit / fact-checker docs jump the nightly
        # batch — only for docs still 'pending' (near-dups stay T0-only).
        if self.enrich_fast_path is not None and status == "pending":
            try:
                await self.enrich_fast_path(conn, doc_id, source_id,
                                            bool(hits))
            except Exception:  # noqa: BLE001
                log.exception("enrich fast path failed for document %s",
                              doc_id)

        document = await doc_dao.get(conn, doc_id)
        assert document is not None
        return IngestResult(document=document, created=True)

    # -- Phase 0.5: links -----------------------------------------------------

    async def _process_links(self, conn: psycopg.AsyncConnection,
                             document: Document, *, raw: bytes,
                             base_url: str | None,
                             is_link_follow: bool) -> None:
        """Extract+store in-content links, then (depth 0 only) auto-follow.

        Runs after the parent row is committed (_store's insert is its own
        transaction) — a crash mid-follow loses nothing. Never fatal.
        """
        try:
            kept = links_mod.collect_links(
                raw, base_url=base_url, content_text=document.content_text,
                self_urls=(document.url, base_url),
                official_domains=self.official_domains,
                max_links=self.link_max_per_doc)
            await link_dao.insert_links(conn, document.id, kept)
        except Exception:  # noqa: BLE001 — link extraction is best-effort
            log.exception("link extraction failed for document %s", document.id)
            return
        if is_link_follow or not self.link_follow_enabled:
            return
        try:
            await link_follow.auto_follow(
                conn, self, parent_id=document.id,
                parent_url=base_url or document.url,
                max_per_doc=self.link_follow_max_per_doc)
        except Exception:  # noqa: BLE001 — follow failures stay on link rows
            log.exception("link auto-follow failed for document %s", document.id)

    async def _process_link_urls(self, conn: psycopg.AsyncConnection,
                                 document: Document,
                                 urls: Sequence[str]) -> None:
        """document_link rows from adapter-supplied URL lists + auto-follow
        (the prefetched twin of _process_links). Never fatal."""
        try:
            kept = links_mod.classify_urls(
                urls, self_urls=(document.url,),
                official_domains=self.official_domains,
                max_links=self.link_max_per_doc)
            await link_dao.insert_links(conn, document.id, kept)
        except Exception:  # noqa: BLE001 — link storage is best-effort
            log.exception("link storage failed for document %s", document.id)
            return
        if not self.link_follow_enabled:
            return
        try:
            await link_follow.auto_follow(
                conn, self, parent_id=document.id,
                parent_url=document.url,
                max_per_doc=self.link_follow_max_per_doc)
        except Exception:  # noqa: BLE001 — follow failures stay on link rows
            log.exception("link auto-follow failed for document %s", document.id)

    async def _embed(self, conn: psycopg.AsyncConnection, doc_id: int,
                     extracted: Extracted) -> None:
        if not self.embeddings_enabled:
            return
        try:
            text = f"{extracted.title or ''}\n{extracted.text[:_EMBED_CHARS]}"
            vectors = self.embedder.embed([text])
            if vectors:
                await self.vectors.add(conn, doc_id, vectors[0],
                                       self.embedder.model_name)
        except Exception:  # noqa: BLE001
            log.exception("embedding failed for document %s", doc_id)
