"""Backfill in-content links for documents ingested before Phase 0.5.

    .venv/bin/python -m connect.tools.backfill_links [--follow] [--db DSN]

Iterates HTML documents that have a raw blob and ZERO document_link rows,
extracts + classifies + stores their links; with --follow it also
auto-follows per the normal policy (official first, same-domain files,
per-doc cap). Prints a summary.

Wiring mirrors the composition root (same Settings, same construction) but
deliberately skips the JobRunner (whose orphan reconciliation would mark a
LIVE server's running jobs failed), the poller and the seeds — this tool
must be safe to run while a server has the database open (PG MVCC makes
the concurrent writes harmless; each insert is its own transaction).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from connect.ingestion import link_follow
from connect.ingestion import links as links_mod
from connect.ingestion.blobs import BlobStore
from connect.ingestion.fetcher import Fetcher
from connect.ingestion.pipeline import IngestionPipeline
from connect.knowledge.embedder import Embedder, FastEmbedEmbedder, NullEmbedder
from connect.knowledge.vector import create_vector_index
from connect.orchestration.config import Settings
from connect.storage import links as link_dao
from connect.storage import pg as pg_mod

log = logging.getLogger(__name__)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="connect.tools.backfill_links", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--follow", action="store_true",
                        help="also auto-follow per policy after extraction")
    parser.add_argument("--db", type=str, default=None,
                        help="DSN (default: settings CONNECT_DATABASE_URL)")
    return parser.parse_args(argv)


async def run(settings: Settings, *, follow: bool) -> dict[str, int]:
    version = await pg_mod.init_db(settings.database_url)
    conn = await pg_mod.connect(settings.database_url)
    blobs = BlobStore(settings.blob_dir)
    embedder: Embedder = (FastEmbedEmbedder() if settings.embeddings_enabled
                          else NullEmbedder())
    fetcher = Fetcher(
        per_domain_interval=settings.fetch_per_domain_interval,
        timeout_seconds=settings.fetch_timeout_seconds,
        max_bytes=settings.fetch_max_bytes,
        respect_robots=settings.fetch_respect_robots,
    )
    pipeline = IngestionPipeline(
        fetcher=fetcher,
        blobs=blobs,
        embedder=embedder,
        vectors=create_vector_index(enabled=settings.embeddings_enabled),
        embeddings_enabled=settings.embeddings_enabled,
        dedup_window_days=settings.dedup_window_days,
        simhash_max_hamming=settings.simhash_max_hamming,
        link_follow_enabled=settings.link_follow_enabled,
        link_follow_max_per_doc=settings.link_follow_max_per_doc,
        link_max_per_doc=settings.link_max_per_doc,
        official_domains=settings.link_official_domains,
    )
    counts = {"scanned": 0, "links_stored": 0,
              "followed_ok": 0, "followed_failed": 0}
    try:
        cur = await conn.execute(
            "SELECT d.id, d.url, d.canonical_url, d.content_text,"
            "       d.raw_blob_path"
            " FROM document d"
            " WHERE d.media_type = 'html' AND d.raw_blob_path IS NOT NULL"
            "   AND NOT EXISTS (SELECT 1 FROM document_link l"
            "                   WHERE l.document_id = d.id)"
            " ORDER BY d.id")
        rows = await cur.fetchall()
        print(f"db={settings.database_url} schema=v{version} "
              f"candidates={len(rows)} follow={follow}")
        for row in rows:
            counts["scanned"] += 1
            if not blobs.exists(row["raw_blob_path"]):
                log.warning("document %s: blob %s missing, skipped",
                            row["id"], row["raw_blob_path"])
                continue
            raw = blobs.get(row["raw_blob_path"])
            base_url = row["canonical_url"] or row["url"]
            try:
                kept = links_mod.collect_links(
                    raw, base_url=base_url,
                    content_text=row["content_text"],
                    self_urls=(row["url"], row["canonical_url"]),
                    official_domains=settings.link_official_domains,
                    max_links=settings.link_max_per_doc)
                counts["links_stored"] += await link_dao.insert_links(
                    conn, row["id"], kept)
            except Exception:  # noqa: BLE001 — one bad blob must not stop the sweep
                log.exception("document %s: link extraction failed", row["id"])
                continue
            if follow:
                ok, failed = await link_follow.auto_follow(
                    conn, pipeline, parent_id=row["id"], parent_url=base_url,
                    max_per_doc=settings.link_follow_max_per_doc)
                counts["followed_ok"] += ok
                counts["followed_failed"] += failed
    finally:
        await fetcher.aclose()
        await conn.close()
    return counts


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    overrides: dict = {"poller_enabled": False}
    if args.db is not None:
        overrides["database_url"] = args.db
    settings = Settings(**overrides)
    counts = asyncio.run(run(settings, follow=args.follow))
    print(f"docs scanned:    {counts['scanned']}\n"
          f"links stored:    {counts['links_stored']}\n"
          f"followed ok:     {counts['followed_ok']}\n"
          f"followed failed: {counts['followed_failed']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
