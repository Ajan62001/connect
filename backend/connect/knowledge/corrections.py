"""Corrections, retractions & verdict propagation (S3).

When the corpus changes its mind — a claim's verdict flips (e.g. supported ->
refuted) or an upstream source edits/withdraws a document — the published
content that relied on that evidence must not silently stand. This module turns
those events into durable `correction` rows and fans them out to the affected
content_items via the S1 provenance reverse index, recording exactly what was
done so the editorial trail is auditable and the propagation is idempotent.

The detection points feed in:
  * analysis.writeback.update_verdict -> record_verdict_flip on a real flip
  * the source_recheck job -> record_source_issue on a changed/withdrawn page
A periodic `content_correction` sweep then calls propagate_open.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import psycopg

from connect.storage.pg import utc_now

_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_WS = re.compile(r"\s+")

log = logging.getLogger(__name__)

# content_item statuses still in the review pipeline (a correction soft-flags
# them); 'published' is live (a correction retracts or marks it corrected).
_REVIEW = ("draft", "verifying", "approved", "scheduled", "flagged")

# verdicts that make a flip worth propagating (an item grounded in a now-refuted
# or newly-contested claim needs a human look)
_PROPAGATING_VERDICTS = {"refuted", "mixed"}


def is_propagating_flip(from_verdict: str | None, to_verdict: str | None) -> bool:
    return bool(from_verdict and to_verdict and from_verdict != to_verdict
                and ({from_verdict, to_verdict} & _PROPAGATING_VERDICTS))


async def record_verdict_flip(conn: psycopg.AsyncConnection, *, claim_id: int,
                              from_verdict: str, to_verdict: str) -> int | None:
    """Open a verdict_flip correction (idempotent: skip if an identical open one
    already exists for this claim+target). Returns the new id or None."""
    cur = await conn.execute(
        "SELECT 1 FROM correction WHERE kind = 'verdict_flip' AND claim_id = %s"
        " AND to_verdict = %s AND status = 'open'", (claim_id, to_verdict))
    if await cur.fetchone():
        return None
    cur = await conn.execute(
        "INSERT INTO correction (kind, claim_id, from_verdict, to_verdict,"
        " detail, status, created_at) VALUES"
        " ('verdict_flip', %s, %s, %s, %s, 'open', %s) RETURNING id",
        (claim_id, from_verdict, to_verdict,
         f"claim verdict {from_verdict} → {to_verdict}", utc_now()))
    return int((await cur.fetchone())["id"])


async def record_source_issue(conn: psycopg.AsyncConnection, *, document_id: int,
                              kind: str, detail: str) -> int | None:
    """Open a source_edit / source_retraction correction for a document
    (idempotent per open kind+document)."""
    cur = await conn.execute(
        "SELECT 1 FROM correction WHERE kind = %s AND document_id = %s"
        " AND status = 'open'", (kind, document_id))
    if await cur.fetchone():
        return None
    cur = await conn.execute(
        "INSERT INTO correction (kind, document_id, detail, status, created_at)"
        " VALUES (%s, %s, %s, 'open', %s) RETURNING id",
        (kind, document_id, detail, utc_now()))
    return int((await cur.fetchone())["id"])


def _action_for(kind: str, to_verdict: str | None, status: str) -> str | None:
    """The status an affected item should move to (None => leave it)."""
    severe = kind == "source_retraction" or to_verdict == "refuted"
    if status == "published":
        return "retracted" if severe else "corrected"
    if status in _REVIEW:
        return "flagged"
    return None  # rejected/failed/corrected/retracted: already handled


async def _affected_documents(conn: psycopg.AsyncConnection,
                              c: dict[str, Any]) -> list[int]:
    if c["kind"] == "verdict_flip" and c["claim_id"] is not None:
        cur = await conn.execute(
            "SELECT DISTINCT document_id FROM claim_sighting WHERE claim_id = %s",
            (c["claim_id"],))
        return [int(r["document_id"]) for r in await cur.fetchall()]
    if c["document_id"] is not None:
        return [int(c["document_id"])]
    return []


async def propagate_open(conn: psycopg.AsyncConnection, *,
                         limit: int = 200) -> dict[str, int]:
    """Process open corrections: fan each out to the content_items grounded in
    the affected documents (S1 reverse index), set their status, record the
    link, and mark the correction acknowledged. Idempotent — the link table's
    UNIQUE(correction_id, content_item_id) means a re-run never double-acts."""
    counts = {"corrections": 0, "items": 0}
    async with conn.transaction():
        cur = await conn.execute(
            "SELECT id, kind, claim_id, document_id, to_verdict FROM correction"
            " WHERE status = 'open' ORDER BY id LIMIT %s", (limit,))
        open_rows = [dict(r) for r in await cur.fetchall()]
        for c in open_rows:
            doc_ids = await _affected_documents(conn, c)
            if doc_ids:
                cur = await conn.execute(
                    "SELECT DISTINCT cis.content_item_id, ci.status"
                    " FROM content_item_source cis"
                    " JOIN content_item ci ON ci.id = cis.content_item_id"
                    " WHERE cis.document_id = ANY(%s)", (doc_ids,))
                for r in await cur.fetchall():
                    item_id, status = int(r["content_item_id"]), r["status"]
                    action = _action_for(c["kind"], c["to_verdict"], status)
                    if action is None:
                        continue
                    ins = await conn.execute(
                        "INSERT INTO content_item_correction (correction_id,"
                        " content_item_id, prior_status, action, created_at)"
                        " VALUES (%s, %s, %s, %s, %s)"
                        " ON CONFLICT (correction_id, content_item_id)"
                        " DO NOTHING", (c["id"], item_id, status, action,
                                        utc_now()))
                    if ins.rowcount:
                        await conn.execute(
                            "UPDATE content_item SET status = %s,"
                            " updated_at = %s WHERE id = %s",
                            (action, utc_now(), item_id))
                        counts["items"] += 1
            await conn.execute(
                "UPDATE correction SET status = 'acknowledged', resolved_at = %s"
                " WHERE id = %s", (utc_now(), c["id"]))
            counts["corrections"] += 1
    if counts["corrections"]:
        log.info("corrections: propagated %s correction(s) to %s item(s)",
                 counts["corrections"], counts["items"])
    return counts


# -- source re-check (the source_recheck job) --------------------------------

def _numkeys(text: str) -> set[str]:
    return {m.group(0).replace(",", "") for m in _NUM.finditer(text or "")}


def _materially_changed(old: str, new: str) -> bool:
    """A correction-worthy change: the figures stated changed, or the body
    grew/shrank substantially. Cosmetic boilerplate churn is ignored."""
    if _numkeys(old) != _numkeys(new):
        return True
    o = _WS.sub(" ", old or "").strip()
    n = _WS.sub(" ", new or "").strip()
    if not o:
        return False
    return abs(len(n) - len(o)) / max(len(o), 1) > 0.25


async def recheck_document(conn: psycopg.AsyncConnection, document_id: int, *,
                           gone: bool, new_text: str | None) -> str | None:
    """Apply a re-fetch outcome for one document: open a source_retraction (the
    page is gone) or source_edit (figures/body changed) correction, and stamp
    the source's recheck cursor. Pure logic — the caller does the network fetch
    and decides ``gone``/``new_text`` (so this is offline-testable)."""
    cur = await conn.execute(
        "SELECT content_text, source_id FROM document WHERE id = %s",
        (document_id,))
    row = await cur.fetchone()
    if row is None:
        return None
    outcome: str | None = None
    if gone:
        await record_source_issue(
            conn, document_id=document_id, kind="source_retraction",
            detail="source URL no longer reachable")
        outcome = "retracted"
    elif new_text is not None and _materially_changed(
            row["content_text"] or "", new_text):
        await record_source_issue(
            conn, document_id=document_id, kind="source_edit",
            detail="source content changed since ingest")
        outcome = "edited"
    if row["source_id"] is not None:
        await conn.execute(
            "UPDATE source SET last_rechecked_at = %s WHERE id = %s",
            (utc_now(), row["source_id"]))
    return outcome


async def due_for_recheck(conn: psycopg.AsyncConnection, *,
                          max_age_days: int = 7, limit: int = 50) -> list[int]:
    """Documents that back a PUBLISHED item and whose source hasn't been
    re-checked recently — the nightly source_recheck work-list."""
    cur = await conn.execute(
        "SELECT DISTINCT cis.document_id"
        " FROM content_item_source cis"
        " JOIN content_item ci ON ci.id = cis.content_item_id"
        " JOIN document d ON d.id = cis.document_id"
        " LEFT JOIN source s ON s.id = d.source_id"
        " WHERE ci.status = 'published' AND cis.document_id IS NOT NULL"
        "   AND d.url IS NOT NULL"
        "   AND (s.last_rechecked_at IS NULL"
        "        OR s.last_rechecked_at < now() - make_interval(days => %s))"
        " LIMIT %s", (max_age_days, limit))
    return [int(r["document_id"]) for r in await cur.fetchall()]


async def list_for_item(conn: psycopg.AsyncConnection,
                        content_item_id: int) -> list[dict[str, Any]]:
    """The correction events that touched one content_item (for the trust
    panel / item detail)."""
    cur = await conn.execute(
        "SELECT cic.action, cic.prior_status, cic.created_at, c.kind,"
        " c.from_verdict, c.to_verdict, c.detail"
        " FROM content_item_correction cic"
        " JOIN correction c ON c.id = cic.correction_id"
        " WHERE cic.content_item_id = %s ORDER BY cic.id DESC",
        (content_item_id,))
    return [dict(r) for r in await cur.fetchall()]
