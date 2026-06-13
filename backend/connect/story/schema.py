"""Story-mode contracts: the seed (one of four fact sources), options, the
LLM's structured narrative output, and the API/detail shapes."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

StoryLength = Literal["brief", "standard", "feature"]
StoryTone = Literal["neutral", "explanatory"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class StorySeed(_Frozen):
    """Exactly one source must be set — the fact-set the story is told from."""
    story_id: int | None = None
    investigation_id: int | None = None
    workspace_id: int | None = None
    topic: str | None = None
    since: str | None = None   # ISO date (topic source, optional)
    until: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "StorySeed":
        sources = [self.story_id, self.investigation_id, self.workspace_id,
                   (self.topic or "").strip() or None]
        if sum(1 for s in sources if s) != 1:
            raise ValueError(
                "exactly one of story_id / investigation_id / workspace_id /"
                " topic is required")
        return self


class StoryOptions(_Frozen):
    length: StoryLength = "standard"
    tone: StoryTone = "explanatory"
    # free-text voice/style guidance (e.g. "tight newswire", "narrative
    # feature for a general audience"). Shapes voice + structure only — the
    # grounding gate (citation verification) is unaffected.
    style: str | None = Field(default=None, max_length=200)


class StoryCreate(BaseModel):
    """POST /api/stories body — seed fields flattened + options + visibility."""
    model_config = ConfigDict(extra="forbid")
    story_id: int | None = None
    investigation_id: int | None = None
    workspace_id: int | None = None
    topic: str | None = Field(default=None, max_length=400)
    since: str | None = None
    until: str | None = None
    options: StoryOptions = StoryOptions()
    visibility: str | None = None


class StoryAccepted(_Frozen):
    story_id: int
    job_id: int


class StoryOutput(BaseModel):
    """The model's structured narrative (validated at the tool-call layer)."""
    model_config = ConfigDict(extra="forbid")
    title: str
    narrative_md: str


# -- detail / list -------------------------------------------------------------


class StorySource(_Frozen):
    ref: str                       # the closed-menu id the narrative cites (E#)
    document_id: int | None = None
    title: str | None = None
    url: str | None = None
    source_name: str | None = None
    quote: str | None = None
    occurred_on: str | None = None
    finding_id: int | None = None


class StoryGrounding(_Frozen):
    menu_size: int = 0
    cited_count: int = 0
    stripped_markers: list[str] = Field(default_factory=list)
    regenerated: bool = False
    edited: bool = False   # the narrative was hand-edited after generation


class StoryEdit(BaseModel):
    """PATCH /api/stories/{id} — hand-edit the generated narrative."""
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=300)
    narrative_md: str | None = Field(default=None, max_length=100_000)


class StoryDetail(_Frozen):
    id: int
    title: str | None
    subject: str
    input_type: str
    status: str
    narrative_md: str | None = None
    sources: list[StorySource] = Field(default_factory=list)
    grounding: StoryGrounding | None = None
    visibility: str = "shared"
    owner_id: int | None = None
    last_seq: int = 0
    error: str | None = None
    created_at: str
    updated_at: str | None = None


class StoryListItem(_Frozen):
    id: int
    title: str | None
    subject: str
    input_type: str
    status: str
    created_at: str


class StoryPage(_Frozen):
    items: list[StoryListItem] = Field(default_factory=list)
    total: int = 0


# the value object the gather stage produces and the synthesize stage consumes;
# also what the 'scope' section persists (so synthesis is re-runnable).
class StoryFact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: int | None = None
    quote: str
    title: str | None = None
    source_name: str | None = None
    credibility_tier: int | None = None
    occurred_on: str | None = None
    finding_id: int | None = None


class StoryFactSet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str
    facts: list[StoryFact] = Field(default_factory=list)

    def as_content(self) -> dict[str, Any]:
        return {"subject": self.subject,
                "facts": [f.model_dump() for f in self.facts]}
