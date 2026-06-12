"""FROZEN contracts for investigation mode (design doc §1, §5, §8).

Three families:
- input contracts (InvestigationSeed / InvestigationOptions),
- the deterministic ScopePack the zero-LLM scope stage freezes,
- LLM structured-output schemas (question generation + synthesis) and the
  API response models the router serves.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

QuestionType = Literal[
    "why_now", "why_this_design", "why_not_alternative", "who_pushed",
    "what_triggered", "why_silent", "who_benefits", "what_next"]
QuestionStatus = Literal["open", "partial", "answered", "dropped"]
FindingKind = Literal[
    "reaction", "trigger", "alternative", "actor_motive", "timing",
    "context", "consequence"]
AboutType = Literal["entity", "event", "claim", "document", "dossier"]

MAX_QUESTIONS = 10


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


# -- input ---------------------------------------------------------------------------


class InvestigationSeed(_Frozen):
    """Exactly one of topic / entity_id / event_id / story_id / question_id
    (question_id = manual recursion from an open question)."""
    topic: str | None = None
    entity_id: int | None = None
    event_id: int | None = None
    story_id: int | None = None
    question_id: int | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "InvestigationSeed":
        set_fields = [name for name in ("topic", "entity_id", "event_id",
                                        "story_id", "question_id")
                      if getattr(self, name) is not None]
        if self.topic is not None and not self.topic.strip():
            raise ValueError("topic is empty")
        if len(set_fields) != 1:
            raise ValueError(
                "exactly one of topic/entity_id/event_id/story_id/"
                f"question_id must be set (got {set_fields or 'none'})")
        return self


class InvestigationOptions(BaseModel):
    budget_usd: float = 1.00
    max_iterations: int = 14
    max_web_fetches: int = 8

    @field_validator("budget_usd")
    @classmethod
    def _clamp_budget(cls, v: float) -> float:
        return min(10.0, max(0.01, v))

    @field_validator("max_iterations")
    @classmethod
    def _clamp_iterations(cls, v: int) -> int:
        return min(30, max(1, v))

    @field_validator("max_web_fetches")
    @classmethod
    def _clamp_fetches(cls, v: int) -> int:
        return min(20, max(0, v))


# -- the frozen scope pack -------------------------------------------------------------


class AnchorEntity(_Frozen):
    entity_id: int
    name: str
    entity_type: str


class ScopeDoc(_Frozen):
    document_id: int
    title: str | None = None
    source_name: str | None = None
    credibility_tier: int | None = None
    published_at: str | None = None
    url: str | None = None


class ScopeEdge(_Frozen):
    edge_id: int
    src_type: str
    src_id: int
    dst_type: str
    dst_id: int
    relation: str
    speculation: bool = False
    grade: int | None = None


class TimelineItem(_Frozen):
    event_id: int | None = None
    document_id: int | None = None
    date: str | None = None
    title: str
    event_type: str | None = None
    doc_count: int = 0


class CalendarItem(_Frozen):
    calendar_event_id: int
    kind: str
    occurs_on: str
    label: str


class ReactionCandidate(_Frozen):
    """One inferred B-after-A pair from the closed candidate menu —
    speculative reaction/trigger findings MUST cite one of these ids."""
    candidate_id: str          # "RC1"
    src_event_id: int          # B — the later event (the reaction)
    dst_event_id: int          # A — the earlier event (the trigger)
    src_title: str
    dst_title: str
    days_apart: float
    entity_jaccard: float
    cosine: float


class CausalMarkerHit(_Frozen):
    """Scope-time pre-scan: one causal-marker phrase found in a slice doc;
    the agent reads the doc and grounds (or discards) the sentence."""
    document_id: int
    title: str | None = None
    marker: str
    sentence: str


class StoryRef(_Frozen):
    story_id: int
    title: str | None = None


class ScopePack(_Frozen):
    """The deterministic pre-pass output, persisted as the 'scope'
    dossier_section content. Built without a single LLM call."""
    input_text: str
    input_type: str
    anchors: list[AnchorEntity] = []
    documents: list[ScopeDoc] = []
    edges: list[ScopeEdge] = []
    stories: list[StoryRef] = []
    timeline: list[TimelineItem] = []
    calendar: list[CalendarItem] = []
    reaction_candidates: list[ReactionCandidate] = []
    causal_markers: list[CausalMarkerHit] = []
    coverage: Literal["ok", "thin"] = "ok"

    def candidate(self, candidate_id: str) -> ReactionCandidate | None:
        for c in self.reaction_candidates:
            if c.candidate_id == candidate_id:
                return c
        return None


# -- question generation (one BALANCED structured call) ---------------------------------


class GeneratedQuestion(_Frozen):
    qtype: QuestionType
    text: str
    about_type: AboutType | None = None
    about_id: int | None = None
    priority: float = 0.5

    @field_validator("priority")
    @classmethod
    def _clamp_priority(cls, v: float) -> float:
        return min(1.0, max(0.0, v))


class GeneratedQuestions(_Frozen):
    questions: list[GeneratedQuestion] = []

    @field_validator("questions")
    @classmethod
    def _clamp(cls, v: list[GeneratedQuestion]) -> list[GeneratedQuestion]:
        return v[:MAX_QUESTIONS]


# -- synthesis (one DEEP structured call over the findings menu) -------------------------


class ChainStep(_Frozen):
    node_type: str
    node_id: int
    title: str
    relation_to_next: str | None = None
    speculation: bool = False
    finding_id: int | None = None


class CausalChain(_Frozen):
    steps: list[ChainStep] = []
    speculation: bool = False


class ActorItem(_Frozen):
    entity_id: int
    name: str
    role: str
    motive_md: str
    finding_ids: list[int] = []
    speculation: bool = False


class WatchNextItem(_Frozen):
    text: str
    question_id: int | None = None
    calendar_event_id: int | None = None
    watch_suggestion: str | None = None


class SynthesisOutput(_Frozen):
    narrative_md: str
    chains: list[CausalChain] = []
    actors: list[ActorItem] = []
    watch_next: list[WatchNextItem] = []


# -- API contracts -------------------------------------------------------------------


class InvestigationCreate(BaseModel):
    """POST /api/investigations body — seed fields flattened + options."""
    topic: str | None = None
    entity_id: int | None = None
    event_id: int | None = None
    story_id: int | None = None
    options: InvestigationOptions = InvestigationOptions()
    # None -> 'shared' (design §1 default), or 'private' when the seed is a
    # question of a private dossier; 'private' is the explicit opt-in.
    visibility: str | None = None


class InvestigationAccepted(_Frozen):
    investigation_id: int
    job_id: int


class InvestigationCounts(_Frozen):
    findings: int = 0
    questions_open: int = 0
    questions_answered: int = 0
    docs_added: int = 0


class InvestigationListItem(_Frozen):
    id: int
    status: str
    input_text: str
    input_type: str | None = None
    title: str | None = None
    created_at: str
    finished_at: str | None = None
    counts: InvestigationCounts = InvestigationCounts()
    visibility: str = "shared"
    owner_id: int | None = None
    owner_name: str | None = None


class InvestigationPage(_Frozen):
    items: list[InvestigationListItem] = []
    total: int = 0
    page: int = 1
    page_size: int = 20


class StageInfo(_Frozen):
    stage: str
    status: str
    summary: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class QuestionInfo(_Frozen):
    id: int
    qtype: str
    text: str
    about_type: str | None = None
    about_id: int | None = None
    status: str
    priority: float = 0.5
    answer_summary: str | None = None
    answer_finding_ids: list[int] = []
    spawned_dossier_id: int | None = None


class FindingEvidenceInfo(_Frozen):
    document_id: int
    quote: str
    quote_start: int | None = None
    quote_end: int | None = None
    title: str | None = None
    url: str | None = None
    source_name: str | None = None
    credibility_tier: int | None = None


class FindingInfo(_Frozen):
    id: int
    kind: str
    text: str
    speculation: bool = False
    confidence: float | None = None
    question_id: int | None = None
    edge_id: int | None = None
    payload: dict[str, Any] = {}
    evidence: list[FindingEvidenceInfo] = []


class InvestigationDetail(_Frozen):
    id: int
    status: str
    input_text: str
    input_type: str | None = None
    title: str | None = None
    parent_question_id: int | None = None
    budget_usd: float | None = None
    error: str | None = None
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    stages: list[StageInfo] = []
    sections: dict[str, Any] = {}
    questions: list[QuestionInfo] = []
    findings: list[FindingInfo] = []
    counts: InvestigationCounts = InvestigationCounts()
    cost_usd: float = 0.0
    last_seq: int = 0
    visibility: str = "shared"
    owner_id: int | None = None
    owner_name: str | None = None
