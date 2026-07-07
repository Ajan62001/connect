"""Script-type presets — the shape/pacing/hook guidance a campaign is written to.

Built-ins live here as code constants (slugs); custom presets are ``script_preset``
rows exposed as ids ``custom:<n>``. A campaign's pick is one string
(``ContentOptions.script_type``): a builtin slug or ``custom:<id>`` — the same
namespacing convention as voice ids (``vb:<profile>``).

A preset is framing-only: its ``guidance`` shapes the FORM of the script (hook
style, structure, pacing) and never relaxes the closed-menu citation rules. It
can also carry render defaults (scene_count / caption_style / visual_style) that
fold into a campaign's ContentOptions only where the caller left them unset.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ScriptPreset(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str                                   # builtin slug or 'custom:<n>'
    name: str
    guidance: str = Field(default="", max_length=800)
    formats: list[str] = Field(default_factory=list)   # empty => any format
    scene_count: int | None = None
    caption_style: str | None = None
    visual_style: str | None = None
    builtin: bool = False


class ScriptPresetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    guidance: str = Field(default="", max_length=800)
    formats: list[str] = Field(default_factory=list)
    scene_count: int | None = Field(default=None, ge=3, le=8)
    caption_style: str | None = None
    visual_style: str | None = None


class ScriptPresetUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=80)
    guidance: str | None = Field(default=None, max_length=800)
    formats: list[str] | None = None
    scene_count: int | None = Field(default=None, ge=3, le=8)
    caption_style: str | None = None
    visual_style: str | None = None


BUILTIN_PRESETS: dict[str, ScriptPreset] = {
    p.id: p for p in (
        ScriptPreset(
            id="breaking_brief", name="Breaking brief", builtin=True,
            scene_count=3, visual_style="poster",
            guidance=(
                "Newsflash energy. Open on the single most surprising fact —"
                " the number, reversal or stake — in spoken language, not a"
                " headline. Keep it tight (3 fast beats), present tense, and"
                " end on what happens next or who is affected.")),
        ScriptPreset(
            id="explainer", name="Explainer", builtin=True,
            scene_count=4,
            guidance=(
                "Teach one thing clearly. Hook with the question the viewer"
                " is already asking, then answer it step by step so someone"
                " with zero context follows. Calm, authoritative pacing; each"
                " scene advances the understanding, last scene lands the"
                " 'so what'.")),
        ScriptPreset(
            id="listicle", name="Listicle", builtin=True,
            scene_count=5,
            guidance=(
                "A numbered rundown. Hook promises the count ('3 things...'),"
                " each scene is one crisp item with its own concrete visual,"
                " momentum builds to the most important/surprising item last."
                " Punchy, scannable, no filler between items.")),
        ScriptPreset(
            id="story_style", name="Story", builtin=True,
            scene_count=4,
            guidance=(
                "Narrative arc. Hook drops us into a moment or a person, then"
                " build tension scene by scene ('but/so' turns), and land a"
                " payoff — the consequence or twist. Human, cinematic pacing;"
                " every scene except the last ends leaning forward.")),
        ScriptPreset(
            id="fact_check", name="Fact-check", builtin=True,
            scene_count=4,
            guidance=(
                "Claim vs reality. Hook states the viral claim plainly, then"
                " weigh it against the evidence beat by beat, and end with a"
                " clear verdict (true / false / misleading) and why. Measured,"
                " skeptical, never sensational — the discipline is the point.")),
    )
}


def get_builtin(slug: str) -> ScriptPreset | None:
    return BUILTIN_PRESETS.get(slug)


def preset_lines(preset: ScriptPreset | None) -> str:
    """The framing-only prompt line for a script preset (form/pacing ONLY —
    never relaxes the citation rules). Empty when there is no preset."""
    if preset is None or not (preset.guidance or "").strip():
        return ""
    return (f"SCRIPT TYPE — {preset.name}: {preset.guidance.strip()} This"
            " shapes the FORM (hook, structure, pacing) ONLY; every factual"
            " claim still cites [[E#]].\n")
