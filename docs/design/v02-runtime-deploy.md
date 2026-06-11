I have what I need on Anthropic rate-limit behavior (SDK auto-retry on 429/529 with `retry-after`, Batches limits: 100k requests/256MB, ~1h typical, 24h max). Here is the design document.

---

# connect v0.2 — Runtime, Workers, and Deployment (Design)

Scope: process topology, job queue, cross-process SSE, scheduling/politeness, LLM concurrency, blobs, docker-compose, observability. Assumes the PG port (schema/DAOs) and auth/tenancy (Google OAuth, `app_user`, per-user visibility) are designed by their own workstreams; integration points are called out explicitly. All paths relative to `/home/nomam/workspace/connect`.

Philosophy preserved: raw SQL, frozen Pydantic contracts, one composition root, hand-rolled forward-only migrations, grounding discipline untouched, **no broker** — Postgres is the queue, the bus, and the shared rate-limit state. **Decision: no Redis.** At hundreds of users the system runs at most tens of jobs/minute and a few hundred SSE connections; SKIP LOCKED + LISTEN/NOTIFY cover both with margin. Revisit only if measured NOTIFY fan-out or claim contention hurts (it won't at this scale).

---

## 1. Process topology

Six containers on one box, VPS-ready:

```
caddy (reverse proxy, TLS)
 ├── /api/*  ──────────────► api        uvicorn, 2 procs (`--workers 2`)
 └── /*      ──────────────► frontend   next start (standalone)
postgres (pgvector/pgvector:pg17)
worker  (xN, default 2)     python -m connect.workers.main
migrate (one-shot)          python -m connect.tools.migrate
backup  (prod profile)      crond: pg_dump + blob rsync
```

- **No separate beat container.** The scheduler is a coroutine inside every worker; exactly-one is elected via a Postgres advisory lock (§4). One fewer service, no single point that can be forgotten in scaling.
- **api**: uvicorn `--workers 2` in one container. Each worker process opens its own psycopg pool + one dedicated LISTEN connection. The api **never executes jobs** — it enqueues, reads, and streams. The lifespan poller (`Container.start_background`) is removed from the api flavor.
- **worker**: new entrypoint, claims jobs from the PG queue, runs all background work (polls, enrichment, analyses, investigations, briefs). Scale with `docker compose up -d --scale worker=3`.
- **Proxy pick: Caddy** over nginx. Why: automatic Let's Encrypt TLS when `CONNECT_DOMAIN` is set (the whole VPS TLS story is two Caddyfile lines), automatic self-signed certs for `localhost`, and it does not buffer streamed responses by default — the nginx SSE footguns (`proxy_buffering`, `X-Accel-Buffering`) disappear. nginx wins only on exotic tuning we don't need.
- **DB driver pick: psycopg3** (`psycopg[binary,pool]`) — async for api/workers, the *same* driver sync for migrations/CLI/tests, first-class LISTEN/NOTIFY (`AsyncConnection.notifies()`), server-side params. asyncpg is faster but adds a second param style and no sync mode. (Final call coordinated with the storage workstream; this design only requires "async PG driver with LISTEN/NOTIFY".)

The composition root splits by flavor but stays the only wiring site:

```
backend/connect/orchestration/container.py   # shared graph (db pool, blobs, fetcher, llm, services)
backend/connect/orchestration/runtime.py     # NEW: ApiRuntime / WorkerRuntime — which background pieces start where
```

---

## 2. Job queue: Postgres SKIP LOCKED (no arq, no Redis)

**Pick: port the `job` table to a SKIP LOCKED queue.** Rationale vs arq+Redis: jobs are *already* durable rows whose ids appear in API contracts (`JobAccepted.job_id`, `job_event.seq` = SSE id); arq would duplicate that state in Redis and force a two-phase enqueue. PG gives transactional enqueue (dossier row + job row commit atomically), and the throughput ceiling of SKIP LOCKED (thousands/sec) is ~3 orders of magnitude above community-scale need.

### DDL sketch (replaces v0.1 `job`)

```sql
CREATE TABLE job (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind             TEXT NOT NULL,            -- CHECK from domain/enums.JOB_KINDS
    priority         SMALLINT NOT NULL DEFAULT 50,  -- 10 interactive, 50 enrichment, 90 polls
    payload          JSONB NOT NULL DEFAULT '{}',
    status           TEXT NOT NULL DEFAULT 'queued', -- queued|running|done|failed|cancelled
    user_id          BIGINT REFERENCES app_user(id), -- NULL = system job
    dossier_id       BIGINT REFERENCES dossier(id),
    attempts         INT NOT NULL DEFAULT 0,
    max_attempts     INT NOT NULL DEFAULT 1,    -- polls get 1; idempotent batch-poll gets 3
    run_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
    claimed_by       TEXT,                      -- worker id: "{hostname}:{pid}"
    heartbeat_at     TIMESTAMPTZ,
    error            TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at       TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ
);
CREATE INDEX idx_job_claim ON job (priority, run_at, id) WHERE status = 'queued';
CREATE INDEX idx_job_heartbeat ON job (heartbeat_at) WHERE status = 'running';
-- dedup: at most one live poll job per source
CREATE UNIQUE INDEX uq_job_poll_source ON job ((payload->>'source_id'))
  WHERE kind = 'poll_source' AND status IN ('queued','running');
```

### Claim (one statement, atomic)

```sql
WITH next AS (
  SELECT id FROM job
  WHERE status='queued' AND run_at <= now() AND kind = ANY(%(kinds)s)
  ORDER BY priority, run_at, id
  LIMIT 1
  FOR UPDATE SKIP LOCKED
)
UPDATE job j SET status='running', claimed_by=%(worker)s,
       started_at=now(), heartbeat_at=now(), attempts=attempts+1
FROM next WHERE j.id = next.id
RETURNING j.*;
```

`kinds` is computed per claim from the worker's free capacity slots — this is how **per-kind concurrency limits** work without a broker: each worker holds a local map, e.g. `{investigation: 1, analysis: 2, enrich_t1_batch: 1, poll_source: 4, default: 4}`; a kind with no free slot is excluded from the next claim. Cluster-wide caps for the expensive kinds (e.g. max 3 concurrent investigations across all workers) use advisory-lock slots: try `pg_try_advisory_lock(KIND_NS, slot_i)` for `i in range(cap)` after claiming; on failure, release the job back to `queued` with `run_at = now() + 5s`. Simple, crash-safe (locks die with the session).

### The closure problem — the one real refactor

`JobRunner.submit(kind, payload, runner_coroutine)` cannot cross processes. Replace with a **handler registry**:

```
backend/connect/workers/__init__.py
backend/connect/workers/queue.py      # enqueue/claim/heartbeat/finish/reclaim DAO (raw SQL above)
backend/connect/workers/registry.py   # HANDLERS: dict[str, Handler]; Handler = (ctx: WorkerContext, payload: dict) -> Awaitable[str|None]
backend/connect/workers/handlers/     # analysis.py, investigation.py, enrichment.py, poll.py, brief.py, ingest.py
backend/connect/workers/main.py       # entrypoint: claim loop(s), LISTEN, beat candidacy, signals
```

Services change from "build a closure and submit" to "persist state, enqueue a self-describing payload": `AnalysisService.start()` already creates the dossier row first — it now enqueues `('analysis', {'analysis_id': id}, user_id=...)` and the worker handler reconstructs the run from the row. Payloads in v0.1 are already nearly self-describing (`document_id`, `limit`, `target`), so the refactor is mechanical but touches every service + its tests. Test strategy: an in-process `ImmediateQueue` test double implementing the same `enqueue()` API plus a `run_pending()` helper that dispatches through the real registry — keeps the 367 no-network tests meaningful.

### Wake-up, cancellation, retries, orphans

- **Wake-up**: workers `LISTEN job_new`; api executes `SELECT pg_notify('job_new', kind)` *in the enqueue transaction* (NOTIFY delivers on commit — exactly the semantics we want). Idle workers also poll every 1s as the missed-NOTIFY backstop. Interactive latency ≈ ms.
- **Cancellation across processes**: API sets `cancel_requested=TRUE`; if status is still `queued`, a CAS `UPDATE ... SET status='cancelled' WHERE status='queued'` finishes it immediately. If running, api also `pg_notify('job_control', '{"job_id":N}')`; every worker LISTENs and cancels the local asyncio task if it owns the job. Belt-and-braces: a `CancelToken` (reads `cancel_requested` at stage/iteration boundaries — analysis stages, investigation loop iterations already have natural checkpoints) so cancellation works even if the NOTIFY is lost. Replaces `JobRunner.cancel_job`'s task-name scan.
- **Heartbeats + orphan reclaim**: workers `UPDATE job SET heartbeat_at=now() WHERE id = ANY(...)` every 15s for their running jobs. Beat sweeps every 60s: `running` with `heartbeat_at < now()-'90 seconds'` → requeue if `attempts < max_attempts`, else `failed: 'orphaned'` + terminal `job_event`. This **replaces** `reconcile_orphans()` at startup, which is wrong with >1 process.

---

## 3. Cross-process SSE

`job_event` rows remain the durable replay log and `seq` remains the SSE id — the v0.1 client contract (replay via `?after=`/`Last-Event-ID`, terminal event ends stream) is preserved byte-for-byte.

```sql
CREATE TABLE job_event (
    seq    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id BIGINT NOT NULL REFERENCES job(id) ON DELETE CASCADE,
    ts     TIMESTAMPTZ NOT NULL DEFAULT now(),
    type   TEXT NOT NULL,
    data   JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_job_event_job ON job_event(job_id, seq);
```

**Channel/payload design**: writer (worker) inserts the row and, in the same autocommit statement batch, `pg_notify('job_events', '{"job_id":123,"seq":456}')`. Payload is the pointer only — never the data (8000-byte NOTIFY limit; rows are the truth). One channel for all jobs; api-side filtering is a dict lookup. Critical port detail: v0.1 commits each event write immediately (`with conn:` per insert) — keep **autocommit-per-event** in PG (events must not sit invisible inside a long handler transaction).

**API side** — `backend/connect/orchestration/bus.py`:

- Per api process: one dedicated LISTEN connection (`LISTEN job_events; LISTEN job_new_noop`), an `EventBus` holding `dict[job_id, set[asyncio.Queue]]`.
- SSE endpoint flow (same for analyses/investigations/jobs): (1) subscribe to bus for `job_id` *first*, (2) replay rows `seq > after`, (3) on each bus ping fetch rows `> last_seq` and yield, (4) fallback re-query every 5s (missed-NOTIFY/listener-reconnect backstop — degrades to v0.1's polling, never breaks), (5) terminal event or job-row-terminal safety valve ends the stream, exactly like the current `analyses.py` logic.
- LISTEN connection death: bus reconnects with backoff; while down, endpoints are on the 5s fallback poll — degraded latency, no lost events (rows are replayable).

**Connection scaling & proxy flags**: a few hundred idle async SSE generators are trivial for uvicorn; set container `ulimits: nofile: 65536`. sse-starlette's built-in ping (15s) defeats idle timeouts. Caddyfile: `reverse_proxy api:8000 { flush_interval -1 }` for `/api/*` (explicit, though Caddy auto-detects streams). **Do not route SSE through Next**: in production Caddy splits `/api/*` directly to the backend, so Next's rewrite (and its buffering/build-time-env behavior) is out of the request path entirely.

---

## 4. Beat, poller, and shared politeness

### Exactly-one-beat

`backend/connect/workers/beat.py`: every worker runs `beat_candidate()` — try `pg_try_advisory_lock(LOCK_BEAT)` on a **dedicated connection** (advisory locks are session-scoped; if the worker dies, PG releases it). Holder runs the tick loop; losers retry every 30s. Beat **only enqueues**, never works:

| Tick (60s) | Action |
|---|---|
| due sources | `SELECT` sources where `last_polled_at + interval < now()` (logic ported from `poller._is_due`) → enqueue one `poll_source` job each, priority 90; the partial unique index dedups |
| orphan sweep | reclaim stale-heartbeat jobs (§2) |
| nightly batch | at `CONNECT_NIGHTLY_SWEEP_UTC_HOUR`: enqueue `enrich_t1_batch` (priority 50); guarded by a `beat_run(task TEXT PRIMARY KEY, last_run_at TIMESTAMPTZ)` row so restarts don't double-fire |
| brief pre-gen | nightly, staggered: enqueue `brief_generate` per user active in the last 7 days (priority 60, `run_at` spread over an hour); inactive users get on-demand generation |

`SourcePoller`'s per-source logic (adapter dispatch, caps, dedup, statuses) moves intact into `workers/handlers/poll.py` — one source per job means polls parallelize across workers and a slow feed can't stall the cycle, which the v0.1 sequential loop allowed.

### Per-domain politeness across processes — pick: PG-reserved slots

Single-flight fetcher worker rejected: it turns every interactive analysis/investigation web fetch into an RPC hop and a new failure mode. Instead the Fetcher's `_throttle` becomes an atomic PG slot reservation (Fetcher API unchanged — same seam, new backing):

```sql
-- backend/connect/storage/fetchstate.py
INSERT INTO fetch_domain (domain, next_at) VALUES (%(d)s, now() + %(iv)s)
ON CONFLICT (domain) DO UPDATE
  SET next_at = GREATEST(fetch_domain.next_at, now()) + %(iv)s
RETURNING next_at - %(iv)s AS slot_at;
```

Caller sleeps locally until `slot_at`, then fetches. One cheap UPDATE per fetch, exact 1-req/2s-per-domain politeness across any number of processes, no daemon. `GREATEST` handles idle domains (no backlog accumulation).

**Robots cache shared**: `robots_cache(origin TEXT PRIMARY KEY, body TEXT NULL, fetched_at TIMESTAMPTZ)`, TTL 1h, read-through with a small in-process LRU (5 min) so hot domains don't query per fetch. Parsing stays local (`RobotFileParser` over the cached body); `body NULL` keeps the v0.1 "unreachable robots ⇒ allow" semantics.

---

## 5. LLM concurrency & spend safety

- **Per-worker semaphore**: `asyncio.Semaphore(CONNECT_LLM_MAX_CONCURRENT)` (default 4) wrapped around `AnthropicProvider` calls in the worker flavor → cluster ceiling = workers × 4 ≈ 8–12 concurrent calls. That sits comfortably inside typical Anthropic tier RPM/ITPM limits; the SDK already retries 429/529 with exponential backoff honoring `retry-after` (raise `max_retries` to 4 in the worker flavor). Don't build a token-rate model — the governors cap dollars, the semaphore caps burst, the SDK absorbs the rest.
- **Governors go per-user + global**: `llm_call` gains `user_id`; `Governor` (already parameterized by purposes/WHERE-clause) gains a `user_id` scope. Each job runs under (a) the requesting user's general or investigation governor (defaults from settings, per-user override column on `app_user`) and (b) a deployment-wide backstop governor. Two `check()` calls, same `BudgetExceeded` discipline — over-budget degrades exactly as today (work stays `pending`/refused upfront, never lost). System jobs (polls, nightly batch) charge the system scope only.
- **Backpressure → 429**: before accepting `POST /analyses` / `POST /investigations`: per-user in-flight cap (`CONNECT_USER_MAX_INTERACTIVE`, default 2) and global interactive queue depth cap (`CONNECT_INTERACTIVE_QUEUE_LIMIT`, default 20). Over either → HTTP 429 with `Retry-After: 60` and a friendly detail ("the system is busy verifying other claims — try again in a minute"). Cheap COUNT queries on the partial claim index.
- **Batch API stays the nightly default** for T1 (50% off, one batch covers the day's backlog — limits of 100k requests/256MB are orders of magnitude away). `enrich_t1_batch` keeps its poll-loop shape as a worker handler with `max_attempts=3` (resubmission-safe: persist path is an idempotent per-doc replace, and the batch_id is recorded on the job payload so a retry resumes polling rather than resubmitting).

---

## 6. Blob store — confirm FS, add backup

**Confirmed: content-addressed FS on a named volume.** Content addressing makes multi-writer concurrency a non-issue (idempotent by hash; keep writes `tmp + os.rename` atomic — verify `BlobStore.put` does this during the port). At ~12MB/user-scale growth, object storage is ceremony. The seam already exists (`ingestion/blobs.py` is injected via the container); an S3/MinIO `BlobStore` drops in behind it later. api (uploads) and workers (ingest) mount the same `blobs` volume.

**Backup**: `backup` service (prod profile), `alpine` + `postgresql17-client` + `rsync`, crond:
- 02:30 UTC `pg_dump -Fc` → `/backups/pg/connect-YYYYMMDD.dump`
- 03:00 UTC `rsync -a --link-dest=<yesterday>` hardlink snapshot of `/blobs` → `/backups/blobs/YYYYMMDD/` (content-addressed files never mutate, so hardlink snapshots are near-free)
- retention: 7 daily + 4 weekly, pruned by the same script; `/backups` is a separate volume (and on a VPS, a separate disk/offsite rsync target — documented in README, not automated in v0.2).

---

## 7. docker-compose, Dockerfiles, env, local dev

### Images

- **`backend/Dockerfile`** — one image, two commands (api & worker share it; one build, no drift). Multi-stage `python:3.12-slim`: builder pip-installs into `/opt/venv`; runtime copies venv + **bakes the fastembed model**: `ENV FASTEMBED_CACHE_PATH=/opt/fastembed` + `RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')"`. Trade-off: ~130MB bigger image vs. cold-start model download on every fresh container (slow, flaky, breaks offline) — bake wins; startup time matters and the model version is pinned by the image digest.
- **`frontend/Dockerfile`** — add `output: "standalone"` to `next.config.ts`; multi-stage `node:22-alpine`, run `node server.js`.

**The BACKEND_URL footgun, resolved structurally**: rewrites are evaluated at *build* time for `next build`, which is exactly the trap the v0.1 config comment records. In production we don't use the rewrite at all — Caddy routes `/api/*` to api and everything else to frontend, so the browser's same-origin `/api` calls never touch Next. The rewrite stays only for the no-docker dev flow (where `next dev` evaluates it at startup). No build-arg needed; the footgun is deleted rather than handled.

### Caddyfile

```
{$CONNECT_DOMAIN:localhost} {
    handle /api/* { reverse_proxy api:8000 { flush_interval -1 } }
    handle       { reverse_proxy frontend:3000 }
}
```

Real domain in `CONNECT_DOMAIN` ⇒ automatic Let's Encrypt; unset ⇒ localhost with Caddy's internal CA. That is the entire VPS TLS story.

### compose sketch (`docker-compose.yml`)

```yaml
services:
  postgres:
    image: pgvector/pgvector:pg17        # ships the extension — no custom build
    volumes: [pgdata:/var/lib/postgresql/data]
    environment: { POSTGRES_DB: connect, POSTGRES_USER: connect, POSTGRES_PASSWORD: ${POSTGRES_PASSWORD} }
    healthcheck: { test: ["CMD-SHELL", "pg_isready -U connect"], interval: 5s, retries: 10 }
    ports: ["127.0.0.1:5432:5432"]       # exposed for no-docker local dev
  migrate:
    build: ./backend
    command: python -m connect.tools.migrate
    depends_on: { postgres: { condition: service_healthy } }
  api:
    build: ./backend
    command: uvicorn connect.api.main:app --host 0.0.0.0 --port 8000 --workers 2
    env_file: .env
    volumes: [blobs:/data/blobs]
    ulimits: { nofile: 65536 }
    depends_on: { migrate: { condition: service_completed_successfully } }
    healthcheck: { test: ["CMD", "python", "-m", "connect.tools.healthcheck"], interval: 15s }
  worker:
    build: ./backend
    command: python -m connect.workers.main
    env_file: .env
    volumes: [blobs:/data/blobs]
    deploy: { replicas: 2 }
    depends_on: { migrate: { condition: service_completed_successfully } }
    healthcheck: { test: ["CMD", "python", "-m", "connect.tools.healthcheck", "--worker"], interval: 30s }
  frontend:
    build: ./frontend
    depends_on: [api]
  proxy:
    image: caddy:2-alpine
    ports: ["80:80", "443:443"]
    volumes: [./Caddyfile:/etc/caddy/Caddyfile:ro, caddy_data:/data]
  backup:
    profiles: [prod]
    build: ./ops/backup
    volumes: [blobs:/blobs:ro, backups:/backups]
    environment: { PGHOST: postgres, ... }
volumes: { pgdata: {}, blobs: {}, caddy_data: {}, backups: {} }
```

Profiles: default brings up everything but backup; `--profile prod` adds it; `docker compose up postgres` alone is the dev profile in practice.

**Migrations are single-flight** twice over: the one-shot `migrate` service gates api/worker via `service_completed_successfully`, and `init_db` itself wraps in `pg_advisory_lock(LOCK_MIGRATE)` so even racing manual starts can't corrupt. Forward-only `migrate_N_to_N+1` pattern carries over unchanged.

### `.env.example`

```
POSTGRES_PASSWORD=changeme
CONNECT_DATABASE_URL=postgresql://connect:changeme@postgres:5432/connect
CONNECT_BLOB_DIR=/data/blobs
CONNECT_DOMAIN=                       # empty = localhost; set for VPS auto-TLS
ANTHROPIC_API_KEY=
TAVILY_API_KEY=
TWITTERAPI_IO_API_KEY=
GOOGLE_OAUTH_CLIENT_ID=
GOOGLE_OAUTH_CLIENT_SECRET=
CONNECT_SESSION_SECRET=
CONNECT_DAILY_LLM_BUDGET_USD=2.0      # per-user default
CONNECT_INVESTIGATION_DAILY_BUDGET_USD=10.0
CONNECT_GLOBAL_DAILY_BUDGET_USD=50.0  # deployment backstop
CONNECT_LLM_MAX_CONCURRENT=4
CONNECT_NIGHTLY_SWEEP_UTC_HOUR=21
CONNECT_USER_MAX_INTERACTIVE=2
CONNECT_INTERACTIVE_QUEUE_LIMIT=20
```

### Local dev WITHOUT docker (must keep working)

`docker compose up -d postgres`, then exactly the current flow plus one process:
```
uvicorn connect.api.main:app --port 8001          # CONNECT_DATABASE_URL=...localhost:5432...
python -m connect.workers.main                     # one worker incl. beat — full background machinery
npm run dev                                        # rewrite → localhost:8001, unchanged
```
Tests keep running against ephemeral PG databases (per-test `CREATE DATABASE` from a template, or a session-scoped schema reset — storage workstream's call), still no-network, still MockProvider.

---

## 8. Observability (minimum)

- **Structured logs**: `backend/connect/orchestration/logging.py` — a ~40-line stdlib `logging.Formatter` emitting JSON (`ts, level, logger, msg, job_id, user_id, worker`) with `contextvars` for job/user correlation, installed by both entrypoints. Pick hand-rolled over structlog: zero new deps, zero churn across the existing `log.info` call sites; structlog's niceties aren't worth converting a working codebase.
- **`GET /api/health`** stays cheap (process up, pool acquirable). **`GET /api/health?deep=1`** adds: `SELECT 1` + schema version; queue depth by status/kind + oldest-queued age; beat liveness (`beat_run` freshness) ; last successful poll per enabled source (worst-case staleness); blob dir writable; vector/embedder backend; spend today vs global budget.
- **`GET /api/metrics`** (admin-only): Prometheus text format via a tiny hand formatter (a dozen `name{label} value` lines — no client lib): `connect_jobs{status,kind}`, `connect_job_queue_oldest_seconds`, `connect_llm_spend_usd_today{scope}`, `connect_sse_connections{proc}` (per-process gauge, labeled), `connect_polls_stale_total`, `connect_fetch_throttle_wait_seconds` (rolling avg). Scrapeable later by a real Prometheus without rework; human-readable now.

---

## Phasing

| Phase | Content | Exit criterion |
|---|---|---|
| **A — queue & registry (pre-PG-merge-able)** | `workers/` package, handler registry, services refactored from closures to enqueue(payload), `ImmediateQueue` test double, cancellation via flag+token | all 367 tests green on the registry path, still SQLite |
| **B — PG runtime** (after storage workstream lands the port) | SKIP LOCKED queue DAO, worker entrypoint, beat + advisory locks, LISTEN/NOTIFY bus, SSE endpoints on bus+replay, fetch_domain + robots_cache, orphan/heartbeat sweeps | two workers + two api procs against one PG; kill -9 a mid-investigation worker → job reclaimed; SSE reconnect replays correctly |
| **C — deployment** | Dockerfiles (model baked), compose + Caddy + migrate one-shot, standalone Next, `.env.example`, no-docker dev verified | `docker compose up` cold → working system on localhost; same compose with `CONNECT_DOMAIN` on a VPS → TLS |
| **D — safety & ops polish** | per-user + global governors on the queue path (with auth workstream), 429 backpressure, deep health, metrics, JSON logs, backup service | load test: 20 simulated users firing investigations → caps enforced, friendly 429s, spend ledger sane |

---

## Risks

1. **Closure→registry refactor breadth** (Phase A) — touches analysis/investigation/enrichment services and many tests. Mitigation: it's behavior-preserving and lands *before* the PG port, so failures are attributable.
2. **NOTIFY is fire-and-forget** — dropped listener connection loses pings. Mitigated structurally: rows are the truth; 5s fallback poll bounds staleness; bus auto-reconnects.
3. **Event visibility inside long transactions** — if a ported handler wraps its whole run in one tx, job_events appear only at commit. Rule: job_event inserts are autocommit; enforce in `events.emit` (own connection/commit), assert in tests.
4. **fastembed RAM per worker** (~100–150MB ONNX each) — fine at 2–3 workers on a 4GB box; if worker count grows, dedicate embedding to ingest-kind workers via the per-kind slot map. Document in sizing notes.
5. **Per-domain politeness now adds a PG round-trip per fetch** — negligible (~1ms) vs the 2s interval, but the fetcher must not hold the reservation UPDATE inside any larger transaction (slot reservations must commit immediately or they serialize).
6. **uvicorn `--workers 2` + lifespan** — each api proc builds the full container; harmless except duplicate seeding, which is `INSERT ... ON CONFLICT DO NOTHING` after the port, and migrations are out of the api path entirely (migrate one-shot).
7. **Beat starvation if all workers are busy** — beat is a coroutine, not a job, so it ticks regardless of claim-slot saturation; only enqueue latency is at stake. No action needed; noted to prevent "make beat a job" regressions.

---

## Open questions (user-owned)

1. **Domain & TLS**: will v0.2 deploy under a real domain (Caddy auto-TLS, Google OAuth redirect URI needs it) or stay localhost/LAN for now? OAuth on bare `http://localhost` works for dev but a LAN-IP deployment needs a decision (domain, or documented OAuth loopback-only limitation).
2. **Global deployment budget**: per-user defaults stay $2/$10 — what is the deployment-wide daily backstop (`CONNECT_GLOBAL_DAILY_BUDGET_USD`)? This caps your actual Anthropic bill when hundreds of users are active; $50/day is the placeholder.
3. **Brief pre-generation policy**: pre-generate nightly for all users, only recently-active users (proposed: 7-day window), or on-demand only? Cost scales linearly with user count and it's your spend.
4. **VPS sizing target** (informs default worker replicas and worker RAM budget): is 2 vCPU / 4GB the planning envelope, or larger?

---

### Critical Files for Implementation
- /home/nomam/workspace/connect/backend/connect/orchestration/jobs.py — the JobRunner replaced by the queue + registry (Phase A core)
- /home/nomam/workspace/connect/backend/connect/orchestration/container.py — composition root; splits into api/worker runtime flavors
- /home/nomam/workspace/connect/backend/connect/api/routers/analyses.py — reference SSE replay/terminal semantics to preserve on the LISTEN/NOTIFY bus
- /home/nomam/workspace/connect/backend/connect/ingestion/fetcher.py — in-memory throttle + robots cache → PG-backed shared state
- /home/nomam/workspace/connect/backend/connect/ingestion/poller.py — lifespan loop → beat enqueue + `workers/handlers/poll.py`
- /home/nomam/workspace/connect/frontend/next.config.ts — the build-time rewrite footgun retired by Caddy path routing