# Analysis evals

Measures the **quality of the LLM-driven decisions** in the analysis pipeline.
The pipeline's deterministic machinery (weighting, the verdict formula, the
verbatim-span and closed-menu guards) is already unit-tested in
`tests/test_analysis_engine.py`; these evals grade the parts that *vary* — the
model's judgment.

Four dimensions:

| Eval | What it grades | How |
|---|---|---|
| `stance` | claim × document → `supports`/`refutes`/`mixed`/`unrelated` | gold labels → accuracy, per-class P/R/F1, confusion matrix, + verbatim-span grounding rate |
| `verdict` | claim + fixed mini-corpus → `supported`/`refuted`/`mixed`/`unverified` | gold labels → accuracy / F1 (runs real stance + real weighting + real verdict math) |
| `normalize` | input text → atomic, correctly-typed, honestly-flagged claims | property bounds (deterministic) + an LLM judge (atomicity / kind / checkable-honesty) |
| `reasoning` | the written verdict-explanation | LLM judge: faithful to the fixed verdict, no outside knowledge, on-menu only |

The harness drives the **real production stage functions** (`normalize.run`,
`verify.stance_one`, `verify.write_reasoning`) — they each depend only on
`ctx.call_structured`, so `EvalContext` runs them verbatim against any provider.
There is no reimplementation of pipeline logic to drift out of sync.

## Run it

**Live (real Anthropic API)** — gold accuracy/F1 + judge scores. Gated on
`ANTHROPIC_API_KEY`/`CONNECT_ANTHROPIC_API_KEY`; a keyless run is a clean no-op.
The judge uses the DEEP tier (`claude-opus-4-8`); stance/verdict use FAST/
BALANCED as in production.

```bash
cd backend
python -m evals                 # all four
python -m evals stance verdict  # a subset
```

Evals are **ungoverned and unledgered by design** (`EvalContext` skips the
budget Governor and the spend ledger), so a run never charges a user budget or
pollutes the production spend dashboard — but it does spend real tokens.

**Offline (MockProvider, CI)** — deterministic regression of the harness +
metrics + weighting/verdict math, no network:

```bash
python -m pytest tests/test_evals.py -q
```

## Add a case

Append one JSON line to the relevant file in `evals/datasets/`. The schemas are
in `evals/cases.py`; loaders validate each line and point at the offending line
number on a bad row. Lines starting with `#` are comments.

- `stance.jsonl` — give a `doc_text` that contains a verbatim sentence bearing
  on the claim (so a correct judgment can ground).
- `verdict.jsonl` — set each doc's `credibility_tier` (1 best … 4 worst) and
  `domain`; design the corpus so correct stances yield the gold verdict under
  the weighting math (tier-weight × relevance, per-domain independence
  discount, `Σw < 0.5` ⇒ `unverified`).
- `normalize.jsonl` — `expect_uncheckable: true` asserts the model keeps a
  normative/predictive claim flagged `checkable: false` rather than forcing it
  factual.
- `reasoning.jsonl` — the `menu` is the closed evidence set; the model may cite
  only these.

## Reading the output

`accuracy` and `macro_f1` are the headline classification numbers; the
confusion matrix shows *where* it errs (e.g. `mixed` misread as `supports`).
`grounded_rate` on stance is the share of non-`unrelated` judgments whose quote
was verbatim. For the judge dimensions, watch `pass_rate`,
`mean_checkable_honesty` (normalize), and `outside_knowledge_rate` (reasoning —
should stay 0).
