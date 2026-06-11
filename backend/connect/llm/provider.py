"""LLMProvider ABC + the small value objects every caller codes against.

Callers (enrichment, later the dossier pipeline) never import the anthropic
SDK — they get text or a validated Pydantic instance plus token usage, and
the provider hides how structured output is obtained. ``tool_loop`` is a
declared-but-stubbed capability: the agentic analysis phase lands later.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Generic, Sequence, TypeVar

from pydantic import BaseModel, ConfigDict

from connect.llm.tiers import ModelTier

TModel = TypeVar("TModel", bound=BaseModel)


class LLMError(Exception):
    """Provider-level failure (refusal, truncation, schema mismatch after
    retry, transport error). Callers treat it as 'this document failed'."""


class Usage(BaseModel):
    model_config = ConfigDict(frozen=True)

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


class Completion(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    model: str
    usage: Usage


class StructuredCompletion(BaseModel, Generic[TModel]):
    """A validated instance of the requested schema + what it cost."""
    model_config = ConfigDict(frozen=True)

    output: TModel
    model: str
    usage: Usage


class LLMProvider(ABC):
    @abstractmethod
    async def complete(self, *, system: str | None,
                       messages: Sequence[dict[str, Any]],
                       tier: ModelTier,
                       max_tokens: int) -> Completion:
        """One free-text completion."""

    @abstractmethod
    async def complete_structured(self, *, system: str | None,
                                  messages: Sequence[dict[str, Any]],
                                  schema: type[TModel],
                                  tier: ModelTier,
                                  max_tokens: int,
                                  ) -> StructuredCompletion[TModel]:
        """One completion constrained to ``schema``; raises LLMError when a
        valid instance cannot be obtained."""

    async def tool_loop(self, *args: Any, **kwargs: Any) -> Any:
        """Agentic tool loop — arrives with the analysis (dossier) phase."""
        raise NotImplementedError("tool_loop lands in a later phase")

    def model_for(self, tier: ModelTier) -> str:
        """The model id this provider routes the tier to (for cost
        projection before a call is made)."""
        raise NotImplementedError

    async def aclose(self) -> None:  # pragma: no cover - trivial default
        """Release transport resources (default: nothing to do)."""
        return None
