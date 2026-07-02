"""Langfuse tracing for the LLM layer (optional, fully degradable).

When CONNECT_LANGFUSE_PUBLIC_KEY + CONNECT_LANGFUSE_SECRET_KEY are set, every
Anthropic call made through ``AnthropicProvider`` is recorded as a Langfuse
*generation*: model, tier, the system/messages input, the produced output,
token usage, latency, and any error. Unset (or langfuse not installed) =>
``build_tracer`` returns a no-op tracer and the hot path is untouched — the
same "no key, clean degrade" rule the rest of the container follows.

Built against the langfuse>=3 SDK (OTEL-based): one client per process,
generations opened via the ``start_as_current_generation`` context manager,
spans flushed on shutdown. The provider stays SDK-agnostic — it only ever
talks to ``LLMTracer``.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, Iterator, Sequence

from connect.llm.provider import Usage
from connect.llm.spend import CURRENT_USER_ID
from connect.llm.tiers import ModelTier

log = logging.getLogger(__name__)


def usage_details(u: Usage) -> dict[str, int]:
    """Token counts in Langfuse's ``usage_details`` shape (cost is derived by
    Langfuse from its own model catalog; we report the units we have)."""
    return {
        "input": u.input_tokens,
        "output": u.output_tokens,
        "cache_read_input_tokens": u.cache_read_tokens,
        "cache_creation_input_tokens": u.cache_creation_tokens,
    }


class _NullGeneration:
    """The disabled-tracer handle: every update is a cheap no-op."""

    def update(self, **_kwargs: Any) -> None:
        pass


class LLMTracer:
    """A thin seam over a Langfuse client (or nothing).

    The provider opens one generation per Anthropic call with
    ``with tracer.generation(...) as gen:`` and reports the result via
    ``gen.update(output=..., usage_details=...)``. When tracing is disabled
    the context manager yields a ``_NullGeneration`` and does no work, so
    instrumented call sites read identically whether or not Langfuse is on.
    """

    def __init__(self, client: Any | None = None):
        self._client = client

    @property
    def enabled(self) -> bool:
        return self._client is not None

    @contextlib.contextmanager
    def generation(self, *, name: str, model: str,
                   messages: Sequence[dict[str, Any]],
                   system: str | None,
                   tier: ModelTier,
                   extra: dict[str, Any] | None = None,
                   ) -> Iterator[Any]:
        if self._client is None:
            yield _NullGeneration()
            return
        metadata: dict[str, Any] = {"tier": tier.value}
        uid = CURRENT_USER_ID.get()
        if uid is not None:
            # carried in metadata (version-robust + filterable in Langfuse)
            metadata["user_id"] = uid
        if extra:
            metadata.update(extra)
        try:
            cm = self._client.start_as_current_observation(
                name=name, as_type="generation", model=model,
                input={"system": system, "messages": list(messages)},
                metadata=metadata)
        except Exception:  # pragma: no cover - never let tracing break a call
            log.exception("langfuse: failed to open generation %r", name)
            yield _NullGeneration()
            return
        with cm as gen:
            yield gen

    def record_generation(self, *, name: str, model: str,
                          input: Any, output: Any,
                          usage: Usage,
                          tier: ModelTier | None = None,
                          extra: dict[str, Any] | None = None,
                          is_error: bool = False,
                          status_message: str | None = None) -> None:
        """Record an ALREADY-COMPLETED generation (no active span around live
        code) — for the Message Batches path, where the LLM work ran remotely
        and we replay each result after the batch ends. A no-op when disabled.
        """
        if self._client is None:
            return
        metadata: dict[str, Any] = {}
        if tier is not None:
            metadata["tier"] = tier.value
        uid = CURRENT_USER_ID.get()
        if uid is not None:
            metadata["user_id"] = uid
        if extra:
            metadata.update(extra)
        try:
            gen = self._client.start_observation(
                name=name, as_type="generation", model=model,
                input=input, metadata=metadata,
                level="ERROR" if is_error else "DEFAULT",
                status_message=status_message)
            gen.update(output=output, usage_details=usage_details(usage))
            gen.end()
        except Exception:  # pragma: no cover - tracing never breaks ingestion
            log.exception("langfuse: failed to record generation %r", name)

    def score(self, *, name: str, value: float, comment: str | None = None,
              metadata: dict[str, Any] | None = None) -> None:
        """Emit a numeric integrity score (S5) — grounding rate, entailment-
        failure rate, verdict-accuracy, etc. Best-effort and fully no-op when
        disabled; a tracing failure must never break the analysis/publish path.
        """
        if self._client is None:
            return
        try:
            self._client.create_score(name=name, value=value, comment=comment,
                                      metadata=metadata or {})
        except Exception:  # pragma: no cover - tracing never breaks the caller
            log.exception("langfuse: failed to record score %r", name)

    def flush(self) -> None:
        """Force pending spans out (call on shutdown — exports are batched)."""
        if self._client is None:
            return
        try:
            self._client.flush()
        except Exception:  # pragma: no cover
            log.exception("langfuse: flush failed")


_NULL_TRACER = LLMTracer(None)


def null_tracer() -> LLMTracer:
    """The shared disabled tracer (provider default when none is injected)."""
    return _NULL_TRACER


def build_tracer(settings: Any) -> LLMTracer:
    """Construct a tracer from Settings; a no-op tracer when Langfuse is
    unconfigured or the SDK is not installed."""
    public = getattr(settings, "langfuse_public_key", None)
    secret = getattr(settings, "langfuse_secret_key", None)
    if not (public and secret):
        return _NULL_TRACER
    try:
        from langfuse import Langfuse
    except ImportError:
        log.warning("langfuse keys are set but the package is not installed "
                    "(`pip install langfuse`); LLM tracing disabled")
        return _NULL_TRACER
    try:
        client = Langfuse(
            public_key=public, secret_key=secret,
            host=getattr(settings, "langfuse_host", None)
            or "https://cloud.langfuse.com")
    except Exception:  # pragma: no cover - bad config must not break startup
        log.exception("langfuse: client init failed; LLM tracing disabled")
        return _NULL_TRACER
    log.info("langfuse: LLM tracing enabled (host=%s)",
             getattr(settings, "langfuse_host", None)
             or "https://cloud.langfuse.com")
    return LLMTracer(client)
