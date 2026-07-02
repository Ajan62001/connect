"""Live eval runner — real Anthropic API.

    python -m evals               # all four dimensions
    python -m evals stance verdict

Gated on a configured Anthropic key (Settings.anthropic_api_key). Without one
it prints how to enable and exits 0, so a keyless CI invocation is a clean
no-op rather than a failure.
"""

from __future__ import annotations

import asyncio
import sys

from connect.llm.anthropic_provider import AnthropicProvider
from connect.llm.tiers import tier_models
from connect.orchestration.config import Settings

from evals.harness import EVALUATORS, run_all


async def _main(which: list[str]) -> int:
    settings = Settings()
    if not settings.anthropic_api_key:
        print("evals: no Anthropic key configured "
              "(set ANTHROPIC_API_KEY or CONNECT_ANTHROPIC_API_KEY) — "
              "skipping live evals.")
        return 0
    provider = AnthropicProvider(settings.anthropic_api_key,
                                 tier_models=tier_models(settings))
    try:
        reports = await run_all(provider, which or None)
    finally:
        await provider.aclose()
    print("\n".join(r.render() for r in reports))
    return 0


def main() -> int:
    which = [a for a in sys.argv[1:] if not a.startswith("-")]
    bad = [w for w in which if w not in EVALUATORS]
    if bad:
        print(f"unknown eval(s): {', '.join(bad)} "
              f"(choose from {', '.join(EVALUATORS)})")
        return 2
    return asyncio.run(_main(which))


if __name__ == "__main__":
    raise SystemExit(main())
