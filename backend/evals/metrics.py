"""Classification metrics for the gold-labelled evals (stance, verdict).

Pure functions over (gold, predicted) label pairs — no LLM, no I/O — so the
offline suite tests them table-driven. Macro-averaged P/R/F1 plus a confusion
matrix; ``None`` predictions (a discarded judgment) count as wrong and surface
in the matrix under the "∅" column so silent drop-outs are visible.
"""

from __future__ import annotations

from dataclasses import dataclass

NONE_LABEL = "∅"   # a predicted None (e.g. a stance judgment discarded)


@dataclass(frozen=True)
class ClassScore:
    label: str
    support: int          # gold instances of this label
    precision: float
    recall: float
    f1: float


@dataclass(frozen=True)
class ClassificationReport:
    n: int
    accuracy: float
    macro_f1: float
    per_class: list[ClassScore]
    # confusion[gold][pred] = count
    confusion: dict[str, dict[str, int]]

    def as_dict(self) -> dict:
        return {
            "n": self.n,
            "accuracy": round(self.accuracy, 4),
            "macro_f1": round(self.macro_f1, 4),
            "per_class": [
                {"label": c.label, "support": c.support,
                 "precision": round(c.precision, 4),
                 "recall": round(c.recall, 4), "f1": round(c.f1, 4)}
                for c in self.per_class],
            "confusion": self.confusion,
        }


def _safe_div(a: float, b: float) -> float:
    return a / b if b else 0.0


def score_classification(pairs: list[tuple[str, str | None]],
                         labels: list[str]) -> ClassificationReport:
    """``pairs`` is (gold, predicted); predicted None becomes NONE_LABEL.
    ``labels`` is the closed gold label set (precision/recall computed over
    these; NONE_LABEL is a prediction-only column)."""
    n = len(pairs)
    norm = [(g, p if p is not None else NONE_LABEL) for g, p in pairs]
    correct = sum(1 for g, p in norm if g == p)

    pred_labels = labels + [NONE_LABEL]
    confusion = {g: {p: 0 for p in pred_labels} for g in labels}
    for g, p in norm:
        confusion.setdefault(g, {pl: 0 for pl in pred_labels})
        confusion[g].setdefault(p, 0)
        confusion[g][p] += 1

    per_class: list[ClassScore] = []
    f1s: list[float] = []
    for label in labels:
        tp = sum(1 for g, p in norm if g == label and p == label)
        fp = sum(1 for g, p in norm if g != label and p == label)
        fn = sum(1 for g, p in norm if g == label and p != label)
        support = sum(1 for g, _ in norm if g == label)
        precision = _safe_div(tp, tp + fp)
        recall = _safe_div(tp, tp + fn)
        f1 = _safe_div(2 * precision * recall, precision + recall)
        per_class.append(ClassScore(label, support, precision, recall, f1))
        if support:                      # macro-F1 over represented classes
            f1s.append(f1)

    return ClassificationReport(
        n=n, accuracy=_safe_div(correct, n),
        macro_f1=_safe_div(sum(f1s), len(f1s)),
        per_class=per_class, confusion=confusion)


def render_classification(title: str, rep: ClassificationReport) -> str:
    lines = [f"== {title} ==",
             f"  n={rep.n}  accuracy={rep.accuracy:.3f}  "
             f"macro-F1={rep.macro_f1:.3f}",
             "  per-class  (support / P / R / F1):"]
    for c in rep.per_class:
        lines.append(f"    {c.label:<11} {c.support:>3}  "
                     f"{c.precision:.2f} / {c.recall:.2f} / {c.f1:.2f}")
    return "\n".join(lines)
