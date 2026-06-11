# connect — Information Intelligence System

`connect` is a single-user, local-first system for understanding Indian policy, politics
and finance, built on two first-class pillars. **Pillar A — Analysis (on-demand):** hand it
a claim, policy or event and it backtracks provenance to the primary source, verifies
against independent sources, maps driving forces, impacts and loopholes, and produces a
fully-cited dossier. **Pillar B — Knowledge base (continuous):** it polls a user-extensible
registry of sources around the clock (RBI, PIB, fact-checkers, national media, …) and
accumulates structured knowledge — entities, events, story threads, claims, contradictions
— so every analysis starts from, and writes back to, an evolving picture of what is going
on. Trust is the product: every assertion cites a stored immutable document snapshot, and
the citation discipline is enforced mechanically, not by prompt-hoping.

## Quickstart

Backend (FastAPI + SQLite; Python 3.11+):

```bash
cd backend
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/uvicorn connect.api.main:app --port 8000
# tests:
.venv/bin/python -m pytest -q
```

Frontend (Next.js App Router; proxies `/api/*` to the backend — one origin, no CORS):

```bash
cd frontend
npm install
npm run dev          # http://localhost:3000
```

On first boot the backend migrates the schema, seeds the India source registry
(RBI / PIB / Alt News / BOOM Live / ET / LiveMint / Scroll / Google News) and — when
enabled — starts the RSS poller and embeds every ingested document locally
(fastembed BGE-small; the model downloads once on first use, ~100 MB).

### Environment variables

All backend settings use the `CONNECT_` prefix (see `backend/connect/orchestration/config.py`):

| Variable | Default | Meaning |
|---|---|---|
| `CONNECT_DB_PATH` | `backend/data/connect.db` | SQLite database location |
| `CONNECT_BLOB_DIR` | `backend/data/blobs` | Immutable raw-document blob store |
| `CONNECT_POLLER_ENABLED` | `true` | Background RSS poller (set `false` for offline / dev work) |
| `CONNECT_EMBEDDINGS_ENABLED` | `true` | Local fastembed embeddings + sqlite-vec index (set `false` to skip the model entirely) |
| `CONNECT_POLL_TICK_SECONDS` / `CONNECT_MAX_ITEMS_PER_POLL` / `CONNECT_MAX_ITEMS_PER_DAY` | `60` / `25` / `200` | Poller cadence and flood caps |
| `CONNECT_FETCH_RESPECT_ROBOTS` | `true` | robots.txt enforcement for discovered links (registered feed URLs are always fetched — feed-reader semantics) |

Frontend: `BACKEND_URL` (default `http://localhost:8000`) controls where the Next.js
rewrite proxies `/api/*` — useful when the backend runs on another port.

**Phase 1+** introduces the LLM layer: `ANTHROPIC_API_KEY` (tier-routed Anthropic models,
Batch API for background enrichment, spend ledger + daily budget governor) and
`TAVILY_API_KEY` (web search behind the `SearchClient` ABC). Both will be read from the
backend environment (`.env` / shell) — no LLM key is needed for Phase 0.

## Phase status

| Phase | Scope | Status |
|---|---|---|
| 0 | Corpus skeleton + T0 (no LLM): full schema, blob store, ingestion (URL/file/paste + RSS poller), simhash dedup, FTS5, fastembed + sqlite-vec, watches + deterministic matching, source registry CRUD + India seeds, `/feed` + `/library` + ingest UI | **Done** |
| 1 | KB alive (first LLM): provider ABC + Anthropic tiers, spend ledger + $2/day governor, Batch runner, T1 nightly enrichment + watch fast-path, entity mentions + aliases, `/entity/[id]`, `/watchlist` badges, `/search`, `/sources` UI | Planned |
| 2 | Events, threads, Brief: T2 promotion, event clustering + audit, story threading, thread pages, Today brief, calendar seed, view-cursor deltas | Planned |
| 3 | Verification + contradictions: claim decomposition, stance → deterministic verdicts, Tavily SearchClient, contradiction ledger, verdict history, minimal dossier + SSE | Planned |
| 4 | Provenance + deep sources + Ask: provenance tool loop, PRS + Indian Kanoon adapters, ProvenanceTimeline, hybrid RRF search, Ask-the-KB | Planned |
| 5 | Graph deepening + interpretation: grade-2 writeback, edge bitemporality, forces/impacts stages grounded by the accumulated graph | Planned |
| 6 | Loopholes, explorer, patterns, polish: loophole stage + cards, full narrative + citation verifier, `/graph` explorer, pattern dashboards, source quality scores, OllamaProvider | Planned |

The full design lives in the v0.1 plan (two pillars, tiered enrichment ladder, grounding
discipline, data model). Each phase ends runnable; the knowledge base accretes from day one.
