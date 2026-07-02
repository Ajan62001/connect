"""Eval-case schemas + JSONL dataset loaders.

Each gold case is one line of JSON in evals/datasets/<name>.jsonl. The schemas
are deliberately small so adding cases is just appending a line — see
evals/README.md.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from connect.analysis.schema import Stance, VerdictLabel

DATASETS_DIR = Path(__file__).resolve().parent / "datasets"


class _Case(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# -- stance classification (claim x document -> stance) -----------------------------


class StanceCase(_Case):
    """One claim judged against one document; ``gold`` is the expected stance.
    ``note`` is free-text provenance, ignored by the grader."""
    claim: str
    doc_title: str
    doc_text: str
    gold: Stance
    note: str = ""


# -- end-to-end verdict (claim + fixed mini-corpus -> verdict) ----------------------


class VerdictDoc(_Case):
    title: str
    text: str
    credibility_tier: int | None = None
    domain: str = ""          # publisher domain for the independence discount


class VerdictCase(_Case):
    claim: str
    docs: list[VerdictDoc]
    gold: VerdictLabel
    note: str = ""


# -- claim decomposition (input text -> normalized claims) --------------------------


class NormalizeCase(_Case):
    """Property bounds + a judge rubric for one decomposition. The numeric
    bounds are graded deterministically; atomicity/kind-honesty go to the
    LLM judge."""
    input_text: str
    expected_kind: str | None = None
    min_claims: int = 1
    max_claims: int = 6
    expect_uncheckable: bool = False   # at least one normative/predictive claim
    note: str = ""


# -- reasoning faithfulness (claim + fixed verdict + evidence menu -> prose) ---------


class MenuItem(_Case):
    quote: str
    source_name: str = "unknown source"
    credibility_tier: int | None = None
    stance: str = ""


class ReasoningCase(_Case):
    claim: str
    verdict: str
    menu: list[MenuItem]
    note: str = ""


# -- loaders ------------------------------------------------------------------------


def _load(name: str, model: type[_Case]) -> list:
    path = DATASETS_DIR / f"{name}.jsonl"
    out: list = []
    with path.open(encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw or raw.startswith("#"):
                continue
            try:
                out.append(model.model_validate_json(raw))
            except Exception as e:  # noqa: BLE001 — point at the offending line
                raise ValueError(
                    f"{name}.jsonl line {line_no}: {e}") from e
    return out


def load_stance() -> list[StanceCase]:
    return _load("stance", StanceCase)


def load_verdict() -> list[VerdictCase]:
    return _load("verdict", VerdictCase)


def load_normalize() -> list[NormalizeCase]:
    return _load("normalize", NormalizeCase)


def load_reasoning() -> list[ReasoningCase]:
    return _load("reasoning", ReasoningCase)
