"""The contradiction ledger — MATERIALIZED, pure SQL, zero LLM
(design result.consumption §3e).

``scan`` runs after any evidence insert batch (the verify stage calls it):
claims holding BOTH supporting and refuting evidence get a contradiction
row upserted with fresh counts and best credibility tier per side. Counts
refresh on every scan; ``status`` is user state (open/dismissed/resolved)
and is NEVER touched by the scanner — dismissing stays dismissed even when
counts move. Documents without a registered source count toward n_* but at
tier 4 (unverified) for the best-tier columns.
"""

from __future__ import annotations

from typing import Any, Mapping

import psycopg

from connect.storage.pg import utc_now

_SCAN_SQL = """
WITH stances AS (
    SELECT e.claim_id, e.stance,
           COALESCE(s.credibility_tier, 4) AS tier
    FROM evidence e
    JOIN document d ON d.id = e.document_id
    LEFT JOIN source s ON s.id = d.source_id
    WHERE e.stance IN ('supports', 'refutes')
)
SELECT claim_id,
       COUNT(*) FILTER (WHERE stance = 'supports') AS n_s,
       COUNT(*) FILTER (WHERE stance = 'refutes')  AS n_r,
       MIN(tier) FILTER (WHERE stance = 'supports') AS t_s,
       MIN(tier) FILTER (WHERE stance = 'refutes')  AS t_r
FROM stances
GROUP BY claim_id
HAVING COUNT(*) FILTER (WHERE stance = 'supports') > 0
   AND COUNT(*) FILTER (WHERE stance = 'refutes') > 0
"""


async def scan(conn: psycopg.AsyncConnection) -> int:
    """Upsert contradiction rows for every claim with evidence on both
    sides; returns how many rows were inserted or updated."""
    cur = await conn.execute(_SCAN_SQL)
    rows = await cur.fetchall()
    now = utc_now()
    touched = 0
    async with conn.transaction():
        for row in rows:
            await conn.execute(
                "INSERT INTO contradiction (claim_id, n_support, n_refute,"
                " best_tier_support, best_tier_refute, status, detected_at)"
                " VALUES (%s,%s,%s,%s,%s, 'open', %s)"
                " ON CONFLICT (claim_id) DO UPDATE SET"
                " n_support=EXCLUDED.n_support, n_refute=EXCLUDED.n_refute,"
                " best_tier_support=EXCLUDED.best_tier_support,"
                " best_tier_refute=EXCLUDED.best_tier_refute",
                (row["claim_id"], row["n_s"], row["n_r"], row["t_s"],
                 row["t_r"], now))
            touched += 1
    return touched


# -- read / mutate (the /api/contradictions surface) --------------------------------


def _to_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "claim": {"id": row["claim_id"], "text": row["claim_text"],
                  "verdict": row["claim_verdict"]},
        "n_support": row["n_support"],
        "n_refute": row["n_refute"],
        "best_tier_support": row["best_tier_support"],
        "best_tier_refute": row["best_tier_refute"],
        "status": row["status"],
        "detected_at": row["detected_at"],
    }


_LIST_SQL = """
SELECT k.id, k.claim_id, c.text AS claim_text, c.verdict AS claim_verdict,
       k.n_support, k.n_refute, k.best_tier_support, k.best_tier_refute,
       k.status, k.detected_at
FROM contradiction k JOIN claim c ON c.id = k.claim_id
"""


async def list_page(conn: psycopg.AsyncConnection, *,
                    status: str | None = None,
                    page: int = 1, page_size: int = 20,
                    ) -> tuple[list[dict[str, Any]], int]:
    where, params = "", []
    if status is not None:
        where = " WHERE k.status = %s"
        params.append(status)
    cur = await conn.execute(
        f"SELECT COUNT(*) AS n FROM contradiction k{where}", params)
    total = (await cur.fetchone())["n"]
    cur = await conn.execute(
        f"{_LIST_SQL}{where} ORDER BY k.detected_at DESC, k.id DESC"
        f" LIMIT %s OFFSET %s",
        (*params, page_size, (page - 1) * page_size))
    rows = await cur.fetchall()
    return [_to_dict(r) for r in rows], int(total)


async def get(conn: psycopg.AsyncConnection,
              contradiction_id: int) -> dict[str, Any] | None:
    cur = await conn.execute(f"{_LIST_SQL} WHERE k.id = %s",
                             (contradiction_id,))
    row = await cur.fetchone()
    return _to_dict(row) if row else None


async def dismiss(conn: psycopg.AsyncConnection,
                  contradiction_id: int) -> dict[str, Any] | None:
    async with conn.transaction():
        cur = await conn.execute(
            "UPDATE contradiction SET status = 'dismissed', resolved_at = %s"
            " WHERE id = %s", (utc_now(), contradiction_id))
    if cur.rowcount == 0:
        return None
    return await get(conn, contradiction_id)
