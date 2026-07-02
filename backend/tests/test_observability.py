"""LLM tracing seam (connect.llm.observability).

Hermetic — a fake Langfuse client stands in for the SDK, so these run with
no network and whether or not the real package is installed. Covers the
no-op path (no keys), the recorded path (model/input/metadata/usage), the
ambient-user attribution, and that errors propagate through the span.
"""

from __future__ import annotations

import contextlib

import pytest

from connect.llm.observability import (
    LLMTracer,
    build_tracer,
    null_tracer,
    usage_details,
)
from connect.llm.provider import Usage
from connect.llm.spend import CURRENT_USER_ID
from connect.llm.tiers import ModelTier


class FakeGeneration:
    def __init__(self) -> None:
        self.updates: list[dict] = []

    def update(self, **kwargs) -> None:
        self.updates.append(kwargs)


class FakeEndableGeneration(FakeGeneration):
    def __init__(self) -> None:
        super().__init__()
        self.ended = False

    def end(self) -> None:
        self.ended = True


class FakeLangfuse:
    """Records observations opened on it; mimics the v4 context-manager API."""

    def __init__(self) -> None:
        self.opened: list[dict] = []
        self.gens: list[FakeGeneration] = []
        self.started: list[dict] = []
        self.started_gens: list[FakeEndableGeneration] = []
        self.flushed = 0

    @contextlib.contextmanager
    def start_as_current_observation(self, **kwargs):
        self.opened.append(kwargs)
        gen = FakeGeneration()
        self.gens.append(gen)
        yield gen

    def start_observation(self, **kwargs):
        self.started.append(kwargs)
        gen = FakeEndableGeneration()
        self.started_gens.append(gen)
        return gen

    def flush(self) -> None:
        self.flushed += 1


def test_build_tracer_without_keys_is_noop():
    class S:
        langfuse_public_key = None
        langfuse_secret_key = None
        langfuse_host = "https://cloud.langfuse.com"

    tracer = build_tracer(S())
    assert tracer.enabled is False


def test_null_tracer_generation_is_a_silent_noop():
    tracer = null_tracer()
    with tracer.generation(name="complete", model="m",
                           messages=[{"role": "user", "content": "hi"}],
                           system=None, tier=ModelTier.FAST) as gen:
        gen.update(output="x", usage_details=usage_details(Usage()))
    tracer.flush()  # no client -> no error


def test_usage_details_maps_all_token_buckets():
    u = Usage(input_tokens=10, output_tokens=5, cache_read_tokens=2,
              cache_creation_tokens=1)
    assert usage_details(u) == {
        "input": 10, "output": 5,
        "cache_read_input_tokens": 2, "cache_creation_input_tokens": 1}


def test_generation_records_model_input_and_usage():
    client = FakeLangfuse()
    tracer = LLMTracer(client)
    with tracer.generation(name="complete_structured", model="claude-haiku-4-5",
                           messages=[{"role": "user", "content": "hi"}],
                           system="sys", tier=ModelTier.FAST,
                           extra={"schema": "Foo"}) as gen:
        gen.update(output={"a": 1},
                   usage_details=usage_details(Usage(input_tokens=3,
                                                     output_tokens=2)))
    assert client.opened[0]["name"] == "complete_structured"
    assert client.opened[0]["as_type"] == "generation"
    assert client.opened[0]["model"] == "claude-haiku-4-5"
    assert client.opened[0]["input"] == {
        "system": "sys", "messages": [{"role": "user", "content": "hi"}]}
    meta = client.opened[0]["metadata"]
    assert meta["tier"] == "fast"
    assert meta["schema"] == "Foo"
    assert client.gens[0].updates[0]["usage_details"]["input"] == 3


def test_generation_metadata_carries_ambient_user():
    client = FakeLangfuse()
    tracer = LLMTracer(client)
    token = CURRENT_USER_ID.set(42)
    try:
        with tracer.generation(name="complete", model="m", messages=[],
                               system=None, tier=ModelTier.DEEP):
            pass
    finally:
        CURRENT_USER_ID.reset(token)
    assert client.opened[0]["metadata"]["user_id"] == 42


def test_exception_inside_generation_propagates():
    client = FakeLangfuse()
    tracer = LLMTracer(client)
    with pytest.raises(ValueError):
        with tracer.generation(name="complete", model="m", messages=[],
                               system=None, tier=ModelTier.FAST):
            raise ValueError("boom")
    # span was still opened (and would be marked errored by the real SDK)
    assert client.opened


def test_open_failure_falls_back_to_noop():
    class Broken:
        def start_as_current_observation(self, **_kwargs):
            raise RuntimeError("sdk exploded")

    tracer = LLMTracer(Broken())
    # must not raise — tracing never breaks the LLM call
    with tracer.generation(name="complete", model="m", messages=[],
                           system=None, tier=ModelTier.FAST) as gen:
        gen.update(output="x")


# --- AnthropicProvider drives the tracer ---------------------------------------


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Resp:
    def __init__(self, content, model="claude-haiku-4-5"):
        self.content = content
        self.model = model
        self.stop_reason = "end_turn"

        class _U:
            input_tokens = 7
            output_tokens = 3
            cache_read_input_tokens = 0
            cache_creation_input_tokens = 0
        self.usage = _U()


class _FakeMessages:
    async def create(self, **_kwargs):
        return _Resp([_Block(type="text", text="hello")])


class _FakeClient:
    def __init__(self):
        self.messages = _FakeMessages()


def test_record_generation_for_completed_work():
    client = FakeLangfuse()
    tracer = LLMTracer(client)
    tracer.record_generation(
        name="enrich_t1_batch", model="claude-haiku-4-5",
        input={"system": "s", "messages": [{"role": "user", "content": "d"}]},
        output='{"x":1}', usage=Usage(input_tokens=20, output_tokens=8),
        tier=ModelTier.FAST,
        extra={"purpose": "enrich_t1", "batch_id": "b1", "document_id": 7})
    started = client.started[0]
    assert started["name"] == "enrich_t1_batch"
    assert started["as_type"] == "generation"
    assert started["level"] == "DEFAULT"
    assert started["metadata"]["batch_id"] == "b1"
    assert started["metadata"]["document_id"] == 7
    gen = client.started_gens[0]
    assert gen.updates[0]["usage_details"]["input"] == 20
    assert gen.ended is True


def test_record_generation_error_marks_level_error():
    client = FakeLangfuse()
    tracer = LLMTracer(client)
    tracer.record_generation(
        name="enrich_t1_batch", model="m", input={}, output=None,
        usage=Usage(), tier=ModelTier.FAST, is_error=True,
        status_message="errored: boom")
    assert client.started[0]["level"] == "ERROR"
    assert client.started[0]["status_message"] == "errored: boom"


def test_record_generation_noop_when_disabled():
    null_tracer().record_generation(name="x", model="m", input={},
                                    output=None, usage=Usage())  # no raise


@pytest.mark.asyncio
async def test_provider_complete_opens_a_generation():
    from connect.llm.anthropic_provider import AnthropicProvider

    client = FakeLangfuse()
    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider._client = _FakeClient()
    provider._tier_models = {ModelTier.FAST: "claude-haiku-4-5"}
    provider._tracer = LLMTracer(client)

    out = await provider.complete(system="sys", messages=[
        {"role": "user", "content": "hi"}], tier=ModelTier.FAST,
        max_tokens=64)

    assert out.text == "hello"
    assert client.opened[0]["name"] == "complete"
    assert client.gens[0].updates[0]["output"] == "hello"
    assert client.gens[0].updates[0]["usage_details"]["input"] == 7
