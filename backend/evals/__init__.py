"""Analysis-pipeline evals.

Measures the QUALITY of the LLM-driven decisions in the analysis pipeline —
claim decomposition, stance classification, end-to-end verdicts, and reasoning
faithfulness — by driving the REAL production stage functions (no
reimplementation) through a lightweight EvalContext.

Two run modes:
  - live  (real Anthropic API): gold-labelled accuracy / F1 + LLM-judge scores.
           Gated on ANTHROPIC_API_KEY; ``python -m evals``.
  - offline (MockProvider): deterministic regression of the harness + the pure
           weighting/verdict math. Runs in CI via tests/test_evals.py.

The stage functions (normalize.run, verify.stance_one, verify.write_reasoning)
depend only on ``ctx.call_structured``, so EvalContext can run them verbatim
against any LLMProvider — real or mock.
"""
