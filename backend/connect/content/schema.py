"""Content-pipeline contracts: the seed (one of five fact sources), options,
the per-format LLM output schemas (field descriptions ARE the instructions),
and the API/detail shapes. Sources reuse story mode's StorySource — the
closed-menu citation currency is identical.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from connect.domain.enums import CONTENT_FORMATS, ContentFormat
from connect.domain.models import SocialPost
from connect.story.schema import StorySource


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# -- seed / options ------------------------------------------------------------


class ContentSeed(_Frozen):
    """The subject the campaign is built from — exactly one source must be set.

    ``story_dossier_id`` repurposes an existing story-mode narrative (its
    already-verified sources become the menu); the other four resolve a fresh
    fact-set the same way story mode does (thread / investigation / workspace /
    topic)."""
    story_dossier_id: int | None = None
    story_id: int | None = None          # a KB story thread
    investigation_id: int | None = None
    workspace_id: int | None = None
    topic: str | None = None
    since: str | None = None             # ISO date (topic source, optional)
    until: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "ContentSeed":
        sources = [self.story_dossier_id, self.story_id, self.investigation_id,
                   self.workspace_id, (self.topic or "").strip() or None]
        if sum(1 for s in sources if s) != 1:
            raise ValueError(
                "exactly one of story_dossier_id / story_id /"
                " investigation_id / workspace_id / topic is required")
        return self


class ContentOptions(_Frozen):
    """Voice + shape knobs. Tone/style shape voice ONLY — the grounding gate
    (citation verification) is unaffected. Card styling (accent, sign-off,
    brand) still comes from the effective PostSettings at render time."""
    tone: str = "neutral, factual, engaging"
    style: str | None = Field(default=None, max_length=200)
    slide_count: int = Field(default=5, ge=3, le=8)      # carousel target
    thread_length: int = Field(default=5, ge=2, le=10)   # x thread target
    scene_count: int = Field(default=3, ge=3, le=5)      # reel scene target
    hashtag_count: int = Field(default=8, ge=0, le=30)
    # reel render controls (also used when re-rendering an edited reel)
    voice_id: str | None = None          # ElevenLabs voice override
    tts_engine: str | None = None        # 'elevenlabs' | 'piper' | 'none'
    music: bool = True                   # include a background-music bed
    music_volume: float | None = Field(default=None, ge=0.0, le=1.0)
    presenter: bool = False              # use the HeyGen avatar presenter for
                                         # reels (needs HeyGen configured; falls
                                         # back to the local slideshow otherwise)
    caption_style: str | None = None     # on-screen captions: 'karaoke' |
                                         # 'lower_third' | 'centered' | 'boxed'
                                         # (None => server default)
    video: bool | None = None            # stock VIDEO b-roll on/off (None =>
                                         # server default; needs a Pexels key)


# -- per-format LLM output schemas ---------------------------------------------
# ig_card reuses domain.models.SocialPost verbatim. The others mirror its
# style: the field descriptions are the model's instructions, and grounding
# ([[E#]] markers, verified in code) is required by the shared system prompt.


class CarouselSlide(BaseModel):
    model_config = ConfigDict(extra="forbid")
    heading: str = Field(
        description="A short slide heading (<=60 chars), factual and grounded"
                    " in the evidence.")
    bullets: list[str] = Field(
        default_factory=list,
        description="1-3 very short factual lines (<=100 chars each) for the"
                    " slide body, each grounded in a cited evidence item.")


class CarouselContent(BaseModel):
    """A multi-slide Instagram carousel: a cover, content slides, and the
    caption. Every factual line must be supported by a cited [[E#]] item."""
    model_config = ConfigDict(extra="forbid")
    title: str = Field(
        description="The cover-slide title (<=70 chars), punchy and factual.")
    slides: list[CarouselSlide] = Field(
        default_factory=list,
        description="The content slides, in reading order (3-8 of them).")
    caption: str = Field(
        description="The post caption: an engaging but factual 1-3 sentence"
                    " summary. End by crediting the source.")
    hashtags: list[str] = Field(
        default_factory=list,
        description="Relevant hashtags WITHOUT the '#' (e.g. 'RBI').")
    source_label: str = Field(
        default="",
        description="Short source credit for the card footer, e.g."
                    " 'Source: RBI'.")
    alt_text: str = Field(
        default="",
        description="One-sentence accessibility description of the carousel.")
    suggested_palette: str | None = Field(
        default=None,
        description="Optional: a palette name that fits the news mood, chosen"
                    " ONLY from the list given in the instructions; null"
                    " otherwise.")


class ReelScene(BaseModel):
    model_config = ConfigDict(extra="forbid")
    narration: str = Field(
        description="ONE short spoken sentence (<=130 chars) the voiceover reads"
                    " for this scene — tight, punchy, natural to say aloud, and"
                    " grounded in a cited [[E#]] item. No filler; every scene"
                    " earns its place.")
    on_screen_caption: str = Field(
        description="A SHORT, punchy on-screen caption (<=55 chars) — the few"
                    " big words the viewer reads on this frame. Make it land.")
    bullets: list[str] = Field(
        default_factory=list,
        description="0-1 optional supporting on-screen line (<=60 chars),"
                    " grounded in a cited evidence item. Usually leave empty —"
                    " reels are visual, not bullet lists.")
    image_query: str = Field(
        description="2-4 plain words to search the web for a background photo"
                    " that fits this scene — a concrete, photographable subject"
                    " (e.g. 'Reserve Bank India building', 'Indian rupee cash',"
                    " 'Mumbai stock exchange', 'oil refinery'). No punctuation.")


class ReelContent(BaseModel):
    """A vertical Instagram Reel: a scroll-stopping hook, 3-6 narrated scenes,
    a caption and hashtags, each scene over a relevant background photo. Every
    factual narration/on-screen line must be supported by a cited [[E#]] item;
    the hook and image queries are framing, not factual claims."""
    model_config = ConfigDict(extra="forbid")
    title: str = Field(
        description="The HOOK: a scroll-stopping opening line (<=60 chars) that"
                    " makes someone stop and watch — a sharp question, a"
                    " surprising number, or a bold stake. Punchy, never"
                    " clickbait-false; it must be supported by the evidence.")
    image_query: str = Field(
        default="",
        description="2-4 plain words for the HOOK's background photo (a"
                    " concrete, photographable subject tied to the topic).")
    scenes: list[ReelScene] = Field(
        default_factory=list,
        description="3-6 scenes in order; together they pay off the hook.")
    caption: str = Field(
        description="The post caption: an engaging but factual 1-3 sentence"
                    " summary. End by crediting the source.")
    hashtags: list[str] = Field(
        default_factory=list,
        description="Relevant hashtags WITHOUT the '#' (e.g. 'RBI').")
    source_label: str = Field(
        default="",
        description="Short source credit for the closing card, e.g."
                    " 'Source: RBI'.")
    alt_text: str = Field(
        default="",
        description="One-sentence accessibility description of the reel.")
    suggested_palette: str | None = Field(
        default=None,
        description="Optional: a palette name that fits the news mood, chosen"
                    " ONLY from the list given in the instructions; null"
                    " otherwise.")


class ThreadContent(BaseModel):
    """A platform-native text thread (X). The first tweet is the hook; each
    factual tweet cites its [[E#]] support inline."""
    model_config = ConfigDict(extra="forbid")
    tweets: list[str] = Field(
        default_factory=list,
        description="2-10 tweets, each <=270 chars. The first is a hook; the"
                    " last credits the source. Every factual claim cites a"
                    " [[E#]] item.")
    hashtags: list[str] = Field(
        default_factory=list,
        description="A few relevant hashtags WITHOUT the '#', appended to the"
                    " final tweet.")


class LinkedInContent(BaseModel):
    """A single LinkedIn post — a short explanatory note grounded in the
    evidence, ending with a source credit."""
    model_config = ConfigDict(extra="forbid")
    body: str = Field(
        description="The post body (<=2800 chars): 2-5 short paragraphs,"
                    " factual and accessible. Cite [[E#]] items inline and end"
                    " by crediting the source.")
    hashtags: list[str] = Field(
        default_factory=list,
        description="A few relevant hashtags WITHOUT the '#'.")


# the output schema for each format (ig_card == SocialPost)
FORMAT_SCHEMA: dict[str, type[BaseModel]] = {
    "ig_card": SocialPost,
    "ig_carousel": CarouselContent,
    "x_thread": ThreadContent,
    "linkedin_post": LinkedInContent,
    "ig_reel": ReelContent,
}


# -- API request / response ----------------------------------------------------


class CampaignCreate(BaseModel):
    """POST /api/campaigns — seed fields flattened + formats + options."""
    model_config = ConfigDict(extra="forbid")
    story_dossier_id: int | None = None
    story_id: int | None = None
    investigation_id: int | None = None
    workspace_id: int | None = None
    topic: str | None = Field(default=None, max_length=400)
    since: str | None = None
    until: str | None = None
    formats: list[ContentFormat] = Field(min_length=1)
    options: ContentOptions = ContentOptions()
    visibility: str | None = None

    @model_validator(mode="after")
    def _dedup_formats(self) -> "CampaignCreate":
        bad = [f for f in self.formats if f not in CONTENT_FORMATS]
        if bad:
            raise ValueError(f"unknown format(s): {bad}")
        return self


class CampaignAccepted(_Frozen):
    campaign_id: int
    job_id: int


class ContentItemDetail(_Frozen):
    id: int
    campaign_id: int
    platform: str
    format: str
    status: str
    content: dict[str, Any] = Field(default_factory=dict)
    sources: list[StorySource] = Field(default_factory=list)
    grounding: dict[str, Any] = Field(default_factory=dict)
    card_shas: list[str] = Field(default_factory=list)
    visibility: str = "shared"
    owner_id: int | None = None
    edited: bool = False
    scheduled_at: str | None = None
    published_at: str | None = None
    publish_ref: dict[str, Any] | None = None
    error: str | None = None
    created_at: str
    updated_at: str | None = None


class CampaignDetail(_Frozen):
    id: int
    subject: str
    input_type: str
    status: str
    formats: list[str] = Field(default_factory=list)
    visibility: str = "shared"
    owner_id: int | None = None
    last_seq: int = 0
    error: str | None = None
    items: list[ContentItemDetail] = Field(default_factory=list)
    created_at: str
    updated_at: str | None = None


class CampaignListItem(_Frozen):
    id: int
    subject: str
    input_type: str
    status: str
    formats: list[str] = Field(default_factory=list)
    item_count: int = 0
    created_at: str


class CampaignPage(_Frozen):
    items: list[CampaignListItem] = Field(default_factory=list)
    total: int = 0


class ContentItemPage(_Frozen):
    items: list[ContentItemDetail] = Field(default_factory=list)
    total: int = 0


class ContentEdit(BaseModel):
    """PATCH /api/content/{id} — hand-edit the generated payload (owner-only).
    Replaces the whole format payload; flags grounding.edited."""
    model_config = ConfigDict(extra="forbid")
    content: dict[str, Any]


class ScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scheduled_at: str   # ISO 8601 timestamp (UTC)


class VoiceOption(_Frozen):
    id: str
    name: str
    description: str = ""


class RerenderRequest(BaseModel):
    """Re-render an item's media (reel video / cards) with edited content +
    render controls. ``content`` (optional) replaces the payload first."""
    model_config = ConfigDict(extra="forbid")
    content: dict[str, Any] | None = None
    options: ContentOptions = ContentOptions()
