# Agent-runtime consolidation (F-ARCH-1 / F-ARCH-2)

Date: 2026-06-13
Status: IMPLEMENTED (Phases 1–4) — all four phases landed; 629 backend tests green;
schema migrated to v14 on the dev DB.
Addresses: REVIEW_FINDINGS.md F-ARCH-1 (workspace-tasks is a budget knob, not a feature;
reinvents async-agent machinery), F-ARCH-2 (workspace agent bypasses the mechanical
citation gate), and the F-COR-1/2/3 correctness bugs (resolved by deleting the
workspace_task row).

## Outcome (as built)

- **Decision 1: deep mode on the chat agent.** `POST /workspaces/{id}/chat/async` enqueues a
  `workspace_task` job that runs the SAME agent at `mode='deep'`, streams `job_event`
  progress over `GET /workspaces/{id}/chats/{chat_id}/events` (the shared SSE contract), and
  appends the answer to the chat transcript. `POST .../chats/{chat_id}/cancel` cancels it.
  The `workspace_task` TABLE, its DAO, the `steps`-polling endpoints, and the frontend
  polling were deleted (migration v14 drops the table; the job kind is kept).
- **Decision 2: post table + quote columns.** `post` gained `quote/quote_start/quote_end`
  (migration v13). The workspace agent's `post_finding` now requires `{document_id, quote}`
  and verbatim-verifies via the shared `analysis/grounding.py::span_is_verbatim` — the same
  gate the investigation `record_finding` uses. Human posts leave the columns NULL.
- **Decision 3: Phase 4 done.** `connect/agents/metered_loop.py::run_metered_tool_loop` is
  the single control-loop skeleton; both `investigation/runner.py` and
  `agents/workspace_agent.py` drive it, injecting their own policy (budget ladder, metering,
  progress sink, terminal tool, empty-turn handling) via hooks. The second loop copy is gone.

Original proposal (decisions taken above) follows.

## 1. The problem, precisely

There are **three hand-rolled agent tool-loops** and **two-and-a-half invocation modes** of
the workspace one:

- `investigation/runner.py::_stage_investigate` (runner.py:294-344) — the gold standard:
  durable rows, `job_event` SSE with `?after=` replay, cooperative cancel, 3-tier degrade
  ladder, and a **mechanical grounding gate** (`investigation/writeback.py::record_finding`).
- `agents/workspace_agent.py::run_workspace_agent` (workspace_agent.py:434-471) — the same
  control loop with weaker policy: 1-tier budget, **no grounding gate**, and either a
  synchronous request or an async job depending on the caller.
- The async path duplicates the sync path: `workers/handlers/workspace_task.py` and
  `api/routers/workspaces.py::workspace_chat` (workspaces.py:254) call the *identical*
  `run_workspace_agent`; the only differences are `max_iters` (20 vs 6), `cap_usd`
  ($1.00 vs $0.10), and that the task discards the conversation.

Both serious loops deliberately bypass the generic `LLMProvider.tool_loop` (provider.py:187)
— see the runner docstring — because that template has no spend metering, budget ladder,
SSE, or cancel. So the skeleton (iterate → cancel-check → compute `force` →
`complete_with_tools` → `record_call`+`budget.add` → progress → append messages → dispatch
→ append results) is copied twice and will be copied a third time the next time someone
needs an agent.

### Two concrete harms

1. **Async machinery reinvented, weaker.** Workspace tasks report progress by polling a
   `steps` JSON column (tool *names* only), with no resume and no SSE — instead of the
   `job_event` stream the rest of the system standardized on. This is the root of bugs
   F-COR-1/2/3 (stuck `running`, no cancel, unconditional `set_running`).
2. **Two contradictory trust standards write to overlapping surfaces.** `record_finding`
   verbatim-verifies every quote against stored document text (writeback.py:98-101, via the
   shared `analysis/grounding.py::span_is_verbatim`). `post_finding` (workspace_agent.py:321)
   checks only that the cited doc is *in scope* — no quote, `document_id` optional — then
   writes to the **same `post` table** as human findings. The README's core promise
   ("every assertion cites a stored immutable snapshot, enforced mechanically, not by
   prompt-hoping") is upheld in one engine and bypassed in the other.

## 2. Target architecture

Three moves, independently shippable, lowest-risk first. The guiding principle: **policy is
data, the loop is shared, grounding is a gate the runtime owns — not a courtesy each agent
extends.**

### Move B (F-ARCH-2) — grounding as a gate, reusing the existing verifier  ← do first, cheapest

Make `post_finding` mechanically grounded with the primitive that already exists:

- Require `evidence: [{document_id, quote}]` (drop the bare optional `document_id`).
- For each item: fetch the in-scope doc, run `grounding.span_is_verbatim(quote, content_text)`
  (the exact call writeback.py:99 makes), reject with the exact reason on failure so the
  model retries — identical UX to `record_finding`.
- Persist the verified `quote_start/quote_end` alongside the post (add columns, or reuse the
  `finding_evidence` shape — see Decision 2).

~30 lines, no new infra, and it closes the trust hole immediately. The verifier, the
reject-and-retry contract, and the tenancy rule for private evidence are all already written
in `writeback.py::_validate_evidence` — this is a lift-and-narrow, not new design.

### Move A (F-ARCH-1) — one workspace-agent entry, progress on the job/SSE contract

Collapse the three invocation modes into one helper and retire the bespoke async stack:

1. **Single entry point.** Both the chat endpoint and the worker call one
   `run_workspace_agent(..., mode)` where `mode ∈ {quick, deep}` selects the
   `(max_iters, cap_usd)` preset. No duplicated invocation logic.
2. **Async = a job that streams `job_event`, not a `steps` column.** Replace the
   `on_turn → append_step` sink with the investigation `emit("iteration", …)` sink writing
   to `job_event`. The frontend then reuses the *existing* SSE/`?after=` replay hook instead
   of a second polling mechanism. This deletes the `workspace_task` table, the `steps` JSON,
   `set_running`/`append_step`/`finish`, and the polling React Query — and with them
   F-COR-1/2/3 (the durable, cancellable, terminal-state-correct semantics come for free
   from the job machinery the investigation runner already proved).
3. **`workspace_task` job kind becomes a thin pointer** (or is dropped entirely if "deep"
   chat runs as an `investigation`-style job keyed to the workspace — see Decision 1).

### Move C (optional) — extract the metered loop primitive

Promote the copied skeleton to one `MeteredAgentLoop` (or enrich `LLMProvider.tool_loop`)
that takes: `system`, `tools`, `executor`, `governor`, `purpose`, a `BudgetPolicy` (the
degrade ladder as a strategy object), a `ProgressSink` (SSE emit | noop), a `terminal_tool`
name, and a `cancel` token. Then:

- Investigation = loop + investigation tools + 3-tier `BudgetPolicy` + SSE sink + `conclude`.
- Workspace = loop + workspace tools + 1-tier `BudgetPolicy` + (SSE|noop) sink + `final_answer`.

This is the real dedup, but it touches the *working* investigation runner, so it is highest
risk and lowest marginal value. Recommended only after A and B land and are green.

## 3. Phased plan

| Phase | Scope | Risk | Deletes / fixes |
|---|---|---|---|
| 1 | Move B — verbatim gate on `post_finding` | Low (additive) | closes F-ARCH-2 |
| 2 | Move A.1 — single `run_workspace_agent(mode=…)` entry; dedupe chat vs task callers | Low | removes the F-ARCH-1 duplication |
| 3 | Move A.2 — async path streams `job_event`; frontend reuses SSE hook | Medium | deletes `workspace_task` table/steps + polling; closes F-COR-1/2/3 |
| 4 | Move C — extract `MeteredAgentLoop`; migrate both engines | Higher | one loop, deletes the second copy |

Phases 1–3 fully resolve F-ARCH-1/2 and three correctness bugs without touching the
investigation runner. Phase 4 is the "one engine" cleanup and is optional.

## 4. Decisions needed

**Decision 1 — What is "deep / long" workspace work?**
- (a) A `deep` *mode* on the workspace chat agent (bigger caps, async job, SSE) — keep the
  workspace agent as its own engine. *Smallest change; recommended.*
- (b) Route "long task over a workspace" into **investigation mode** scoped to the workspace
  lens — reuse the runner's grounding + SSE wholesale, delete the workspace async path.
  *Most consolidation; biggest behavior change; investigation output is a dossier, not a
  chat answer.*
- (c) Keep `workspace_task` table but re-plumb it onto `job_event` (Move A.2 without dropping
  the table). *Middle ground.*

**Decision 2 — Where do grounded workspace findings live?**
- (a) Keep the `post` table; add `quote/quote_start/quote_end` columns (or a `post_evidence`
  child) and the verbatim gate. *Keeps human + agent findings unified; minimal schema.*
- (b) Agent findings become first-class `finding` rows (investigation schema) and stop
  sharing the `post` table. *Cleaner separation; larger change; needs a workspace→dossier link.*

**Decision 3 — Ambition: stop at Phase 3, or do Phase 4 (one engine)?**
- Phases 1–3 resolve the findings with low risk. Phase 4 is a quality/maintainability play
  that touches working code.

## 5. Dependencies / non-goals

- F-ARCH-3 (Instagram / `draft_social_post`) is **out of scope** here but interacts: if
  `draft_social_post` stays on the workspace agent, it remains an ungrounded generation verb
  on an otherwise-grounded runtime. Recommend cutting it from the toolset regardless (tracked
  in REVIEW_FINDINGS F-ARCH-3).
- The analysis pipeline (`analysis/pipeline.py`) is a *fixed-stage* engine, not a tool-loop;
  it is not part of this consolidation.
