"""Reader-facing trust report (S6).

Assembles the 'why trust this' summary for one published content_item from the
data the editorial-integrity suite already produces: the provenance link rows
(S1) with their credibility-tier snapshot, the dynamic reliability score (S4),
the gate report (S2) for flags/contested evidence, and the correction trail (S3).
Adds a balance score over the source mix (outlet diversity / one-sidedness).

Pure read assembly — no LLM, no writes — so it is cheap to compute on demand.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import psycopg

from connect.domain.models import TrustReport, TrustSource
from connect.knowledge import corrections


def _publisher_key(source_name: str | None, url: str | None) -> str | None:
    if source_name:
        return source_name.strip().lower()
    if url:
        host = urlsplit(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host or None
    return None


def _balance(n_publishers: int, n_tiers: int) -> tuple[float, str, bool]:
    """A coarse source-diversity score: more independent publishers AND more
    distinct credibility tiers => broader, less one-sided coverage."""
    if n_publishers <= 0:
        return 0.0, "unknown", False
    pub = min(1.0, n_publishers / 3.0)            # 3+ outlets => full marks
    tier = min(1.0, n_tiers / 2.0)                # 2+ tiers => full marks
    score = round(0.7 * pub + 0.3 * tier, 3)
    label = ("single" if n_publishers == 1 else
             "narrow" if score < 0.5 else
             "moderate" if score < 0.8 else "broad")
    return score, label, n_publishers == 1


def _confidence(gate_verdict: str | None, n_publishers: int,
                flagged: int) -> float:
    base = {"pass": 0.8, None: 0.6, "error": 0.6,
            "flagged": 0.4}.get(gate_verdict, 0.6)
    # corroboration lifts confidence; flags pull it down
    base += min(0.15, 0.05 * max(0, n_publishers - 1))
    base -= min(0.3, 0.1 * flagged)
    return round(max(0.0, min(1.0, base)), 3)


async def build_trust_report(conn: psycopg.AsyncConnection, item_id: int, *,
                             gate: dict[str, Any] | None = None) -> TrustReport:
    gate = gate or {}
    # provenance rows joined to the live source for reliability + name
    cur = await conn.execute(
        "SELECT cis.ref, cis.document_id, cis.source_name, cis.title, cis.url,"
        " cis.quote, cis.credibility_tier,"
        " s.name AS live_source_name, s.reliability_score,"
        " d.published_at, d.fetched_at"
        " FROM content_item_source cis"
        " LEFT JOIN document d ON d.id = cis.document_id"
        " LEFT JOIN source s ON s.id = d.source_id"
        " WHERE cis.content_item_id = %s ORDER BY cis.id", (item_id,))
    rows = await cur.fetchall()

    sources: list[TrustSource] = []
    tier_mix: dict[str, int] = {}
    publishers: set[str] = set()
    freshest: str | None = None
    for r in rows:
        name = r["source_name"] or r["live_source_name"]
        sources.append(TrustSource(
            ref=r["ref"], document_id=r["document_id"], source_name=name,
            title=r["title"], url=r["url"], quote=r["quote"],
            credibility_tier=r["credibility_tier"],
            reliability_score=r["reliability_score"]))
        if r["credibility_tier"] is not None:
            k = str(r["credibility_tier"])
            tier_mix[k] = tier_mix.get(k, 0) + 1
        pk = _publisher_key(name, r["url"])
        if pk:
            publishers.add(pk)
        dt = r["published_at"] or r["fetched_at"]
        if dt is not None:
            s = str(dt)
            if freshest is None or s > freshest:
                freshest = s

    n_pub = len(publishers)
    score, label, one_sided = _balance(n_pub, len(tier_mix))
    flagged_count = len(gate.get("flagged") or [])
    gate_verdict = gate.get("verdict")
    return TrustReport(
        gate_verdict=gate_verdict,
        flagged_count=flagged_count,
        confidence=_confidence(gate_verdict, n_pub, flagged_count),
        sources=sources,
        tier_mix=tier_mix,
        independent_publishers=n_pub,
        balance_score=score,
        balance_label=label,
        one_sided=one_sided,
        contested=list(gate.get("contested") or []),
        corrections=await corrections.list_for_item(conn, item_id),
        freshness=freshest,
        ai_disclosure=True)
