"""The storage contract — the ONLY module in the codebase that contains DDL.

v0.2: the canonical schema is the PostgreSQL baseline (``PG_SCHEMA_VERSION``,
``PG_DDL``, ``create_all_pg``) — the full v9 SQLite schema translated per
docs/design/v02-postgres-port.md §1 PLUS the tenancy DDL from
docs/design/v02-tenancy-auth.md §2 (app_user / user_session / invite /
app_setting and the ownership/visibility columns), shipped together so there
is exactly one schema cutover and no PG-side rebuild a week later.

PG conventions:
- ``bigint GENERATED ALWAYS AS IDENTITY`` PKs (the ETL inserts explicit ids
  with ``OVERRIDING SYSTEM VALUE`` then ``setval``).
- ``timestamptz`` for timestamps, ``date`` for day-precision columns; both
  are read back as the v0.1 ISO strings via the loaders in storage/pg.py, so
  the frozen Pydantic contracts (timestamps as ``str``) stay untouched.
- ``jsonb`` for JSON-in-TEXT columns (same ``'{}'``/``'[]'`` defaults).
- Every CHECK constraint has an explicit name (``ck_<table>_<column>``) so
  vocabulary changes become one ``ALTER TABLE ... DROP/ADD CONSTRAINT`` —
  the SQLite copy-out/drop/recreate rebuild dance is dead.
- FTS5 external-content tables + their 6 sync triggers are replaced by
  GENERATED tsvector columns + GIN (desync structurally impossible);
  pg_trgm GIN indexes keep entity substring-search parity.
- Vector BLOBs become pgvector ``vector(384)`` + HNSW (dim column dies —
  the type enforces 384).
- Self/circular FKs (document.canonical_document_id, edge.superseded_by_
  edge_id, dossier.parent_question_id) are DEFERRABLE INITIALLY IMMEDIATE:
  free at runtime, lets the one-shot ETL defer.
- Tenancy ownership columns: document.owner_id / job.owner_id /
  llm_call.user_id stay NULLABLE (NULL = system); v3 (Phase C tenancy)
  tightened the PER-USER surfaces — dossier.owner_id, watch.user_id,
  brief.user_id, view_cursor.user_id are NOT NULL and view_cursor's PK is
  (user_id, surface, ref_id). Existing DBs get there via
  migrations.pg_migrate_2_to_3 (backfill NULLs to the first admin, then
  cheap ALTERs).

Every CHECK vocabulary is built from domain/enums.py — the single home of
controlled vocabularies. ``PG_SCHEMA_VERSION`` starts a FRESH lineage at 1
(the SQLite v1–v10 lineage below is closed; the ETL targets PG v1).

The closed v0.1 SQLite lineage moved to connect/tools/legacy_sqlite/ at
port phase P2 (dead reference for the one-shot ETL only).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from connect.domain import enums as E

if TYPE_CHECKING:  # psycopg is only needed at runtime by storage/pg.py
    import psycopg


# --- error hierarchy ---------------------------------------------------------

class StorageError(Exception):
    """Base for every storage failure; a failed DB write is fatal."""


class StorageVersionError(StorageError):
    """DB schema_version is newer than this code's — refuse to open."""


# ==============================================================================
# PostgreSQL baseline schema (v0.2, the canonical DDL) — fresh lineage, v1.
# ==============================================================================

PG_SCHEMA_VERSION = 8

# Extensions first: the compose image is pgvector/pgvector:pg17, so both are
# present; IF NOT EXISTS keeps re-entry harmless.
_PG_DDL_EXTENSIONS = (
    "CREATE EXTENSION IF NOT EXISTS vector",
    "CREATE EXTENSION IF NOT EXISTS pg_trgm",
)

_PG_DDL_META = """
CREATE TABLE IF NOT EXISTS meta (
    key   text PRIMARY KEY,
    value text NOT NULL
)"""


# --- tenancy (v02-tenancy-auth.md §2; tables only — auth CODE is the next
# workstream). 'app_user' not 'user': reserved word in PG. ---------------------

_PG_DDL_APP_USER = f"""
CREATE TABLE IF NOT EXISTS app_user (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    google_sub  text UNIQUE,
    email       text NOT NULL UNIQUE,
    name        text,
    avatar_url  text,
    role        text NOT NULL DEFAULT 'member'
                CONSTRAINT ck_app_user_role CHECK (role IN {E.sql_in(E.ROLES)}),
    disabled    boolean NOT NULL DEFAULT false,
    daily_budget_usd               double precision,
    investigation_daily_budget_usd double precision,
    created_at    timestamptz NOT NULL,
    last_login_at timestamptz
)"""
# google_sub is NULLABLE: the ETL pre-creates the admin by email; filled at
# first login. daily budget columns NULL = member default from app_setting.

_PG_DDL_USER_SESSION = """
CREATE TABLE IF NOT EXISTS user_session (
    id           text PRIMARY KEY,
    user_id      bigint NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    created_at   timestamptz NOT NULL,
    expires_at   timestamptz NOT NULL,
    last_seen_at timestamptz
)"""

_PG_DDL_USER_SESSION_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_session_expires ON user_session(expires_at)",
)

_PG_DDL_INVITE = """
CREATE TABLE IF NOT EXISTS invite (
    email      text PRIMARY KEY,
    invited_by bigint REFERENCES app_user(id),
    note       text,
    created_at timestamptz NOT NULL
)"""

_PG_DDL_APP_SETTING = """
CREATE TABLE IF NOT EXISTS app_setting (
    key   text PRIMARY KEY,
    value text NOT NULL
)"""


# --- core ingestion ----------------------------------------------------------

_PG_DDL_SOURCE = f"""
CREATE TABLE IF NOT EXISTS source (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name               text NOT NULL UNIQUE,
    type               text NOT NULL
                       CONSTRAINT ck_source_type CHECK (type IN {E.sql_in(E.SOURCE_TYPES)}),
    config             jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    credibility_tier   integer NOT NULL DEFAULT 3
                       CONSTRAINT ck_source_credibility_tier
                       CHECK (credibility_tier IN {E.sql_in(E.CREDIBILITY_TIERS)}),
    enabled            boolean NOT NULL DEFAULT true,
    t1_exempt          boolean NOT NULL DEFAULT false,
    notes              text,
    max_items_per_poll integer,
    max_items_per_day  integer,
    quality_score      double precision,
    created_at         timestamptz NOT NULL,
    last_polled_at     timestamptz,
    last_poll_status   text
)"""

# search_tsv replaces the FTS5 external-content table + 3 triggers. The
# left(..., 200000) guard exists because tsvector rows cap at ~1MB and
# positions at 16383 — a runaway PDF must not fail the INSERT.
# to_tsvector with an explicit config is IMMUTABLE, so the generated column
# is legal. Ownership: owner NULL = system (polled/web docs); visibility
# 'shared' + origin 'polled' are the single-user-safe defaults.
_PG_DDL_DOCUMENT = f"""
CREATE TABLE IF NOT EXISTS document (
    id                    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id             bigint REFERENCES source(id) ON DELETE SET NULL,
    url                   text,
    canonical_url         text,
    title                 text,
    author                text,
    published_at          timestamptz,
    fetched_at            timestamptz NOT NULL,
    media_type            text NOT NULL DEFAULT 'html'
                          CONSTRAINT ck_document_media_type
                          CHECK (media_type IN {E.sql_in(E.MEDIA_TYPES)}),
    language              text,
    content_text          text NOT NULL,
    content_hash          text NOT NULL UNIQUE,
    raw_blob_path         text,
    enrichment_tier       integer NOT NULL DEFAULT 0,
    enrichment_status     text NOT NULL DEFAULT 'pending'
                          CONSTRAINT ck_document_enrichment_status
                          CHECK (enrichment_status IN {E.sql_in(E.ENRICHMENT_STATUSES)}),
    simhash               bigint,
    canonical_document_id bigint REFERENCES document(id)
                          DEFERRABLE INITIALLY IMMEDIATE,
    watch_hit             boolean NOT NULL DEFAULT false,
    owner_id              bigint REFERENCES app_user(id),
    visibility            text NOT NULL DEFAULT 'shared'
                          CONSTRAINT ck_document_visibility
                          CHECK (visibility IN {E.sql_in(E.VISIBILITIES)}),
    origin                text NOT NULL DEFAULT 'polled'
                          CONSTRAINT ck_document_origin
                          CHECK (origin IN {E.sql_in(E.DOCUMENT_ORIGINS)}),
    search_tsv            tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
        setweight(to_tsvector('english', left(content_text, 200000)), 'B')
    ) STORED
)"""

_PG_DDL_DOCUMENT_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_document_source ON document(source_id)",
    "CREATE INDEX IF NOT EXISTS idx_document_url ON document(url)",
    "CREATE INDEX IF NOT EXISTS idx_document_published ON document(published_at)",
    "CREATE INDEX IF NOT EXISTS idx_document_fetched ON document(fetched_at)",
    "CREATE INDEX IF NOT EXISTS idx_document_status ON document(enrichment_status)",
    "CREATE INDEX IF NOT EXISTS idx_document_tsv ON document USING gin(search_tsv)",
    "CREATE INDEX IF NOT EXISTS idx_document_owner ON document(owner_id)"
    " WHERE owner_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_document_visibility_fetched"
    " ON document(visibility, fetched_at DESC)",
)

_PG_DDL_DOCUMENT_LINK = f"""
CREATE TABLE IF NOT EXISTS document_link (
    id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id          bigint NOT NULL REFERENCES document(id),
    url                  text NOT NULL,
    anchor_text          text,
    is_file              boolean NOT NULL DEFAULT false,
    is_official          boolean NOT NULL DEFAULT false,
    status               text NOT NULL DEFAULT 'not_followed'
                         CONSTRAINT ck_document_link_status
                         CHECK (status IN {E.sql_in(E.LINK_STATUSES)}),
    resolved_document_id bigint REFERENCES document(id),
    error                text,
    created_at           timestamptz,
    UNIQUE (document_id, url)
)"""

_PG_DDL_DOCUMENT_LINK_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_document_link_document"
    " ON document_link(document_id)",
    "CREATE INDEX IF NOT EXISTS idx_document_link_resolved"
    " ON document_link(resolved_document_id)",
)

_PG_DDL_DOCUMENT_TOPIC = f"""
CREATE TABLE IF NOT EXISTS document_topic (
    document_id bigint NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    topic       text NOT NULL,
    source      text NOT NULL
                CONSTRAINT ck_document_topic_source
                CHECK (source IN {E.sql_in(E.TOPIC_SOURCES)}),
    PRIMARY KEY (document_id, topic)
)"""

_PG_DDL_DOCUMENT_ENRICHMENT = """
CREATE TABLE IF NOT EXISTS document_enrichment (
    document_id    bigint PRIMARY KEY REFERENCES document(id) ON DELETE CASCADE,
    summary        text NOT NULL,
    event_type     text NOT NULL,
    model          text NOT NULL,
    prompt_version text NOT NULL,
    created_at     timestamptz NOT NULL
)"""


# --- claims / evidence -------------------------------------------------------

_PG_DDL_CLAIM = f"""
CREATE TABLE IF NOT EXISTS claim (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    text               text NOT NULL,
    claim_type         text CONSTRAINT ck_claim_claim_type
                       CHECK (claim_type IN {E.sql_in(E.CLAIM_TYPES)}),
    first_document_id  bigint REFERENCES document(id),
    verdict            text NOT NULL DEFAULT 'unverified'
                       CONSTRAINT ck_claim_verdict
                       CHECK (verdict IN {E.sql_in(E.VERDICTS)}),
    confidence         double precision,
    check_worthiness   double precision,
    verdict_updated_at timestamptz,
    created_at         timestamptz NOT NULL,
    search_tsv         tsvector GENERATED ALWAYS AS
                       (to_tsvector('english', text)) STORED
)"""

_PG_DDL_CLAIM_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_claim_tsv ON claim USING gin(search_tsv)",
)


def _pg_grade_cols(table: str) -> str:
    """The provenance grade triple with a per-table named CHECK.

    grade (1=enriched 2=analyzed 3=curated) + extractor_model +
    prompt_version — same columns as v0.1's _GRADE_COLS.
    """
    return f"""
    grade           integer NOT NULL DEFAULT 1
                    CONSTRAINT ck_{table}_grade CHECK (grade IN {E.sql_in(E.GRADES)}),
    extractor_model text,
    prompt_version  text"""


_PG_DDL_EVIDENCE = f"""
CREATE TABLE IF NOT EXISTS evidence (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    claim_id    bigint NOT NULL REFERENCES claim(id) ON DELETE CASCADE,
    document_id bigint NOT NULL REFERENCES document(id),
    stance      text NOT NULL CONSTRAINT ck_evidence_stance
                CHECK (stance IN {E.sql_in(E.EVIDENCE_STANCES)}),
    confidence  double precision,
    rationale   text,
    quote       text,
    method      text CONSTRAINT ck_evidence_method
                CHECK (method IN {E.sql_in(E.EVIDENCE_METHODS)}),
    model_id    text,
{_pg_grade_cols('evidence')},
    created_at  timestamptz NOT NULL,
    UNIQUE (claim_id, document_id)
)"""


# --- entities ------------------------------------------------------------------

# Two search paths (design §1): ranked word search via the 'simple'-config
# generated tsvector (proper names must not be stemmed; the jsonb::text cast
# is immutable, brackets/quotes are punctuation the parser discards) and
# ILIKE-substring parity via pg_trgm GIN on name and aliases::text.
_PG_DDL_ENTITY = f"""
CREATE TABLE IF NOT EXISTS entity (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        text NOT NULL,
    entity_type text NOT NULL DEFAULT 'other'
                CONSTRAINT ck_entity_entity_type
                CHECK (entity_type IN {E.sql_in(E.ENTITY_TYPES)}),
    aliases     jsonb NOT NULL DEFAULT '[]'::jsonb,
    description text,
    wikidata_id text,
    attrs       jsonb NOT NULL DEFAULT '{{}}'::jsonb,
{_pg_grade_cols('entity')},
    created_at  timestamptz NOT NULL,
    updated_at  timestamptz,
    search_tsv  tsvector GENERATED ALWAYS AS (to_tsvector('simple',
                    name || ' ' || coalesce(aliases::text, ''))) STORED,
    UNIQUE (name, entity_type)
)"""

_PG_DDL_ENTITY_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_entity_tsv ON entity USING gin(search_tsv)",
    "CREATE INDEX IF NOT EXISTS idx_entity_name_trgm"
    " ON entity USING gin (name gin_trgm_ops)",
    "CREATE INDEX IF NOT EXISTS idx_entity_aliases_trgm"
    " ON entity USING gin ((aliases::text) gin_trgm_ops)",
)

_PG_DDL_ENTITY_MENTION = f"""
CREATE TABLE IF NOT EXISTS entity_mention (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id bigint NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    entity_id   bigint NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    surface     text,
    span_start  integer,
    span_end    integer,
    method      text CONSTRAINT ck_entity_mention_method
                CHECK (method IN {E.sql_in(E.MENTION_METHODS)}),
{_pg_grade_cols('entity_mention')},
    created_at  timestamptz NOT NULL
)"""

_PG_DDL_ENTITY_MENTION_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_mention_entity"
    " ON entity_mention(entity_id, document_id)",
    "CREATE INDEX IF NOT EXISTS idx_mention_document"
    " ON entity_mention(document_id)",
)


# --- events / stories ----------------------------------------------------------

_PG_DDL_EVENT_TYPE = """
CREATE TABLE IF NOT EXISTS event_type (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name            text NOT NULL UNIQUE,
    lifecycle_group text,
    window_days     integer NOT NULL DEFAULT 7
)"""

_PG_DDL_STORY = f"""
CREATE TABLE IF NOT EXISTS story (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    title         text,
    root_event_id bigint,
    status        text NOT NULL DEFAULT 'active'
                  CONSTRAINT ck_story_status
                  CHECK (status IN {E.sql_in(E.STORY_STATUSES)}),
    doc_count     integer NOT NULL DEFAULT 0,
    summary_text  text,
    summary_stale boolean NOT NULL DEFAULT false,
    created_at    timestamptz NOT NULL,
    updated_at    timestamptz
)"""

# occurred_on / window_start / window_end / last_seen_at all receive
# doc_date (the first 10 chars of published_at|fetched_at) — day-precision
# by construction, so they are `date` and round-trip as 'YYYY-MM-DD'.
_PG_DDL_EVENT = f"""
CREATE TABLE IF NOT EXISTS event (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    title          text NOT NULL,
    description    text,
    event_type     text NOT NULL DEFAULT 'other',
    story_id       bigint REFERENCES story(id),
    occurred_on    date,
    date_precision text CONSTRAINT ck_event_date_precision
                   CHECK (date_precision IN {E.sql_in(E.DATE_PRECISIONS)}),
    geo_scope      text,
    doc_count      integer NOT NULL DEFAULT 0,
    window_start   date,
    window_end     date,
    last_seen_at   date,
{_pg_grade_cols('event')},
    created_at     timestamptz NOT NULL
)"""

_PG_DDL_EVENT_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_event_story ON event(story_id)",
    "CREATE INDEX IF NOT EXISTS idx_event_type ON event(event_type)",
    "CREATE INDEX IF NOT EXISTS idx_event_occurred ON event(occurred_on)",
)

_PG_DDL_EVENT_ASSIGNMENT = f"""
CREATE TABLE IF NOT EXISTS event_assignment (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id  bigint NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    event_id     bigint REFERENCES event(id),
    method       text NOT NULL CONSTRAINT ck_event_assignment_method
                 CHECK (method IN {E.sql_in(E.ASSIGNMENT_METHODS)}),
    score        double precision,
    adjudication text,
    created_at   timestamptz NOT NULL
)"""


# --- dossiers / questions / findings (Postgres validates FK targets at
# CREATE TABLE, so: dossier is created WITHOUT the parent_question_id FK,
# question follows, then the FK is added by ALTER — both directions
# DEFERRABLE for the ETL). ------------------------------------------------------

_PG_DDL_DOSSIER = f"""
CREATE TABLE IF NOT EXISTS dossier (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind               text NOT NULL DEFAULT 'analysis'
                       CONSTRAINT ck_dossier_kind
                       CHECK (kind IN {E.sql_in(E.DOSSIER_KINDS)}),
    title              text,
    input_text         text NOT NULL,
    input_type         text CONSTRAINT ck_dossier_input_type
                       CHECK (input_type IN {E.sql_in(E.DOSSIER_INPUT_TYPES)}),
    input_document_id  bigint REFERENCES document(id),
    parent_question_id bigint,
    budget_usd         double precision,
    status             text NOT NULL DEFAULT 'pending'
                       CONSTRAINT ck_dossier_status
                       CHECK (status IN {E.sql_in(E.DOSSIER_STATUSES)}),
    current_stage      text CONSTRAINT ck_dossier_current_stage
                       CHECK (current_stage IN {E.sql_in(E.DOSSIER_STAGES)}),
    error              text,
    model_usage        jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    created_at         timestamptz NOT NULL,
    started_at         timestamptz,
    finished_at        timestamptz,
    owner_id           bigint NOT NULL REFERENCES app_user(id),
    visibility         text NOT NULL DEFAULT 'shared'
                       CONSTRAINT ck_dossier_visibility
                       CHECK (visibility IN {E.sql_in(E.VISIBILITIES)})
)"""
# owner_id NOT NULL since v3 (dossiers are always user-initiated); the
# v2->v3 migration backfilled pre-tenancy rows to the first admin.

_PG_DDL_DOSSIER_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_dossier_owner"
    " ON dossier(owner_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_dossier_visible"
    " ON dossier(visibility, kind, created_at DESC)",
)

_PG_DDL_DOSSIER_SECTION = f"""
CREATE TABLE IF NOT EXISTS dossier_section (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dossier_id bigint NOT NULL REFERENCES dossier(id) ON DELETE CASCADE,
    stage      text NOT NULL CONSTRAINT ck_dossier_section_stage
               CHECK (stage IN {E.sql_in(E.DOSSIER_STAGES)}),
    status     text NOT NULL DEFAULT 'pending'
               CONSTRAINT ck_dossier_section_status
               CHECK (status IN {E.sql_in(E.SECTION_STATUSES)}),
    content    jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    created_at timestamptz NOT NULL,
    updated_at timestamptz,
    UNIQUE (dossier_id, stage)
)"""

_PG_DDL_QUESTION = f"""
CREATE TABLE IF NOT EXISTS question (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dossier_id         bigint NOT NULL REFERENCES dossier(id) ON DELETE CASCADE,
    qtype              text NOT NULL CONSTRAINT ck_question_qtype
                       CHECK (qtype IN {E.sql_in(E.QUESTION_TYPES)}),
    text               text NOT NULL,
    about_type         text CONSTRAINT ck_question_about_type
                       CHECK (about_type IN {E.sql_in(E.NODE_TYPES)}),
    about_id           bigint,
    status             text NOT NULL DEFAULT 'open'
                       CONSTRAINT ck_question_status
                       CHECK (status IN {E.sql_in(E.QUESTION_STATUSES)}),
    priority           double precision NOT NULL DEFAULT 0.5,
    answer_summary     text,
    answer_finding_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    spawned_dossier_id bigint REFERENCES dossier(id),
    created_at         timestamptz NOT NULL,
    updated_at         timestamptz
)"""

_PG_DDL_QUESTION_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_question_dossier ON question(dossier_id)",
    "CREATE INDEX IF NOT EXISTS idx_question_about"
    " ON question(about_type, about_id)",
)

# Closes the dossier <-> question circular pair; idempotent (guarded ALTER —
# ADD CONSTRAINT has no IF NOT EXISTS).
_PG_DDL_DOSSIER_PARENT_QUESTION_FK = """
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'fk_dossier_parent_question') THEN
        ALTER TABLE dossier
            ADD CONSTRAINT fk_dossier_parent_question
            FOREIGN KEY (parent_question_id) REFERENCES question(id)
            DEFERRABLE INITIALLY IMMEDIATE;
    END IF;
END $$"""


# --- the knowledge graph ------------------------------------------------------

# Polymorphic typed-edge table; bitemporal (SCD2): single-valued relations
# close the old edge (status='superseded'), never delete. Uniqueness applies
# only to ACTIVE edges (native partial unique index, also the ON CONFLICT
# target for the idempotent insert). superseded_by_edge_id points to NEWER
# rows — DEFERRABLE for the ETL. valid_from/valid_to are day-precision
# validity bounds (date); asserted_at is a real timestamp.
_PG_DDL_EDGE = f"""
CREATE TABLE IF NOT EXISTS edge (
    id                      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    src_type                text NOT NULL CONSTRAINT ck_edge_src_type
                            CHECK (src_type IN {E.sql_in(E.NODE_TYPES)}),
    src_id                  bigint NOT NULL,
    dst_type                text NOT NULL CONSTRAINT ck_edge_dst_type
                            CHECK (dst_type IN {E.sql_in(E.NODE_TYPES)}),
    dst_id                  bigint NOT NULL,
    relation                text NOT NULL,
    properties              jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    provenance_document_id  bigint REFERENCES document(id),
    provenance_dossier_id   bigint REFERENCES dossier(id),
    confidence              double precision,
    valid_from              date,
    valid_to                date,
    asserted_at             timestamptz,
    status                  text NOT NULL DEFAULT 'active'
                            CONSTRAINT ck_edge_status
                            CHECK (status IN {E.sql_in(E.EDGE_STATUSES)}),
    superseded_by_edge_id   bigint REFERENCES edge(id)
                            DEFERRABLE INITIALLY IMMEDIATE,
{_pg_grade_cols('edge')},
    created_at              timestamptz NOT NULL
)"""

_PG_DDL_EDGE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_edge_src ON edge(src_type, src_id)",
    "CREATE INDEX IF NOT EXISTS idx_edge_dst ON edge(dst_type, dst_id)",
    "CREATE INDEX IF NOT EXISTS idx_edge_relation ON edge(relation)",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_edge_active_unique
       ON edge(src_type, src_id, dst_type, dst_id, relation)
       WHERE status = 'active'""",
)

_PG_DDL_FINDING = f"""
CREATE TABLE IF NOT EXISTS finding (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dossier_id  bigint NOT NULL REFERENCES dossier(id) ON DELETE CASCADE,
    kind        text NOT NULL CONSTRAINT ck_finding_kind
                CHECK (kind IN {E.sql_in(E.FINDING_KINDS)}),
    text        text NOT NULL,
    speculation boolean NOT NULL DEFAULT false,
    confidence  double precision,
    question_id bigint REFERENCES question(id),
    edge_id     bigint REFERENCES edge(id),
    payload     jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    created_at  timestamptz NOT NULL
)"""

_PG_DDL_FINDING_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_finding_dossier ON finding(dossier_id)",
)

_PG_DDL_FINDING_EVIDENCE = """
CREATE TABLE IF NOT EXISTS finding_evidence (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    finding_id  bigint NOT NULL REFERENCES finding(id) ON DELETE CASCADE,
    document_id bigint NOT NULL REFERENCES document(id),
    quote       text NOT NULL,
    quote_start integer,
    quote_end   integer
)"""

_PG_DDL_FINDING_EVIDENCE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_finding_evidence_finding"
    " ON finding_evidence(finding_id)",
)

_PG_DDL_CLAIM_SIGHTING = f"""
CREATE TABLE IF NOT EXISTS claim_sighting (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    claim_id    bigint NOT NULL REFERENCES claim(id) ON DELETE CASCADE,
    document_id bigint NOT NULL REFERENCES document(id),
    quote       text,
    quote_start integer,
    quote_end   integer,
    stance      text NOT NULL CONSTRAINT ck_claim_sighting_stance
                CHECK (stance IN {E.sql_in(E.SIGHTING_STANCES)}),
{_pg_grade_cols('claim_sighting')},
    created_at  timestamptz NOT NULL
)"""

_PG_DDL_CLAIM_SIGHTING_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_sighting_claim ON claim_sighting(claim_id)",
    "CREATE INDEX IF NOT EXISTS idx_sighting_document"
    " ON claim_sighting(document_id)",
)


# --- statements / position tracking -------------------------------------------

_PG_DDL_STATEMENT = f"""
CREATE TABLE IF NOT EXISTS statement (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id      bigint NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    entity_id        bigint NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    quote            text NOT NULL,
    quote_start      integer,
    quote_end        integer,
    topics           jsonb NOT NULL DEFAULT '[]'::jsonb,
    position_summary text,
    stated_at        timestamptz,
{_pg_grade_cols('statement')},
    created_at       timestamptz NOT NULL
)"""

_PG_DDL_STATEMENT_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_statement_entity"
    " ON statement(entity_id, stated_at)",
    "CREATE INDEX IF NOT EXISTS idx_statement_document"
    " ON statement(document_id)",
)

_PG_DDL_POSITION_SHIFT = f"""
CREATE TABLE IF NOT EXISTS position_shift (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entity_id         bigint NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    topic             text NOT NULL,
    from_statement_id bigint NOT NULL REFERENCES statement(id) ON DELETE CASCADE,
    to_statement_id   bigint NOT NULL REFERENCES statement(id) ON DELETE CASCADE,
    kind              text CONSTRAINT ck_position_shift_kind
                      CHECK (kind IN {E.sql_in(E.POSITION_SHIFT_KINDS)}),
    note              text,
    detected_at       timestamptz NOT NULL,
    status            text NOT NULL DEFAULT 'open'
                      CONSTRAINT ck_position_shift_status
                      CHECK (status IN {E.sql_in(E.POSITION_SHIFT_STATUSES)})
)"""

_PG_DDL_POSITION_SHIFT_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_position_shift_entity"
    " ON position_shift(entity_id, topic)",
)

_PG_DDL_VIEW_SUMMARY = """
CREATE TABLE IF NOT EXISTS view_summary (
    entity_id              bigint NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    topic                  text NOT NULL,
    text                   text,
    citations              jsonb NOT NULL DEFAULT '[]'::jsonb,
    statement_count_at_gen integer,
    generated_at           timestamptz,
    PRIMARY KEY (entity_id, topic)
)"""


# --- verification --------------------------------------------------------------

_PG_DDL_VERDICT_HISTORY = f"""
CREATE TABLE IF NOT EXISTS verdict_history (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    claim_id          bigint NOT NULL REFERENCES claim(id) ON DELETE CASCADE,
    verdict           text NOT NULL CONSTRAINT ck_verdict_history_verdict
                      CHECK (verdict IN {E.sql_in(E.VERDICTS)}),
    computed_at       timestamptz NOT NULL,
    "trigger"         text,
    evidence_snapshot jsonb NOT NULL DEFAULT '[]'::jsonb
)"""

_PG_DDL_CONTRADICTION = f"""
CREATE TABLE IF NOT EXISTS contradiction (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    claim_id            bigint NOT NULL UNIQUE REFERENCES claim(id) ON DELETE CASCADE,
    n_support           integer NOT NULL DEFAULT 0,
    n_refute            integer NOT NULL DEFAULT 0,
    best_tier_support   integer,
    best_tier_refute    integer,
    status              text NOT NULL DEFAULT 'open'
                        CONSTRAINT ck_contradiction_status
                        CHECK (status IN {E.sql_in(E.CONTRADICTION_STATUSES)}),
    resolved_dossier_id bigint REFERENCES dossier(id),
    detected_at         timestamptz NOT NULL,
    resolved_at         timestamptz
)"""


# --- jobs ----------------------------------------------------------------------

# v2 (runtime design §2): the job table IS the queue — SKIP LOCKED claims
# order by (priority, run_at, id); 10 = interactive, 50 = enrichment/
# background, 90 = polls. claimed_by/heartbeat_at drive the beat's orphan
# reclaim (replaces startup reconcile_orphans, which is wrong with >1
# process); max_attempts bounds requeues (polls get 1; the idempotent
# batch poll gets 3). run_at is the visibility time (retry backoff, brief
# pre-gen stagger).
_PG_DDL_JOB = f"""
CREATE TABLE IF NOT EXISTS job (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind             text NOT NULL CONSTRAINT ck_job_kind
                     CHECK (kind IN {E.sql_in(E.JOB_KINDS)}),
    priority         smallint NOT NULL DEFAULT 50,
    payload          jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    dossier_id       bigint REFERENCES dossier(id),
    status           text NOT NULL DEFAULT 'queued'
                     CONSTRAINT ck_job_status
                     CHECK (status IN {E.sql_in(E.JOB_STATUSES)}),
    attempts         integer NOT NULL DEFAULT 0,
    max_attempts     integer NOT NULL DEFAULT 1,
    run_at           timestamptz NOT NULL DEFAULT now(),
    cancel_requested boolean NOT NULL DEFAULT false,
    claimed_by       text,
    heartbeat_at     timestamptz,
    error            text,
    created_at       timestamptz NOT NULL,
    started_at       timestamptz,
    finished_at      timestamptz,
    owner_id         bigint REFERENCES app_user(id)
)"""
# owner_id NULL = system job (poller sweeps, batch enrichment).

_PG_DDL_JOB_INDEXES = (
    # the claim path: queued jobs in claim order
    "CREATE INDEX IF NOT EXISTS idx_job_claim ON job (priority, run_at, id)"
    " WHERE status = 'queued'",
    # the orphan sweep: running jobs by heartbeat staleness
    "CREATE INDEX IF NOT EXISTS idx_job_heartbeat ON job (heartbeat_at)"
    " WHERE status = 'running'",
    # dedup: at most one LIVE poll job per source (beat enqueues blindly;
    # the enqueue's ON CONFLICT targets this index)
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_job_poll_source"
    " ON job ((payload->>'source_id'))"
    " WHERE kind = 'poll_source' AND status IN ('queued','running')",
)

# seq: identity values are allocated at INSERT, not commit — globally, a
# later-committed row can carry a smaller seq. Harmless here: SSE replay is
# always scoped WHERE job_id = %s AND seq > %s and one job's events are
# written sequentially by one worker task, so per-job monotonicity (the only
# property the SSE contract needs) holds.
_PG_DDL_JOB_EVENT = """
CREATE TABLE IF NOT EXISTS job_event (
    seq    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id bigint NOT NULL REFERENCES job(id) ON DELETE CASCADE,
    ts     timestamptz NOT NULL,
    type   text NOT NULL,
    data   jsonb NOT NULL DEFAULT '{}'::jsonb
)"""

_PG_DDL_JOB_EVENT_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_job_event_job ON job_event(job_id, seq)",
)


# --- shared runtime state (v2, runtime design §4) -------------------------------

# Per-domain politeness across processes: one atomic slot reservation per
# fetch (GREATEST handles idle domains — no backlog accumulation). Callers
# sleep locally until their reserved slot; the UPDATE must commit
# immediately (autocommit), never inside a larger transaction.
_PG_DDL_FETCH_DOMAIN = """
CREATE TABLE IF NOT EXISTS fetch_domain (
    domain  text PRIMARY KEY,
    next_at timestamptz NOT NULL
)"""

# Shared robots.txt cache (TTL enforced at read time, 1h); body NULL keeps
# the v0.1 "unreachable robots => allow" semantics. Parsing stays local.
_PG_DDL_ROBOTS_CACHE = """
CREATE TABLE IF NOT EXISTS robots_cache (
    origin     text PRIMARY KEY,
    body       text,
    fetched_at timestamptz NOT NULL
)"""

# Beat's once-per-period guard (nightly batch, brief pre-gen): leader
# restarts must not double-fire a period's task.
_PG_DDL_BEAT_RUN = """
CREATE TABLE IF NOT EXISTS beat_run (
    task        text PRIMARY KEY,
    last_run_at timestamptz NOT NULL
)"""


# --- watches / consumption ------------------------------------------------------

_PG_DDL_WATCH = f"""
CREATE TABLE IF NOT EXISTS watch (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind         text NOT NULL CONSTRAINT ck_watch_kind
                 CHECK (kind IN {E.sql_in(E.WATCH_KINDS)}),
    label        text NOT NULL,
    entity_id    bigint REFERENCES entity(id),
    thread_id    bigint REFERENCES story(id),
    claim_id     bigint REFERENCES claim(id),
    query_fts    text,
    promote      boolean NOT NULL DEFAULT true,
    muted        boolean NOT NULL DEFAULT false,
    last_seen_at timestamptz,
    created_at   timestamptz NOT NULL,
    user_id      bigint NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    workspace_id bigint REFERENCES workspace(id) ON DELETE SET NULL
)"""
# user_id NOT NULL since v3 (watches are personal); pre-tenancy rows were
# backfilled to the first admin by the v2->v3 migration.
# workspace_id (v7): optional tag grouping a watch into a workspace.

_PG_DDL_WATCH_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_watch_user ON watch(user_id)",
)

_PG_DDL_WATCH_HIT = f"""
CREATE TABLE IF NOT EXISTS watch_hit (
    watch_id    bigint NOT NULL REFERENCES watch(id) ON DELETE CASCADE,
    object_type text NOT NULL
                CONSTRAINT ck_watch_hit_object_type
                CHECK (object_type IN {E.sql_in(E.WATCH_HIT_OBJECT_TYPES)}),
    object_id   bigint NOT NULL,
    created_at  timestamptz NOT NULL,
    PRIMARY KEY (watch_id, object_type, object_id)
)"""

_PG_DDL_WATCH_HIT_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_watch_hit_created"
    " ON watch_hit(watch_id, created_at)",
)

# User-authored findings board (v6): short notes/insights a user posts, often
# sourced from a news document. Owned + shared/private like dossier; the
# optional document_id ties a finding to the article it came from. No FTS /
# content_hash — posts are mutable user content, not immutable snapshots.
_PG_DDL_POST = f"""
CREATE TABLE IF NOT EXISTS post (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_id    bigint NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    title       text NOT NULL,
    body        text NOT NULL,
    document_id bigint REFERENCES document(id) ON DELETE SET NULL,
    visibility  text NOT NULL DEFAULT 'shared'
                CONSTRAINT ck_post_visibility
                CHECK (visibility IN {E.sql_in(E.VISIBILITIES)}),
    workspace_id bigint REFERENCES workspace(id) ON DELETE SET NULL,
    created_at  timestamptz NOT NULL,
    updated_at  timestamptz
)"""

_PG_DDL_POST_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_post_owner"
    " ON post(owner_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_post_visibility"
    " ON post(visibility, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_post_document ON post(document_id)",
)

# Workspaces (v7): a SAVED LENS over the shared corpus — a named focus
# (topics / sources / FTS query) that drives a focused feed, and a tag that
# groups a user's findings/watches. Owned + shared/private like dossiers. The
# focus is stored as jsonb (topics: list[str], source_ids: list[int]).
_PG_DDL_WORKSPACE = f"""
CREATE TABLE IF NOT EXISTS workspace (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_id    bigint NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    name        text NOT NULL,
    description text NOT NULL DEFAULT '',
    topics      jsonb NOT NULL DEFAULT '[]'::jsonb,
    source_ids  jsonb NOT NULL DEFAULT '[]'::jsonb,
    query_fts   text,
    visibility  text NOT NULL DEFAULT 'shared'
                CONSTRAINT ck_workspace_visibility
                CHECK (visibility IN {E.sql_in(E.VISIBILITIES)}),
    created_at  timestamptz NOT NULL,
    updated_at  timestamptz
)"""

_PG_DDL_WORKSPACE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_workspace_owner"
    " ON workspace(owner_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_workspace_visibility"
    " ON workspace(visibility, created_at DESC)",
)

# Saved workspace-agent conversations (v8): per-user, per-workspace. ``messages``
# is the provider-shaped wire transcript (replayed to resume the agent);
# ``transcript`` is the human-readable display turns the UI renders.
_PG_DDL_WORKSPACE_CHAT = """
CREATE TABLE IF NOT EXISTS workspace_chat (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workspace_id bigint NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
    owner_id     bigint NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    title        text NOT NULL DEFAULT '',
    messages     jsonb NOT NULL DEFAULT '[]'::jsonb,
    transcript   jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at   timestamptz NOT NULL,
    updated_at   timestamptz
)"""

_PG_DDL_WORKSPACE_CHAT_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_workspace_chat_owner"
    " ON workspace_chat(workspace_id, owner_id, updated_at DESC)",
)

# One brief per (user, day) — the UNIQUE doubles as the ON CONFLICT target
# guarding concurrent first-GET generation (tenancy design §5). NULLS NOT
# DISTINCT is kept for DDL continuity with v2 (user_id is NOT NULL now, so
# it is inert).
_PG_DDL_BRIEF = """
CREATE TABLE IF NOT EXISTS brief (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    brief_date   date NOT NULL,
    generated_at timestamptz NOT NULL,
    gloss_text   text,
    gloss_model  text,
    user_id      bigint NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    UNIQUE NULLS NOT DISTINCT (user_id, brief_date)
)"""

_PG_DDL_BRIEF_ITEM = f"""
CREATE TABLE IF NOT EXISTS brief_item (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    brief_id    bigint NOT NULL REFERENCES brief(id) ON DELETE CASCADE,
    section     text NOT NULL CONSTRAINT ck_brief_item_section
                CHECK (section IN {E.sql_in(E.BRIEF_SECTIONS)}),
    rank        integer NOT NULL,
    object_type text NOT NULL,
    object_id   bigint NOT NULL,
    reason_json jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    payload     jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    seen        boolean NOT NULL DEFAULT false
)"""

_PG_DDL_BRIEF_ITEM_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_brief_item_brief ON brief_item(brief_id)",
)

# v3: the read cursor is per-user — PK (user_id, surface, ref_id), the
# upsert key the tenancy design §2 specifies.
_PG_DDL_VIEW_CURSOR = """
CREATE TABLE IF NOT EXISTS view_cursor (
    surface      text NOT NULL,
    ref_id       bigint NOT NULL DEFAULT 0,
    last_seen_at timestamptz NOT NULL,
    user_id      bigint NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, surface, ref_id)
)"""

_PG_DDL_CALENDAR_EVENT = f"""
CREATE TABLE IF NOT EXISTS calendar_event (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind      text NOT NULL CONSTRAINT ck_calendar_event_kind
              CHECK (kind IN {E.sql_in(E.CALENDAR_KINDS)}),
    scope     text,
    occurs_on date NOT NULL,
    ends_on   date,
    label     text NOT NULL
)"""

_PG_DDL_LLM_CALL = """
CREATE TABLE IF NOT EXISTS llm_call (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    purpose           text NOT NULL,
    model             text NOT NULL,
    input_tokens      integer NOT NULL DEFAULT 0,
    output_tokens     integer NOT NULL DEFAULT 0,
    cache_read_tokens integer NOT NULL DEFAULT 0,
    batch_id          text,
    cost_estimate     double precision,
    created_at        timestamptz NOT NULL,
    user_id           bigint REFERENCES app_user(id)
)"""
# user_id NULL = system spend (poller-driven sweeps, batch, T2 triggers).

_PG_DDL_LLM_CALL_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_llm_call_user_day"
    " ON llm_call(user_id, created_at)",
)

_PG_DDL_SOURCE_STATS = """
CREATE TABLE IF NOT EXISTS source_stats (
    source_id        bigint NOT NULL REFERENCES source(id) ON DELETE CASCADE,
    day              date NOT NULL,
    items            integer NOT NULL DEFAULT 0,
    dups             integer NOT NULL DEFAULT 0,
    t1_failures      integer NOT NULL DEFAULT 0,
    claims_extracted integer NOT NULL DEFAULT 0,
    claims_false     integer NOT NULL DEFAULT 0,
    quality_score    double precision,
    PRIMARY KEY (source_id, day)
)"""


# --- vectors (pgvector) ---------------------------------------------------------

# Same 1:1-table layout as v0.1 — vector availability never breaks the
# aggregate, and `model` provenance survives. vector(384) enforces the dim
# (the v0.1 dim column dies). HNSW everywhere: documents need it; claims/
# events were brute-force in v0.1 and an exact ORDER BY <=> scan reproduces
# that, but the index costs nothing and removes a scale cliff.
_PG_DDL_DOCUMENT_EMBEDDING = """
CREATE TABLE IF NOT EXISTS document_embedding (
    document_id bigint PRIMARY KEY REFERENCES document(id) ON DELETE CASCADE,
    model       text NOT NULL,
    embedding   vector(384) NOT NULL
)"""

_PG_DDL_EVENT_EMBEDDING = """
CREATE TABLE IF NOT EXISTS event_embedding (
    event_id  bigint PRIMARY KEY REFERENCES event(id) ON DELETE CASCADE,
    model     text NOT NULL,
    embedding vector(384) NOT NULL
)"""

_PG_DDL_CLAIM_EMBEDDING = """
CREATE TABLE IF NOT EXISTS claim_embedding (
    claim_id  bigint PRIMARY KEY REFERENCES claim(id) ON DELETE CASCADE,
    model     text NOT NULL,
    embedding vector(384) NOT NULL
)"""

_PG_DDL_EMBEDDING_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_document_embedding_hnsw"
    " ON document_embedding USING hnsw (embedding vector_cosine_ops)",
    "CREATE INDEX IF NOT EXISTS idx_event_embedding_hnsw"
    " ON event_embedding USING hnsw (embedding vector_cosine_ops)",
    "CREATE INDEX IF NOT EXISTS idx_claim_embedding_hnsw"
    " ON claim_embedding USING hnsw (embedding vector_cosine_ops)",
)


# FK-dependency order; PG validates FK targets at CREATE TABLE (unlike
# SQLite's name-resolution at DML time), so app_user precedes everything
# that carries ownership columns and the dossier<->question circular pair
# is closed by the guarded ALTER after both exist.
PG_DDL: tuple[str, ...] = (
    *_PG_DDL_EXTENSIONS,
    _PG_DDL_META,
    _PG_DDL_APP_USER,
    _PG_DDL_USER_SESSION,
    *_PG_DDL_USER_SESSION_INDEXES,
    _PG_DDL_INVITE,
    _PG_DDL_APP_SETTING,
    _PG_DDL_SOURCE,
    _PG_DDL_DOCUMENT,
    *_PG_DDL_DOCUMENT_INDEXES,
    _PG_DDL_DOCUMENT_LINK,
    *_PG_DDL_DOCUMENT_LINK_INDEXES,
    _PG_DDL_DOCUMENT_TOPIC,
    _PG_DDL_DOCUMENT_ENRICHMENT,
    _PG_DDL_CLAIM,
    *_PG_DDL_CLAIM_INDEXES,
    _PG_DDL_EVIDENCE,
    _PG_DDL_ENTITY,
    *_PG_DDL_ENTITY_INDEXES,
    _PG_DDL_ENTITY_MENTION,
    *_PG_DDL_ENTITY_MENTION_INDEXES,
    _PG_DDL_EVENT_TYPE,
    _PG_DDL_STORY,
    _PG_DDL_EVENT,
    *_PG_DDL_EVENT_INDEXES,
    _PG_DDL_EVENT_ASSIGNMENT,
    _PG_DDL_DOSSIER,           # without the parent_question FK (added below)
    *_PG_DDL_DOSSIER_INDEXES,
    _PG_DDL_DOSSIER_SECTION,
    _PG_DDL_QUESTION,
    *_PG_DDL_QUESTION_INDEXES,
    _PG_DDL_DOSSIER_PARENT_QUESTION_FK,
    _PG_DDL_EDGE,
    *_PG_DDL_EDGE_INDEXES,
    _PG_DDL_FINDING,           # FKs dossier + question + edge
    *_PG_DDL_FINDING_INDEXES,
    _PG_DDL_FINDING_EVIDENCE,
    *_PG_DDL_FINDING_EVIDENCE_INDEXES,
    _PG_DDL_CLAIM_SIGHTING,
    *_PG_DDL_CLAIM_SIGHTING_INDEXES,
    _PG_DDL_STATEMENT,
    *_PG_DDL_STATEMENT_INDEXES,
    _PG_DDL_POSITION_SHIFT,
    *_PG_DDL_POSITION_SHIFT_INDEXES,
    _PG_DDL_VIEW_SUMMARY,
    _PG_DDL_VERDICT_HISTORY,
    _PG_DDL_CONTRADICTION,
    _PG_DDL_JOB,
    *_PG_DDL_JOB_INDEXES,
    _PG_DDL_JOB_EVENT,
    *_PG_DDL_JOB_EVENT_INDEXES,
    _PG_DDL_FETCH_DOMAIN,
    _PG_DDL_ROBOTS_CACHE,
    _PG_DDL_BEAT_RUN,
    _PG_DDL_WORKSPACE,
    *_PG_DDL_WORKSPACE_INDEXES,
    _PG_DDL_WORKSPACE_CHAT,
    *_PG_DDL_WORKSPACE_CHAT_INDEXES,
    _PG_DDL_WATCH,
    *_PG_DDL_WATCH_INDEXES,
    _PG_DDL_WATCH_HIT,
    *_PG_DDL_WATCH_HIT_INDEXES,
    _PG_DDL_POST,
    *_PG_DDL_POST_INDEXES,
    _PG_DDL_BRIEF,
    _PG_DDL_BRIEF_ITEM,
    *_PG_DDL_BRIEF_ITEM_INDEXES,
    _PG_DDL_VIEW_CURSOR,
    _PG_DDL_CALENDAR_EVENT,
    _PG_DDL_LLM_CALL,
    *_PG_DDL_LLM_CALL_INDEXES,
    _PG_DDL_SOURCE_STATS,
    _PG_DDL_DOCUMENT_EMBEDDING,
    _PG_DDL_EVENT_EMBEDDING,
    _PG_DDL_CLAIM_EMBEDDING,
    *_PG_DDL_EMBEDDING_INDEXES,
)


async def create_all_pg(conn: "psycopg.AsyncConnection") -> None:
    """Create the full PG baseline schema and stamp ``meta.schema_version``.

    Idempotent (IF NOT EXISTS everywhere; the one ALTER is guarded). The
    caller owns the transaction AND the migration advisory lock
    (storage/migrations.py::pg_migrate) — PG DDL is transactional, so a
    failed create leaves nothing behind.
    """
    for ddl in PG_DDL:
        await conn.execute(ddl)
    await conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', %s)"
        " ON CONFLICT (key) DO NOTHING",
        (str(PG_SCHEMA_VERSION),))
