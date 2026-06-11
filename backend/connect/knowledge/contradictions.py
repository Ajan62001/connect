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

import sqlite3
from typing import Any

from connect.storage.db import utc_now

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
       SUM(stance = 'supports') AS n_s,
       SUM(stance = 'refutes')  AS n_r,
       MIN(CASE WHEN stance = 'supports' THEN tier END) AS t_s,
       MIN(CASE WHEN stance = 'refutes'  THEN tier END) AS t_r
FROM stances
GROUP BY claim_id
HAVING n_s > 0 AND n_r > 0
"""


def scan(conn: sqlite3.Connection) -> int:
    """Upsert contradiction rows for every claim with evidence on both
    sides; returns how many rows were inserted or updated."""
    rows = conn.execute(_SCAN_SQL).fetchall()
    now = utc_now()
    touched = 0
    with conn:
        for row in rows:
            conn.execute(
                "INSERT INTO contradiction (claim_id, n_support, n_refute,"
                " best_tier_support, best_tier_refute, status, detected_at)"
                " VALUES (?,?,?,?,?, 'open', ?)"
                " ON CONFLICT (claim_id) DO UPDATE SET"
                " n_support=excluded.n_support, n_refute=excluded.n_refute,"
                " best_tier_support=excluded.best_tier_support,"
                " best_tier_refute=excluded.best_tier_refute",
                (row["claim_id"], row["n_s"], row["n_r"], row["t_s"],
                 row["t_r"], now))
            touched += 1
    return touched


# -- read / mutate (the /api/contradictions surface) --------------------------------


def _to_dict(row: sqlite3.Row) -> dict[str, Any]:
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


def list_page(conn: sqlite3.Connection, *, status: str | None = None,
              page: int = 1, page_size: int = 20,
              ) -> tuple[list[dict[str, Any]], int]:
    where, params = "", []
    if status is not None:
        where = " WHERE k.status = ?"
        params.append(status)
    total = conn.execute(
        f"SELECT COUNT(*) FROM contradiction k{where}", params).fetchone()[0]
    rows = conn.execute(
        f"{_LIST_SQL}{where} ORDER BY k.detected_at DESC, k.id DESC"
        f" LIMIT ? OFFSET ?",
        (*params, page_size, (page - 1) * page_size)).fetchall()
    return [_to_dict(r) for r in rows], int(total)


def get(conn: sqlite3.Connection,
        contradiction_id: int) -> dict[str, Any] | None:
    row = conn.execute(f"{_LIST_SQL} WHERE k.id = ?",
                       (contradiction_id,)).fetchone()
    return _to_dict(row) if row else None


def dismiss(conn: sqlite3.Connection,
            contradiction_id: int) -> dict[str, Any] | None:
    with conn:
        cur = conn.execute(
            "UPDATE contradiction SET status = 'dismissed', resolved_at = ?"
            " WHERE id = ?", (utc_now(), contradiction_id))
    if cur.rowcount == 0:
        return None
    return get(conn, contradiction_id)
