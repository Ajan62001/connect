"""T1 light extraction — the frozen contract + the one provider call.

EnrichmentT1 is what the FAST-tier model must return (structured output).
Size limits are enforced by clamping validators rather than schema
constraints: the structured-outputs grammar can't express max-lengths, and
an over-long list from the model should degrade (truncate) rather than fail
the document. Vocabulary enforcement (topics, event_type, entity types) and
grounding (verbatim quoted_span) happen in persist.py.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator

from connect.knowledge.enrichment.prompts import T1_SYSTEM
from connect.llm.provider import LLMProvider, StructuredCompletion
from connect.llm.tiers import ModelTier

MAX_SUMMARY_CHARS = 240
MAX_TOPICS = 5
MAX_ENTITIES = 15
MAX_CLAIMS = 5
MAX_STATEMENTS = 6
MAX_STATEMENT_TOPICS = 3
MAX_POSITION_SUMMARY_CHARS = 140
MAX_CONTENT_WORDS = 1200   # title + first ~1200 words go to the model
T1_MAX_OUTPUT_TOKENS = 1536


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class T1Entity(_Frozen):
    surface: str
    type: str


class T1Claim(_Frozen):
    text: str
    check_worthiness: float
    quoted_span: str

    @field_validator("check_worthiness")
    @classmethod
    def _clamp_worthiness(cls, v: float) -> float:
        return min(1.0, max(0.0, v))


class T1Statement(_Frozen):
    """One attributed utterance: speaker_surface SAID quote (verbatim).
    Span verification + speaker-type filtering happen in persist.py."""
    speaker_surface: str
    quote: str
    topics: list[str] = []
    position_summary: str = ""

    @field_validator("topics")
    @classmethod
    def _clamp_topics(cls, v: list[str]) -> list[str]:
        return v[:MAX_STATEMENT_TOPICS]

    @field_validator("position_summary")
    @classmethod
    def _clamp_position_summary(cls, v: str) -> str:
        return v.strip()[:MAX_POSITION_SUMMARY_CHARS]


class EnrichmentT1(_Frozen):
    summary: str
    event_type: str
    topics: list[str] = []
    entities: list[T1Entity] = []
    claims: list[T1Claim] = []
    statements: list[T1Statement] = []

    @field_validator("summary")
    @classmethod
    def _clamp_summary(cls, v: str) -> str:
        v = v.strip()
        return v[:MAX_SUMMARY_CHARS]

    @field_validator("topics")
    @classmethod
    def _clamp_topics(cls, v: list[str]) -> list[str]:
        return v[:MAX_TOPICS]

    @field_validator("entities")
    @classmethod
    def _clamp_entities(cls, v: list[T1Entity]) -> list[T1Entity]:
        return v[:MAX_ENTITIES]

    @field_validator("claims")
    @classmethod
    def _clamp_claims(cls, v: list[T1Claim]) -> list[T1Claim]:
        return v[:MAX_CLAIMS]

    @field_validator("statements")
    @classmethod
    def _clamp_statements(cls, v: list[T1Statement]) -> list[T1Statement]:
        return v[:MAX_STATEMENTS]


def build_user_message(title: str | None, content_text: str,
                       max_words: int = MAX_CONTENT_WORDS) -> str:
    """Title + first ~max_words of body — the exact text the model sees.

    Used by both the sync path and the batch request builder so the two
    deliveries are prompt-identical.
    """
    words = content_text.split()
    body = " ".join(words[:max_words])
    truncated = " [truncated]" if len(words) > max_words else ""
    return f"TITLE: {title or '(untitled)'}\n\nTEXT:\n{body}{truncated}"


async def extract(provider: LLMProvider, *, title: str | None,
                  content_text: str) -> StructuredCompletion[EnrichmentT1]:
    """One FAST-tier structured extraction over a document."""
    return await provider.complete_structured(
        system=T1_SYSTEM,
        messages=[{"role": "user",
                   "content": build_user_message(title, content_text)}],
        schema=EnrichmentT1,
        tier=ModelTier.FAST,
        max_tokens=T1_MAX_OUTPUT_TOKENS,
    )
