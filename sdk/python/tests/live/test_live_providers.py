"""Opt-in live smoke tests: one tiny real call per provider integration.

SKIPPED unless ``AOBS_LIVE_TESTS=1``, the provider's API key and a model name are all set in the
environment; the default ``pytest`` run and CI never call a provider. They cost real (small) money
on the caller's account. See docs/sdk.md, "Live smoke tests".

    AOBS_LIVE_TESTS=1 ANTHROPIC_API_KEY=... AOBS_LIVE_ANTHROPIC_MODEL=<model> \\
        uv run pytest sdk/python/tests/live -v

There is deliberately no default model: whoever runs them chooses (and pays for) it.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

import pytest

import aobs
from aobs.export import InMemoryExporter
from sdk_support import check_bodies, end_by_name, flush

PROMPT = [{"role": "user", "content": "Reply with the single word: ok"}]


def live_model(key_env: str, model_env: str) -> str:
    """The model to call, or skip with the reason the test cannot run."""
    if os.environ.get("AOBS_LIVE_TESTS") != "1":
        pytest.skip("live provider test: opt in with AOBS_LIVE_TESTS=1 (see docs/sdk.md)")
    if not os.environ.get(key_env):
        pytest.skip(f"live provider test: {key_env} is not set")
    model = os.environ.get(model_env, "").strip()
    if not model:
        pytest.skip(f"live provider test: {model_env} is not set (no default model)")
    return model


def check_llm_span(sdk: InMemoryExporter, provider: str, model: str) -> dict[str, Any]:
    flush()
    check_bodies(sdk.bodies)
    event = end_by_name(sdk, "chat")
    assert event["status"] == "ok", event.get("error")
    llm = event["llm"]
    assert llm["provider"] == provider and llm["request_model"] == model
    # Providers resolve aliases to dated versions (e.g. "<alias>-2026..."); never a different model.
    assert llm["response_model"].startswith(model), llm["response_model"]
    assert llm.get("provider_request_id")
    usage = event["usage"]
    assert usage["source"] == "provider_reported"
    assert usage["input_tokens"] > 0 and usage["output_tokens"] > 0
    return event


def test_live_openai_compat(make_sdk: Callable[..., InMemoryExporter]) -> None:
    model = live_model("OPENAI_API_KEY", "AOBS_LIVE_OPENAI_MODEL")
    from openai import OpenAI

    from aobs.integrations import openai_compat

    sdk = make_sdk()
    client = openai_compat.wrap(OpenAI(max_retries=0), provider="openai")
    with aobs.run("live-smoke"):
        client.chat.completions.create(model=model, messages=PROMPT, max_completion_tokens=64)
    check_llm_span(sdk, "openai", model)


def test_live_anthropic(make_sdk: Callable[..., InMemoryExporter]) -> None:
    model = live_model("ANTHROPIC_API_KEY", "AOBS_LIVE_ANTHROPIC_MODEL")
    from anthropic import Anthropic

    from aobs.integrations import anthropic as aobs_anthropic

    sdk = make_sdk()
    client = aobs_anthropic.wrap(Anthropic(max_retries=0), provider="anthropic")
    with aobs.run("live-smoke"):
        client.messages.create(model=model, max_tokens=64, messages=PROMPT)
    check_llm_span(sdk, "anthropic", model)
