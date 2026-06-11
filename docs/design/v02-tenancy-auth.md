I have a complete picture of the codebase. Writing the design document now.

# connect v0.2 — Tenancy, Auth, and the Multiuser API (Design)

Scope: this document covers the multiuser workstream (data-model deltas, Google OAuth, per-user budgets/rate limits, API tenancy, frontend auth/admin, single-user data migration). DDL sketches are PostgreSQL (storage decision locked); the PG port mechanics (FTS/pgvector/connection pooling/worker split) belong to the sibling workstream — coordination points are flagged inline.

Philosophy preserved: raw SQL in DAOs, all DDL in `storage/schema.py`, frozen Pydantic contracts in `domain/models.py`, vocabularies in `domain/enums.py`, wiring only in `orchestration/container.py`, forward-only migrations, grounding gates untouched.

---

## 1. The one invariant that makes shared reads safe

**Invariant I1: every row in a shared-KB table (claim, evidence, claim_sighting, entity_mention, statement, event_assignment, edge, document_enrichment, position_shift, plus findings/finding_evidence of a *shared* dossier) references only `visibility='shared'` documents.**

Enforced at the three write gates, not by filtering reads:

1. **Enrichment eligibility** — `_ELIGIBLE_SQL` in `backend/connect/knowledge/enrichment/sweep.py` gains `AND d.visibility = 'shared'` (same predicate in the statements-backfill query and the fast-path `enrich_document`). Private docs simply stay `enrichment_status='pending'`; sharing a doc later requires no special action — the next sweep picks it up. No new enrichment status needed.
2. **Analysis writeback** (`backend/connect/analysis/writeback.py`) — claim/evidence/verdict rows are written only when the dossier is shared; evidence referencing a private document is dropped from shared writeback (kept in the dossier_section content, which is dossier-scoped).
3. **Investigation writeback** (`backend/connect/investigation/writeback.py::record_finding`) — evidence on private docs is allowed only when `doc.owner_id == dossier.owner_id` AND the dossier is private. For private dossiers the grade-2 edge insert into the shared graph is **deferred**: the validated `link` dict is already reconstructible; persist it in `finding.payload["link"]` and skip `edge_dao.insert_causal`. Flipping the dossier to shared materializes deferred edges.

**Share cascade**: `PATCH dossier visibility private→shared` requires that every document cited by its findings/evidence be shared. Documents the owner owns are auto-shared (UI lists them for confirmation); if a cited private doc belongs to someone else (impossible by gate 3, defensive check anyway) the share is rejected with 409. After doc shares, deferred edges are inserted. Shared→private is **not allowed** once writeback occurred (edges/claims already compounded; retracting them would tear the graph) — return 409 with explanation.

Defense in depth on reads (cheap, few surfaces): document list/search/feed/detail/blob-download add `WHERE (d.visibility='shared' OR d.owner_id=:me)`. Derived-table read surfaces (entity pages, events, threads, contradictions, leader views) need **no** tenancy predicates thanks to I1 — that is the entire payoff of write-gating over read-filtering.

Boundary cases resolved:
- **Investigation-fetched web docs**: public articles → ingested with `origin='investigation_fetch'`, `visibility='shared'`, `owner_id=NULL`. Same for user-initiated URL ingests (`origin='user_url'`, shared) and link-follow docs.
- **User uploads / pasted text**: `origin='user_upload'|'user_text'`, `owner_id=:me`, `visibility='private'` default, share action available.
- **Shifts/contradictions/verdicts derived from shared docs**: global, no owner — they are properties of the shared corpus.
- **Dossier default visibility**: `'shared'` for analyses and investigations (they consume public web + shared corpus and the community compounding is the point; private is the opt-in for sensitive inquiry), `'private'` only when the seed input is a private document, the toggle visible at creation.

---

## 2. Schema deltas (PG DDL sketches — ship inside the PG baseline schema, see Phasing)

All in `backend/connect/storage/schema.py` as today. `app_user` not `user` (reserved word in PG). Timestamps follow whatever convention the PG-port workstream sets (recommend `timestamptz`, ISO-serialized at the DAO boundary so the frozen contracts keep ISO strings).

```sql
CREATE TABLE app_user (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    google_sub      TEXT UNIQUE,              -- NULLABLE: ETL pre-creates the admin
                                              -- by email; filled at first login
    email           TEXT NOT NULL UNIQUE,     -- verified email from Google ID token
    name            TEXT,
    avatar_url      TEXT,
    role            TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('admin','member')),
    disabled        BOOLEAN NOT NULL DEFAULT FALSE,
    -- per-user budget overrides; NULL = member default from app_setting
    daily_budget_usd               REAL,
    investigation_daily_budget_usd REAL,
    created_at      TIMESTAMPTZ NOT NULL,
    last_login_at   TIMESTAMPTZ
);

CREATE TABLE user_session (
    id           TEXT PRIMARY KEY,            -- secrets.token_urlsafe(32)
    user_id      BIGINT NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    created_at   TIMESTAMPTZ NOT NULL,
    expires_at   TIMESTAMPTZ NOT NULL,        -- sliding 30d, hard cap 90d from created_at
    last_seen_at TIMESTAMPTZ
);
CREATE INDEX idx_session_expires ON user_session(expires_at);

CREATE TABLE invite (                          -- the allowlist gate (default ON)
    email      TEXT PRIMARY KEY,
    invited_by BIGINT REFERENCES app_user(id),
    note       TEXT,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE app_setting (                     -- admin-editable globals (budgets);
    key   TEXT PRIMARY KEY,                    -- falls back to env Settings defaults
    value TEXT NOT NULL
);
```

Ownership / visibility deltas on existing tables:

```sql
-- enums.py: VISIBILITIES=('private','shared'), ROLES=('admin','member'),
-- DOCUMENT_ORIGINS=('polled','link_follow','investigation_fetch','user_url','user_upload','user_text')

ALTER TABLE document ADD COLUMN owner_id BIGINT REFERENCES app_user(id);   -- NULL = system
ALTER TABLE document ADD COLUMN visibility TEXT NOT NULL DEFAULT 'shared'
      CHECK (visibility IN ('private','shared'));
ALTER TABLE document ADD COLUMN origin TEXT NOT NULL DEFAULT 'polled'
      CHECK (origin IN (...DOCUMENT_ORIGINS...));
CREATE INDEX idx_document_owner ON document(owner_id) WHERE owner_id IS NOT NULL;
CREATE INDEX idx_document_visibility_fetched ON document(visibility, fetched_at DESC);

ALTER TABLE dossier ADD COLUMN owner_id BIGINT NOT NULL REFERENCES app_user(id);
ALTER TABLE dossier ADD COLUMN visibility TEXT NOT NULL DEFAULT 'shared'
      CHECK (visibility IN ('private','shared'));
CREATE INDEX idx_dossier_owner ON dossier(owner_id, created_at DESC);
CREATE INDEX idx_dossier_visible ON dossier(visibility, kind, created_at DESC);
-- question/finding/finding_evidence/dossier_section inherit scope via dossier FK.

ALTER TABLE watch ADD COLUMN user_id BIGINT NOT NULL REFERENCES app_user(id) ON DELETE CASCADE;
CREATE INDEX idx_watch_user ON watch(user_id);
-- watch_hit inherits per-user scope via watch FK; document.watch_hit stays a
-- global "any watch fired" priority bit (it only gates fast-path enrichment).

ALTER TABLE brief ADD COLUMN user_id BIGINT NOT NULL REFERENCES app_user(id) ON DELETE CASCADE;
-- brief_date UNIQUE  ->  UNIQUE(user_id, brief_date); brief_item unchanged (FK brief).

-- view_cursor PK becomes (user_id, surface, ref_id), user_id FK CASCADE.

ALTER TABLE job ADD COLUMN owner_id BIGINT REFERENCES app_user(id);       -- NULL = system
ALTER TABLE llm_call ADD COLUMN user_id BIGINT REFERENCES app_user(id);   -- NULL = system
CREATE INDEX idx_llm_call_user_day ON llm_call(user_id, created_at);
```

**Stays global, untouched**: `source` (admin-managed; optional `created_by` audit column), `entity`, `entity_mention`, `event`, `event_assignment`, `story`, `claim`, `evidence`, `claim_sighting`, `verdict_history`, `contradiction`, `edge`, `statement`, `position_shift`, `view_summary`, `calendar_event`, `event_type`, `source_stats`, embeddings tables.

---

## 3. Auth design

**Pick: backend-owned auth; authlib code flow; server-side sessions in PG; opaque httpOnly cookie. No next-auth, no JWT.**

Why each:
- **authlib** — the standard OAuth/OIDC client for Starlette/FastAPI; gives discovery, PKCE, nonce/ID-token verification for ~zero code. (Locked by decision anyway.)
- **Server-side PG sessions over JWT** — instant revocation (admin disables a user, logout-all), no signing-key rotation, no clock skew; PG is already the hot path and one indexed PK lookup per request is nothing at community scale. JWT's only advantage (statelessness) buys nothing on a single deployment.
- **Opaque cookie + EventSource = the SSE answer** — EventSource cannot set headers, but it *sends cookies automatically on same-origin requests*. All traffic flows through the Next.js rewrite (`frontend/next.config.ts`) so SSE is same-origin and cookie auth covers it with zero extra machinery. **Pick: cookie auth for SSE; no signed-ticket query param.** (If SSE ever goes direct-to-backend cross-origin — the buffering fallback documented in `frontend/src/lib/api.ts` — enable CORS with `allow_credentials=True` for the one frontend origin; a ticket param remains the documented-but-unbuilt escape hatch.)
- **No next-auth** — it would create a second session authority in the Node layer while tenancy is enforced in Python; one source of truth, and the frontend stays a dumb proxy + `GET /api/me` consumer.

Mechanics:
- Cookie `connect_session`: httpOnly, `SameSite=Lax`, `Secure` when behind TLS (uvicorn `--proxy-headers` so the reverse proxy's `X-Forwarded-Proto` is honored), `Path=/`. Sliding expiry: touch `last_seen_at`/extend `expires_at` at most once per hour; hard cap 90 days.
- OAuth handshake state/nonce: Starlette `SessionMiddleware` (itsdangerous-signed, separate short-lived cookie `oauth_state`) — authlib's Starlette integration requires it; it carries nothing after the callback.
- Scopes: `openid email profile` only. **Google access/refresh tokens are not stored** — Google is identity-only; "refresh" is the sliding app session, period.
- Login flow: `GET /api/auth/login` → Google → `GET /api/auth/callback` → verify ID token → user lookup by `google_sub`, else by verified `email` (account-linking: fills `google_sub` — this is also how the ETL'd admin row activates) → gate check → create session row → set cookie → 302 to `/`.
- Gate (default ON, `CONNECT_OPEN_SIGNUP=false`): allowed if existing user, or email in `invite`, or **first user ever** (count==0 inside the same transaction → `role='admin'`), or email in `CONNECT_ADMIN_EMAILS` (bootstrap override). Rejected emails get a friendly signed-out page, no user row.
- Logout: `POST /api/auth/logout` deletes the session row + clears cookie. Disabled users: `get_current_user` rejects (sessions can also be bulk-deleted on disable).
- **CSRF posture**: `SameSite=Lax` blocks cookie-bearing cross-site POSTs (including multipart form posts — Lax attaches cookies only on top-level GET navigations); plus an Origin/Referer check middleware on state-changing methods as defense-in-depth (`connect/api/security.py`). No CSRF tokens.
- Session GC: delete expired rows in the poller tick path or a daily job.

New modules: `backend/connect/auth/oauth.py` (authlib wiring, callback logic), `backend/connect/auth/sessions.py` (create/get/touch/delete), `backend/connect/storage/users.py` + `storage/sessions.py` (DAOs), `backend/connect/api/routers/auth.py`, `backend/connect/api/security.py`. `api/deps.py` gains `get_current_user` (loads `CurrentUser` frozen model; 401 on missing/expired/disabled) and `require_admin`.

---

## 4. Budgets and rate limits

**Layered governors — the existing two `Governor`s become the global layer; a per-user layer nests inside.**

- `llm_call.user_id`: threaded through `record_call(...)`. Attribution rule: user-initiated work charges the initiator — analyses/investigations charge `dossier.owner_id` (the runner reads it once at start); manual T2 promote charges the requesting user; poller-driven T1 sweeps, batch sweeps, and T2 promotion triggers are system (`user_id=NULL`, charge only the global envelope). Briefs are $0 (deterministic SQL) — no change.
- `connect/llm/spend.py` grows `spent_today(conn, *, user_id=...)` (one more WHERE clause, same single-ledger philosophy) and a `TenantGovernor(global_governor, conn, member_default_usd)` whose `check(projected, user_id)` enforces **both** ceilings: global first (`BudgetExceeded` → 429 as today), then the user ceiling (per-user override column, else `app_setting`, else env default). Member defaults: **$0.50/day general, $2.00/day investigation**, admin-configurable per user and globally.
- Global ceilings move from env-only to `app_setting` (env value = seed/fallback) so admins adjust without redeploys; `Container.startup()` reads them, admin PATCH updates the row and the in-memory value.

**Rate limiting pick: slowapi** (wrapper over `limits`) — decorator-per-route, keyed on `current_user.id`, in-memory storage. Why: zero new infrastructure; community scale runs one API container so cross-replica state is moot; if replicas ever appear, `limits` swaps to a Redis storage URI without code changes. A hand-rolled PG token bucket adds a write per request for no benefit. Defaults: 120/min per user on reads, 10/min on ingest, 5/min on analysis/investigation creation; SSE endpoints exempt (long-lived).

**Spend UI**: `GET /api/spend` becomes "my spend" — `{my_caps, my_today, my_days[]}` plus global caps/today for context (read-only for members). Admins get `GET /api/admin/spend`: system totals + per-user breakdown (`GROUP BY user_id`).

---

## 5. API surface changes

New routers:
- `connect/api/routers/auth.py` — `GET /auth/login`, `GET /auth/callback`, `POST /auth/logout`, `GET /me` (user + role + budgets + today's spend).
- `connect/api/routers/admin.py` (all behind `require_admin`) — `GET/PATCH /admin/users/{id}` (role, disabled, budget overrides), `GET /admin/users`, `GET/POST/DELETE /admin/invites`, `GET /admin/spend`, `GET/PATCH /admin/settings` (global budgets).

Every existing router gains `Depends(get_current_user)` (the whole `/api` surface is authenticated; `/api/health` stays open for compose healthchecks). Tenancy per router:

| Router | Change |
|---|---|
| `watches`, `cursors`, `brief` | all queries `WHERE user_id=:me`; `briefing.get_or_generate(conn, user_id, date)` — sections already read watches + shared KB, so per-user lazy generation on first GET stays $0 and stays fine; guard concurrent first-GETs with `UNIQUE(user_id, brief_date)` + `ON CONFLICT DO NOTHING` retry |
| `analyses`, `investigations` | list/detail/SSE: `WHERE (owner_id=:me OR visibility='shared')`, 404 otherwise; rows carry `owner` display info + `visibility`; create sets `owner_id=:me`; cancel/visibility-PATCH: owner or admin; create checks TenantGovernor (429) |
| `documents`, `search`, `feed`, blobs | visibility predicate `(visibility='shared' OR owner_id=:me)` in `storage/documents.py`, `storage/fts.py`, feed queries, and the retrieval seam used by analyses/investigations (viewer = dossier owner); ingest sets owner/origin/visibility per §1; `PATCH /documents/{id}` visibility (owner only); promote allowed for members, charged to their budget |
| `sources` | GET readable by members (transparency); POST/PATCH/DELETE/poll → admin |
| `enrichment` (manual sweep triggers) | admin |
| `entities`, `events`, `threads`, `contradictions`, `position_shifts`, `calendar` | global reads, auth only — safe via I1, no predicates |
| `spend` | per-user as §4 |

DAO signatures change from `(conn, ...)` to `(conn, viewer: int | None, ...)` only where a predicate exists — keep global DAOs untouched.

Pagination/indexes: existing page/page_size pattern survives community cardinality; the new indexes in §2 cover the hot multiuser queries (dossier lists by owner/visibility, per-user ledger sums, watch lookups). No keyset migration needed at hundreds-of-users scale — explicitly out of scope.

SSE: unchanged protocol (`job_event` replay by seq, `Last-Event-ID`); auth via cookie (§3); the read-predicate guard sits on the route before the stream starts.

---

## 6. Frontend (scope-honest)

- `src/app/signin/page.tsx` — Google button (`window.location = "/api/auth/login"`), error states (`?error=not_invited`).
- `src/lib/api.ts` — `getMe()`, and the shared fetch helper redirects to `/signin` on 401 (no Next middleware; client-side guard is sufficient when every page is API-backed).
- `src/components/shell/UserMenu.tsx` in `Topbar.tsx` — avatar, name, my-spend line (replacing the global `SpendBadge` for members), Sign out, Admin link when `role==='admin'`.
- `src/app/admin/` — users table (role/disable/budget overrides), invites, global spend; `/sources` page stays but mutating controls render only for admins.
- Visibility: toggle at analysis/investigation creation; owner-only switch + shared-by chip on detail pages; **Mine | Shared | All** tabs on the existing `/investigations` and analyses lists (this *is* the community feed — no new page). Library rows get visibility chips + a Share action with the cascade confirmation dialog (lists private docs that will be shared).
- Watch/brief/today UX unchanged.

---

## 7. Migration of existing single-user data

Runs inside the SQLite→PG ETL (sibling workstream); tenancy rules owned here:

1. ETL flag `--owner-email <email>` pre-creates the admin `app_user` row (`google_sub=NULL`, `role='admin'`); first Google login with that verified email links and fills `google_sub`.
2. All `watch`, `brief`, `view_cursor` rows → `user_id = admin`.
3. All `dossier` rows → `owner_id = admin`, `visibility='shared'` — **required, not optional**: existing dossiers already wrote edges/claims/evidence into the graph, so marking them private would violate I1 retroactively.
4. All `document` rows → `visibility='shared'`, `owner_id=NULL` for polled/link-followed docs, `owner_id=admin` for manual ingests; `origin` backfilled from `source_id IS NULL` + media_type heuristics (best-effort; default `'polled'`).
5. `llm_call`/`job` history → `user_id/owner_id = NULL` (historic system spend; don't pollute the admin's personal ledger).

---

## 8. Phasing

- **Phase A (with the PG port)**: tenancy DDL ships **inside the PG baseline schema** (users/sessions/invites/settings + ownership columns + indexes), so there is exactly one schema cutover and no PG-side rebuild migration a week later. ETL bootstrap rules (§7) included. Code still runs effectively single-user (a temporary "system viewer").
- **Phase B — Auth**: authlib flow, sessions, `/api/me`, Origin-check middleware, signin page, user menu, first-user-admin + invite gate. Whole API behind auth; no tenancy filters yet. Ship + dogfood.
- **Phase C — Tenancy**: per-user watches/brief/cursors; dossier/document ownership + visibility defaults; read predicates; the three I1 write gates; share cascade + deferred-edge materialization. The leak-test matrix lands here (see Risks).
- **Phase D — Budgets, limits, admin**: `llm_call.user_id`, TenantGovernor, slowapi, spend UI split, admin area (users/invites/settings/spend).
- **Phase E — Community polish**: Mine/Shared/All tabs, shared-by attribution, library share UX.

B and C are the only order-sensitive pair; D can overlap C.

## 9. Risks

- **Private-doc leakage via derived tables** — the central risk. Mitigation: I1 write gates + defense-in-depth read predicates + a dedicated test suite: for each shared surface (entity detail, events, threads, contradictions, leader views, search, feed, brief), assert a second user never sees text from another user's private doc after enrichment/analysis/investigation runs against it (extend `tests/kb_factories.py` with a two-user fixture).
- **Share-cascade complexity** — deferred edges + auto-share confirmation is the trickiest flow; bounded by forbidding shared→private downgrades after writeback.
- **SSE through the Next rewrite with cookies** — works same-origin, but the known dev-buffering fallback (direct EventSource to backend) now requires CORS-with-credentials config; document it next to `ANALYSIS_EVENTS_BASE`.
- **authlib + SessionMiddleware interplay** — keep the `oauth_state` cookie distinct and short-lived so it never shadows `connect_session`.
- **In-memory rate limits** don't survive restarts or replicas — acceptable and documented; budget governors (PG-backed) remain the hard cost backstop.
- **Concurrent per-user brief generation** — solved by the unique constraint + conflict retry; worth a regression test.
- **Settings drift** (env vs `app_setting`) — rule: `app_setting` wins when present; admin UI is the only writer.

## 10. Open questions (user-owned)

1. Default dossier visibility: this design picks **shared** (community compounding); confirm, or default private with share-on-publish.
2. Member self-serve invites (members invite by email) vs admin-only invites — design assumes admin-only.
3. Should admins be able to *view* members' private dossiers/documents (moderation power), or is private absolute (admin can only delete users)? Design assumes private is absolute.
4. Member default budgets ($0.50 general / $2 investigation per day) — confirm numbers.

### Critical Files for Implementation
- /home/nomam/workspace/connect/backend/connect/storage/schema.py — all new DDL (app_user, user_session, invite, app_setting, ownership columns) lands here
- /home/nomam/workspace/connect/backend/connect/api/deps.py — get_current_user / require_admin; the auth seam every router consumes
- /home/nomam/workspace/connect/backend/connect/llm/spend.py — user_id threading + TenantGovernor layering over the existing Governor
- /home/nomam/workspace/connect/backend/connect/knowledge/enrichment/sweep.py — write gate 1 (visibility predicate in `_ELIGIBLE_SQL`), the I1 anchor
- /home/nomam/workspace/connect/backend/connect/investigation/writeback.py — write gate 3 (private-evidence rule, deferred edge materialization)
- /home/nomam/workspace/connect/backend/connect/orchestration/container.py — composition root wiring for auth/session/governor changes