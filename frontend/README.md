# connect — frontend

Next.js 16 (App Router, TypeScript strict) + Tailwind v4 + shadcn/ui (Base UI
primitives) + TanStack Query. Phase 0 surface for the connect corpus: feed,
library, document reading view, source registry, watchlist, and the shared
"Add to corpus" ingest dialog.

## Run

```bash
npm install
npm run dev          # http://localhost:3000
```

All `/api/*` requests are rewritten to the FastAPI backend at
`http://localhost:8000` (override with the `BACKEND_URL` env var, see
`next.config.ts`). The UI degrades gracefully when the backend is down: every
query renders a skeleton then an error state with a retry button, and the
topbar health dot turns red.

## Checks

```bash
npx tsc --noEmit     # type-check
npm run lint
npm run build
```

## Layout

```
src/lib/api.ts        typed fetch wrapper + contract types (no any)
src/lib/queries.ts    TanStack Query hooks; mutations invalidate affected keys
src/lib/format.ts     SQLite-UTC-safe timestamp parsing + display helpers
src/app/              feed | library | documents/[id] | sources | watchlist | today
src/components/
  shell/              Sidebar, Topbar (health dot + Add to corpus)
  ingest/             IngestDialog (text / url / file tabs)
  shared/             StatusChip, EmptyState, PageHeader, Paginator, SearchBar, QueryError
  sources/            SourceForm (zod discriminated union), SourceHealthBadge, TierDots
  ui/                 shadcn-generated primitives
```
