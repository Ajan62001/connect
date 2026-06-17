"""Auto-theming — curated card palettes, topic→palette rules, and the resolver
that turns the effective PostSettings + a post's topic / AI suggestion into the
PostSettings the card is actually rendered with.

Two controllable signals, applied ONLY when ``settings.auto_theme`` is on:
1. topic rules — the user's ``topic_palettes`` override, else a sensible
   DEFAULT_TOPIC_PALETTE keyed by the document's T1 topic (deterministic);
2. the model's ``suggested_palette`` (a name from PALETTES) — the fallback when
   no topic rule matches. Topic rules win over the AI so user/topic intent is
   never overridden. A palette only swaps colors + template; the user's logo,
   sign-off, headline size and alignment always carry through.
"""

from __future__ import annotations

from connect.domain.models import PostSettings

# name -> the four colors + a layout template. Hand-tuned to read well at a
# glance and stay on-brand across the topic spread.
PALETTES: dict[str, dict[str, str]] = {
    "midnight": {"card_bg": "#0f172a", "card_text": "#f1f5f9",
                 "card_muted": "#94a3b8", "card_accent": "#38bdf8",
                 "card_template": "classic"},
    "paper": {"card_bg": "#ffffff", "card_text": "#111827",
              "card_muted": "#6b7280", "card_accent": "#2563eb",
              "card_template": "minimal"},
    "crimson": {"card_bg": "#1a0a0e", "card_text": "#fee2e2",
                "card_muted": "#fca5a5", "card_accent": "#ef4444",
                "card_template": "bold"},
    "forest": {"card_bg": "#07140f", "card_text": "#dcfce7",
               "card_muted": "#86efac", "card_accent": "#22c55e",
               "card_template": "classic"},
    "royal": {"card_bg": "#14122e", "card_text": "#ede9fe",
              "card_muted": "#c4b5fd", "card_accent": "#8b5cf6",
              "card_template": "bold"},
    "gold": {"card_bg": "#1c1606", "card_text": "#fef9c3",
             "card_muted": "#fde68a", "card_accent": "#f59e0b",
             "card_template": "classic"},
    "slate": {"card_bg": "#f8fafc", "card_text": "#0f172a",
              "card_muted": "#475569", "card_accent": "#0ea5e9",
              "card_template": "minimal"},
    "ink": {"card_bg": "#0a0a0a", "card_text": "#fafafa",
            "card_muted": "#a3a3a3", "card_accent": "#e5e5e5",
            "card_template": "minimal"},
}

PALETTE_NAMES = tuple(PALETTES)

# the out-of-the-box topic→palette map (T1 topic vocabulary). Users override per
# topic via PostSettings.topic_palettes; anything unmapped falls to the AI pick.
DEFAULT_TOPIC_PALETTE: dict[str, str] = {
    "monetary-policy": "gold", "banking": "gold", "markets": "gold",
    "budget": "gold", "taxation": "gold", "securities-regulation": "gold",
    "trade": "gold",
    "elections": "crimson", "misinformation": "crimson",
    "parliament": "royal", "judiciary": "royal", "federalism": "royal",
    "foreign-policy": "royal",
    "agriculture": "forest", "environment": "forest", "energy": "forest",
    "welfare-schemes": "forest", "health": "forest",
    "defence": "ink", "data-privacy": "ink",
    "infrastructure": "slate", "telecom": "slate", "labour": "slate",
    "education": "paper",
}


def palette_for(settings: PostSettings, *, topic: str | None = None,
                suggested: str | None = None) -> str | None:
    """The palette NAME that should theme this post (None to keep the user's
    fixed look). topic_palettes > DEFAULT_TOPIC_PALETTE > AI suggestion."""
    if not settings.auto_theme:
        return None
    if topic:
        name = (settings.topic_palettes or {}).get(topic) \
            or DEFAULT_TOPIC_PALETTE.get(topic)
        if name in PALETTES:
            return name
    return suggested if suggested in PALETTES else None


def apply_palette(settings: PostSettings, name: str | None) -> PostSettings:
    """Overlay a named palette's colors + template onto ``settings`` (logo /
    sign-off / headline size+align are kept). No-op for an unknown name."""
    pal = PALETTES.get(name or "")
    return settings.model_copy(update=pal) if pal else settings


def resolve_post_theme(settings: PostSettings, *, topic: str | None = None,
                       suggested: str | None = None) -> tuple[PostSettings,
                                                              str | None]:
    """The (rendered settings, chosen palette name). Identity when auto_theme is
    off or nothing matches."""
    name = palette_for(settings, topic=topic, suggested=suggested)
    return apply_palette(settings, name), name


def palette_guidance() -> str:
    """Prompt fragment listing the palettes for the model's suggested_palette."""
    return (
        "If a palette fits the news mood, set suggested_palette to ONE of: "
        f"{', '.join(PALETTE_NAMES)}. Rough intent — gold: finance/markets/"
        "budget; crimson: crises/elections/misinformation; royal: law/"
        "parliament/foreign policy; forest: agriculture/environment/welfare; "
        "ink: defence/privacy; slate or paper: neutral; midnight: default. "
        "Leave it null if unsure.")
