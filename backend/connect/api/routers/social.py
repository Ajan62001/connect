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
import io
import re

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile

import psycopg

from connect.api.deps import (
    get_container,
    get_current_user,
    get_db,
    require_admin,
)
from connect.domain.enums import T1_TOPICS
from connect.domain.models import (
    CurrentUser,
    InstagramStatus,
    PostSettings,
    PostSettingsUpdate,
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
from connect.social import generate, instagram, palettes
from connect.social import settings as post_settings
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

# caption/card prompt construction is shared with the workspace agent's draft
# tool (connect/social/generate.py) so the style stays consistent.
_system_for = generate.system_for


def _build_prompt(doc, enrichment) -> str:
    return generate.build_prompt(
        title=doc.title, source_name=doc.source_name,
        summary=enrichment.summary if enrichment is not None else None,
        claims=[c.text for c in enrichment.claims]
        if enrichment is not None else None,
        topics=enrichment.topics if enrichment is not None else None,
        content_text=doc.content_text)


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

    settings = await post_settings.effective(db)
    enrichment = await enrichment_dao.get_for_document(db, document_id)
    user_prompt = _build_prompt(doc, enrichment)
    system = _system_for(settings)
    est_in = (len(system) + len(user_prompt)) // 4 + 200
    projected = spend.cost_usd(
        container.llm.model_for(ModelTier.BALANCED),
        input_tokens=est_in, output_tokens=GEN_EST_OUT_TOKENS)
    try:
        await container.governor.check(projected, user_id=user.id)
    except BudgetExceeded as e:
        raise HTTPException(status_code=429, detail=str(e)) from e

    try:
        completion = await container.llm.complete_structured(
            system=system,
            messages=[{"role": "user", "content": user_prompt}],
            schema=SocialPost, tier=ModelTier.BALANCED,
            max_tokens=GEN_MAX_TOKENS)
    except LLMError as e:
        raise HTTPException(status_code=502,
                            detail=f"could not generate post: {e}") from e
    await spend.record_call(db, purpose=PURPOSE_SOCIAL,
                            model=completion.model, usage=completion.usage,
                            user_id=user.id)

    # auto-theme: pick a palette from the doc's topic + the model's suggestion,
    # bake the chosen name into the content so publish re-renders identically.
    topic = (enrichment.topics[0]
             if enrichment is not None and enrichment.topics else None)
    themed, palette = palettes.resolve_post_theme(
        settings, topic=topic, suggested=completion.output.suggested_palette)
    content = (completion.output.model_copy(update={"suggested_palette": palette})
               if palette else completion.output)
    logo = post_settings.load_logo(container.logo_store, themed)
    image = await asyncio.to_thread(
        render_card, content, settings=themed, logo=logo)
    return SocialPostDraft(
        content=content,
        image_b64=base64.b64encode(image).decode("ascii"))


@router.get("/settings", response_model=PostSettings)
async def get_post_settings(db: psycopg.AsyncConnection = Depends(get_db),
                            user: CurrentUser = Depends(get_current_user)):
    """The global default post-generation settings (readable by any user)."""
    return await post_settings.get_global(db)


@router.put("/settings", response_model=PostSettings,
            dependencies=[Depends(require_admin)])
async def put_post_settings(body: PostSettingsUpdate,
                            db: psycopg.AsyncConnection = Depends(get_db)):
    """Update the global default post-generation settings (admin)."""
    current = await post_settings.get_global(db)
    merged = post_settings._merge(current, body.model_dump(exclude_unset=True))
    await post_settings.set_global(db, merged)
    return merged


# a representative post for the live style preview (no LLM, never persisted)
_PREVIEW_SAMPLE = SocialPost(
    headline="RBI holds the repo rate at 6.5% as inflation cools",
    caption="The central bank kept its key rate steady, citing easing price"
            " pressures and a steady growth outlook. Source: RBI.",
    hashtags=["RBI", "MonetaryPolicy", "India"],
    key_points=["Repo rate unchanged at 6.5%",
                "Retail inflation eased to 4.8%",
                "FY27 growth seen at 6.6%"],
    source_label="Source: RBI",
    alt_text="A summary card preview.")
LOGO_MAX_BYTES = 2_000_000


@router.post("/preview")
async def preview_card(settings: PostSettings,
                       container: Container = Depends(get_container),
                       user: CurrentUser = Depends(get_current_user)):
    """Render a sample card with the given settings — no LLM call — so the
    style editor can show a live preview as colors/template/align change. The
    sample is a monetary-policy story, so with auto-theme on the preview shows
    the palette that topic would get."""
    themed, _ = palettes.resolve_post_theme(
        settings, topic="monetary-policy", suggested="gold")
    logo = post_settings.load_logo(container.logo_store, themed)
    image = await asyncio.to_thread(
        render_card, _PREVIEW_SAMPLE, settings=themed, logo=logo)
    return Response(content=image, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@router.post("/logo")
async def upload_logo(file: UploadFile = File(...),
                      container: Container = Depends(get_container),
                      user: CurrentUser = Depends(get_current_user)):
    """Upload a brand logo (normalized to a ≤512px PNG, stored by content sha).
    Returns ``logo_sha`` to put on PostSettings.logo_sha; the card renderer
    composites it and it is served at /api/social/logo/<sha>.png."""
    raw = await file.read()
    if len(raw) > LOGO_MAX_BYTES:
        raise HTTPException(status_code=413, detail="logo too large (max 2MB)")
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(raw)).convert("RGBA")
        img.thumbnail((512, 512))
        out = io.BytesIO()
        img.save(out, format="PNG")
        data = out.getvalue()
    except Exception as e:  # noqa: BLE001 — any decode failure is a bad upload
        raise HTTPException(status_code=422,
                            detail="not a valid image file") from e
    rel = container.logo_store.put(data)
    return {"logo_sha": rel.rsplit("/", 1)[-1]}


@router.get("/palettes")
async def list_palettes(user: CurrentUser = Depends(get_current_user)):
    """The built-in card palettes (name -> colors+template), the default
    topic->palette map, and the topic vocabulary — for the auto-theme editor."""
    return {"palettes": palettes.PALETTES,
            "topic_defaults": palettes.DEFAULT_TOPIC_PALETTE,
            "topics": list(T1_TOPICS)}


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

    # S2 editorial gate: the publish body carries CLIENT-supplied content, so
    # re-verify it server-side against the real source document before it can
    # reach Instagram. In enforcing mode an unsupported (e.g. fabricated/drifted)
    # figure hard-blocks; warn-only skips the check (it would not block anyway).
    if (getattr(s, "integrity_gate_enforcing", False)
            and container.llm is not None and (doc.content_text or "").strip()):
        from connect.analysis.budget import AnalysisBudget
        from connect.content import gate as gate_mod
        report = await gate_mod.assess(
            db, container.llm,
            content_texts=gate_mod.texts_from_content(body.content.model_dump()),
            source_quotes=[doc.content_text], document_ids=[document_id],
            budget=AnalysisBudget(0.20), governor=container.governor,
            viewer=user.id, enforcing=True)
        if report.blocks():
            raise HTTPException(
                status_code=409,
                detail="editorial gate blocked this post: a published statement"
                       " is not supported by the source document")

    settings = await post_settings.effective(db)
    # re-apply the palette the draft chose (carried on the content) so the
    # published card matches the preview.
    themed, _ = palettes.resolve_post_theme(
        settings, suggested=body.content.suggested_palette)
    logo = post_settings.load_logo(container.logo_store, themed)
    image = await asyncio.to_thread(
        render_card, body.content, settings=themed, logo=logo)
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


# PUBLIC (no auth): the rendered reel video. Instagram's servers fetch it from
# here to publish a Reel; served ONLY from the dedicated reel store, keyed by
# content sha, so it can never expose a (possibly private) document blob.
@public_router.get("/reel/{sha}.mp4")
async def get_reel(sha: str,
                   container: Container = Depends(get_container)):
    if not _SHA_RE.match(sha):
        raise HTTPException(status_code=404, detail="not found")
    rel = f"{sha[:2]}/{sha}"
    if not container.reel_store.exists(rel):
        raise HTTPException(status_code=404, detail="not found")
    return Response(
        content=container.reel_store.get(rel), media_type="video/mp4",
        headers={"Cache-Control": "public, max-age=86400",
                 "Accept-Ranges": "bytes"})


# PUBLIC (no auth): the rendered card composites the logo server-side, but the
# style editor also shows the uploaded logo directly. Served ONLY from the
# dedicated logo store, keyed by content sha — it can never expose a doc blob.
@public_router.get("/logo/{sha}.png")
async def get_logo(sha: str,
                   container: Container = Depends(get_container)):
    if not _SHA_RE.match(sha):
        raise HTTPException(status_code=404, detail="not found")
    rel = f"{sha[:2]}/{sha}"
    if not container.logo_store.exists(rel):
        raise HTTPException(status_code=404, detail="not found")
    return Response(
        content=container.logo_store.get(rel), media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"})
