"""Dynamic, auditable source-credibility scoring (S4).

The manual credibility_tier (1-4) is a hand-assigned PRIOR. This module derives
a continuous reliability score in [0, 1] from each source's track record: how
often the evidence it provided aligned with the claim's eventual verdict, recency
-weighted, anchored to the tier prior by a Beta-style pseudo-count so a thin
record stays at its prior and only a real track record moves it — within a floor
so a source can never be driven to zero by data alone (which bounds the
self-reinforcing-feedback risk: reliability is derived from verdicts that are
themselves reliability-weighted).

The score feeds analysis.stages.verify.source_weight (replacing the static tier
weight when present) and is surfaced to readers via the trust panel (S6). Every
recompute appends a source_credibility_history row, so a score is explainable.
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg

from connect.analysis.stages.verify import tier_weight
from connect.storage.pg import Jsonb, utc_now

log = logging.getLogger(__name__)

# Beta pseudo-count: how much evidence it takes to move a source off its tier
# prior. ~KAPPA aligned/misaligned outcomes ≈ half the prior's pull.
KAPPA = 8.0
# a source can't be driven below FLOOR_FRAC × its tier prior by data alone.
FLOOR_FRAC = 0.5
# recency half-life (days) for weighting an outcome by claim verdict age.
HALFLIFE_DAYS = 180.0

# recency-weighted alignment of a source's evidence with claim verdicts:
# aligned = it supported a supported claim / refuted a refuted claim.
_RECOMPUTE_SQL = """
WITH ev AS (
    SELECT e.stance, c.verdict,
           exp(-GREATEST(extract(epoch FROM
                 (now() - COALESCE(c.verdict_updated_at, c.created_at))), 0)
               / (86400 * %(halflife)s)) AS rw
      FROM evidence e
      JOIN document d ON d.id = e.document_id
      JOIN claim c    ON c.id = e.claim_id
     WHERE d.source_id = %(sid)s
       AND e.stance IN ('supports', 'refutes')
       AND c.verdict IN ('supported', 'refuted')
)
SELECT
    COALESCE(sum(rw) FILTER (
        WHERE (stance = 'supports' AND verdict = 'supported')
           OR (stance = 'refutes'  AND verdict = 'refuted')), 0) AS aligned_w,
    COALESCE(sum(rw), 0) AS total_w,
    count(*)             AS n
  FROM ev
"""


def reliability(*, aligned_w: float, total_w: float, prior: float) -> float:
    """Beta-posterior alignment rate, anchored to ``prior`` by KAPPA, floored at
    FLOOR_FRAC×prior and capped at 1.0. With no record (total_w=0) it returns the
    prior exactly."""
    score = (aligned_w + KAPPA * prior) / (total_w + KAPPA)
    return max(FLOOR_FRAC * prior, min(1.0, score))


async def _source_tier(conn: psycopg.AsyncConnection,
                       source_id: int) -> int | None:
    cur = await conn.execute(
        "SELECT credibility_tier FROM source WHERE id = %s", (source_id,))
    row = await cur.fetchone()
    return int(row["credibility_tier"]) if row else None


async def recompute_one(conn: psycopg.AsyncConnection, source_id: int, *,
                        trigger: str = "manual") -> float | None:
    """Recompute and persist one source's reliability. Returns the new score (or
    None if the source is gone)."""
    tier = await _source_tier(conn, source_id)
    if tier is None:
        return None
    prior = tier_weight(tier)
    cur = await conn.execute(
        _RECOMPUTE_SQL, {"sid": source_id, "halflife": HALFLIFE_DAYS})
    row = await cur.fetchone()
    aligned_w = float(row["aligned_w"] or 0.0)
    total_w = float(row["total_w"] or 0.0)
    n = int(row["n"] or 0)
    score = reliability(aligned_w=aligned_w, total_w=total_w, prior=prior)
    components = {"aligned_w": round(aligned_w, 4), "total_w": round(total_w, 4),
                  "prior": round(prior, 4), "n": n, "tier": tier}
    now = utc_now()
    async with conn.transaction():
        await conn.execute(
            "UPDATE source SET reliability_score = %s,"
            " reliability_updated_at = %s WHERE id = %s",
            (score, now, source_id))
        await conn.execute(
            "INSERT INTO source_credibility_history (source_id, computed_at,"
            " reliability_score, prior, sample_size, trigger, components)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (source_id, now, score, prior, n, trigger, Jsonb(components)))
    return score


async def recompute(conn: psycopg.AsyncConnection, *,
                    source_ids: list[int] | None = None,
                    trigger: str = "nightly") -> dict[int, float]:
    """Recompute reliability for the given sources (default: all enabled).
    Returns {source_id: score}."""
    if source_ids is None:
        cur = await conn.execute(
            "SELECT id FROM source WHERE enabled ORDER BY id")
        source_ids = [int(r["id"]) for r in await cur.fetchall()]
    out: dict[int, float] = {}
    for sid in source_ids:
        score = await recompute_one(conn, sid, trigger=trigger)
        if score is not None:
            out[sid] = score
    if out:
        log.info("credibility: recomputed %s source(s)", len(out))
    return out
