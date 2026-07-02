"""The /api/campaigns + /api/content surface — the social content pipeline.

`POST /campaigns` (202 + job) generates a batch of grounded drafts for a
subject; `GET /campaigns/{id}/events` streams generation progress (same SSE
contract as stories). `/content` is the review queue: list/detail, hand-edit,
and the owner-only transitions approve / reject / schedule / publish-now.
Tenancy mirrors stories/posts: reads are owner-or-shared (404 otherwise),
mutations are owner-only.
"""

from __future__ import annotations

from datetime import datetime

import psycopg
import pydantic
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sse_starlette.sse import EventSourceResponse

from connect.api.deps import get_container, get_current_user, get_db
from connect.api.sse import job_event_stream
from connect.content import publish as publish_mod
from connect.content import video as video_mod
from connect.content.schema import (
    CampaignAccepted,
    CampaignCreate,
    CampaignDetail,
    CampaignPage,
    ContentEdit,
    ContentItemDetail,
    ContentItemPage,
    ContentSeed,
    FORMAT_SCHEMA,
    RerenderRequest,
    ScheduleRequest,
    VoiceOption,
)
from connect.domain.enums import ContentItemStatus
from connect.domain.models import CurrentUser, JobAccepted, TrustReport
from connect.knowledge import trust
from connect.social import heygen, tts
from connect.llm.spend import BudgetExceeded
from connect.orchestration.container import Container
from connect.storage import campaigns as campaign_dao
from connect.storage import content_items as item_dao

campaigns_router = APIRouter(prefix="/campaigns", tags=["content"])
content_router = APIRouter(prefix="/content", tags=["content"])

MAX_PAGE_SIZE = 100


# === campaigns ================================================================


@campaigns_router.post("", response_model=CampaignAccepted, status_code=202)
async def create_campaign(body: CampaignCreate,
                          container: Container = Depends(get_container),
                          user: CurrentUser = Depends(get_current_user)):
    if body.visibility not in (None, "private", "shared"):
        raise HTTPException(status_code=422,
                            detail="visibility must be private or shared")
    try:
        seed = ContentSeed(
            story_dossier_id=body.story_dossier_id, story_id=body.story_id,
            investigation_id=body.investigation_id,
            workspace_id=body.workspace_id, topic=body.topic,
            since=body.since, until=body.until)
    except pydantic.ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    service = container.content
    assert service is not None
    if service.provider is None:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not set")
    if container.jobs is None:
        raise HTTPException(status_code=503,
                            detail="background jobs are unavailable")
    try:
        # one balanced call per format — a small look-ahead per requested
        # format. Empty formats = 'auto': the planner decides, so budget for
        # a typical commission (~3 formats + the plan call itself).
        await service.governor.check(0.05 * max(len(body.formats), 3),
                                     user_id=user.id)
    except BudgetExceeded as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    try:
        campaign_id, job_id = await service.start(
            seed, list(body.formats), body.options, owner_id=user.id,
            visibility=body.visibility)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return CampaignAccepted(campaign_id=campaign_id, job_id=job_id)


@campaigns_router.get("", response_model=CampaignPage)
async def list_campaigns(limit: int = Query(default=50, ge=1,
                                            le=MAX_PAGE_SIZE),
                         offset: int = Query(default=0, ge=0),
                         workspace_id: int | None = Query(default=None),
                         db: psycopg.AsyncConnection = Depends(get_db),
                         user: CurrentUser = Depends(get_current_user)):
    return await campaign_dao.list_page(db, viewer=user.id, limit=limit,
                                        offset=offset,
                                        workspace_id=workspace_id)


@campaigns_router.get("/{campaign_id}", response_model=CampaignDetail)
async def get_campaign(campaign_id: int,
                       db: psycopg.AsyncConnection = Depends(get_db),
                       user: CurrentUser = Depends(get_current_user)):
    detail = await campaign_dao.get_detail(db, campaign_id, viewer=user.id)
    if detail is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    return detail


def _translate(type_: str, data: dict) -> tuple[str, dict] | None:
    if type_ == "started":
        return None
    if type_ == "cancelled":
        return "error", {"message": "campaign cancelled"}
    if type_ == "error":
        return "error", {"message": (data.get("message") or data.get("error")
                                     or "campaign failed")}
    if type_ == "done":
        return "done", {}
    return type_, data


@campaigns_router.get("/{campaign_id}/events")
async def campaign_events(campaign_id: int, request: Request,
                          after: int = Query(default=0, ge=0),
                          container: Container = Depends(get_container),
                          db: psycopg.AsyncConnection = Depends(get_db),
                          user: CurrentUser = Depends(get_current_user)):
    row = await campaign_dao.get_row(db, campaign_id, viewer=user.id)
    if row is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    job_id = await campaign_dao.job_id_for(db, campaign_id)
    if job_id is None:
        raise HTTPException(status_code=404, detail="campaign has no job")
    last_event_id = request.headers.get("last-event-id")
    if last_event_id and last_event_id.isdigit():
        after = max(after, int(last_event_id))
    return EventSourceResponse(
        job_event_stream(request, container, job_id, after, _translate))


@campaigns_router.post("/{campaign_id}/cancel", response_model=JobAccepted,
                       status_code=202)
async def cancel_campaign(campaign_id: int,
                          container: Container = Depends(get_container),
                          db: psycopg.AsyncConnection = Depends(get_db),
                          user: CurrentUser = Depends(get_current_user)):
    service = container.content
    assert service is not None
    row = await campaign_dao.get_row(db, campaign_id, viewer=user.id)
    if row is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    if row["owner_id"] != user.id and user.role != "admin":
        raise HTTPException(status_code=403,
                            detail="only the owner (or an admin) may cancel")
    await service.cancel(campaign_id)
    job_id = await campaign_dao.job_id_for(db, campaign_id) or 0
    return JobAccepted(job_id=job_id)


@campaigns_router.delete("/{campaign_id}", status_code=204)
async def delete_campaign(campaign_id: int,
                          container: Container = Depends(get_container),
                          db: psycopg.AsyncConnection = Depends(get_db),
                          user: CurrentUser = Depends(get_current_user)):
    """Delete a campaign and its drafts (owner-only). A still-running
    generation is cancelled first so the worker stops cleanly."""
    row = await campaign_dao.get_row(db, campaign_id, viewer=user.id)
    if row is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    if row["owner_id"] != user.id and user.role != "admin":
        raise HTTPException(status_code=403,
                            detail="only the owner (or an admin) may delete")
    service = container.content
    if service is not None and row["status"] in ("pending", "running"):
        await service.cancel(campaign_id)
    await campaign_dao.delete(db, campaign_id, owner_id=row["owner_id"])
    return None


# === review queue =============================================================


@content_router.get("/platforms")
async def platform_status(container: Container = Depends(get_container),
                          user: CurrentUser = Depends(get_current_user)):
    """Which platforms can publish directly right now (creds configured)."""
    return publish_mod.connected_platforms(container.settings)


@content_router.get("/voices", response_model=list[VoiceOption])
async def list_voices(container: Container = Depends(get_container),
                      user: CurrentUser = Depends(get_current_user)):
    """ElevenLabs voices available for reel narration (empty if no key)."""
    return tts.list_elevenlabs_voices(container.settings)


@content_router.get("/capabilities")
async def reel_capabilities(container: Container = Depends(get_container),
                            user: CurrentUser = Depends(get_current_user)):
    """Optional reel features that are configured right now — drives which
    controls the UI offers. ``presenter`` is the HeyGen AI avatar; ``video`` is
    stock-video b-roll (needs a Pexels key)."""
    return {"presenter": heygen.is_configured(container.settings),
            "video": video_mod.available(container.settings),
            "zapier": publish_mod.zapier_configured(container.settings)}


@content_router.get("", response_model=ContentItemPage)
async def list_content(status: ContentItemStatus | None = Query(default=None),
                       platform: str | None = Query(default=None),
                       campaign_id: int | None = Query(default=None),
                       limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
                       offset: int = Query(default=0, ge=0),
                       db: psycopg.AsyncConnection = Depends(get_db),
                       user: CurrentUser = Depends(get_current_user)):
    """The review queue the caller may see; filter by status / platform /
    campaign."""
    return await item_dao.list_queue(
        db, viewer=user.id, status=status, platform=platform,
        campaign_id=campaign_id, limit=limit, offset=offset)


@content_router.get("/{item_id}", response_model=ContentItemDetail)
async def get_content(item_id: int,
                      db: psycopg.AsyncConnection = Depends(get_db),
                      user: CurrentUser = Depends(get_current_user)):
    item = await item_dao.get(db, item_id, viewer=user.id)
    if item is None:
        raise HTTPException(status_code=404, detail="content item not found")
    return item


async def _owned_item(db: psycopg.AsyncConnection, item_id: int,
                      user: CurrentUser) -> ContentItemDetail:
    item = await item_dao.get(db, item_id, viewer=user.id)
    if item is None:
        raise HTTPException(status_code=404, detail="content item not found")
    if item.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403,
                            detail="only the owner (or an admin) may do this")
    return item


def _gate_block_reason(item: ContentItemDetail,
                       container: Container) -> str | None:
    """In enforcing mode, a 'flagged' editorial-gate verdict hard-blocks any
    publish path. Warn-only (the default) returns None — the flag is advisory."""
    if not getattr(container.settings, "integrity_gate_enforcing", False):
        return None
    if (item.gate or {}).get("verdict") == "flagged":
        n = len((item.gate or {}).get("flagged") or [])
        return (f"editorial gate flagged {n} unsupported statement(s); edit the"
                " item to ground them before publishing")
    return None


async def _enqueue_verify(container: Container, item_id: int, owner_id: int, *,
                          promote_to: str | None = None) -> None:
    """Best-effort: enqueue a content_verify job to (re)compute the gate. No-op
    when the queue is unavailable (the generation-time report still stands)."""
    if container.jobs is None:
        return
    payload: dict = {"item_id": item_id}
    if promote_to is not None:
        payload["promote_to"] = promote_to
    await container.jobs.enqueue("content_verify", payload, owner_id=owner_id)


@content_router.get("/{item_id}/trust", response_model=TrustReport)
async def content_trust(item_id: int,
                        db: psycopg.AsyncConnection = Depends(get_db),
                        user: CurrentUser = Depends(get_current_user)):
    """The reader-facing 'why trust this' report for one item (S6): source mix +
    credibility tiers, dynamic reliability, gate verdict, balance, corrections,
    and the AI-generated disclosure."""
    item = await _owned_item(db, item_id, user)
    return await trust.build_trust_report(db, item_id, gate=item.gate)


@content_router.patch("/{item_id}", response_model=ContentItemDetail)
async def edit_content(item_id: int, body: ContentEdit,
                       container: Container = Depends(get_container),
                       db: psycopg.AsyncConnection = Depends(get_db),
                       user: CurrentUser = Depends(get_current_user)):
    """Hand-edit the generated payload (owner-only; flags grounding.edited). A
    hand-edit invalidates the generation-time gate report, so it re-runs the
    editorial gate against the edited text."""
    await _owned_item(db, item_id, user)
    ok = await item_dao.update_content(db, item_id, owner_id=user.id,
                                       content=body.content)
    if not ok:
        raise HTTPException(status_code=409,
                            detail="a published item cannot be edited")
    await _enqueue_verify(container, item_id, user.id)
    item = await item_dao.get(db, item_id, viewer=user.id)
    assert item is not None
    return item


@content_router.post("/{item_id}/rerender", response_model=JobAccepted,
                     status_code=202)
async def rerender_content(item_id: int, body: RerenderRequest,
                           container: Container = Depends(get_container),
                           db: psycopg.AsyncConnection = Depends(get_db),
                           user: CurrentUser = Depends(get_current_user)):
    """Re-render an item's media (reel video / cards) from an edited script +
    render controls (voice / music). Persists the edit, then enqueues the
    render job; the preview updates when it finishes."""
    item = await _owned_item(db, item_id, user)
    if item.status == "published":
        raise HTTPException(status_code=409,
                            detail="a published item cannot be re-rendered")
    if body.content is not None:
        try:
            FORMAT_SCHEMA[item.format](**body.content)   # validate the edit
        except (pydantic.ValidationError, TypeError, KeyError) as e:
            raise HTTPException(status_code=422,
                                detail=f"invalid content: {e}") from e
        await item_dao.update_content(db, item_id, owner_id=user.id,
                                      content=body.content)
        # a hand-edit invalidates the generation-time gate report, same as
        # the PATCH path — re-run the editorial gate against the edited text
        await _enqueue_verify(container, item_id, user.id)
    if container.jobs is None:
        raise HTTPException(status_code=503,
                            detail="background jobs are unavailable")
    job_id = await container.jobs.enqueue(
        "content_render",
        {"item_id": item_id, "options": body.options.model_dump(),
         "owner_id": user.id}, owner_id=user.id)
    return JobAccepted(job_id=job_id)


@content_router.post("/{item_id}/approve", response_model=ContentItemDetail)
async def approve_content(item_id: int,
                          container: Container = Depends(get_container),
                          db: psycopg.AsyncConnection = Depends(get_db),
                          user: CurrentUser = Depends(get_current_user)):
    """Approve a draft/rejected/flagged item. In enforcing mode the item first
    enters 'verifying' and a content_verify job re-checks it against its
    provenance, promoting it to 'approved' or back to 'flagged'. In warn-only
    mode it is approved directly (the gate annotation rides along; approving a
    flagged item is an explicit owner override)."""
    await _owned_item(db, item_id, user)
    froms = ("draft", "rejected", "flagged")
    enforcing = getattr(container.settings, "integrity_gate_enforcing", False)
    if enforcing and container.jobs is not None:
        ok = await item_dao.set_status(
            db, item_id, owner_id=user.id, status="verifying",
            from_statuses=froms)
    else:
        ok = await item_dao.set_status(
            db, item_id, owner_id=user.id, status="approved",
            from_statuses=froms)
    if not ok:
        raise HTTPException(
            status_code=409,
            detail="only a draft/rejected/flagged item can be approved")
    if enforcing and container.jobs is not None:
        await _enqueue_verify(container, item_id, user.id,
                              promote_to="approved")
    item = await item_dao.get(db, item_id, viewer=user.id)
    assert item is not None
    return item


@content_router.post("/{item_id}/reject", response_model=ContentItemDetail)
async def reject_content(item_id: int,
                         db: psycopg.AsyncConnection = Depends(get_db),
                         user: CurrentUser = Depends(get_current_user)):
    await _owned_item(db, item_id, user)
    ok = await item_dao.set_status(
        db, item_id, owner_id=user.id, status="rejected",
        from_statuses=("draft", "approved", "scheduled"))
    if not ok:
        raise HTTPException(status_code=409,
                            detail="a published item cannot be rejected")
    item = await item_dao.get(db, item_id, viewer=user.id)
    assert item is not None
    return item


@content_router.post("/{item_id}/schedule", response_model=ContentItemDetail)
async def schedule_content(item_id: int, body: ScheduleRequest,
                           container: Container = Depends(get_container),
                           db: psycopg.AsyncConnection = Depends(get_db),
                           user: CurrentUser = Depends(get_current_user)):
    """Set the publish time and move the item to 'scheduled' — beat publishes
    it when the time arrives. The editorial gate is enforced here too (not just
    on approve) so scheduling cannot bypass it."""
    item = await _owned_item(db, item_id, user)
    block = _gate_block_reason(item, container)
    if block:
        raise HTTPException(status_code=409, detail=block)
    when = _parse_ts(body.scheduled_at)
    ok = await item_dao.set_status(
        db, item_id, owner_id=user.id, status="scheduled",
        from_statuses=("draft", "approved", "scheduled", "flagged"),
        scheduled_at=when)
    if not ok:
        raise HTTPException(
            status_code=409,
            detail="only a draft/approved/scheduled item can be scheduled")
    item = await item_dao.get(db, item_id, viewer=user.id)
    assert item is not None
    return item


@content_router.post("/{item_id}/publish", response_model=JobAccepted,
                     status_code=202)
async def publish_content(item_id: int,
                          target: str = Query(default="direct"),
                          container: Container = Depends(get_container),
                          db: psycopg.AsyncConnection = Depends(get_db),
                          user: CurrentUser = Depends(get_current_user)):
    """Publish an item now (owner-only): move it to 'approved' and enqueue a
    content_publish job. ``target`` is 'direct' (platform adapter) or 'zapier'
    (POST to the configured Zapier webhook). The dedup index allows at most one
    live publish job per item."""
    if target not in ("direct", "zapier"):
        raise HTTPException(status_code=422,
                            detail="target must be direct or zapier")
    if target == "zapier" and not publish_mod.zapier_configured(
            container.settings):
        raise HTTPException(status_code=503,
                            detail="Zapier is not connected"
                                   " (set CONNECT_ZAPIER_WEBHOOK_URL)")
    item = await _owned_item(db, item_id, user)
    if container.jobs is None:
        raise HTTPException(status_code=503,
                            detail="background jobs are unavailable")
    if item.status == "published":
        raise HTTPException(status_code=409, detail="item already published")
    block = _gate_block_reason(item, container)
    if block:
        raise HTTPException(status_code=409, detail=block)
    await item_dao.set_status(
        db, item_id, owner_id=user.id, status="approved",
        from_statuses=("draft", "approved", "scheduled", "rejected", "flagged"))
    job_id = await container.jobs.enqueue(
        "content_publish", {"item_id": item_id, "target": target},
        owner_id=user.id)
    return JobAccepted(job_id=job_id)


def _parse_ts(value: str) -> str:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as e:
        raise HTTPException(
            status_code=422,
            detail="scheduled_at must be an ISO 8601 timestamp") from e
    return value
