"""Stage 0 — input normalization + claim decomposition (BALANCED, 1 call).

One structured call turns free input text into <= 6 atomic, context-free
claims with HONEST checkable flags (normative/predictive claims are never
forced into 'factual'), plus seed queries for the verify stage.
"""

from __future__ import annotations

from connect.analysis.context import (
    EST_NORMALIZE_IN,
    EST_NORMALIZE_OUT,
    AnalysisContext,
)
from connect.analysis.prompts import NORMALIZE_SYSTEM
from connect.analysis.schema import NormalizedInput
from connect.llm.tiers import ModelTier

MAX_INPUT_WORDS = 2000   # the model sees at most this much of the input
MAX_OUTPUT_TOKENS = 1500


def build_user_message(input_text: str,
                       max_words: int = MAX_INPUT_WORDS) -> str:
    words = input_text.split()
    body = " ".join(words[:max_words])
    truncated = " [truncated]" if len(words) > max_words else ""
    return f"INPUT TEXT:\n{body}{truncated}"


async def run(ctx: AnalysisContext, input_text: str) -> NormalizedInput:
    completion = await ctx.call_structured(
        system=NORMALIZE_SYSTEM,
        user=build_user_message(input_text),
        schema=NormalizedInput,
        tier=ModelTier.BALANCED,
        max_tokens=MAX_OUTPUT_TOKENS,
        est_in=EST_NORMALIZE_IN, est_out=EST_NORMALIZE_OUT)
    return completion.output


def summarize(normalized: NormalizedInput) -> str:
    checkable = sum(1 for c in normalized.claims if c.checkable)
    return (f"{len(normalized.claims)} claims ({checkable} checkable),"
            f" kind {normalized.input_kind}")
