"""Everything the SDK emits conforms to wire schema v1.0, whatever the application passes in.

Each batch is checked against the normative JSON Schema (ECMA pattern semantics) AND the Phase 1
collector model (strict bytes path), and must be accepted without a single rejected event."""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import aobs
from aobs.export import InMemoryExporter
from sdk_support import check_bodies, flush, traces

anything = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=True, allow_infinity=True) | st.text(),
    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=8), inner, max_size=4),
    max_leaves=12,
)
weird_text = st.text(min_size=0, max_size=300) | st.sampled_from(
    ["", " ", "Planner Step", "UPPER", "a" * 400, "x\ny", "é", "\ud800", "1abc", "..", "a..b", "kind.", "-x"]
)


@st.composite
def scenario(draw: st.DrawFn) -> dict[str, Any]:
    return {
        "run_name": draw(weird_text),
        "kind": draw(weird_text),
        "span_name": draw(weird_text),
        "provider": draw(weird_text),
        "model": draw(weird_text | st.none()),
        "attributes": draw(st.dictionaries(weird_text, anything, max_size=6)),
        "usage": draw(
            st.dictionaries(
                st.sampled_from(["input_tokens", "output_tokens", "total_tokens", "reasoning_tokens"]),
                st.integers(-5, 2**60) | st.floats() | st.booleans() | st.none(),
                max_size=4,
            )
        ),
        "cost": draw(st.none() | st.text(max_size=20) | st.floats() | st.integers(-10, 10**20)),
        "currency": draw(st.sampled_from(["USD", "eur", "dollars", ""])),
        "content": draw(anything),
        "error_message": draw(weird_text),
        "link": draw(st.tuples(weird_text, weird_text, weird_text)),
        "event_name": draw(weird_text),
        "session": draw(weird_text),
        "retry": draw(st.integers(-3, 20000) | st.booleans()),
        "finish_reasons": draw(st.lists(weird_text, max_size=20)),
        "temperature": draw(st.floats() | st.integers() | st.none()),
    }


def _exercise(s: dict[str, Any]) -> None:
    with aobs.run(s["run_name"], agent=s["run_name"], session_id=s["session"], attributes=s["attributes"]) as run:
        with aobs.span(s["span_name"], kind=s["kind"], attributes=s["attributes"]) as sp:
            sp.set_input(s["content"])
            sp.set_output(s["content"], "messages")
            sp.add_link(*s["link"])
            sp.set_retry(s["retry"])
            sp.event(s["event_name"], s["attributes"])
        with aobs.llm_span(provider=s["provider"], request_model=s["model"]) as llm:
            llm.record_usage(**s["usage"])
            llm.record_cost(s["cost"], s["currency"])
            llm.set_llm(
                request={"temperature": s["temperature"], "max_tokens": s["retry"]},
                response={"id": s["span_name"], "finish_reasons": s["finish_reasons"]},
            )
        with aobs.tool_span(s["span_name"], tool_type=s["kind"]) as tool:
            tool.fingerprint_input(s["content"])
            tool.set_error(s["error_message"] or "E", message=s["error_message"], origin=s["kind"])
        try:
            with aobs.span("raises"):
                raise ValueError(s["error_message"])
        except ValueError:
            pass
        run.set_attribute(s["span_name"], s["content"])


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(scenario(), st.sampled_from(["metadata_only", "redacted", "full"]))
def test_arbitrary_input_always_produces_valid_batches(
    make_sdk: Callable[..., InMemoryExporter], s: dict[str, Any], mode: str
) -> None:
    sdk = make_sdk(capture=mode, capture_stack_traces=True)
    _exercise(s)
    flush()
    accepted = check_bodies(sdk.bodies)
    assert len(accepted) == 6  # run, span, llm, tool, raises (5 span_end) + 1 span_event; all accepted
    (view,) = traces(sdk).values()
    assert view.status == "ok_with_errors" and not view.orphans
    aobs.shutdown()


def test_every_api_surface_emits_valid_events(make_sdk: Callable[..., InMemoryExporter]) -> None:
    sdk = make_sdk(capture="full", capture_stack_traces=True, service_version="git:abc")

    @aobs.tool(name="Lookup Tool", tool_type="HTTP API")
    def lookup(q: str, *, limit: int = 3) -> list[str]:
        return [q] * limit

    @aobs.agent(version="1.2.3")
    async def sub_agent(x: float) -> float:
        return x * 2

    import asyncio

    with aobs.run("Answer Ticket", agent="Triage Bot", session_id="sess-1", attributes={"app.tier": "gold"}) as run:
        lookup("q", limit=2)
        asyncio.run(sub_agent(math.pi))
        with aobs.llm_span(provider="Azure OpenAI", request_model="gpt-x", operation="Chat Completions") as llm:
            llm.record_usage(
                input_tokens=1,
                output_tokens=2,
                total_tokens=3,
                cache_read_input_tokens=0,
                cache_write_input_tokens=0,
                reasoning_tokens=0,
            )
            llm.record_cost("0.01")
            llm.set_llm(
                response_model="gpt-x-1",
                provider_request_id="r",
                request={"temperature": 0.5, "top_p": 1, "max_tokens": 10, "stream": False},
                response={"id": "x", "finish_reasons": ["stop"]},
                streaming={"time_to_first_token_ms": 1.5, "chunk_count": 3, "completed": True},
            )
            llm.set_content_unavailable("input")
        run.event("retry.scheduled", {"attempt": 2})
    flush()
    check_bodies(sdk.bodies)
    resource = sdk.batches[0]["resource"]
    assert resource["service_version"] == "git:abc" and resource["sdk"]["name"] == "aobs-python"
    llm_event = next(e for e in sdk.events() if e.get("kind") == "llm")
    assert llm_event["llm"]["provider"] == "azure_openai"
    assert llm_event["llm"]["operation"] == "chat_completions"
    assert next(e for e in sdk.events() if e.get("kind") == "tool")["tool"] == {
        "name": "Lookup Tool",
        "type": "http_api",
    }
