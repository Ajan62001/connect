"""Grounding mechanics — THE trust core, shared by enrichment and analysis.

Two mechanical defenses (engine design §4):

1. Verbatim-span verification: every quoted_span an LLM returns must be a
   literal substring of the stored document text (whitespace-normalized).
   This is the T1 span-verifier, extracted here so the enrichment persist
   path and the verification stage run the IDENTICAL check.

2. Closed evidence menus: analysis prompts see retrieved snippets tagged
   with short opaque ids ("E1"); any id the model cites that is not in the
   per-prompt menu is rejected in code — the model cannot invent a citation
   that passes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_WS_RE = re.compile(r"\s+")


def norm_ws(text: str) -> str:
    """Whitespace-normalize (the verbatim-span comparison form)."""
    return _WS_RE.sub(" ", text).strip()


def span_is_verbatim(span: str, content_text: str) -> bool:
    """Is ``span`` a verbatim substring of the document text, modulo
    whitespace? Empty spans never ground anything."""
    span_n = norm_ws(span)
    return bool(span_n) and span_n in norm_ws(content_text)


def find_span(needle: str, haystack: str) -> tuple[int | None, int | None]:
    """Char offsets of the first case-insensitive occurrence (None if the
    surface only matches after whitespace normalization)."""
    idx = haystack.lower().find(needle.lower())
    if idx < 0:
        return None, None
    return idx, idx + len(needle)


# -- closed evidence menus ---------------------------------------------------------


@dataclass(frozen=True)
class MenuEntry:
    """One snippet the model is allowed to cite."""
    menu_id: str               # "E1" — the only handle the model ever sees
    document_id: int
    quote: str
    source_name: str | None = None
    credibility_tier: int | None = None
    stance: str | None = None


@dataclass
class EvidenceMenu:
    """Per-prompt id -> snippet menu; the validator side of defense #2."""
    entries: list[MenuEntry] = field(default_factory=list)

    @property
    def ids(self) -> set[str]:
        return {e.menu_id for e in self.entries}

    def add(self, *, document_id: int, quote: str,
            source_name: str | None = None,
            credibility_tier: int | None = None,
            stance: str | None = None) -> MenuEntry:
        entry = MenuEntry(
            menu_id=f"E{len(self.entries) + 1}", document_id=document_id,
            quote=quote, source_name=source_name,
            credibility_tier=credibility_tier, stance=stance)
        self.entries.append(entry)
        return entry

    def invalid_ids(self, cited: list[str]) -> list[str]:
        """Cited ids that are NOT on the menu (order-preserving)."""
        allowed = self.ids
        return [c for c in cited if c not in allowed]

    def render(self) -> str:
        """The menu exactly as a prompt shows it."""
        lines = []
        for e in self.entries:
            src = e.source_name or "unknown source"
            tier = f"tier {e.credibility_tier}" if e.credibility_tier else \
                "tier unknown"
            stance = f", {e.stance}" if e.stance else ""
            lines.append(f"[{e.menu_id}] ({src}, {tier}{stance}) "
                         f"\"{e.quote}\"")
        return "\n".join(lines)
