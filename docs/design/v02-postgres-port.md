I have a complete picture of the storage layer. Writing the design document now.

# connect v0.2 — Design Document: The Postgres Port (storage layer + data migration)

Scope: SQLite v9 -> PostgreSQL 17 + pgvector. This document covers schema translation, driver/DAO port, migration tooling, hybrid search, the one-shot ETL, test strategy, phasing, and risks. Tenancy columns (user_id etc.) are a separate workstream; coordination points are flagged where this port must leave room.

---

## 0. Library picks (one-line whys)

| Concern | Pick | Why |
|---|---|---|
| Driver | **psycopg 3 (async) + psycopg_pool** | API is closest to sqlite3 (`execute/fetchone/fetchall`, `dict_row` ≈ `sqlite3.Row`), `%s` params, nested `transaction()` becomes SAVEPOINTs — preserves the existing `with conn:` DAO idiom with a near-mechanical diff; same library does sync for the ETL script. |
| | asyncpg — **rejected** | `$1` placeholders + bespoke fetch API = larger, less mechanical diff; prepared-statement cache fights poolers; speed advantage irrelevant at community scale. |
| Vectors | **pgvector + `pgvector` pip package** | `vector(384)` columns + HNSW in the same database; the pip package registers psycopg adapters so `list[float]` round-trips without `struct.pack`. |
| FTS | **built-in tsvector / GIN** (no extension) | Generated columns replace FTS5 external-content tables AND their 6 sync triggers; `websearch_to_tsquery` never raises, killing the `safe_match` two-attempt fallback. |
| Substring entity search | **pg_trgm** | tsvector is word-based; `ILIKE '%Mod%'` parity for entity name/alias search needs trigram GIN. |
| Migrations | **keep hand-rolled forward-only** (no alembic) | See §3. |
| Test DB | **docker-compose `db-test` service** (postgres:17, tmpfs) | See §6. |

`pyproject.toml`: add `psycopg[binary]`, `psycopg_pool`, `pgvector`; **remove** `sqlite-vec`.

---

## 1. Schema translation: table-by-table

New module `backend/connect/storage/schema.py` is rewritten in place (it stays "the ONLY module with DDL"); `domain/enums.py` and `E.sql_in()` survive verbatim — CHECK vocabularies are still built from enums. New rule: **every CHECK constraint gets an explicit name** (`CONSTRAINT ck_document_media_type CHECK (...)`) so vocab changes become `ALTER TABLE ... DROP CONSTRAINT ck_x, ADD CONSTRAINT ck_x CHECK (...)` — the SQLite copy-out/drop/recreate rebuild dance (migrations v2→3, 3→4, 4→5, 5→6, 7→8, 8→9) **dies**; that entire class of migration becomes one ALTER.

### Global type mapping

| SQLite | Postgres | Notes |
|---|---|---|
| `INTEGER PRIMARY KEY` | `bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY` | ETL inserts explicit ids with `OVERRIDING SYSTEM VALUE`, then `setval`. |
| `TEXT` ISO-8601 timestamps | **`timestamptz`** | See "timestamp decision" below. |
| JSON-in-TEXT (`config`, `aliases`, `attrs`, `properties`, `payload`, `topics`, `reason_json`, `citations`, `answer_finding_ids`, `evidence_snapshot`, `model_usage`, `data`, `content`) | **`jsonb`** with same defaults (`'{}'::jsonb` / `'[]'::jsonb`) | psycopg returns parsed objects; writes wrapped in `psycopg.types.json.Jsonb` — `json.dumps`/`loads` at DAO boundaries deleted. |
| `INTEGER` booleans (`enabled`, `t1_exempt`, `watch_hit`, `promote`, `muted`, `seen`, `speculation`, `is_file`, `is_official`, `summary_stale`) | `boolean` | psycopg maps Python bool natively; `bool(row["watch_hit"])` coercions in mappers become no-ops. |
| `REAL` | `double precision` | direct. |
| `simhash INTEGER` | `bigint` | Both are signed 64-bit; `to_signed64`/`from_signed64` in `t0.py` survive unchanged and ETL copies values verbatim. Optional later optimization: SQL-side prefilter `bit_count((simhash # %s::bigint)::bit(64)) <= 3` replaces the Python window scan in `dedup.find_near_duplicate`; keep the Python scan in the port for parity. |
| `BLOB` vectors | `vector(384)` | document/claim/event embedding tables, see below. |

**Timestamp decision (pick: timestamptz + a global ISO-string loader).** The frozen Pydantic contracts in `domain/models.py` declare timestamps as `str` and dozens of queries compare ISO strings. Storing `timestamptz` but registering one psycopg **custom loader for the timestamptz OID that returns the canonical `YYYY-MM-DDTHH:MM:SS.mmmZ` string** (matching `db.utc_now()` output) gives real types in the database with **zero churn in contracts or row mappers**. Writes keep passing ISO strings — Postgres coerces text params into timestamptz columns. The loader lives in the new `storage/pg.py`. Sites that did string surgery on dates must be rewritten (they are few and enumerated): `spend.py` (`substr(created_at,1,10)` → `(created_at AT TIME ZONE 'utc')::date`, `date('now', ?)` → computed in Python or `(now() AT TIME ZONE 'utc')::date - %s`), `sources.py:121` (`strftime(...'now')` → `fetched_at >= date_trunc('day', now())`), `calendar.py`, `promotion.py:107`, `investigation/tools.py:551`, `investigation/scoping.py:346` (`date('now', '-N days')` → `(now() - interval 'N days')`). Rejected alternative — keep TEXT columns: smallest diff, but a "full port" that leaves timestamps as text forfeits date arithmetic and type integrity forever; the loader trick makes the cost of doing it right ~one adapter module plus six call sites.

### Straight ports (rename types, name the CHECKs, nothing tricky)

`meta`, `source`, `document_topic`, `document_enrichment`, `evidence`, `entity_mention`, `event_type`, `story`, `event`, `event_assignment`, `claim_sighting`, `verdict_history`, `contradiction`, `dossier`, `dossier_section`, `question`, `finding`, `finding_evidence`, `statement`, `position_shift`, `view_summary`, `job`, `watch`, `watch_hit`, `brief`, `brief_item`, `view_cursor`, `calendar_event`, `llm_call`, `source_stats`, `document_link`. All indexes port verbatim (`CREATE INDEX IF NOT EXISTS` is valid PG).

### Tricky cases, called out

**document.** Self-FK `canonical_document_id REFERENCES document(id)` — declare `DEFERRABLE INITIALLY IMMEDIATE` (free at runtime, lets the ETL defer). FTS becomes a generated column (next section). Sketch:

```sql
CREATE TABLE document (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id bigint REFERENCES source(id) ON DELETE SET NULL,
    url text, canonical_url text, title text, author text,
    published_at timestamptz, fetched_at timestamptz NOT NULL,
    media_type text NOT NULL DEFAULT 'html'
        CONSTRAINT ck_document_media_type CHECK (media_type IN (...)),
    language text,
    content_text text NOT NULL,
    content_hash text NOT NULL UNIQUE,
    raw_blob_path text,
    enrichment_tier int NOT NULL DEFAULT 0,
    enrichment_status text NOT NULL DEFAULT 'pending'
        CONSTRAINT ck_document_enrichment_status CHECK (...),
    simhash bigint,
    canonical_document_id bigint REFERENCES document(id) DEFERRABLE INITIALLY IMMEDIATE,
    watch_hit boolean NOT NULL DEFAULT false,
    search_tsv tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(title,'')), 'A') ||
        setweight(to_tsvector('english', left(content_text, 200000)), 'B')
    ) STORED
);
CREATE INDEX idx_document_tsv ON document USING gin(search_tsv);
```

The `left(..., 200000)` guard exists because tsvector rows cap at ~1MB and positions at 16383 — a runaway PDF must not fail the INSERT. `to_tsvector('english', ...)` with explicit config is IMMUTABLE, so generated columns are legal. **All six FTS triggers die**; external-content desync (a real FTS5 failure mode) becomes impossible.

**claim.** Same pattern: `search_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED` + GIN.

**entity (incl. aliases).** Aliases become `jsonb NOT NULL DEFAULT '[]'::jsonb`. Two search paths:
- Ranked word search: `search_tsv GENERATED ALWAYS AS (to_tsvector('simple', name || ' ' || coalesce(aliases::text,''))) STORED` + GIN. The `jsonb::text` cast is immutable and deterministic; brackets/quotes are punctuation the parser discards — no trigger, no flattening function. `'simple'` config because proper names must not be stemmed.
- Substring parity (current behavior is `LIKE '%q%'` over name and the aliases JSON text): `CREATE INDEX idx_entity_name_trgm ON entity USING gin (name gin_trgm_ops)` and `... USING gin ((aliases::text) gin_trgm_ops)`; `entities.list_page` keeps its ILIKE shape (note: PG `LIKE` is case-sensitive — switch to `ILIKE`, and the `ESCAPE '\'` clause ports as-is). This preserves UX exactly while the tsvector powers the ranked `/search` surface.

**edge (polymorphic + bitemporal).** Ports almost verbatim — Postgres partial unique indexes are native:

```sql
CREATE UNIQUE INDEX idx_edge_active_unique
    ON edge(src_type, src_id, dst_type, dst_id, relation)
    WHERE status = 'active';
```

`superseded_by_edge_id REFERENCES edge(id)` points to **newer** rows — declare `DEFERRABLE INITIALLY IMMEDIATE` for the ETL. `properties` → jsonb. The SCD2 discipline (close, never delete; uniqueness only over active) transfers with zero semantic change. The idempotent insert maps to `INSERT ... ON CONFLICT (src_type, src_id, dst_type, dst_id, relation) WHERE status = 'active' DO NOTHING RETURNING id` — PG supports conflict targets on partial indexes via the index predicate; `edges.insert` returns `None` exactly when no row comes back, and `insert_causal`'s re-select fallback survives unchanged.

**dossier ↔ question circular FKs.** SQLite resolved FK targets at DML time; Postgres validates at CREATE TABLE. Order DDL as: `dossier` (without the `parent_question_id` FK) → `question` → `ALTER TABLE dossier ADD CONSTRAINT fk_dossier_parent_question FOREIGN KEY (parent_question_id) REFERENCES question(id) DEFERRABLE INITIALLY IMMEDIATE`. Both directions DEFERRABLE for the ETL.

**job_event.** `seq INTEGER PRIMARY KEY AUTOINCREMENT` → `seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY`. Caveat to document in the DDL comment: identity values are allocated at insert, not commit — globally, a later-committed row can carry a smaller seq. This is harmless here because SSE replay is always scoped `WHERE job_id = %s AND seq > %s` and one job's events are written sequentially by one worker task, so per-job monotonicity (the only property the SSE contract needs) holds.

**Vector tables.** Keep the v0.1 layout (separate 1:1 tables, not columns on the aggregate — preserves "vector availability never breaks the aggregate" and keeps `model` provenance):

```sql
CREATE TABLE document_embedding (
    document_id bigint PRIMARY KEY REFERENCES document(id) ON DELETE CASCADE,
    model text NOT NULL,
    embedding vector(384) NOT NULL
);
CREATE INDEX idx_document_embedding_hnsw
    ON document_embedding USING hnsw (embedding vector_cosine_ops);
```

Same for `claim_embedding` and `event_embedding` (dim column dies — the type enforces 384). HNSW on documents; claims/events were brute-force in v0.1 and a bare `ORDER BY embedding <=> %s LIMIT k` (exact scan) reproduces that semantics — add their HNSW indexes anyway, it costs nothing and removes a scale cliff. The `vec_document` virtual table, `VEC_DOCUMENT_DDL`, `SqliteVecIndex`, `BruteForceIndex`, and the `struct.pack` helpers in `knowledge/vector.py` all die; `VectorIndex` Protocol survives with two implementations: `PgVectorIndex` (backend `"pgvector"`) and `DisabledIndex` (embeddings off — tests).

Extensions migration v1 runs first: `CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pg_trgm;` (the compose postgres image is `pgvector/pgvector:pg17`, so the extension is present).

**Tenancy coordination points (do NOT implement here):** `watch`, `brief`(+`brief_item`), `view_cursor` (PK grows to `(user_id, surface, ref_id)`), `dossier`, `llm_call` (per-user budgets), `job` will gain `user_id` in the tenancy workstream as PG migrations v2+. The port keeps them 1:1 with v9 so the ETL is a pure copy; nothing in this DDL blocks those ALTERs.

---

## 2. Driver + DAO port

### Connection architecture

New `backend/connect/storage/pg.py` replaces `storage/db.py`:
- `create_pool(dsn, *, min_size, max_size) -> AsyncConnectionPool` with a `configure` hook per connection: `row_factory=dict_row`, register pgvector adapter, register the timestamptz→ISO-string loader.
- `utc_now()` survives verbatim (still the one clock, still returns the ISO string).
- `init_db(pool)` → advisory-lock-guarded migrate (§3).

The single shared `sqlite3.Connection` on the Container becomes `Container.pool`. `api/deps.get_db` becomes an async dependency: `async with pool.connection() as conn: yield conn` — request-scoped connection, returned to the pool at response end. Services (`EnrichmentService`, `AnalysisService`, `InvestigationService`, `SourcePoller`, `JobRunner`, `Governor`) take the **pool** and acquire a connection per unit of work (per job, per poll tick, per check). `Container.startup()` becomes `async def` (the lifespan already awaits; tests' `c.startup()` calls gain `asyncio.run`/fixture await).

**Pool sizing:** API replica `min=2, max=10`; worker replica `max = job_concurrency + 3` (jobs + poller + governor/spend reads). Default `max_connections=100` comfortably holds 2 API + 2 worker replicas (~46 peak) with headroom for psql/ETL. SSE endpoints must not hold a pooled connection across the stream — acquire per poll tick.

### Transaction discipline

The pervasive DAO idiom `with conn:` (commit-or-rollback) maps 1:1 to `async with conn.transaction():`. Critically, psycopg's `transaction()` **nests as SAVEPOINTs**, so a caller (e.g., `enrichment/persist.py`, which makes many DAO calls per document) can wrap an outer transaction around DAO functions that open their own — both the v0.1 "each DAO write is atomic" behavior and future multi-call atomicity work without rewriting DAOs. Connections run non-autocommit; reads outside explicit transactions ride implicit ones as today.

### The mechanical port recipe (applies to all 15 DAO modules + the 12 service modules with inline SQL)

1. `def f(conn: sqlite3.Connection, ...)` → `async def f(conn: psycopg.AsyncConnection, ...)`; callers add `await`. Routers and services are already async (verified: routers, pipeline, sweep, runner), so the ripple is wide but shallow. The few sync paths (`IngestionPipeline.ingest_text`, seed functions, `Container.startup`) become async.
2. `?` placeholders → `%s` (pure textual; the dynamic `','.join('?' * n)` sites in `spend.py`/list-builders become `','.join(['%s'] * n)`).
3. `conn.execute(...).fetchone()` → `cur = await conn.execute(...)`, `await cur.fetchone()`. `dict_row` keeps `row["col"]` working; positional `row[0]` sites (a handful) switch to names.
4. `cur.lastrowid` → append `RETURNING id` and `(await cur.fetchone())["id"]` (16 modules — `documents`, `edges`, `jobs`, `spend`, `sources`, `watches`, `briefing`, `persist`, `writeback` ×2, `questions`, `runner`, `event_clusterer`, `story_threader`, `pipeline`, `tools`).
5. `INSERT OR IGNORE` → `ON CONFLICT (...) DO NOTHING` with the explicit conflict target (`watch_hit` PK, `document_link(document_id,url)`, `event_type(name)`, `brief(brief_date)`, `document_topic` PK, `entity(name,entity_type)`, edge partial index as in §1). `cur.rowcount` still distinguishes inserted vs ignored.
6. `INSERT OR REPLACE` → `INSERT ... ON CONFLICT (pk) DO UPDATE SET ...` (`event_embedding`, `claim_embedding`, `view_summary`, vector adds). Semantic note: OR REPLACE deletes+reinserts (fires cascades); DO UPDATE doesn't — strictly an improvement, no current behavior depends on the delete.
7. `json.loads(row["x"])` / `json.dumps(v)` → delete / wrap in `Jsonb(v)` (jsonb columns round-trip as Python objects).
8. `json_each` (4 sites: `statements.py` ×3, `position_tracker.py` ×3, `synthesize.py:234`) → `jsonb_array_elements_text(s.topics)` in a LATERAL/EXISTS — shape-identical rewrites.
9. Date-function sites: the enumerated list in §1.
10. `LIKE` → `ILIKE` where case-insensitive behavior was assumed (SQLite LIKE is case-insensitive for ASCII; PG's is not — **this is the sneakiest SQLite-ism in the codebase**: `entities.list_page`, watch query matching, any LIKE search path).
11. `bm25(document_fts)` / `snippet(...)` / `MATCH` → §4.
12. Plain CTEs (`entity_stats`, co-occurrence `pairs`) port verbatim; there are no recursive CTEs in the codebase; integer division gotchas don't apply (the one ratio already multiplies by `1.0`).

`storage/fts.py` is rewritten (same module name, same public signatures) — see §4. `storage/migrations.py` is replaced by the PG version (§3); the SQLite v1–v9 migration chain is retired with the ETL.

---

## 3. Migration tooling: keep hand-rolled, add advisory lock

**Pick: keep the hand-rolled forward-only pattern.** Rationale: the project's philosophy is raw SQL + a single DDL home + explicit forward-only functions; alembic brings an env.py harness, revision graphs, and autogenerate temptation — machinery this project deliberately rejected, solving problems (branching, downgrades, ORM model diffing) it doesn't have. Postgres makes hand-rolled *simpler* than SQLite: DDL is transactional (each migration is atomic — a failed migration leaves no half-rebuilt tables) and CHECK constraints are ALTER-able (the rebuild pattern dies).

Mechanics (new `storage/migrations.py`):

- **Fresh lineage**: PG `meta.schema_version` starts at **1** (the full v0.2 schema). The SQLite v9 lineage is closed; the ETL targets PG v1. Code refuses a newer DB (`StorageVersionError` preserved verbatim).
- **Concurrency**: many api/worker replicas race at startup. `init_db` does:
  ```
  async with conn.transaction():
      await conn.execute("SELECT pg_advisory_xact_lock(%s)", (CONNECT_MIGRATION_LOCK_KEY,))
      version = read_version(conn)        # re-read AFTER acquiring the lock
      for n in range(version, SCHEMA_VERSION): MIGRATIONS[n](conn)
      stamp(conn, SCHEMA_VERSION)
  ```
  One 64-bit constant lock key (e.g. `0x636f6e6e656374` — "connect"). `pg_advisory_xact_lock` releases automatically on commit/rollback/crash — no leak path. Losers of the race block, then re-read the version and find nothing to do. Each migration version runs in this one transaction; a migration that needs `CREATE INDEX CONCURRENTLY` (not transaction-safe) is out of scope at this data size — plain `CREATE INDEX` is milliseconds.
- Migration registry stays `MIGRATIONS: dict[int, Callable]` of `migrate_N_to_N+1(conn)`; `schema.py` remains the single DDL home and exports the constants migrations mirror, exactly as today.

---

## 4. RRF hybrid search port

`retrieval/search.py` becomes the actual hybrid layer it was named for:

- **Lexical** (`storage/fts.py`, rewritten same-signature): 
  ```sql
  SELECT d.id, ts_rank_cd(d.search_tsv, q) AS rank
  FROM document d, websearch_to_tsquery('english', %s) q
  WHERE d.search_tsv @@ q
  ORDER BY rank DESC LIMIT %s
  ```
  `websearch_to_tsquery` accepts arbitrary user input without ever raising — the `safe_match` quoted-phrase fallback and its `sqlite3.OperationalError` retry loop are deleted. `match_document` (watch matching) becomes `WHERE id = %s AND search_tsv @@ websearch_to_tsquery('english', %s)`.
- **Vector**: `PgVectorIndex.search(vector, k)` → `SELECT document_id, 1 - (embedding <=> %s::vector) AS sim FROM document_embedding ORDER BY embedding <=> %s::vector LIMIT %s` (HNSW-served).
- **Fusion in Python, in `retrieval/search.py`** (not one mega-SQL): RRF with k=60 over the two ranked id lists, because the `VectorIndex` seam must stay a seam (the `DisabledIndex`/`NullEmbedder` path degrades hybrid → lexical-only with zero branching beyond an empty list) and the retrieval layer's job is composing DAOs. Fused page of ids is hydrated via one `WHERE d.id = ANY(%s)` query.
- **Snippets / XSS discipline preserved**: `ts_headline` replaces FTS5 `snippet()`, run **only over the final fused page** (it is expensive on long documents — never in the ranking query):
  ```sql
  ts_headline('english', d.content_text, q,
      'StartSel=' || E'\x02' || ', StopSel=' || E'\x03' ||
      ', MaxFragments=2, MaxWords=24, FragmentDelimiter=…')
  ```
  The exact v0.1 mechanic survives: control-char markers pass through `html.escape`, then swap to `<mark>`/`</mark>` — `_escape_snippet` in `fts.py` is reused byte-for-byte, so raw document content still never reaches the client unescaped. (Documents found only by vector get a headline against the same tsquery; zero matches yields the leading fragment, which is acceptable v0.1-parity behavior for the snippet line.)
- Entity search keeps ILIKE+trigram (§1) for substring parity; claim search (used by verification/reconciliation) gets the same tsvector treatment on `claim.search_tsv`.

---

## 5. One-shot ETL: `backend/connect/tools/etl_sqlite_to_pg.py`

Sync script (psycopg sync side; sqlite3 stdlib), runnable inside the compose network or on the host. Flow:

1. **Open SQLite read-only** (`file:...?mode=ro`) — the source physically cannot be corrupted. Refuse if `meta.schema_version != 9`.
2. **Create target**: `--create-db` connects to `postgres` maintenance DB, `DROP DATABASE IF EXISTS connect` (only with `--force`), `CREATE DATABASE connect`, then runs `schema.create_all` + stamps PG v1 (the same code path the app uses — never a second DDL source).
3. **Copy in FK-topological order**, one big transaction, `SET CONSTRAINTS ALL DEFERRED` (this is why §1 marks the self/circular FKs DEFERRABLE):
   `source` → `document` (id order; `canonical_document_id` deferred) → `document_link`, `document_topic`, `document_enrichment` → `entity` → `entity_mention` → `event_type`, `story`, `event`, `event_assignment` → `dossier` + `question` (deferred circular pair) → `edge` (deferred `superseded_by_edge_id`) → `claim`, `evidence`, `claim_sighting`, `verdict_history`, `contradiction` → `finding`, `finding_evidence` → `statement`, `position_shift`, `view_summary` → `job`, `job_event` → `watch`, `watch_hit`, `brief`, `brief_item`, `view_cursor`, `calendar_event`, `llm_call`, `source_stats` → embeddings.
   All identity inserts use `OVERRIDING SYSTEM VALUE` with explicit ids; per-table value transforms: int→bool columns, JSON text→`Jsonb`, ISO text timestamps pass through (PG parses), `aliases`/`topics` JSON arrays validated (`json.loads` or `'[]'`). Batched `executemany` (psycopg pipelines this); at ~12MB/900 entities/330 docs the whole run is seconds — `COPY` is unnecessary complexity.
4. **Vectors copied, not recomputed**: `struct.unpack(f"{len(b)//4}f", blob)` → `pgvector` list; assert dim == 384, skip-and-log otherwise. **No re-embedding** (same bge-small vectors). **tsvector columns regenerate themselves** (generated columns — zero ETL work, and FTS5-vs-tsvector desync is structurally impossible).
5. **Sequences**: `SELECT setval(pg_get_serial_sequence('document','id'), max(id))` for every identity table incl. `job_event.seq`.
6. **Verification (runs automatically, non-zero exit on mismatch)**:
   - row counts per table, SQLite vs PG;
   - order-insensitive checksums on content-bearing tables, computed identically on both sides in Python (e.g., sha256 over sorted `content_hash` list; sum of `simhash`; sha256 over sorted `(claim_id, document_id, stance)` for evidence);
   - spot semantic checks: top-10 FTS ids for 3 canned queries overlap ≥ 7/10 (rankers differ; identity isn't expected), cosine(doc_k embedding, fixed probe) equal within 1e-6 for 10 sampled docs, active-edge unique index count == SQLite active edge count.
7. **Blob store untouched**: `raw_blob_path` strings copy verbatim; compose mounts the existing content-addressed blob dir into api/worker containers at the same path (or `CONNECT_BLOB_DIR` points at the volume). No blob bytes move.
8. **Rollback story**: the source is read-only and stays on disk as the archive; the target is disposable until cutover (`--force` re-runs drop/recreate). Cutover = flip `CONNECT_DATABASE_URL` and stop the SQLite-era process; roll back = point v0.1 back at the untouched `.db` file. No dual-write window — this is a one-shot offline migration measured in seconds.

---

## 6. Test strategy

**Pick: a `db-test` service in docker-compose** (postgres pgvector image, `tmpfs: /var/lib/postgresql/data`, `fsync=off full_page_writes=off synchronous_commit=off`, port 5433). Rationale against the alternatives: **testcontainers** spawns/pulls containers from inside pytest — first run breaks the no-network discipline and adds a dependency to do what compose already does in this project; **pg_tmp** needs host-installed postgres binaries, a second install path that diverges from the deployment story on a docker-compose project. `docker compose up -d db-test` is an explicit, documented dev step (like installing deps); pytest itself stays no-network — Postgres on loopback is blessed as local infra, same standing as the SQLite file. Tests **skip with a loud message** when `CONNECT_TEST_DATABASE_URL` is unreachable.

Fixture design (`tests/conftest.py`):
- session-scoped: connect, create `connect_template` database, apply `schema.create_all` once;
- per-xdist-worker: `CREATE DATABASE connect_test_{worker} TEMPLATE connect_template` (~150ms, once per worker);
- function-scoped: `TRUNCATE <all tables> RESTART IDENTITY CASCADE` (one statement, ~10ms) — replaces "fresh tmp_path db per test" with equivalent isolation;
- `settings` fixture gains `database_url`; `container`/`client` fixtures otherwise unchanged — **MockProvider/no-LLM discipline untouched** (it lives above the storage layer).

Test triage:
- **Port mechanically** (fixture + await churn only): all API tests, enrichment, clustering, threading, position tracking, watches, briefing, spend, investigation suite — the overwhelming majority of the 367.
- **Survive untouched**: pure-function tests (`test_simhash`, t0 normalize/hash, adapters, extract_*, search_client, mock_llm plumbing).
- **Die, replaced**: `test_migrations.py` (SQLite rebuild chain) → new PG tests: fresh-create == migrated parity (when v2 exists), version-refusal, and an advisory-lock race test (two concurrent `init_db`s, one applies, both succeed); `test_vector.py` backend-fallback cases → `PgVectorIndex` add/search + HNSW recall smoke; `test_schema.py` assertions against `sqlite_master` → `information_schema`/`pg_catalog` equivalents or deletion where they tested SQLite mechanics (trigger existence).
- **New**: ETL round-trip test (build a small SQLite v9 fixture via the existing factories, run the ETL into the test PG, assert verification passes), fts XSS-escaping parity test, RRF fusion unit test.

---

## 7. Phasing

- **P1 — storage core**: `storage/pg.py` (pool, loaders, utc_now), rewritten `schema.py` (full PG v1 DDL), rewritten `migrations.py` (advisory lock), compose `db-test`, conftest infra. Exit: schema creates clean, migration race test green.
- **P2 — mechanical DAO port**: the 15 `storage/*` DAOs + inline-SQL service modules, in dependency order (documents/sources/fts → entities/edges/links → events/statements → jobs/spend → analyses/investigations), `deps.py`, Container→pool, async `startup()`. This is the bulk diff; land it as one branch with the ported test suite — there is no value in a half-ported tree. Exit: full suite green on PG.
- **P3 — retrieval**: `PgVectorIndex`, RRF fusion in `retrieval/search.py`, `ts_headline` snippets, entity trigram search. Exit: search API parity tests + XSS test green.
- **P4 — ETL**: `tools/etl_sqlite_to_pg.py` + round-trip test; run against the live 12MB DB into a staging PG; eyeball the app against it.
- **P5 — cutover & cull**: flip `CONNECT_DATABASE_URL`, archive the `.db`, delete dead code (`sqlite-vec` dep, `SqliteVecIndex`/`BruteForceIndex`, FTS5 helpers, SQLite migration chain v1–9, `check_same_thread` machinery). Exit: no `import sqlite3` outside the ETL tool.

## Risk register (top 5)

1. **The async ripple is wider than the storage layer** — every DAO call site across analysis/investigation/knowledge gains `await`; a missed sync call (e.g., `ingest_text`, seeds) deadlocks or blocks the loop. *Mitigation*: make DAO functions `async def` so any un-awaited call is an immediately visible coroutine warning; CI greps for `sqlite3` and for sync `conn.execute` patterns; P2 lands atomically with the full suite.
2. **SQLite LIKE case-insensitivity silently becomes case-sensitive** — entity search and watch matching would "work" but return fewer rows, the kind of regression tests miss. *Mitigation*: dedicated case-folding parity tests written against v0.1 behavior before the port; blanket `LIKE`→`ILIKE` sweep in review checklist.
3. **Single-writer assumptions hiding outside the schema** — v0.1 had one process, one connection; read-modify-write sequences (event clustering doc_count, story doc_count, governor check-then-spend) were race-free by construction. Under a pool + multiple replicas they aren't. *Mitigation*: in this port, wrap known RMW sequences in explicit transactions with `SELECT ... FOR UPDATE`; flag governor check-then-record as a known benign race (worst case slight overspend) for the multiuser workstream to own with per-user budgets.
4. **tsvector behavioral drift from FTS5** (stemming, stopwords, phrase semantics) changes search results and watch hits. *Mitigation*: canned-query overlap checks in ETL verification; `websearch_to_tsquery` chosen specifically for forgiving user syntax; watch `query_fts` strings reviewed once at migration (there is one user today).
5. **ETL silently drops or mangles edge-case rows** (NULL JSON, malformed aliases, dangling FKs that SQLite tolerated with `PRAGMA foreign_keys` historically off during rebuilds). *Mitigation*: deferred-constraint single transaction means any dangling FK aborts the whole run loudly; per-table counts + checksums are mandatory, not optional; a pre-flight `PRAGMA foreign_key_check` on the source.

## Open questions (user-owned)

1. **FTS language config**: corpus is Indian English news with Hindi names/terms — `'english'` (stemming) is proposed for documents/claims; if Hindi-script content is expected in v0.2, `'simple'` for documents too (no stemming, better for mixed-script) is the safer pick. Which?
2. **Postgres major version & image**: design assumes `pgvector/pgvector:pg17` in compose — any constraint from the target VPS that forces an older major?
3. **Cutover data**: is the live ~12MB DB the only SQLite instance to migrate, or do other machines' DBs need merging (the ETL as specified is single-source, id-preserving — merging would be a different tool)?

### Critical Files for Implementation

- /home/nomam/workspace/connect/backend/connect/storage/schema.py — rewritten as the PG DDL home (every table, named CHECKs, generated tsvector columns, pgvector tables)
- /home/nomam/workspace/connect/backend/connect/storage/db.py — replaced by `storage/pg.py` (pool factory, timestamptz ISO loader, pgvector adapter, utc_now)
- /home/nomam/workspace/connect/backend/connect/storage/migrations.py — replaced with the advisory-lock forward-only PG applier
- /home/nomam/workspace/connect/backend/connect/storage/fts.py — FTS5 → tsvector/ts_rank_cd/ts_headline with the escape mechanic preserved; consumed by /home/nomam/workspace/connect/backend/connect/retrieval/search.py for RRF fusion
- /home/nomam/workspace/connect/backend/connect/orchestration/container.py — conn→pool composition root change that every service wiring flows through (plus new /home/nomam/workspace/connect/backend/connect/tools/etl_sqlite_to_pg.py)