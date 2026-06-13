# Code Review Findings — whole-system audit

Date: 2026-06-13
Scope: full system (backend + frontend), all lenses — security/tenancy, correctness,
efficiency, design/maintainability, and product coherence. Method: 5 parallel read-only
audit agents; the sharpest findings re-verified by hand against current code (cited inline).

## TL;DR

- **Security is in good shape.** All 5 tenancy/auth leaks from the 2026-06-12 review are
  **fixed** in the working tree (re-verified line by line). The new workspace-tasks feature
  is correctly owner-scoped. Only one Low-severity write-side gap remains (F-SEC-1).
- **The real problems are now (a) product coherence and (b) lifecycle correctness in the
  new async workspace-task feature** — not data leaks.
- The flagship Pillar-A analysis pipeline is a 3-stage stub while four recent commits added
  peripheral surface (Instagram posts, workspace chat, workspace tasks). The newest agent
  paths *bypass the mechanical citation discipline* the README sells as "the product."

---

## A. Prior findings (2026-06-12) — all resolved

| # | Prior issue | Status |
|---|---|---|
| 1 | `document_links.py:43` resolved-doc leak | **Fixed** — now `get(..., viewer=user.id)` |
| 2 | `scoping.py` anchor-entity FTS leak | **Fixed** — `rank_documents(..., viewer=viewer)` |
| 3 | `analyses.py _evidence_item` metadata leak | **Fixed** — visibility predicate added |
| 4 | `investigations.py findings_for` metadata leak | **Fixed** — `vis_clause` added |
| 5 | `pipeline.py cancel()` missing kind-guard | **Fixed** — `AND kind = 'analysis'` present |
| 6 | `/auth/methods` dev-login disclosure | **Mitigated** — no longer leaks the email; boolean flag only. Operational risk only (don't set `CONNECT_DEV_LOGIN_EMAIL` in reachable envs). |
| 7 | `investigations.py list_page` N+1 | **Still present** → see F-EFF-1 |
| 8 | `analyses.py` list/detail N+1 | **Still present** → see F-EFF-2/3 |

---

## B. Product coherence & architecture (highest-leverage)

### F-ARCH-1 — Workspace-tasks is not a feature; it's a budget knob on chat
**Severity:** High (design) | `workers/handlers/workspace_task.py:49`, `api/routers/workspaces.py:254`

Both the chat endpoint and the async task handler call the **identical** `run_workspace_agent`.
The only differences: `max_iters` 20 vs 6, `cap_usd` $1.00 vs $0.10, sync-vs-async, and the
task throws away the conversation. This one parameterization spawned a new table, job kind,
handler, 3 router endpoints, a frontend component, and the v12 migration. A `mode: "deep"`
flag (or longer-budget chat) delivers ~90% of the value.

Worse, it reinvents — more weakly — the async-agent machinery the system already standardized
on: the **investigation runner** has durable SSE streaming (`job_event` + `?after=` replay),
cancellation, and checkpointing. Workspace tasks instead poll a `steps` JSON column (tool
names only), with no resume and no SSE. **Recommendation:** fold "long agentic task over a
corpus slice" into either a chat `deep` mode or the investigation runner scoped to the
workspace lens; don't maintain a third tool-loop runtime.

### F-ARCH-2 — Agent `post_finding` bypasses the mechanical citation discipline
**Severity:** High (trust/integrity) | `agents/workspace_agent.py:321`

The README's headline promise: *"every assertion cites a stored immutable snapshot... enforced
mechanically, not by prompt-hoping."* `investigation/writeback.py` honors this (verbatim quote
verification, speculation forced through a closed candidate menu). The workspace agent does
**not**: `_tool_post_finding` checks only that the cited doc is *in scope* — never that the
body's claims appear in the doc, no quote check, `document_id` is optional. It then writes to
the **same `post` table** as human-authored findings, indistinguishable, with no provenance
guarantee. Two contradictory epistemic standards now write to overlapping surfaces.
**Recommendation:** route agent findings through a grounding gate analogous to
`writeback.py`, or visibly label them AI-suggested/ungrounded and keep them out of the trusted
findings surface.

### F-ARCH-3 — Instagram/social-publishing is off-mission and partly a dead end
**Severity:** Medium (scope) | `api/routers/social.py`, `social/instagram.py`, `agents/workspace_agent.py` (`draft_social_post`), `frontend/.../WorkspaceDrafts.tsx`

A content-*publishing* studio (caption + branded image card + Instagram Graph API publish,
its own settings table + public card endpoint) welded onto an information-*intelligence* tool.
Nothing in the two-pillar mission implies it. It's also unfinished as a pipeline: the agent's
`draft_social_post` writes `social_draft` rows that `WorkspaceDrafts.tsx` can only
copy/download/delete — **there is no publish path for workspace drafts** (publish exists only
on `/documents/[id]`, drafting fresh content). `draft_social_post` also generates captions with
**zero grounding** and frames them as publishable under the system's brand — the inverse of the
trust promise. **Recommendation:** at minimum remove `draft_social_post` from the agent toolset;
consider spinning the social feature out entirely.

### F-ARCH-4 — Advertised core (Pillar A) is a stub while periphery grows
**Severity:** Medium (roadmap honesty) | `analysis/pipeline.py` (`STAGES`), `domain/enums.py` (`DOSSIER_STAGES`)

`DOSSIER_STAGES` and the README describe provenance → forces → impacts → loopholes as the heart
of on-demand analysis, but `pipeline.py STAGES = ("normalize", "verify", "assemble")`. The
headline capability is the least-built part; the most-recently-built part is the un-advertised
social studio. Either build out the dossier stages or stop narrating them as built.

---

## C. Correctness (new async workspace-task feature)

### F-COR-1 — `workspace_task` row stuck in `running` forever on worker crash
**Severity:** High | `workers/handlers/workspace_task.py:42`, `storage/jobs.py:174-205`, `workers/queue.py:70`

`workspace_task` is absent from `KIND_MAX_ATTEMPTS`, so `max_attempts=1`. The beat orphan
sweep `reclaim_stale` operates only on the **`job`** table — on a stale heartbeat it marks the
job `failed`/`orphaned` but **never touches the `workspace_task` row**, which stays `running`
with NULL result/error/finished_at. The polling UI shows a perpetually-running task.
*Verified: `reclaim_stale` has no `workspace_task` reference; handler `except` blocks only fire
on Python exceptions, not on SIGKILL/OOM.* **Fix:** reconcile task rows from terminal job state
in the sweep (flip `running` tasks whose job is terminal-failed to `failed`), and/or add a retry.

### F-COR-2 — Workspace tasks can't be cancelled
**Severity:** Medium | `api/routers/workspaces.py:323-368` (no cancel endpoint)

The handler honors `should_cancel` and `asyncio.CancelledError`, but nothing ever calls
`jobs.request_cancel` for a workspace_task — there is no cancel endpoint. A runaway 20-iter /
$1.00 task can't be stopped. Also, a cancel arriving while `queued` would flip the job but
leave the task row stuck `queued` (same class as F-COR-1). **Fix:** add a cancel endpoint that
calls `request_cancel` and flips the task row.

### F-COR-3 — `set_running` overwrites status unconditionally
**Severity:** Medium | `storage/workspace_tasks.py:72-76`

`UPDATE workspace_task SET status='running' WHERE id=%s` — no guard. Can resurrect a terminal
(`cancelled`/`failed`) row. **Fix:** `... AND status='queued'`; treat 0 rows as already-taken
and abort.

### F-COR-4 — Cancel race: terminal `_set_dossier` can overwrite `cancelled`
**Severity:** Medium | `analysis/pipeline.py:104-123,296`, `investigation/runner.py:163-176`

`cancel()` sets the dossier `cancelled`, but if the handler is mid-stage it runs to completion
and `_set_dossier(..., "completed")` (no status guard) overwrites it; the user's cancel is lost.
**Fix:** `... WHERE id=%s AND status NOT IN ('cancelled')` on terminal writes, or re-check the
cancel token before the final write.

### F-COR-5 — v12 migration: bare `DROP CONSTRAINT` (low risk)
**Severity:** Low | `storage/migrations.py:192`

`ALTER TABLE job DROP CONSTRAINT ck_job_kind` (no `IF EXISTS`). The whole migration is in one
advisory-locked transactional block so a failed run rolls back cleanly — not a corruption risk,
but not robust against a manually-tinkered DB. **Fix:** `DROP CONSTRAINT IF EXISTS`.

*(Verified clean — not bugs: `append_step` is a single atomic `steps = steps || %s`; poller
per-source isolation; beat leader election; SKIP-LOCKED claim CAS; poll_source dedup.)*

---

## D. Efficiency

### F-EFF-1 — N+1 in `investigations.list_page` (`counts_for` per row)
**Severity:** High | `storage/investigations.py:79-86,31-52` — page of 20 → **82 queries**.
**Fix:** set-based aggregates over the page ids (one grouped COUNT on `finding`, one
conditional-count on `question`, fold `model_usage` into the row SELECT) → ~4 queries.

### F-EFF-2 — N+1 in `analyses.list_page` (`_verdict_summary` per row)
**Severity:** High | `storage/analyses.py:59-65` — page of 20 → **22 queries**.
**Fix:** one `WHERE dossier_id = ANY(%s)` batch → 3 queries.

### F-EFF-3 — N+1 in `analyses.get_detail` (`_evidence_item` per evidence ref)
**Severity:** Medium/High | `storage/analyses.py:140-177` — ~15 claims × 4 refs → **~60 queries/detail**.
**Fix:** collect all `document_id`s, one batched `document JOIN source WHERE id = ANY(%s)` (+ viewer predicate), assemble from the map → 1 query.

### F-EFF-4 — N+1 in `investigations.findings_for` (per-finding evidence)
**Severity:** Medium | `storage/investigations.py:111-142` — N findings → 1+N queries/detail.
**Fix:** one query over `fe.finding_id = ANY(%s)`, group in Python → 2 queries.

### F-EFF-5 — Missing indexes on `event_assignment(event_id)` / `(document_id)`
**Severity:** High (scale cliff) | `storage/schema.py:428` — table has only a PK on `id`, yet
`event_id`/`document_id` are JOIN/filter columns across `events.py` (documents_for_event,
entities_for_events, events_for_entity, event_for_document) and `entities.py` (delta_since_cursor).
Every event/entity detail page seq-scans the table. *Verified: no index grep hit for these cols.*
**Fix:** `CREATE INDEX idx_event_assignment_event ON event_assignment(event_id) WHERE event_id IS NOT NULL;`
and `... idx_event_assignment_document ON event_assignment(document_id);`

### F-EFF-6 — `entities.get_detail` serial fan-out (~10 queries, dup CTE)
**Severity:** Low | `storage/entities.py:99-134` — independent sub-fetches run serially;
`_STATS_CTE` materialized twice. Module docstring accepts single-user scale. **Fix:** `asyncio.gather` the independent fetches; narrow the CTE.

---

## E. Frontend

### F-FE-1 — Workspace task list desyncs; no terminal-status invalidation
**Severity:** High | `lib/queries.ts:828-862`, `components/workspace/WorkspaceTasks.tsx:79-89`

The list only polls while a task in *the list* is running, but a just-created task isn't in the
list until a refetch, and nothing invalidates the list when a task reaches a terminal status. So
completed tasks intermittently go missing from history. **Fix:** mirror `useInvestigation`
(`queries.ts:1609`) — invalidate `workspaceTasks(id)` on terminal status (or seed/refetch the
list in `useStartWorkspaceTask.onSuccess`).

### F-FE-2 — Tasks panel & `TaskDetail` swallow loading/error/empty states
**Severity:** Medium | `components/workspace/WorkspaceTasks.tsx:37,108-110`

`TaskDetail` returns `null` on pending AND error (just-started task shows a blank card; a failed
fetch shows nothing). The panel renders only the composer on `isPending`/`isError`/empty, unlike
every sibling panel. **Fix:** add skeleton / `QueryError` / empty-state branches.

*(Verified solid — not bugs: fetch error handling (`!res.ok → raise`), 401→/signin redirect,
query-key hygiene, the per-task detail poll correctly stops on terminal status, origin-relative
`/api` proxy.)* Minor: F-FE-3 401 hard-redirect can fire on transient 401s from background polls
(`useSpend`/`useWatchBadges`) — consider gating the redirect to user-initiated queries (Low).

---

## F. Security (residual)

### F-SEC-1 — `POST /posts` doesn't validate `workspace_id` ownership
**Severity:** Low (integrity, not confidentiality) | `api/routers/posts.py:53`

`create_post` passes `body.workspace_id` to insert without checking the caller can see/own that
workspace (contrast `document_id`, which is validated). A user can tag a post to another user's
workspace_id; if shared, it surfaces in that workspace's feed — but only the attacker's own
content is exposed, so no data leak. **Fix:** mirror the `document_id` check — `workspace_dao.get(..., viewer=user.id)`, 422 if None.

---

## Suggested order of work

1. **F-COR-1/2/3** — make the async task lifecycle sound (or delete it per F-ARCH-1).
2. **F-ARCH-1/2** — decide the agent-runtime/citation strategy *before* building more on top.
3. **F-EFF-5 + F-EFF-1** — cheapest high-impact perf wins (one migration + one query rewrite).
4. **F-FE-1/2**, **F-COR-4**, then the remaining N+1s and **F-SEC-1**.
