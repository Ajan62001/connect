"""The storage contract — the ONLY module in the codebase that contains DDL.

Schema v1 is the FULL schema (core + KB tables) so later phases only ever
add migrations, never rewrite. Every CHECK vocabulary is built from
domain/enums.py — the single home of controlled vocabularies.

Conventions (log_buster discipline):
- INTEGER PRIMARY KEY ids everywhere (rowid-backed).
- Timestamps are ISO-8601 UTC TEXT.
- FTS5 external-content table over document(title, content_text), trigger-
  maintained.
- ``SCHEMA_VERSION`` is an integer starting at 1; migrations.py reconciles
  older DBs forward and refuses newer ones.
- The sqlite-vec virtual table DDL lives here too (VEC_DOCUMENT_DDL) but is
  executed only by knowledge/vector.py when the extension actually loads —
  vector availability must never break startup.
"""

from __future__ import annotations

import sqlite3

from connect.domain import enums as E

SCHEMA_VERSION = 9


# --- error hierarchy ---------------------------------------------------------

class StorageError(Exception):
    """Base for every storage failure; a failed DB write is fatal."""


class StorageVersionError(StorageError):
    """DB schema_version is newer than this code's — refuse to open."""


# --- DDL ----------------------------------------------------------------------

_DDL_META = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)"""

_DDL_SOURCE = f"""
CREATE TABLE IF NOT EXISTS source (
    id                 INTEGER PRIMARY KEY,
    name               TEXT NOT NULL UNIQUE,
    type               TEXT NOT NULL CHECK (type IN {E.sql_in(E.SOURCE_TYPES)}),
    config             TEXT NOT NULL DEFAULT '{{}}',
    credibility_tier   INTEGER NOT NULL DEFAULT 3
                       CHECK (credibility_tier IN {E.sql_in(E.CREDIBILITY_TIERS)}),
    enabled            INTEGER NOT NULL DEFAULT 1,
    t1_exempt          INTEGER NOT NULL DEFAULT 0,
    notes              TEXT,
    max_items_per_poll INTEGER,
    max_items_per_day  INTEGER,
    quality_score      REAL,
    created_at         TEXT NOT NULL,
    last_polled_at     TEXT,
    last_poll_status   TEXT
)"""

_DDL_DOCUMENT = f"""
CREATE TABLE IF NOT EXISTS document (
    id                    INTEGER PRIMARY KEY,
    source_id             INTEGER REFERENCES source(id) ON DELETE SET NULL,
    url                   TEXT,
    canonical_url         TEXT,
    title                 TEXT,
    author                TEXT,
    published_at          TEXT,
    fetched_at            TEXT NOT NULL,
    media_type            TEXT NOT NULL DEFAULT 'html'
                          CHECK (media_type IN {E.sql_in(E.MEDIA_TYPES)}),
    language              TEXT,
    content_text          TEXT NOT NULL,
    content_hash          TEXT NOT NULL UNIQUE,
    raw_blob_path         TEXT,
    enrichment_tier       INTEGER NOT NULL DEFAULT 0,
    enrichment_status     TEXT NOT NULL DEFAULT 'pending'
                          CHECK (enrichment_status IN {E.sql_in(E.ENRICHMENT_STATUSES)}),
    simhash               INTEGER,
    canonical_document_id INTEGER REFERENCES document(id),
    watch_hit             INTEGER NOT NULL DEFAULT 0
)"""

_DDL_DOCUMENT_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_document_source ON document(source_id)",
    "CREATE INDEX IF NOT EXISTS idx_document_url ON document(url)",
    "CREATE INDEX IF NOT EXISTS idx_document_published ON document(published_at)",
    "CREATE INDEX IF NOT EXISTS idx_document_fetched ON document(fetched_at)",
    "CREATE INDEX IF NOT EXISTS idx_document_status ON document(enrichment_status)",
)

# FTS5 external-content table + sync triggers (the canonical FTS5 pattern).
_DDL_DOCUMENT_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS document_fts USING fts5(
    title, content_text,
    content='document', content_rowid='id'
)"""

_DDL_DOCUMENT_FTS_TRIGGERS = (
    """
CREATE TRIGGER IF NOT EXISTS document_ai AFTER INSERT ON document BEGIN
    INSERT INTO document_fts(rowid, title, content_text)
    VALUES (new.id, new.title, new.content_text);
END""",
    """
CREATE TRIGGER IF NOT EXISTS document_ad AFTER DELETE ON document BEGIN
    INSERT INTO document_fts(document_fts, rowid, title, content_text)
    VALUES ('delete', old.id, old.title, old.content_text);
END""",
    """
CREATE TRIGGER IF NOT EXISTS document_au
AFTER UPDATE OF title, content_text ON document BEGIN
    INSERT INTO document_fts(document_fts, rowid, title, content_text)
    VALUES ('delete', old.id, old.title, old.content_text);
    INSERT INTO document_fts(rowid, title, content_text)
    VALUES (new.id, new.title, new.content_text);
END""",
)

# v2: in-content links extracted from a document's raw HTML. Lineage of a
# followed link lives here (resolved_document_id) AND as a document-[links_to]->
# document edge — the table is the work queue, the edge is the graph.
_DDL_DOCUMENT_LINK = f"""
CREATE TABLE IF NOT EXISTS document_link (
    id                   INTEGER PRIMARY KEY,
    document_id          INTEGER NOT NULL REFERENCES document(id),
    url                  TEXT NOT NULL,
    anchor_text          TEXT,
    is_file              INTEGER NOT NULL DEFAULT 0,
    is_official          INTEGER NOT NULL DEFAULT 0,
    status               TEXT NOT NULL DEFAULT 'not_followed'
                         CHECK (status IN {E.sql_in(E.LINK_STATUSES)}),
    resolved_document_id INTEGER REFERENCES document(id),
    error                TEXT,
    created_at           TEXT,
    UNIQUE (document_id, url)
)"""

_DDL_DOCUMENT_LINK_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_document_link_document"
    " ON document_link(document_id)",
    "CREATE INDEX IF NOT EXISTS idx_document_link_resolved"
    " ON document_link(resolved_document_id)",
)

# Exported for migrations.py (migrate_1_to_2 mirrors the fresh-create DDL —
# schema.py stays the single source of truth).
DOCUMENT_LINK_DDL: tuple[str, ...] = (
    _DDL_DOCUMENT_LINK, *_DDL_DOCUMENT_LINK_INDEXES)

# Exported for migrations.py (migrate_2_to_3 rebuilds source & document with
# the EXACT fresh-create DDL strings, so migrated and fresh DBs declare
# byte-identical tables; the index/trigger tuples are recreated after the
# rebuild because DROP TABLE removes them with the table).
SOURCE_TABLE_DDL: str = _DDL_SOURCE
DOCUMENT_TABLE_DDL: str = _DDL_DOCUMENT
DOCUMENT_INDEX_DDL: tuple[str, ...] = _DDL_DOCUMENT_INDEXES
DOCUMENT_FTS_TRIGGER_DDL: tuple[str, ...] = _DDL_DOCUMENT_FTS_TRIGGERS

_DDL_DOCUMENT_TOPIC = f"""
CREATE TABLE IF NOT EXISTS document_topic (
    document_id INTEGER NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    topic       TEXT NOT NULL,
    source      TEXT NOT NULL CHECK (source IN {E.sql_in(E.TOPIC_SOURCES)}),
    PRIMARY KEY (document_id, topic)
)"""

# v4: one row per T1-enriched document (summary + event_type + provenance).
# Topics/mentions/claims live in their own tables; this row is the marker
# that T1 ran, with which model/prompt — re-enrichment replaces it.
_DDL_DOCUMENT_ENRICHMENT = """
CREATE TABLE IF NOT EXISTS document_enrichment (
    document_id    INTEGER PRIMARY KEY REFERENCES document(id) ON DELETE CASCADE,
    summary        TEXT NOT NULL,
    event_type     TEXT NOT NULL,
    model          TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    created_at     TEXT NOT NULL
)"""

# Exported for migrations.py (migrate_3_to_4 creates the same table).
DOCUMENT_ENRICHMENT_DDL: tuple[str, ...] = (_DDL_DOCUMENT_ENRICHMENT,)

_DDL_CLAIM = f"""
CREATE TABLE IF NOT EXISTS claim (
    id                 INTEGER PRIMARY KEY,
    text               TEXT NOT NULL,
    claim_type         TEXT CHECK (claim_type IN {E.sql_in(E.CLAIM_TYPES)}),
    first_document_id  INTEGER REFERENCES document(id),
    verdict            TEXT NOT NULL DEFAULT 'unverified'
                       CHECK (verdict IN {E.sql_in(E.VERDICTS)}),
    confidence         REAL,
    check_worthiness   REAL,
    verdict_updated_at TEXT,
    created_at         TEXT NOT NULL
)"""

_DDL_CLAIM_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS claim_fts USING fts5(
    text,
    content='claim', content_rowid='id'
)"""

_DDL_CLAIM_FTS_TRIGGERS = (
    """
CREATE TRIGGER IF NOT EXISTS claim_ai AFTER INSERT ON claim BEGIN
    INSERT INTO claim_fts(rowid, text) VALUES (new.id, new.text);
END""",
    """
CREATE TRIGGER IF NOT EXISTS claim_ad AFTER DELETE ON claim BEGIN
    INSERT INTO claim_fts(claim_fts, rowid, text)
    VALUES ('delete', old.id, old.text);
END""",
    """
CREATE TRIGGER IF NOT EXISTS claim_au AFTER UPDATE OF text ON claim BEGIN
    INSERT INTO claim_fts(claim_fts, rowid, text)
    VALUES ('delete', old.id, old.text);
    INSERT INTO claim_fts(rowid, text) VALUES (new.id, new.text);
END""",
)

# Derived-knowledge tables all carry the provenance grade triple:
# grade (1=enriched 2=analyzed 3=curated) + extractor_model + prompt_version.
_GRADE_COLS = f"""
    grade           INTEGER NOT NULL DEFAULT 1 CHECK (grade IN {E.sql_in(E.GRADES)}),
    extractor_model TEXT,
    prompt_version  TEXT"""

_DDL_EVIDENCE = f"""
CREATE TABLE IF NOT EXISTS evidence (
    id          INTEGER PRIMARY KEY,
    claim_id    INTEGER NOT NULL REFERENCES claim(id) ON DELETE CASCADE,
    document_id INTEGER NOT NULL REFERENCES document(id),
    stance      TEXT NOT NULL CHECK (stance IN {E.sql_in(E.EVIDENCE_STANCES)}),
    confidence  REAL,
    rationale   TEXT,
    quote       TEXT,
    method      TEXT CHECK (method IN {E.sql_in(E.EVIDENCE_METHODS)}),
    model_id    TEXT,
{_GRADE_COLS},
    created_at  TEXT NOT NULL,
    UNIQUE (claim_id, document_id)
)"""

_DDL_ENTITY = f"""
CREATE TABLE IF NOT EXISTS entity (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    entity_type TEXT NOT NULL DEFAULT 'other'
                CHECK (entity_type IN {E.sql_in(E.ENTITY_TYPES)}),
    aliases     TEXT NOT NULL DEFAULT '[]',
    description TEXT,
    wikidata_id TEXT,
    attrs       TEXT NOT NULL DEFAULT '{{}}',
{_GRADE_COLS},
    created_at  TEXT NOT NULL,
    updated_at  TEXT,
    UNIQUE (name, entity_type)
)"""

_DDL_ENTITY_MENTION = f"""
CREATE TABLE IF NOT EXISTS entity_mention (
    id          INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    entity_id   INTEGER NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    surface     TEXT,
    span_start  INTEGER,
    span_end    INTEGER,
    method      TEXT CHECK (method IN {E.sql_in(E.MENTION_METHODS)}),
{_GRADE_COLS},
    created_at  TEXT NOT NULL
)"""

_DDL_ENTITY_MENTION_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_mention_entity ON entity_mention(entity_id, document_id)",
    "CREATE INDEX IF NOT EXISTS idx_mention_document ON entity_mention(document_id)",
)

_DDL_EVENT_TYPE = """
CREATE TABLE IF NOT EXISTS event_type (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    lifecycle_group TEXT,
    window_days     INTEGER NOT NULL DEFAULT 7
)"""

_DDL_STORY = f"""
CREATE TABLE IF NOT EXISTS story (
    id            INTEGER PRIMARY KEY,
    title         TEXT,
    root_event_id INTEGER,
    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN {E.sql_in(E.STORY_STATUSES)}),
    doc_count     INTEGER NOT NULL DEFAULT 0,
    summary_text  TEXT,
    summary_stale INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT
)"""

_DDL_EVENT = f"""
CREATE TABLE IF NOT EXISTS event (
    id             INTEGER PRIMARY KEY,
    title          TEXT NOT NULL,
    description    TEXT,
    event_type     TEXT NOT NULL DEFAULT 'other',
    story_id       INTEGER REFERENCES story(id),
    occurred_on    TEXT,
    date_precision TEXT CHECK (date_precision IN {E.sql_in(E.DATE_PRECISIONS)}),
    geo_scope      TEXT,
    doc_count      INTEGER NOT NULL DEFAULT 0,
    window_start   TEXT,
    window_end     TEXT,
    last_seen_at   TEXT,
{_GRADE_COLS},
    created_at     TEXT NOT NULL
)"""

_DDL_EVENT_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_event_story ON event(story_id)",
    "CREATE INDEX IF NOT EXISTS idx_event_type ON event(event_type)",
    "CREATE INDEX IF NOT EXISTS idx_event_occurred ON event(occurred_on)",
)

_DDL_EVENT_ASSIGNMENT = f"""
CREATE TABLE IF NOT EXISTS event_assignment (
    id           INTEGER PRIMARY KEY,
    document_id  INTEGER NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    event_id     INTEGER REFERENCES event(id),
    method       TEXT NOT NULL CHECK (method IN {E.sql_in(E.ASSIGNMENT_METHODS)}),
    score        REAL,
    adjudication TEXT,
    created_at   TEXT NOT NULL
)"""

# Polymorphic typed-edge table = the knowledge graph; bitemporal (SCD2):
# single-valued relations close the old edge (status='superseded'), never
# delete. Uniqueness applies only to ACTIVE edges (partial index below).
_DDL_EDGE = f"""
CREATE TABLE IF NOT EXISTS edge (
    id                      INTEGER PRIMARY KEY,
    src_type                TEXT NOT NULL CHECK (src_type IN {E.sql_in(E.NODE_TYPES)}),
    src_id                  INTEGER NOT NULL,
    dst_type                TEXT NOT NULL CHECK (dst_type IN {E.sql_in(E.NODE_TYPES)}),
    dst_id                  INTEGER NOT NULL,
    relation                TEXT NOT NULL,
    properties              TEXT NOT NULL DEFAULT '{{}}',
    provenance_document_id  INTEGER REFERENCES document(id),
    provenance_dossier_id   INTEGER REFERENCES dossier(id),
    confidence              REAL,
    valid_from              TEXT,
    valid_to                TEXT,
    asserted_at             TEXT,
    status                  TEXT NOT NULL DEFAULT 'active'
                            CHECK (status IN {E.sql_in(E.EDGE_STATUSES)}),
    superseded_by_edge_id   INTEGER REFERENCES edge(id),
{_GRADE_COLS},
    created_at              TEXT NOT NULL
)"""

_DDL_EDGE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_edge_src ON edge(src_type, src_id)",
    "CREATE INDEX IF NOT EXISTS idx_edge_dst ON edge(dst_type, dst_id)",
    "CREATE INDEX IF NOT EXISTS idx_edge_relation ON edge(relation)",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_edge_active_unique
       ON edge(src_type, src_id, dst_type, dst_id, relation)
       WHERE status = 'active'""",
)

_DDL_CLAIM_SIGHTING = f"""
CREATE TABLE IF NOT EXISTS claim_sighting (
    id          INTEGER PRIMARY KEY,
    claim_id    INTEGER NOT NULL REFERENCES claim(id) ON DELETE CASCADE,
    document_id INTEGER NOT NULL REFERENCES document(id),
    quote       TEXT,
    quote_start INTEGER,
    quote_end   INTEGER,
    stance      TEXT NOT NULL CHECK (stance IN {E.sql_in(E.SIGHTING_STANCES)}),
{_GRADE_COLS},
    created_at  TEXT NOT NULL
)"""

_DDL_CLAIM_SIGHTING_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_sighting_claim ON claim_sighting(claim_id)",
    "CREATE INDEX IF NOT EXISTS idx_sighting_document ON claim_sighting(document_id)",
)

_DDL_VERDICT_HISTORY = f"""
CREATE TABLE IF NOT EXISTS verdict_history (
    id                INTEGER PRIMARY KEY,
    claim_id          INTEGER NOT NULL REFERENCES claim(id) ON DELETE CASCADE,
    verdict           TEXT NOT NULL CHECK (verdict IN {E.sql_in(E.VERDICTS)}),
    computed_at       TEXT NOT NULL,
    "trigger"         TEXT,
    evidence_snapshot TEXT NOT NULL DEFAULT '[]'
)"""

_DDL_CONTRADICTION = f"""
CREATE TABLE IF NOT EXISTS contradiction (
    id                  INTEGER PRIMARY KEY,
    claim_id            INTEGER NOT NULL UNIQUE REFERENCES claim(id) ON DELETE CASCADE,
    n_support           INTEGER NOT NULL DEFAULT 0,
    n_refute            INTEGER NOT NULL DEFAULT 0,
    best_tier_support   INTEGER,
    best_tier_refute    INTEGER,
    status              TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN {E.sql_in(E.CONTRADICTION_STATUSES)}),
    resolved_dossier_id INTEGER REFERENCES dossier(id),
    detected_at         TEXT NOT NULL,
    resolved_at         TEXT
)"""

# v8 adds kind (analysis|investigation), parent_question_id (manual question
# recursion lineage) and budget_usd (the per-run cap frozen at creation).
_DDL_DOSSIER = f"""
CREATE TABLE IF NOT EXISTS dossier (
    id                 INTEGER PRIMARY KEY,
    kind               TEXT NOT NULL DEFAULT 'analysis'
                       CHECK (kind IN {E.sql_in(E.DOSSIER_KINDS)}),
    title              TEXT,
    input_text         TEXT NOT NULL,
    input_type         TEXT CHECK (input_type IN {E.sql_in(E.DOSSIER_INPUT_TYPES)}),
    input_document_id  INTEGER REFERENCES document(id),
    parent_question_id INTEGER REFERENCES question(id),
    budget_usd         REAL,
    status             TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN {E.sql_in(E.DOSSIER_STATUSES)}),
    current_stage      TEXT CHECK (current_stage IN {E.sql_in(E.DOSSIER_STAGES)}),
    error              TEXT,
    model_usage        TEXT NOT NULL DEFAULT '{{}}',
    created_at         TEXT NOT NULL,
    started_at         TEXT,
    finished_at        TEXT
)"""

_DDL_DOSSIER_SECTION = f"""
CREATE TABLE IF NOT EXISTS dossier_section (
    id         INTEGER PRIMARY KEY,
    dossier_id INTEGER NOT NULL REFERENCES dossier(id) ON DELETE CASCADE,
    stage      TEXT NOT NULL CHECK (stage IN {E.sql_in(E.DOSSIER_STAGES)}),
    status     TEXT NOT NULL DEFAULT 'pending'
               CHECK (status IN {E.sql_in(E.SECTION_STATUSES)}),
    content    TEXT NOT NULL DEFAULT '{{}}',
    created_at TEXT NOT NULL,
    updated_at TEXT,
    UNIQUE (dossier_id, stage)
)"""

# v8 (investigation mode): typed why-questions — first-class persisted
# objects; the loop's work queue and the open-questions product surface.
# question <-> dossier FKs are mutually circular; SQLite resolves FK targets
# at DML time, so declaration order is irrelevant.
_DDL_QUESTION = f"""
CREATE TABLE IF NOT EXISTS question (
    id                 INTEGER PRIMARY KEY,
    dossier_id         INTEGER NOT NULL REFERENCES dossier(id) ON DELETE CASCADE,
    qtype              TEXT NOT NULL CHECK (qtype IN {E.sql_in(E.QUESTION_TYPES)}),
    text               TEXT NOT NULL,
    about_type         TEXT CHECK (about_type IN {E.sql_in(E.NODE_TYPES)}),
    about_id           INTEGER,
    status             TEXT NOT NULL DEFAULT 'open'
                       CHECK (status IN {E.sql_in(E.QUESTION_STATUSES)}),
    priority           REAL NOT NULL DEFAULT 0.5,
    answer_summary     TEXT,
    answer_finding_ids TEXT NOT NULL DEFAULT '[]',
    spawned_dossier_id INTEGER REFERENCES dossier(id),
    created_at         TEXT NOT NULL,
    updated_at         TEXT
)"""

_DDL_QUESTION_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_question_dossier ON question(dossier_id)",
    "CREATE INDEX IF NOT EXISTS idx_question_about"
    " ON question(about_type, about_id)",
)

# v8: one grounded (or explicitly speculative) connection the loop recorded;
# persisted incrementally so a crash loses nothing already found.
_DDL_FINDING = f"""
CREATE TABLE IF NOT EXISTS finding (
    id          INTEGER PRIMARY KEY,
    dossier_id  INTEGER NOT NULL REFERENCES dossier(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN {E.sql_in(E.FINDING_KINDS)}),
    text        TEXT NOT NULL,
    speculation INTEGER NOT NULL DEFAULT 0,
    confidence  REAL,
    question_id INTEGER REFERENCES question(id),
    edge_id     INTEGER REFERENCES edge(id),
    payload     TEXT NOT NULL DEFAULT '{{}}',
    created_at  TEXT NOT NULL
)"""

_DDL_FINDING_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_finding_dossier ON finding(dossier_id)",
)

# v8: span-verified verbatim quotes backing a finding (offsets best-effort).
_DDL_FINDING_EVIDENCE = """
CREATE TABLE IF NOT EXISTS finding_evidence (
    id          INTEGER PRIMARY KEY,
    finding_id  INTEGER NOT NULL REFERENCES finding(id) ON DELETE CASCADE,
    document_id INTEGER NOT NULL REFERENCES document(id),
    quote       TEXT NOT NULL,
    quote_start INTEGER,
    quote_end   INTEGER
)"""

_DDL_FINDING_EVIDENCE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_finding_evidence_finding"
    " ON finding_evidence(finding_id)",
)

# Exported for migrations.py (migrate_7_to_8 creates the same tables and
# rebuilds dossier/dossier_section/job with the EXACT fresh-create DDL).
DOSSIER_TABLE_DDL: str = _DDL_DOSSIER
DOSSIER_SECTION_TABLE_DDL: str = _DDL_DOSSIER_SECTION
QUESTION_DDL: tuple[str, ...] = (_DDL_QUESTION, *_DDL_QUESTION_INDEXES)
FINDING_DDL: tuple[str, ...] = (_DDL_FINDING, *_DDL_FINDING_INDEXES)
FINDING_EVIDENCE_DDL: tuple[str, ...] = (
    _DDL_FINDING_EVIDENCE, *_DDL_FINDING_EVIDENCE_INDEXES)

# v9 (leader views): one attributed utterance — a speaker entity SAID the
# verbatim quote in a document. Distinct from claim (checkable fact):
# statements capture POSITIONS/views and always carry attribution. Replaced
# per-document with the other T1 rows (idempotent re-enrichment).
_DDL_STATEMENT = f"""
CREATE TABLE IF NOT EXISTS statement (
    id               INTEGER PRIMARY KEY,
    document_id      INTEGER NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    entity_id        INTEGER NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    quote            TEXT NOT NULL,
    quote_start      INTEGER,
    quote_end        INTEGER,
    topics           TEXT NOT NULL DEFAULT '[]',
    position_summary TEXT,
    stated_at        TEXT,
{_GRADE_COLS},
    created_at       TEXT NOT NULL
)"""

_DDL_STATEMENT_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_statement_entity"
    " ON statement(entity_id, stated_at)",
    "CREATE INDEX IF NOT EXISTS idx_statement_document"
    " ON statement(document_id)",
)

# v9: detected drift between two of a speaker's statements on one topic.
# Both sides are verbatim quotes, so a shift is grounded by construction.
# Statement FKs cascade: re-enriching a document replaces its statements
# and any shift built on a replaced quote dies with it.
_DDL_POSITION_SHIFT = f"""
CREATE TABLE IF NOT EXISTS position_shift (
    id                INTEGER PRIMARY KEY,
    entity_id         INTEGER NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    topic             TEXT NOT NULL,
    from_statement_id INTEGER NOT NULL REFERENCES statement(id) ON DELETE CASCADE,
    to_statement_id   INTEGER NOT NULL REFERENCES statement(id) ON DELETE CASCADE,
    kind              TEXT CHECK (kind IN {E.sql_in(E.POSITION_SHIFT_KINDS)}),
    note              TEXT,
    detected_at       TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'open'
                      CHECK (status IN {E.sql_in(E.POSITION_SHIFT_STATUSES)})
)"""

_DDL_POSITION_SHIFT_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_position_shift_entity"
    " ON position_shift(entity_id, topic)",
)

# v9: cached per-(entity, topic) evolution summary — regenerated lazily on
# read when stale (statement count grew by >= 2 or a newer shift exists);
# fresh reads are $0. citations = JSON int list of statement ids.
_DDL_VIEW_SUMMARY = """
CREATE TABLE IF NOT EXISTS view_summary (
    entity_id              INTEGER NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    topic                  TEXT NOT NULL,
    text                   TEXT,
    citations              TEXT NOT NULL DEFAULT '[]',
    statement_count_at_gen INTEGER,
    generated_at           TEXT,
    PRIMARY KEY (entity_id, topic)
)"""

# Exported for migrations.py (migrate_8_to_9 creates the same tables and
# rebuilds brief_item with the EXACT fresh-create DDL — the v9 BRIEF_SECTIONS
# vocabulary adds 'position_shift').
STATEMENT_DDL: tuple[str, ...] = (_DDL_STATEMENT, *_DDL_STATEMENT_INDEXES)
POSITION_SHIFT_DDL: tuple[str, ...] = (
    _DDL_POSITION_SHIFT, *_DDL_POSITION_SHIFT_INDEXES)
VIEW_SUMMARY_DDL: tuple[str, ...] = (_DDL_VIEW_SUMMARY,)

_DDL_JOB = f"""
CREATE TABLE IF NOT EXISTS job (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN {E.sql_in(E.JOB_KINDS)}),
    payload     TEXT NOT NULL DEFAULT '{{}}',
    dossier_id  INTEGER REFERENCES dossier(id),
    status      TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN {E.sql_in(E.JOB_STATUSES)}),
    attempts    INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT
)"""

# Exported for migrations.py (migrate_3_to_4 rebuilds job with the EXACT
# fresh-create DDL string — the v4 JOB_KINDS vocabulary adds 'enrich_t1_sync').
JOB_TABLE_DDL: str = _DDL_JOB

_DDL_JOB_EVENT = """
CREATE TABLE IF NOT EXISTS job_event (
    seq    INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES job(id) ON DELETE CASCADE,
    ts     TEXT NOT NULL,
    type   TEXT NOT NULL,
    data   TEXT NOT NULL DEFAULT '{}'
)"""

_DDL_JOB_EVENT_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_job_event_job ON job_event(job_id, seq)",
)

_DDL_WATCH = f"""
CREATE TABLE IF NOT EXISTS watch (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN {E.sql_in(E.WATCH_KINDS)}),
    label        TEXT NOT NULL,
    entity_id    INTEGER REFERENCES entity(id),
    thread_id    INTEGER REFERENCES story(id),
    claim_id     INTEGER REFERENCES claim(id),
    query_fts    TEXT,
    promote      INTEGER NOT NULL DEFAULT 1,
    muted        INTEGER NOT NULL DEFAULT 0,
    last_seen_at TEXT,
    created_at   TEXT NOT NULL
)"""

_DDL_WATCH_HIT = f"""
CREATE TABLE IF NOT EXISTS watch_hit (
    watch_id    INTEGER NOT NULL REFERENCES watch(id) ON DELETE CASCADE,
    object_type TEXT NOT NULL
                CHECK (object_type IN {E.sql_in(E.WATCH_HIT_OBJECT_TYPES)}),
    object_id   INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (watch_id, object_type, object_id)
)"""

_DDL_WATCH_HIT_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_watch_hit_created ON watch_hit(watch_id, created_at)",
)

_DDL_BRIEF = """
CREATE TABLE IF NOT EXISTS brief (
    id           INTEGER PRIMARY KEY,
    brief_date   TEXT NOT NULL UNIQUE,
    generated_at TEXT NOT NULL,
    gloss_text   TEXT,
    gloss_model  TEXT
)"""

# v6 adds payload: denormalized display fields (title/date/source/counts)
# frozen at generation time so the brief is stable all day.
_DDL_BRIEF_ITEM = f"""
CREATE TABLE IF NOT EXISTS brief_item (
    id          INTEGER PRIMARY KEY,
    brief_id    INTEGER NOT NULL REFERENCES brief(id) ON DELETE CASCADE,
    section     TEXT NOT NULL CHECK (section IN {E.sql_in(E.BRIEF_SECTIONS)}),
    rank        INTEGER NOT NULL,
    object_type TEXT NOT NULL,
    object_id   INTEGER NOT NULL,
    reason_json TEXT NOT NULL DEFAULT '{{}}',
    payload     TEXT NOT NULL DEFAULT '{{}}',
    seen        INTEGER NOT NULL DEFAULT 0
)"""

_DDL_BRIEF_ITEM_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_brief_item_brief ON brief_item(brief_id)",
)

# Exported for migrations.py (migrate_5_to_6 rebuilds brief_item with the
# EXACT fresh-create DDL string — the v6 payload column).
BRIEF_ITEM_TABLE_DDL: str = _DDL_BRIEF_ITEM
BRIEF_ITEM_INDEX_DDL: tuple[str, ...] = _DDL_BRIEF_ITEM_INDEX

_DDL_VIEW_CURSOR = """
CREATE TABLE IF NOT EXISTS view_cursor (
    surface      TEXT NOT NULL,
    ref_id       INTEGER NOT NULL DEFAULT 0,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (surface, ref_id)
)"""

_DDL_CALENDAR_EVENT = f"""
CREATE TABLE IF NOT EXISTS calendar_event (
    id        INTEGER PRIMARY KEY,
    kind      TEXT NOT NULL CHECK (kind IN {E.sql_in(E.CALENDAR_KINDS)}),
    scope     TEXT,
    occurs_on TEXT NOT NULL,
    ends_on   TEXT,
    label     TEXT NOT NULL
)"""

_DDL_LLM_CALL = """
CREATE TABLE IF NOT EXISTS llm_call (
    id                INTEGER PRIMARY KEY,
    purpose           TEXT NOT NULL,
    model             TEXT NOT NULL,
    input_tokens      INTEGER NOT NULL DEFAULT 0,
    output_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    batch_id          TEXT,
    cost_estimate     REAL,
    created_at        TEXT NOT NULL
)"""

_DDL_SOURCE_STATS = """
CREATE TABLE IF NOT EXISTS source_stats (
    source_id        INTEGER NOT NULL REFERENCES source(id) ON DELETE CASCADE,
    day              TEXT NOT NULL,
    items            INTEGER NOT NULL DEFAULT 0,
    dups             INTEGER NOT NULL DEFAULT 0,
    t1_failures      INTEGER NOT NULL DEFAULT 0,
    claims_extracted INTEGER NOT NULL DEFAULT 0,
    claims_false     INTEGER NOT NULL DEFAULT 0,
    quality_score    REAL,
    PRIMARY KEY (source_id, day)
)"""

# Fallback vector store (plain table + brute-force cosine in Python). Always
# created; used when sqlite-vec cannot load (SQLite < 3.41 on this machine).
_DDL_DOCUMENT_EMBEDDING = """
CREATE TABLE IF NOT EXISTS document_embedding (
    document_id INTEGER PRIMARY KEY REFERENCES document(id) ON DELETE CASCADE,
    model       TEXT NOT NULL,
    dim         INTEGER NOT NULL,
    vector      BLOB NOT NULL
)"""

# v6: event centroid vectors (running mean over member-doc embeddings).
# Plain BLOB table — events number in the hundreds at this corpus scale, so
# brute-force cosine within the candidate window is always sufficient and
# works identically under both vector backends.
_DDL_EVENT_EMBEDDING = """
CREATE TABLE IF NOT EXISTS event_embedding (
    event_id INTEGER PRIMARY KEY REFERENCES event(id) ON DELETE CASCADE,
    model    TEXT NOT NULL,
    dim      INTEGER NOT NULL,
    vector   BLOB NOT NULL
)"""

# Exported for migrations.py (migrate_5_to_6 creates the same table).
EVENT_EMBEDDING_DDL: tuple[str, ...] = (_DDL_EVENT_EMBEDDING,)

# v7 (Phase 3 verification): claim text vectors for claim reconciliation
# (vec >= 0.92 auto-merge / 0.80-0.92 adjudication). Plain BLOB table —
# claims number in the thousands at this scale, brute-force cosine is fine
# and works identically under both vector backends.
_DDL_CLAIM_EMBEDDING = """
CREATE TABLE IF NOT EXISTS claim_embedding (
    claim_id INTEGER PRIMARY KEY REFERENCES claim(id) ON DELETE CASCADE,
    model    TEXT NOT NULL,
    dim      INTEGER NOT NULL,
    vector   BLOB NOT NULL
)"""

# Exported for migrations.py (migrate_6_to_7 creates the same table).
CLAIM_EMBEDDING_DDL: tuple[str, ...] = (_DDL_CLAIM_EMBEDDING,)

# sqlite-vec virtual table — executed by knowledge/vector.py ONLY when the
# extension loads; kept here because schema.py owns all DDL text.
VEC_DOCUMENT_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS vec_document USING vec0(
    embedding float[384]
)"""


ALL_DDL: tuple[str, ...] = (
    _DDL_META,
    _DDL_SOURCE,
    _DDL_DOCUMENT,
    *_DDL_DOCUMENT_INDEXES,
    _DDL_DOCUMENT_FTS,
    *_DDL_DOCUMENT_FTS_TRIGGERS,
    _DDL_DOCUMENT_LINK,
    *_DDL_DOCUMENT_LINK_INDEXES,
    _DDL_DOCUMENT_TOPIC,
    _DDL_DOCUMENT_ENRICHMENT,
    _DDL_CLAIM,
    _DDL_CLAIM_FTS,
    *_DDL_CLAIM_FTS_TRIGGERS,
    _DDL_EVIDENCE,
    _DDL_ENTITY,
    _DDL_ENTITY_MENTION,
    *_DDL_ENTITY_MENTION_INDEXES,
    _DDL_EVENT_TYPE,
    _DDL_STORY,
    _DDL_EVENT,
    *_DDL_EVENT_INDEXES,
    _DDL_EVENT_ASSIGNMENT,
    _DDL_DOSSIER,          # before edge (edge FKs dossier)
    _DDL_DOSSIER_SECTION,
    _DDL_QUESTION,         # FKs dossier; dossier's FK back is name-resolved
    *_DDL_QUESTION_INDEXES,
    _DDL_EDGE,
    *_DDL_EDGE_INDEXES,
    _DDL_FINDING,          # FKs dossier + question + edge
    *_DDL_FINDING_INDEXES,
    _DDL_FINDING_EVIDENCE,
    *_DDL_FINDING_EVIDENCE_INDEXES,
    _DDL_CLAIM_SIGHTING,
    *_DDL_CLAIM_SIGHTING_INDEXES,
    _DDL_STATEMENT,          # FKs document + entity
    *_DDL_STATEMENT_INDEXES,
    _DDL_POSITION_SHIFT,     # FKs entity + statement
    *_DDL_POSITION_SHIFT_INDEXES,
    _DDL_VIEW_SUMMARY,
    _DDL_VERDICT_HISTORY,
    _DDL_CONTRADICTION,
    _DDL_JOB,
    _DDL_JOB_EVENT,
    *_DDL_JOB_EVENT_INDEX,
    _DDL_WATCH,
    _DDL_WATCH_HIT,
    *_DDL_WATCH_HIT_INDEX,
    _DDL_BRIEF,
    _DDL_BRIEF_ITEM,
    *_DDL_BRIEF_ITEM_INDEX,
    _DDL_VIEW_CURSOR,
    _DDL_CALENDAR_EVENT,
    _DDL_LLM_CALL,
    _DDL_SOURCE_STATS,
    _DDL_DOCUMENT_EMBEDDING,
    _DDL_EVENT_EMBEDDING,
    _DDL_CLAIM_EMBEDDING,
)


def create_all(conn: sqlite3.Connection) -> None:
    """Create the full current schema and stamp ``meta.schema_version``.

    Idempotent (IF NOT EXISTS everywhere); runs in one transaction.
    """
    with conn:
        for ddl in ALL_DDL:
            conn.execute(ddl)
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),))
