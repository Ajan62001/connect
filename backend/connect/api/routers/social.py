"""Social posting — turn a document's enrichment into an Instagram post.

`draft` generates the caption + card content (a governed LLM call grounded in
the document's enrichment) and renders the image. `publish` re-renders the
card, stores it in the public card namespace, and posts it via the Instagram
Graph API — only when the deployment is connected. `card` is the PUBLIC
endpoint Instagram fetches the image from (unguessable sha, served only from
the dedicated card store — never document blobs).
"""

from __future__ import annotations

import asyncio
import base64
import re

from fastapi import APIRouter, Depends, HTTPException, Response

import psycopg

from connect.api.deps import get_container, get_current_user, get_db
from connect.domain.models import (
    CurrentUser,
    InstagramStatus,
    SocialPost,
    SocialPostDraft,
    SocialPublishRequest,
    SocialPublishResult,
)
from connect.llm import spend
from connect.llm.provider import LLMError
from connect.llm.spend import BudgetExceeded
from connect.llm.tiers import ModelTier
from connect.orchestration.container import Container
from connect.social import instagram
from connect.social.card import render_card
from connect.storage import documents as doc_dao
from connect.storage import enrichment as enrichment_dao

router = APIRouter(prefix="/social", tags=["social"])
# A SEPARATE router for the one public endpoint (Instagram fetches the card
# image with no auth). Registered outside the auth-gated routers in main.py.
public_router = APIRouter(prefix="/social", tags=["social"])

PURPOSE_SOCIAL = "social_post"
DOC_CAP = 8_000
GEN_MAX_TOKENS = 800
GEN_EST_OUT_TOKENS = 500
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")

_SYSTEM = (
    "You turn ONE news document into an Instagram post. Use ONLY the supplied"
    " document and its enrichment — never invent figures, dates, or claims."
    " Tone: factual, neutral, engaging; no hype, no opinion. Always credit the"
    " source. Produce a punchy headline, 2-4 short factual key points for the"
    " image card, a 1-3 sentence caption that ends by crediting the source,"
    " and relevant hashtags.")


def _build_prompt(doc, enrichment) -> str:
    parts = [f"TITLE: {doc.title or 'untitled'}"]
    if doc.source_name:
        parts.append(f"SOURCE: {doc.source_name}")
    if enrichment is not None and enrichment.summary:
        parts.append(f"SUMMARY: {enrichment.summary}")
    if enrichment is not None and enrichment.claims:
        parts.append("KEY CLAIMS:\n"
                     + "\n".join(f"- {c.text}" for c in enrichment.claims[:6]))
    if enrichment is not None and enrichment.topics:
        parts.append("TOPICS: " + ", ".join(enrichment.topics))
    parts.append(f"DOCUMENT:\n{(doc.content_text or '')[:DOC_CAP]}")
    return "\n\n".join(parts)


def _caption_with_tags(content: SocialPost) -> str:
    tags = " ".join(f"#{t.lstrip('#')}" for t in content.hashtags if t.strip())
    return f"{content.caption}\n\n{tags}".strip() if tags else content.caption


@router.post("/documents/{document_id}/draft", response_model=SocialPostDraft)
async def draft_social_post(document_id: int,
                            container: Container = Depends(get_container),
                            db: psycopg.AsyncConnection = Depends(get_db),
                            user: CurrentUser = Depends(get_current_user)):
    """Generate an Instagram-ready caption + a rendered card from one
    document's enrichment. Synchronous, governed single LLM call."""
    doc = await doc_dao.get(db, document_id, viewer=user.id)
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    if container.llm is None:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not set")
    if not (doc.content_text or "").strip():
        raise HTTPException(status_code=422,
                            detail="document has no text to summarize")

    enrichment = await enrichment_dao.get_for_document(db, document_id)
    user_prompt = _build_prompt(doc, enrichment)
    est_in = (len(_SYSTEM) + len(user_prompt)) // 4 + 200
    projected = spend.cost_usd(
        container.llm.model_for(ModelTier.BALANCED),
        input_tokens=est_in, output_tokens=GEN_EST_OUT_TOKENS)
    try:
        await container.governor.check(projected, user_id=user.id)
    except BudgetExceeded as e:
        raise HTTPException(status_code=429, detail=str(e)) from e

    try:
        completion = await container.llm.complete_structured(
            system=_SYSTEM,
            messages=[{"role": "user", "content": user_prompt}],
            schema=SocialPost, tier=ModelTier.BALANCED,
            max_tokens=GEN_MAX_TOKENS)
    except LLMError as e:
        raise HTTPException(status_code=502,
                            detail=f"could not generate post: {e}") from e
    await spend.record_call(db, purpose=PURPOSE_SOCIAL,
                            model=completion.model, usage=completion.usage,
                            user_id=user.id)

    image = await asyncio.to_thread(render_card, completion.output)
    return SocialPostDraft(
        content=completion.output,
        image_b64=base64.b64encode(image).decode("ascii"))


@router.get("/instagram/status", response_model=InstagramStatus)
async def instagram_status(container: Container = Depends(get_container),
                           user: CurrentUser = Depends(get_current_user)):
    s = container.settings
    connected = instagram.is_configured(s)
    return InstagramStatus(
        connected=connected,
        account_id=s.instagram_business_account_id if connected else None)


@router.post("/documents/{document_id}/publish",
             response_model=SocialPublishResult)
async def publish_social_post(document_id: int, body: SocialPublishRequest,
                              container: Container = Depends(get_container),
                              db: psycopg.AsyncConnection = Depends(get_db),
                              user: CurrentUser = Depends(get_current_user)):
    """Render the card, host it publicly, and post it to Instagram via the
    Graph API. Requires the deployment to be connected (tokens + public URL)."""
    doc = await doc_dao.get(db, document_id, viewer=user.id)
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    s = container.settings
    if not instagram.is_configured(s):
        raise HTTPException(
            status_code=409,
            detail="Instagram is not connected (set INSTAGRAM_ACCESS_TOKEN, "
                   "INSTAGRAM_BUSINESS_ACCOUNT_ID and CONNECT_PUBLIC_BASE_URL)")

    image = await asyncio.to_thread(render_card, body.content)
    rel = container.card_store.put(image)
    sha = rel.rsplit("/", 1)[-1]
    image_url = f"{s.public_base_url.rstrip('/')}/api/social/card/{sha}.jpg"
    try:
        result = await instagram.publish(
            s, image_url=image_url, caption=_caption_with_tags(body.content))
    except instagram.InstagramError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return SocialPublishResult(**result)


# PUBLIC (no auth): Instagram's servers fetch the card here. Served ONLY from
# the dedicated card store and keyed by content sha, so it can never expose a
# document blob.
@public_router.get("/card/{sha}.jpg")
async def get_card(sha: str,
                   container: Container = Depends(get_container)):
    if not _SHA_RE.match(sha):
        raise HTTPException(status_code=404, detail="not found")
    rel = f"{sha[:2]}/{sha}"
    if not container.card_store.exists(rel):
        raise HTTPException(status_code=404, detail="not found")
    return Response(
        content=container.card_store.get(rel), media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"})
