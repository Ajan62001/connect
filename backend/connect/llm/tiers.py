"""Model tiers — callers ask for FAST/BALANCED/DEEP, never for a model id.

The tier -> model map is config-routed (Settings.model_fast/_balanced/_deep,
env CONNECT_MODEL_FAST/_BALANCED/_DEEP) so a model swap is an env change,
not a code change. Pricing for the defaults lives in llm/spend.py.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Mapping

if TYPE_CHECKING:
    from connect.orchestration.config import Settings


class ModelTier(str, Enum):
    FAST = "fast"
    BALANCED = "balanced"
    DEEP = "deep"


DEFAULT_TIER_MODELS: Mapping[ModelTier, str] = {
    ModelTier.FAST: "claude-haiku-4-5",
    ModelTier.BALANCED: "claude-sonnet-4-6",
    ModelTier.DEEP: "claude-opus-4-8",
}


def tier_models(settings: "Settings") -> dict[ModelTier, str]:
    """The tier -> model map for this process (Settings overrides defaults)."""
    return {
        ModelTier.FAST: settings.model_fast,
        ModelTier.BALANCED: settings.model_balanced,
        ModelTier.DEEP: settings.model_deep,
    }
