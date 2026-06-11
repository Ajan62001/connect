"""connect.workers — the job-execution seam (v0.2 runtime design §2).

Phase A: handler registry + ``enqueue(kind, payload)`` over the existing
SQLite job table, still executed in-process by asyncio (queue.py). Nothing
crosses the enqueue boundary except (kind, payload) — so Phase B can swap
the execution backing for the Postgres SKIP LOCKED claim loop + worker
entrypoint without touching the enqueue sites again.
"""
