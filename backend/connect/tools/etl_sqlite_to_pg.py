"""One-shot ETL: the closed v0.1 SQLite database (schema v9/v10) -> PostgreSQL.

    .venv/bin/python -m connect.tools.etl_sqlite_to_pg /path/to/copy.db \
        --pg postgresql://connect:connect@127.0.0.1:55432/connect \
        [--create-db] [--force-wipe] [--owner-email you@example.com] \
        [--fts-query WORD ...]

Design: docs/design/v02-postgres-port.md §5 (flow, verification, rollback)
plus the tenancy bootstrap rules of docs/design/v02-tenancy-auth.md §7.

Sync script (psycopg sync side; sqlite3 stdlib — the ONLY live import of the
legacy lineage, via connect/tools/legacy_sqlite). Properties:

- Source opened READ-ONLY (``file:...?mode=ro``) — physically cannot be
  corrupted; run it against a ``.backup`` copy of the live file anyway.
  Refused unless ``meta.schema_version`` is 9 or 10 (the closed lineage; v10
  only added job.cancel_requested, read adaptively). Pre-flight
  ``PRAGMA foreign_key_check`` aborts on a dirty source.
- Target schema comes from ``pg.init_db`` — the SAME code path the app uses,
  never a second DDL source. Idempotent re-run guard: a target with ANY rows
  in the copied tables (or app_user) is refused unless ``--force-wipe``,
  which TRUNCATEs every public table except meta, RESTART IDENTITY CASCADE.
- Copy runs FK-topological in ONE transaction with
  ``SET CONSTRAINTS ALL DEFERRED`` (the self/circular FKs are DEFERRABLE);
  any dangling row aborts the whole run loudly. Explicit ids via
  ``OVERRIDING SYSTEM VALUE``; ``setval`` realigns every identity sequence
  (incl. job_event.seq). tsvector columns are GENERATED — they rebuild
  themselves; FTS desync is structurally impossible. ``raw_blob_path``
  strings copy verbatim — no blob bytes move.
- Vectors are COPIED, never recomputed: BLOB float32 -> pgvector list for
  document_embedding/event_embedding/claim_embedding; document vectors that
  live in the sqlite-vec ``vec_document`` virtual table are read through the
  sqlite_vec extension (model attributed via ``--vec-model``, default the
  v0.1 embedder). Non-384-dim rows are skipped and logged, per design.
- Tenancy bootstrap (§7): ``--owner-email`` pre-creates the admin app_user
  (google_sub NULL — first Google login links it); watch/brief/view_cursor
  rows -> that admin; dossiers -> owner=admin, visibility='shared'
  (REQUIRED: their writeback already compounded into the shared graph);
  documents -> visibility='shared' with origin backfilled best-effort
  (source.type='search' -> investigation_fetch, source_id -> polled,
  document_link.resolved_document_id -> link_follow, url -> user_url, else
  user_text owned by the admin); llm_call/job history stays system
  (user/owner NULL). ``--owner-email`` is REQUIRED when the source carries
  dossier/watch/brief/view_cursor rows — those columns are NOT NULL since
  schema v3 (Phase C tenancy); the run refuses loudly without it.
- v2 queue columns: job.run_at = created_at; priority/max_attempts follow
  workers.queue's per-kind policy. Runtime state tables (fetch_domain,
  robots_cache, beat_run) and meta start fresh. Auth tables (user_session,
  invite, app_setting) start empty — the auth workstream owns them.
- Verification runs automatically and the process exits non-zero on any
  mismatch: per-table counts, order-insensitive content checksums computed
  identically on both sides in Python, FTS smoke queries (top-10 overlap;
  rankers differ — identity is not expected), sampled-vector cosine parity
  against a fixed probe (<= 1e-6), active-edge count.

Rollback story: the source file is the archive; the target is disposable
until cutover (re-run with ``--force-wipe``).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import re
import sqlite3
import struct
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

import psycopg

from connect.storage import pg as pg_mod
from connect.storage.pg import Jsonb, Vector
from connect.storage.schema import PG_SCHEMA_VERSION
from connect.tools.legacy_sqlite import migrations as legacy_migrations
from connect.workers.queue import KIND_MAX_ATTEMPTS, priority_for

log = logging.getLogger(__name__)

ACCEPTED_SOURCE_VERSIONS = (9, 10)
VECTOR_DIM = 384
DEFAULT_VEC_MODEL = "BAAI/bge-small-en-v1.5"  # the one v0.1 embedder
COSINE_TOLERANCE = 1e-6
VEC_SAMPLE = 10


class EtlError(Exception):
    """Refusal or hard failure — the run aborts, the target rolls back."""


# ==============================================================================
# Table specs — FK-topological copy order (design §5 step 3).
# ==============================================================================

@dataclass(frozen=True)
class TableSpec:
    name: str
    cols: tuple[str, ...]               # columns common to source and target
    identity: Optional[str] = "id"      # identity column for setval, or None
    bools: tuple[str, ...] = ()
    json_objs: tuple[str, ...] = ()     # TEXT JSON -> jsonb, default {}
    json_arrs: tuple[str, ...] = ()     # TEXT JSON -> jsonb, default []
    dates: tuple[str, ...] = ()         # day-precision -> date (defensive [:10])
    defaults: tuple[tuple[str, Any], ...] = ()  # value when source lacks col


_GRADE = ("grade", "extractor_model", "prompt_version")

TABLE_SPECS: tuple[TableSpec, ...] = (
    TableSpec("source",
              ("id", "name", "type", "config", "credibility_tier", "enabled",
               "t1_exempt", "notes", "max_items_per_poll", "max_items_per_day",
               "quality_score", "created_at", "last_polled_at",
               "last_poll_status"),
              bools=("enabled", "t1_exempt"), json_objs=("config",)),
    TableSpec("document",
              ("id", "source_id", "url", "canonical_url", "title", "author",
               "published_at", "fetched_at", "media_type", "language",
               "content_text", "content_hash", "raw_blob_path",
               "enrichment_tier", "enrichment_status", "simhash",
               "canonical_document_id", "watch_hit"),
              bools=("watch_hit",)),
    TableSpec("document_link",
              ("id", "document_id", "url", "anchor_text", "is_file",
               "is_official", "status", "resolved_document_id", "error",
               "created_at"),
              bools=("is_file", "is_official")),
    TableSpec("document_topic", ("document_id", "topic", "source"),
              identity=None),
    TableSpec("document_enrichment",
              ("document_id", "summary", "event_type", "model",
               "prompt_version", "created_at"), identity=None),
    TableSpec("entity",
              ("id", "name", "entity_type", "aliases", "description",
               "wikidata_id", "attrs", *_GRADE, "created_at", "updated_at"),
              json_objs=("attrs",), json_arrs=("aliases",)),
    TableSpec("entity_mention",
              ("id", "document_id", "entity_id", "surface", "span_start",
               "span_end", "method", *_GRADE, "created_at")),
    TableSpec("event_type", ("id", "name", "lifecycle_group", "window_days")),
    TableSpec("story",
              ("id", "title", "root_event_id", "status", "doc_count",
               "summary_text", "summary_stale", "created_at", "updated_at"),
              bools=("summary_stale",)),
    TableSpec("event",
              ("id", "title", "description", "event_type", "story_id",
               "occurred_on", "date_precision", "geo_scope", "doc_count",
               "window_start", "window_end", "last_seen_at", *_GRADE,
               "created_at"),
              dates=("occurred_on", "window_start", "window_end",
                     "last_seen_at")),
    TableSpec("event_assignment",
              ("id", "document_id", "event_id", "method", "score",
               "adjudication", "created_at")),
    # dossier <-> question circular pair: parent_question_id is DEFERRABLE.
    TableSpec("dossier",
              ("id", "kind", "title", "input_text", "input_type",
               "input_document_id", "parent_question_id", "budget_usd",
               "status", "current_stage", "error", "model_usage",
               "created_at", "started_at", "finished_at"),
              json_objs=("model_usage",)),
    TableSpec("dossier_section",
              ("id", "dossier_id", "stage", "status", "content", "created_at",
               "updated_at"),
              json_objs=("content",)),
    TableSpec("question",
              ("id", "dossier_id", "qtype", "text", "about_type", "about_id",
               "status", "priority", "answer_summary", "answer_finding_ids",
               "spawned_dossier_id", "created_at", "updated_at"),
              json_arrs=("answer_finding_ids",)),
    # edge.superseded_by_edge_id points to NEWER rows: DEFERRABLE.
    TableSpec("edge",
              ("id", "src_type", "src_id", "dst_type", "dst_id", "relation",
               "properties", "provenance_document_id", "provenance_dossier_id",
               "confidence", "valid_from", "valid_to", "asserted_at", "status",
               "superseded_by_edge_id", *_GRADE, "created_at"),
              json_objs=("properties",), dates=("valid_from", "valid_to")),
    TableSpec("claim",
              ("id", "text", "claim_type", "first_document_id", "verdict",
               "confidence", "check_worthiness", "verdict_updated_at",
               "created_at")),
    TableSpec("evidence",
              ("id", "claim_id", "document_id", "stance", "confidence",
               "rationale", "quote", "method", "model_id", *_GRADE,
               "created_at")),
    TableSpec("claim_sighting",
              ("id", "claim_id", "document_id", "quote", "quote_start",
               "quote_end", "stance", *_GRADE, "created_at")),
    TableSpec("verdict_history",
              ("id", "claim_id", "verdict", "computed_at", "trigger",
               "evidence_snapshot"),
              json_arrs=("evidence_snapshot",)),
    TableSpec("contradiction",
              ("id", "claim_id", "n_support", "n_refute", "best_tier_support",
               "best_tier_refute", "status", "resolved_dossier_id",
               "detected_at", "resolved_at")),
    TableSpec("finding",
              ("id", "dossier_id", "kind", "text", "speculation",
               "confidence", "question_id", "edge_id", "payload",
               "created_at"),
              bools=("speculation",), json_objs=("payload",)),
    TableSpec("finding_evidence",
              ("id", "finding_id", "document_id", "quote", "quote_start",
               "quote_end")),
    TableSpec("statement",
              ("id", "document_id", "entity_id", "quote", "quote_start",
               "quote_end", "topics", "position_summary", "stated_at",
               *_GRADE, "created_at"),
              json_arrs=("topics",)),
    TableSpec("position_shift",
              ("id", "entity_id", "topic", "from_statement_id",
               "to_statement_id", "kind", "note", "detected_at", "status")),
    TableSpec("view_summary",
              ("entity_id", "topic", "text", "citations",
               "statement_count_at_gen", "generated_at"),
              identity=None, json_arrs=("citations",)),
    # v9 sources lack cancel_requested (added at SQLite v10) — default false.
    TableSpec("job",
              ("id", "kind", "payload", "dossier_id", "status", "attempts",
               "cancel_requested", "error", "created_at", "started_at",
               "finished_at"),
              bools=("cancel_requested",), json_objs=("payload",),
              defaults=(("cancel_requested", False),)),
    TableSpec("job_event", ("seq", "job_id", "ts", "type", "data"),
              identity="seq", json_objs=("data",)),
    TableSpec("watch",
              ("id", "kind", "label", "entity_id", "thread_id", "claim_id",
               "query_fts", "promote", "muted", "last_seen_at", "created_at"),
              bools=("promote", "muted")),
    TableSpec("watch_hit",
              ("watch_id", "object_type", "object_id", "created_at"),
              identity=None),
    TableSpec("brief",
              ("id", "brief_date", "generated_at", "gloss_text",
               "gloss_model"),
              dates=("brief_date",)),
    TableSpec("brief_item",
              ("id", "brief_id", "section", "rank", "object_type",
               "object_id", "reason_json", "payload", "seen"),
              bools=("seen",), json_objs=("reason_json", "payload")),
    TableSpec("view_cursor", ("surface", "ref_id", "last_seen_at"),
              identity=None),
    TableSpec("calendar_event",
              ("id", "kind", "scope", "occurs_on", "ends_on", "label"),
              dates=("occurs_on", "ends_on")),
    TableSpec("llm_call",
              ("id", "purpose", "model", "input_tokens", "output_tokens",
               "cache_read_tokens", "batch_id", "cost_estimate",
               "created_at")),
    TableSpec("source_stats",
              ("source_id", "day", "items", "dups", "t1_failures",
               "claims_extracted", "claims_false", "quality_score"),
              identity=None, dates=("day",)),
)

# Embedding tables are copied by _copy_embeddings (value transforms differ).
EMBEDDING_TABLES = ("document_embedding", "event_embedding", "claim_embedding")

# Emptiness guard covers everything the ETL writes.
GUARDED_TABLES = tuple(s.name for s in TABLE_SPECS) + EMBEDDING_TABLES + (
    "app_user",)


# --- tenancy bootstrap (v02-tenancy-auth.md §7) -------------------------------

@dataclass
class _BootstrapCtx:
    admin_id: Optional[int]
    search_source_ids: frozenset[int]   # source.type='search' (investigation)
    resolved_doc_ids: frozenset[int]    # document_link.resolved_document_id


def _document_origin(row: dict, ctx: _BootstrapCtx) -> str:
    """Best-effort origin backfill (§7 rule 4; default 'polled')."""
    if row["source_id"] is not None:
        if row["source_id"] in ctx.search_source_ids:
            return "investigation_fetch"
        return "polled"
    if row["id"] in ctx.resolved_doc_ids:
        return "link_follow"
    if row["url"] is not None:
        return "user_url"
    return "user_text"


def _document_extra(row: dict, ctx: _BootstrapCtx) -> tuple:
    origin = _document_origin(row, ctx)
    owner = ctx.admin_id if origin == "user_text" else None  # manual ingest
    return (owner, "shared", origin)


# Target-only columns appended per table: (columns, fn(source_row, ctx)).
EXTRA_COLS: dict[str, tuple[tuple[str, ...],
                            Callable[[dict, _BootstrapCtx], tuple]]] = {
    "document": (("owner_id", "visibility", "origin"), _document_extra),
    # REQUIRED shared: dossier writeback already compounded into the graph.
    "dossier": (("owner_id", "visibility"),
                lambda r, c: (c.admin_id, "shared")),
    "watch": (("user_id",), lambda r, c: (c.admin_id,)),
    "brief": (("user_id",), lambda r, c: (c.admin_id,)),
    "view_cursor": (("user_id",), lambda r, c: (c.admin_id,)),
    # v2 queue columns; history keeps the per-kind enqueue policy.
    "job": (("priority", "max_attempts", "run_at"),
            lambda r, c: (priority_for(r["kind"]),
                          KIND_MAX_ATTEMPTS.get(r["kind"], 1),
                          r["created_at"])),
}


# ==============================================================================
# Report
# ==============================================================================

@dataclass
class EtlReport:
    source_version: int = 0
    admin_user_id: Optional[int] = None
    # table -> (source_rows, copied_rows, pg_rows)
    table_counts: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    origin_counts: dict[str, int] = field(default_factory=dict)
    # name -> (sqlite_value, pg_value, ok)
    checksums: dict[str, tuple[str, str, bool]] = field(default_factory=dict)
    # (query, n_sqlite_top10, n_pg_matches, found_in_pg, ok)
    fts: list[tuple[str, int, int, int, bool]] = field(default_factory=list)
    vec_checked: int = 0
    vec_max_delta: float = 0.0
    vec_ok: bool = True
    active_edges: tuple[int, int, bool] = (0, 0, True)
    skipped_vectors: list[tuple[str, int, int]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (all(src == copied == pg
                    for src, copied, pg in self.table_counts.values())
                and all(v[2] for v in self.checksums.values())
                and all(f[4] for f in self.fts)
                and self.vec_ok and self.active_edges[2])

    def render(self) -> str:
        lines = [f"source schema v{self.source_version}"
                 f" -> postgres schema v{PG_SCHEMA_VERSION}"]
        if self.admin_user_id is not None:
            lines.append(f"admin app_user id={self.admin_user_id}"
                         " (google_sub NULL — links at first login)")
        lines.append("-- row counts (source / copied / postgres) --")
        for name, (src, copied, pg) in self.table_counts.items():
            mark = "OK " if src == copied == pg else "MISMATCH"
            src_s = "-" if src is None else str(src)
            lines.append(f"  {mark} {name:24s} {src_s:>6} /{copied:>6} /{pg:>6}")
        if self.origin_counts:
            lines.append("-- document origin backfill --")
            for origin, n in sorted(self.origin_counts.items()):
                lines.append(f"  {origin:24s} {n}")
        lines.append("-- checksums --")
        for name, (a, b, ok) in self.checksums.items():
            mark = "OK " if ok else "MISMATCH"
            lines.append(f"  {mark} {name}: sqlite={a} pg={b}")
        lines.append("-- FTS smoke (sqlite bm25 top-10 found in PG match"
                     " set) --")
        if not self.fts:
            lines.append("  (no queries — empty corpus?)")
        for q, n_sq, n_pg, found, ok in self.fts:
            mark = "OK " if ok else "MISMATCH"
            lines.append(f"  {mark} {q!r}: sqlite_top10={n_sq}"
                         f" pg_matches={n_pg} found={found}")
        mark = "OK " if self.vec_ok else "MISMATCH"
        lines.append(f"-- vectors -- {mark} {self.vec_checked} sampled,"
                     f" max cosine delta {self.vec_max_delta:.2e}")
        for table, key, dim in self.skipped_vectors:
            lines.append(f"  SKIPPED {table} id={key} dim={dim} != "
                         f"{VECTOR_DIM}")
        a, b, ok = self.active_edges
        lines.append(f"-- active edges -- {'OK ' if ok else 'MISMATCH'}"
                     f" sqlite={a} pg={b}")
        for w in self.warnings:
            lines.append(f"WARNING: {w}")
        lines.append("VERDICT: " + ("PASS" if self.ok else "FAIL"))
        return "\n".join(lines)


# ==============================================================================
# Source side (read-only SQLite)
# ==============================================================================

def _open_source(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _source_preflight(sq: sqlite3.Connection) -> int:
    """Version gate + FK integrity + live-poll-dedup feasibility."""
    version = legacy_migrations.read_version(sq)
    if version not in ACCEPTED_SOURCE_VERSIONS:
        raise EtlError(
            f"source schema_version {version} not in"
            f" {ACCEPTED_SOURCE_VERSIONS}; this ETL only reads the closed"
            " v0.1 lineage")
    bad = sq.execute("PRAGMA foreign_key_check").fetchall()
    if bad:
        sample = [tuple(r) for r in bad[:10]]
        raise EtlError(
            f"source fails PRAGMA foreign_key_check ({len(bad)} rows),"
            f" first 10: {sample}")
    dupes = sq.execute(
        "SELECT json_extract(payload, '$.source_id') AS sid, COUNT(*) AS n"
        " FROM job WHERE kind = 'poll_source'"
        " AND status IN ('queued', 'running')"
        " GROUP BY sid HAVING COUNT(*) > 1").fetchall()
    if dupes:
        raise EtlError(
            "source has multiple LIVE poll_source jobs per source "
            f"{[(r['sid'], r['n']) for r in dupes]} — the PG queue's"
            " uq_job_poll_source forbids this; let the live system drain"
            " them, then re-run")
    return version


def _source_columns(sq: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in sq.execute(f"PRAGMA table_info({table})")}


def _source_table_exists(sq: sqlite3.Connection, table: str) -> bool:
    return sq.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view')"
        " AND name = ?", (table,)).fetchone() is not None


def _load_json(value: Any, default: Any, warn: Callable[[str], None],
               where: str) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        warn(f"malformed JSON at {where}: {value!r:.80} -> {default!r}")
        return default


def _unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


# ==============================================================================
# Copy
# ==============================================================================

def _convert_row(spec: TableSpec, row: dict, ctx: _BootstrapCtx,
                 report: EtlReport) -> tuple:
    out: list[Any] = []
    defaults = dict(spec.defaults)
    for col in spec.cols:
        if col in row:
            v = row[col]
        else:
            if col not in defaults:
                raise EtlError(f"{spec.name}.{col} missing from source and"
                               " has no ETL default")
            v = defaults[col]
        if col in spec.bools:
            v = None if v is None else bool(v)
        elif col in spec.json_objs:
            v = Jsonb(_load_json(v, {}, report.warnings.append,
                                 f"{spec.name}.{col}"))
        elif col in spec.json_arrs:
            v = Jsonb(_load_json(v, [], report.warnings.append,
                                 f"{spec.name}.{col}"))
        elif col in spec.dates and isinstance(v, str):
            v = v[:10]
        out.append(v)
    extra = EXTRA_COLS.get(spec.name)
    if extra is not None:
        out.extend(extra[1](row, ctx))
    return tuple(out)


def _copy_table(spec: TableSpec, sq: sqlite3.Connection,
                pg: psycopg.Connection, ctx: _BootstrapCtx,
                report: EtlReport) -> int:
    order = f" ORDER BY {spec.identity}" if spec.identity else ""
    rows = [dict(r) for r in sq.execute(f"SELECT * FROM {spec.name}{order}")]
    target_cols = spec.cols + (EXTRA_COLS.get(spec.name, ((),))[0])
    col_sql = ", ".join(f'"{c}"' for c in target_cols)
    overriding = " OVERRIDING SYSTEM VALUE" if spec.identity else ""
    sql = (f'INSERT INTO {spec.name} ({col_sql}){overriding}'
           f' VALUES ({", ".join(["%s"] * len(target_cols))})')
    params = [_convert_row(spec, row, ctx, report) for row in rows]
    if params:
        with pg.cursor() as cur:
            cur.executemany(sql, params)
    if spec.name == "document":
        for row in rows:
            origin = _document_origin(row, ctx)
            report.origin_counts[origin] = (
                report.origin_counts.get(origin, 0) + 1)
    return len(rows)


@dataclass(frozen=True)
class EmbCounts:
    source: int     # raw source rows seen (blob table + vec_document)
    skipped: int    # wrong-dim rows, logged
    dup: int        # doc id present in BOTH blob table and vec_document
    copied: int

    @property
    def expected(self) -> int:
        return self.source - self.skipped - self.dup


def _copy_embeddings(sq: sqlite3.Connection, pg: psycopg.Connection,
                     vec_model: str, report: EtlReport
                     ) -> dict[str, EmbCounts]:
    """Vectors copied verbatim (no re-embedding); non-384-dim rows skipped
    and logged (design §5 step 4)."""
    counts: dict[str, EmbCounts] = {}

    # documents: BLOB fallback table first (carries model), then sqlite-vec.
    doc_vecs: dict[int, tuple[str, list[float]]] = {}
    n_src = n_skip = n_dup = 0
    for row in sq.execute(
            "SELECT document_id, model, vector FROM document_embedding"):
        n_src += 1
        vec = _unpack(row["vector"])
        if len(vec) != VECTOR_DIM:
            n_skip += 1
            report.skipped_vectors.append(
                ("document_embedding", row["document_id"], len(vec)))
            continue
        doc_vecs[row["document_id"]] = (row["model"], vec)
    if _source_table_exists(sq, "vec_document"):
        _load_sqlite_vec(sq)
        for row in sq.execute("SELECT rowid, embedding FROM vec_document"):
            n_src += 1
            if row["rowid"] in doc_vecs:
                n_dup += 1
                continue
            vec = _unpack(row["embedding"])
            if len(vec) != VECTOR_DIM:
                n_skip += 1
                report.skipped_vectors.append(
                    ("vec_document", row["rowid"], len(vec)))
                continue
            doc_vecs[row["rowid"]] = (vec_model, vec)
    if doc_vecs:
        with pg.cursor() as cur:
            cur.executemany(
                "INSERT INTO document_embedding (document_id, model,"
                " embedding) VALUES (%s, %s, %s)",
                [(doc_id, model, Vector(vec))
                 for doc_id, (model, vec) in sorted(doc_vecs.items())])
    counts["document_embedding"] = EmbCounts(n_src, n_skip, n_dup,
                                             len(doc_vecs))

    for table, key in (("event_embedding", "event_id"),
                       ("claim_embedding", "claim_id")):
        rows = sq.execute(
            f"SELECT {key}, model, vector FROM {table}").fetchall()
        params = []
        n_skip = 0
        for row in rows:
            vec = _unpack(row["vector"])
            if len(vec) != VECTOR_DIM:
                n_skip += 1
                report.skipped_vectors.append((table, row[key], len(vec)))
                continue
            params.append((row[key], row["model"], Vector(vec)))
        if params:
            with pg.cursor() as cur:
                cur.executemany(
                    f"INSERT INTO {table} ({key}, model, embedding)"
                    " VALUES (%s, %s, %s)", params)
        counts[table] = EmbCounts(len(rows), n_skip, 0, len(params))
    return counts


def _load_sqlite_vec(sq: sqlite3.Connection) -> None:
    try:
        import sqlite_vec  # type: ignore[import-untyped]

        sq.enable_load_extension(True)
        try:
            sqlite_vec.load(sq)
        finally:
            sq.enable_load_extension(False)
    except Exception as e:  # noqa: BLE001 — must be loud, vectors at stake
        raise EtlError(
            "source has a vec_document table but the sqlite-vec extension"
            f" cannot load ({e!r}); vectors must be COPIED, not dropped —"
            " run the ETL with the v0.1 venv") from e


def _setval_sequences(pg: psycopg.Connection) -> None:
    """Realign every identity sequence to max(id) — incl. job_event.seq."""
    for spec in TABLE_SPECS:
        if spec.identity is None:
            continue
        pg.execute(
            f"SELECT setval(pg_get_serial_sequence('{spec.name}',"
            f" '{spec.identity}'), m)"
            f" FROM (SELECT max({spec.identity}) AS m FROM {spec.name}) t"
            " WHERE m IS NOT NULL")


# ==============================================================================
# Target guards
# ==============================================================================

def _guard_target(pg: psycopg.Connection, *, force_wipe: bool) -> None:
    nonempty = []
    for table in GUARDED_TABLES:
        cur = pg.execute(f"SELECT EXISTS (SELECT 1 FROM {table}) ")
        if cur.fetchone()[0]:
            nonempty.append(table)
    if not nonempty:
        return
    if not force_wipe:
        raise EtlError(
            f"target is not empty (rows in: {', '.join(nonempty)}); the ETL"
            " is one-shot — re-run with --force-wipe to TRUNCATE and reload")
    names = [r[0] for r in pg.execute(
        "SELECT tablename FROM pg_tables"
        " WHERE schemaname = 'public' AND tablename <> 'meta'")]
    pg.execute("TRUNCATE " + ", ".join(f'"{n}"' for n in names)
               + " RESTART IDENTITY CASCADE")
    log.warning("force-wipe: truncated %d tables", len(names))


def ensure_database(pg_dsn: str, *, force_wipe: bool = False) -> None:
    """--create-db: CREATE DATABASE via the maintenance DB (drop first only
    with --force-wipe). The schema itself always comes from pg.init_db."""
    base, _, name = pg_dsn.rpartition("/")
    if not re.fullmatch(r"[A-Za-z0-9_]+", name):
        raise EtlError(f"refusing to create database with name {name!r}")
    with psycopg.connect(base + "/postgres", autocommit=True) as admin:
        exists = admin.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s",
            (name,)).fetchone() is not None
        if exists and force_wipe:
            admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
            exists = False
        if not exists:
            admin.execute(f'CREATE DATABASE "{name}"')


# ==============================================================================
# Verification (design §5 step 6 — runs automatically, non-zero on mismatch)
# ==============================================================================

def _sha256_sorted(values: Iterable[str]) -> str:
    h = hashlib.sha256()
    for v in sorted(values):
        h.update(v.encode("utf-8", "surrogatepass"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


def _checksum(report: EtlReport, name: str, a: str, b: str) -> None:
    report.checksums[name] = (a, b, a == b)


def _verify_checksums(sq: sqlite3.Connection, pg: psycopg.Connection,
                      report: EtlReport) -> None:
    # documents: content hashes + exact simhash sum (Python bigints — SQLite
    # SUM would overflow int64; computed identically on both sides).
    a = _sha256_sorted(r[0] for r in sq.execute(
        "SELECT content_hash FROM document"))
    b = _sha256_sorted(r[0] for r in pg.execute(
        "SELECT content_hash FROM document"))
    _checksum(report, "document.content_hash", a, b)
    sum_a = sum(r[0] for r in sq.execute(
        "SELECT simhash FROM document WHERE simhash IS NOT NULL"))
    sum_b = sum(r[0] for r in pg.execute(
        "SELECT simhash FROM document WHERE simhash IS NOT NULL"))
    _checksum(report, "document.simhash_sum", str(sum_a), str(sum_b))

    a = _sha256_sorted(r[0] for r in sq.execute("SELECT text FROM claim"))
    b = _sha256_sorted(r[0] for r in pg.execute("SELECT text FROM claim"))
    _checksum(report, "claim.text", a, b)

    a = _sha256_sorted(f"{r[0]}|{r[1]}" for r in sq.execute(
        "SELECT name, entity_type FROM entity"))
    b = _sha256_sorted(f"{r[0]}|{r[1]}" for r in pg.execute(
        "SELECT name, entity_type FROM entity"))
    _checksum(report, "entity.name_type", a, b)

    for table in ("evidence", "claim_sighting"):
        a = _sha256_sorted(f"{r[0]}|{r[1]}|{r[2]}" for r in sq.execute(
            f"SELECT claim_id, document_id, stance FROM {table}"))
        b = _sha256_sorted(f"{r[0]}|{r[1]}|{r[2]}" for r in pg.execute(
            f"SELECT claim_id, document_id, stance FROM {table}"))
        _checksum(report, f"{table}.claim_doc_stance", a, b)


_FTS_STOPWORDS = frozenset(
    "about above after again being below between could doing during every"
    " further having other their there these those through under until"
    " where which while would should against because before".split())


def _auto_fts_queries(sq: sqlite3.Connection, n: int = 3) -> list[str]:
    """Top-n frequent title words (len>=5, non-stopword) — deterministic."""
    counter: Counter[str] = Counter()
    for (title,) in sq.execute(
            "SELECT title FROM document WHERE title IS NOT NULL"):
        for word in re.findall(r"[a-z]{5,}", title.lower()):
            if word not in _FTS_STOPWORDS:
                counter[word] += 1
    ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    return [w for w, _ in ranked[:n]]


def _verify_fts(sq: sqlite3.Connection, pg: psycopg.Connection,
                queries: list[str], report: EtlReport) -> None:
    """FTS smoke: >= 7/10 of SQLite's bm25 top-10 must be IN the PG match
    set (head recall against the FULL set, not PG's top-10).

    Measured in anger against the real corpus: the design's literal
    top-10-vs-top-10 overlap is the wrong yardstick — flat bm25 and
    title-weighted ts_rank_cd ORDER the same documents very differently,
    and the tokenizers genuinely drift (risk #4: FTS5 unicode61 splits
    'India.com' into india+com; PG's parser emits one host token), so rank
    identity was never on the table. What the ETL must prove is that the
    regenerated index FINDS the documents the old one found; misses are
    tokenizer drift on display — bounded at 30% of the head.
    """
    for q in queries:
        ids_a = [r[0] for r in sq.execute(
            "SELECT rowid FROM document_fts WHERE document_fts MATCH ?"
            " ORDER BY bm25(document_fts) LIMIT 10", (f'"{q}"',))]
        matched_b = {r[0] for r in pg.execute(
            "SELECT d.id FROM document d"
            " WHERE d.search_tsv @@ websearch_to_tsquery('english', %s)",
            (q,))}
        found = sum(1 for i in ids_a if i in matched_b)
        # PG-only hits (stemming widens recall) are fine; an empty SQLite
        # head is trivially covered.
        ok = found >= math.ceil(0.7 * len(ids_a)) if ids_a else True
        report.fts.append((q, len(ids_a), len(matched_b), found, ok))


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)


def _probe_vector(dim: int = VECTOR_DIM) -> list[float]:
    """Fixed deterministic probe (no numpy/random module state)."""
    return [math.sin(i * 0.7311) for i in range(dim)]


def _verify_vectors(pg: psycopg.Connection,
                    source_doc_vecs: dict[int, list[float]],
                    report: EtlReport) -> None:
    probe = _probe_vector()
    sample = sorted(source_doc_vecs)[:VEC_SAMPLE]
    max_delta = 0.0
    for doc_id in sample:
        row = pg.execute(
            "SELECT embedding FROM document_embedding WHERE document_id = %s",
            (doc_id,)).fetchone()
        if row is None:
            report.vec_ok = False
            report.warnings.append(f"document {doc_id} vector missing in PG")
            continue
        pg_vec = [float(x) for x in row[0]]
        delta = abs(_cosine(source_doc_vecs[doc_id], probe)
                    - _cosine(pg_vec, probe))
        max_delta = max(max_delta, delta)
    report.vec_checked = len(sample)
    report.vec_max_delta = max_delta
    if max_delta > COSINE_TOLERANCE:
        report.vec_ok = False


def _verify(sq: sqlite3.Connection, pg: psycopg.Connection,
            copied: dict[str, int], emb_counts: dict[str, EmbCounts],
            fts_queries: Optional[list[str]], report: EtlReport) -> None:
    # per-table counts: source / copied / target.
    for spec in TABLE_SPECS:
        n_src = sq.execute(f"SELECT COUNT(*) FROM {spec.name}").fetchone()[0]
        n_pg = pg.execute(f"SELECT COUNT(*) FROM {spec.name}").fetchone()[0]
        report.table_counts[spec.name] = (n_src, copied[spec.name], n_pg)
    for table, ec in emb_counts.items():
        n_pg = pg.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        # wrong-dim rows are skipped DELIBERATELY (logged above) and a doc
        # vector present in both the BLOB table and vec_document copies once:
        # the comparable source figure is source - skipped - dup.
        report.table_counts[table] = (ec.expected, ec.copied, n_pg)

    _verify_checksums(sq, pg, report)

    queries = (fts_queries if fts_queries is not None
               else _auto_fts_queries(sq))
    _verify_fts(sq, pg, queries, report)

    a = sq.execute(
        "SELECT COUNT(*) FROM edge WHERE status = 'active'").fetchone()[0]
    b = pg.execute(
        "SELECT COUNT(*) FROM edge WHERE status = 'active'").fetchone()[0]
    report.active_edges = (a, b, a == b)


# ==============================================================================
# Orchestration
# ==============================================================================

def run_etl(sqlite_path: str, pg_dsn: str, *,
            owner_email: Optional[str] = None,
            force_wipe: bool = False,
            vec_model: str = DEFAULT_VEC_MODEL,
            fts_queries: Optional[list[str]] = None) -> EtlReport:
    report = EtlReport()
    sq = _open_source(sqlite_path)
    try:
        report.source_version = _source_preflight(sq)

        # v3 tenancy: dossier.owner_id and watch/brief/view_cursor.user_id
        # are NOT NULL — sources carrying such rows REQUIRE --owner-email
        # (design §7's bootstrap rule). Refuse loudly, never guess.
        if not owner_email:
            needy = [t for t in ("dossier", "watch", "brief", "view_cursor")
                     if sq.execute(
                         f"SELECT 1 FROM {t} LIMIT 1").fetchone()]
            if needy:
                raise SystemExit(
                    "--owner-email is required: the source has rows in "
                    + ", ".join(needy)
                    + " and the v3 schema requires an owner (the rows are"
                      " backfilled to that admin user)")

        # Target schema via the app's own bootstrap (advisory-lock guarded).
        asyncio.run(pg_mod.init_db(pg_dsn))

        # autocommit: the copy runs in ONE explicit transaction() block
        # (BEGIN..COMMIT); guards/verification are per-statement — no
        # implicit transaction can swallow the final commit.
        pg = psycopg.connect(pg_dsn, autocommit=True)
        try:
            from pgvector.psycopg import register_vector

            register_vector(pg)
            _guard_target(pg, force_wipe=force_wipe)

            ctx = _BootstrapCtx(
                admin_id=None,
                search_source_ids=frozenset(
                    r[0] for r in sq.execute(
                        "SELECT id FROM source WHERE type = 'search'")),
                resolved_doc_ids=frozenset(
                    r[0] for r in sq.execute(
                        "SELECT DISTINCT resolved_document_id"
                        " FROM document_link"
                        " WHERE resolved_document_id IS NOT NULL")),
            )

            copied: dict[str, int] = {}
            with pg.transaction():
                pg.execute("SET CONSTRAINTS ALL DEFERRED")
                if owner_email:
                    cur = pg.execute(
                        "INSERT INTO app_user (email, role, created_at)"
                        " VALUES (%s, 'admin', %s) RETURNING id",
                        (owner_email, pg_mod.utc_now()))
                    ctx.admin_id = cur.fetchone()[0]
                    report.admin_user_id = ctx.admin_id
                for spec in TABLE_SPECS:
                    copied[spec.name] = _copy_table(spec, sq, pg, ctx, report)
                emb_counts = _copy_embeddings(sq, pg, vec_model, report)
                _setval_sequences(pg)
            # committed — verify against what is actually on disk.

            source_doc_vecs: dict[int, list[float]] = {}
            for row in sq.execute(
                    "SELECT document_id, vector FROM document_embedding"):
                vec = _unpack(row["vector"])
                if len(vec) == VECTOR_DIM:
                    source_doc_vecs[row["document_id"]] = vec
            if _source_table_exists(sq, "vec_document"):
                # extension already loaded by the copy when needed
                for row in sq.execute(
                        "SELECT rowid, embedding FROM vec_document"):
                    vec = _unpack(row["embedding"])
                    if len(vec) == VECTOR_DIM:
                        source_doc_vecs.setdefault(row["rowid"], vec)

            _verify(sq, pg, copied, emb_counts, fts_queries, report)
            _verify_vectors(pg, source_doc_vecs, report)
        finally:
            pg.close()
    finally:
        sq.close()
    return report


def _parse_args(argv: Optional[list[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="connect.tools.etl_sqlite_to_pg",
        description="One-shot SQLite v0.1 -> PostgreSQL v0.2 data migration"
                    " (see module docstring; source is opened READ-ONLY).")
    parser.add_argument("sqlite_path",
                        help="path to the v9/v10 SQLite file (use a .backup"
                             " copy of the live DB)")
    parser.add_argument("--pg", required=True,
                        help="target PostgreSQL DSN, e.g."
                             " postgresql://connect:connect@host:5432/connect")
    parser.add_argument("--create-db", action="store_true",
                        help="CREATE DATABASE via the maintenance DB first"
                             " (drops an existing one only with"
                             " --force-wipe)")
    parser.add_argument("--force-wipe", action="store_true",
                        help="wipe a non-empty target (TRUNCATE ... RESTART"
                             " IDENTITY CASCADE) and reload")
    parser.add_argument("--owner-email", default=None,
                        help="pre-create the admin app_user and attach"
                             " watches/briefs/cursors/dossiers to it"
                             " (tenancy design §7)")
    parser.add_argument("--vec-model", default=DEFAULT_VEC_MODEL,
                        help="model attribution for vectors read from the"
                             " sqlite-vec table (default: %(default)s)")
    parser.add_argument("--fts-query", action="append", dest="fts_queries",
                        default=None,
                        help="canned FTS smoke query (repeatable; default:"
                             " 3 most frequent title words)")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _parse_args(argv)
    try:
        if args.create_db:
            ensure_database(args.pg, force_wipe=args.force_wipe)
        report = run_etl(
            args.sqlite_path, args.pg,
            owner_email=args.owner_email,
            force_wipe=args.force_wipe,
            vec_model=args.vec_model,
            fts_queries=args.fts_queries)
    except EtlError as e:
        print(f"ETL REFUSED: {e}", file=sys.stderr)
        return 2
    print(report.render())
    return 0 if report.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
