"""The anthropic integration against the real ``anthropic`` client (2.1.0) with a mock HTTP transport.

No network, no credentials: the transport answers requests with JSON bodies and SSE streams built
from the shapes in the public Messages API documentation, so request building, response parsing,
streaming (including the ``messages.stream`` helper) and error classes are the library's own.
"""

from __future__ import annotations

import asyncio
import gc
import json
from collections.abc import Callable
from typing import Any

import anthropic
import httpx2
import pydantic
import pytest
from anthropic import Anthropic, AsyncAnthropic

import aobs
from aobs.export import InMemoryExporter
from aobs.integrations import anthropic as aobs_anthropic
from sdk_support import check_bodies, end_by_name, flush, only_trace

FULL_USAGE = {
    "input_tokens": 10,
    "cache_read_input_tokens": 64,
    "cache_creation_input_tokens": 8,
    "output_tokens": 20,
    "output_tokens_details": {"thinking_tokens": 5},
}
MESSAGES = [{"role": "user", "content": "Hello jane@example.com"}]
SYSTEM = "Be brief."
TOOLS = [
    {
        "name": "get_order_status",
        "description": "Look up an order.",
        "input_schema": {"type": "object", "properties": {"order_id": {"type": "string"}}},
    }
]


def message(
    usage: dict[str, Any] | None = FULL_USAGE,
    content: list[dict[str, Any]] | None = None,
    stop_reason: str = "end_turn",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "model": "claude-test-2026-01",
        "content": content if content is not None else [{"type": "text", "text": "Hi!"}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
    }
    if usage is not None:
        body["usage"] = usage
    return body


TOOL_USE_CONTENT = [
    {"type": "thinking", "thinking": "Need the order.", "signature": "sig"},
    {"type": "text", "text": "Checking."},
    {"type": "tool_use", "id": "toolu_01", "name": "get_order_status", "input": {"order_id": "1042"}},
]


def sse(events: list[dict[str, Any]]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def stream_events(start_usage: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    start = {**message(usage=None, content=[], stop_reason=""), "stop_reason": None, "id": "msg_s"}
    start["usage"] = start_usage or {
        "input_tokens": 9,
        "cache_read_input_tokens": 3,
        "cache_creation_input_tokens": 0,
        "output_tokens": 1,
    }
    return [
        {"type": "message_start", "message": start},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "ping"},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hel"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "lo"}},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "id": "toolu_s", "name": "get_order_status", "input": {}},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '{"order_id": '},
        },
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '"7"}'}},
        {"type": "content_block_stop", "index": 1},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use", "stop_sequence": None},
            "usage": {"output_tokens": 15},
        },
        {"type": "message_stop"},
    ]


STREAM_EVENTS = stream_events()  # 11 events reach the application: `ping` is consumed by the library
MID_STREAM_ERROR = [
    *STREAM_EVENTS[:4],
    {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
]
ERRORS = {
    "fail-400": (400, "invalid_request_error"),
    "fail-429": (429, "rate_limit_error"),
    "fail-500": (500, "api_error"),
    "fail-529": (529, "overloaded_error"),
}


def handler(request: httpx2.Request) -> httpx2.Response:
    body = json.loads(request.content)
    model = body["model"]
    headers = {"request-id": f"req_{model}"}
    if model in ERRORS:
        status, error_type = ERRORS[model]
        error = {"type": "error", "error": {"type": error_type, "message": "nope"}}
        return httpx2.Response(status, json=error, headers=headers)
    if body.get("stream"):
        events = MID_STREAM_ERROR if model == "mid-stream-error" else STREAM_EVENTS
        return httpx2.Response(200, content=sse(events), headers={**headers, "content-type": "text/event-stream"})
    if model == "no-usage":
        return httpx2.Response(200, json=message(usage=None), headers=headers)
    if model == "no-cache-fields":
        return httpx2.Response(200, json=message(usage={"input_tokens": 12, "output_tokens": 3}), headers=headers)
    if model == "tool-use":
        return httpx2.Response(200, json=message(content=TOOL_USE_CONTENT, stop_reason="tool_use"), headers=headers)
    if model == "json-out":
        return httpx2.Response(200, json=message(content=[{"type": "text", "text": '{"answer": 42}'}]), headers=headers)
    return httpx2.Response(200, json=message(), headers=headers)


def sync_client(transport: Any = None) -> Any:
    client = Anthropic(
        api_key="sk-ant-test-not-real-0000000000",
        base_url="http://mock.invalid",
        max_retries=0,
        http_client=httpx2.Client(transport=transport or httpx2.MockTransport(handler)),
    )
    return aobs_anthropic.wrap(client, provider="anthropic")


def async_client() -> Any:
    client = AsyncAnthropic(
        api_key="sk-ant-test-not-real-0000000000",
        base_url="http://mock.invalid",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    return aobs_anthropic.wrap(client, provider="anthropic")


def create(client: Any, model: str = "claude-test", **kwargs: Any) -> Any:
    return client.messages.create(model=model, max_tokens=64, messages=MESSAGES, **kwargs)


def test_tested_library_version() -> None:
    assert anthropic.__version__ == "2.1.0"


def test_sync_message_maps_response_and_usage(sdk: InMemoryExporter) -> None:
    client = sync_client()
    with aobs.run("answer"):
        response = create(client, system=SYSTEM)
    assert response.content[0].text == "Hi!"  # the application gets the real response object
    flush()
    event = end_by_name(sdk, "chat")
    assert event["kind"] == "llm" and event["status"] == "ok"
    assert event["llm"] == {
        "provider": "anthropic",
        "operation": "chat",
        "request_model": "claude-test",
        "response_model": "claude-test-2026-01",
        "provider_request_id": "req_claude-test",
        "request": {"max_tokens": 64},
        "response": {"id": "msg_01", "finish_reasons": ["end_turn"]},
    }
    assert event["usage"] == {
        "input_tokens": 82,  # 10 uncached + 64 cache read + 8 cache write: canonical inclusive count
        "output_tokens": 20,
        "cache_read_input_tokens": 64,
        "cache_write_input_tokens": 8,
        "reasoning_tokens": 5,
        "source": "provider_reported",
    }
    assert "total_tokens" not in event["usage"]  # Anthropic does not report it; never derived
    assert "cost" not in event
    assert event["content"] == {"input": {"status": "not_captured"}, "output": {"status": "not_captured"}}
    assert event["input_fingerprint"].startswith("hmac-sha256:")
    resource = sdk.batches[0]["resource"]
    assert resource["integrations"][0]["name"] == "anthropic"
    assert "llm.usage" in resource["integrations"][0]["capabilities"]


def test_missing_usage_stays_unknown(sdk: InMemoryExporter) -> None:
    with aobs.run("answer"):
        create(sync_client(), model="no-usage")
    flush()
    assert "usage" not in end_by_name(sdk, "chat")
    assert only_trace(sdk).tokens["input_tokens"].value is None


def test_absent_cache_fields_stay_unknown(sdk: InMemoryExporter) -> None:
    with aobs.run("answer"):
        create(sync_client(), model="no-cache-fields")
    flush()
    assert end_by_name(sdk, "chat")["usage"] == {"input_tokens": 12, "output_tokens": 3, "source": "provider_reported"}


def test_content_capture_uses_otel_message_shape(make_sdk: Callable[..., InMemoryExporter]) -> None:
    sdk = make_sdk(capture="redacted")
    with aobs.run("answer"):
        create(sync_client(), system=SYSTEM)
    flush()
    content = end_by_name(sdk, "chat")["content"]
    assert content["input"]["format"] == "messages"
    assert content["input"]["value"] == [
        {"role": "system", "parts": [{"type": "text", "content": "Be brief."}]},
        {"role": "user", "parts": [{"type": "text", "content": "Hello [REDACTED:email]"}]},
    ]
    assert content["output"]["value"] == [
        {"role": "assistant", "parts": [{"type": "text", "content": "Hi!"}], "finish_reason": "end_turn"}
    ]


def test_tool_use_response_is_recorded_as_tool_call_parts(make_sdk: Callable[..., InMemoryExporter]) -> None:
    sdk = make_sdk(capture="full")
    with aobs.run("answer"):
        response = create(sync_client(), model="tool-use", tools=TOOLS)
    assert response.stop_reason == "tool_use"
    flush()
    event = end_by_name(sdk, "chat")
    assert event["llm"]["response"]["finish_reasons"] == ["tool_use"]
    assert event["content"]["output"]["value"] == [
        {
            "role": "assistant",
            "parts": [
                {"type": "reasoning", "content": "Need the order."},
                {"type": "text", "content": "Checking."},
                {"type": "tool_call", "id": "toolu_01", "name": "get_order_status", "arguments": {"order_id": "1042"}},
            ],
            "finish_reason": "tool_use",
        }
    ]


def test_tool_use_and_tool_result_messages_are_mapped() -> None:
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "see"}, {"type": "image", "source": {"data": "x"}}]},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"a": 1}}],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": '{"ok":true}'},
                {"type": "tool_result", "tool_use_id": "toolu_2", "content": [{"type": "text", "text": "done"}]},
            ],
        },
    ]
    system = [{"type": "text", "text": "Be brief.", "cache_control": {"type": "ephemeral"}}]
    assert aobs_anthropic.to_otel_messages(messages, system=system) == [
        {"role": "system", "parts": [{"type": "text", "content": "Be brief."}]},
        {"role": "user", "parts": [{"type": "text", "content": "see"}, {"type": "image"}]},  # image data not kept
        {
            "role": "assistant",
            "parts": [{"type": "tool_call", "id": "toolu_1", "name": "lookup", "arguments": {"a": 1}}],
        },
        {
            "role": "user",
            "parts": [
                {"type": "tool_call_response", "id": "toolu_1", "response": '{"ok":true}'},
                {"type": "tool_call_response", "id": "toolu_2", "response": [{"type": "text", "content": "done"}]},
            ],
        },
    ]


def test_identical_requests_share_a_fingerprint(sdk: InMemoryExporter) -> None:
    client = sync_client()
    with aobs.run("answer"):
        for _ in range(2):
            create(client)
        create(client, timeout=30)  # transport option ignored
        list(create(client, stream=True))  # `stream` is a transport option too
        with client.messages.stream(model="claude-test", max_tokens=64, messages=MESSAGES) as stream:
            stream.until_done()
        create(client, system=SYSTEM)
    flush()
    fps = [e["input_fingerprint"] for e in sdk.events() if e["type"] == "span_end" and e["kind"] == "llm"]
    assert fps[0] == fps[1] == fps[2] == fps[3] == fps[4] != fps[5]


@pytest.mark.parametrize(
    ("model", "error_class", "retryable"),
    [
        ("fail-429", anthropic.RateLimitError, True),
        ("fail-500", anthropic.InternalServerError, True),
        ("fail-529", anthropic.OverloadedError, True),
        ("fail-400", anthropic.BadRequestError, False),
    ],
)
def test_provider_error_is_recorded_and_reraised(
    sdk: InMemoryExporter, model: str, error_class: type[Exception], retryable: bool
) -> None:
    with pytest.raises(error_class) as caught, aobs.run("answer"):
        create(sync_client(), model=model)
    status = ERRORS[model][0]
    assert getattr(caught.value, "status_code", None) == status
    flush()
    event = end_by_name(sdk, "chat")
    assert event["status"] == "error"
    assert event["error"]["type"] == f"anthropic.{error_class.__name__}"
    assert event["error"]["origin"] == "provider" and event["error"]["code"] == str(status)
    assert event["error"].get("retryable") is (True if retryable else None)
    assert event["llm"]["provider_request_id"] == f"req_{model}"
    assert "usage" not in event


def test_connection_error(sdk: InMemoryExporter) -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused")

    with pytest.raises(anthropic.APIConnectionError):
        create(sync_client(httpx2.MockTransport(refuse)))
    flush()
    error = end_by_name(sdk, "chat")["error"]
    assert error["type"] == "anthropic.APIConnectionError" and error["retryable"] is True


def test_async_client(sdk: InMemoryExporter) -> None:
    client = async_client()

    async def main() -> str:
        with aobs.run("answer"):
            response = await create(client)
        return str(response.content[0].text)

    assert asyncio.run(main()) == "Hi!"
    flush()
    view = only_trace(sdk)
    llm = end_by_name(sdk, "chat")
    assert llm["usage"]["input_tokens"] == 82
    assert llm["llm"]["provider_request_id"] == "req_claude-test"
    assert llm["parent_span_id"] == view.roots[0]


def test_async_error(sdk: InMemoryExporter) -> None:
    client = async_client()

    async def main() -> None:
        await create(client, model="fail-429")

    with pytest.raises(anthropic.RateLimitError):
        asyncio.run(main())
    flush()
    error = end_by_name(sdk, "chat")["error"]
    assert error["code"] == "429" and error["retryable"] is True


def test_streaming_records_lifecycle_usage_and_content(make_sdk: Callable[..., InMemoryExporter]) -> None:
    sdk = make_sdk(capture="full")
    with aobs.run("answer"):
        stream = create(sync_client(), stream=True, tools=TOOLS)
        events = list(stream)
    text = "".join(e.delta.text for e in events if e.type == "content_block_delta" and e.delta.type == "text_delta")
    assert text == "Hello" and len(events) == 11
    flush()
    event = end_by_name(sdk, "chat")
    assert event["status"] == "ok"
    streaming = event["llm"]["streaming"]
    assert streaming["chunk_count"] == 11 and streaming["completed"] is True
    assert streaming["time_to_first_token_ms"] >= 0
    assert event["llm"]["request"] == {"max_tokens": 64, "stream": True}
    assert event["llm"]["response"] == {"id": "msg_s", "finish_reasons": ["tool_use"]}
    assert event["llm"]["response_model"] == "claude-test-2026-01"
    assert event["llm"]["provider_request_id"] == "req_claude-test"
    # input and cache counts from message_start; output from message_delta (not the placeholder 1)
    assert event["usage"] == {
        "input_tokens": 12,
        "output_tokens": 15,
        "cache_read_input_tokens": 3,
        "cache_write_input_tokens": 0,
        "source": "provider_reported",
    }
    assert event["content"]["output"]["value"] == [
        {
            "role": "assistant",
            "parts": [
                {"type": "text", "content": "Hello"},
                {"type": "tool_call", "id": "toolu_s", "name": "get_order_status", "arguments": {"order_id": "7"}},
            ],
            "finish_reason": "tool_use",
        }
    ]


def test_stream_closed_early_is_cancelled(sdk: InMemoryExporter) -> None:
    with aobs.run("answer"):
        stream = create(sync_client(), stream=True)
        with stream:
            next(iter(stream))  # message_start only
    flush()
    event = end_by_name(sdk, "chat")
    assert event["status"] == "cancelled"
    assert event["llm"]["streaming"] == {"chunk_count": 1, "completed": False}  # no content delta arrived
    # message_start reported the input; output is unknown (its count there is a placeholder), not zero
    assert event["usage"] == {
        "input_tokens": 12,
        "cache_read_input_tokens": 3,
        "cache_write_input_tokens": 0,
        "source": "provider_reported",
    }


def test_abandoned_stream_is_recorded_as_cancelled_via_finalizer(sdk: InMemoryExporter) -> None:
    with aobs.run("answer"):
        stream = create(sync_client(), stream=True)
        for _ in stream:
            break  # abandon without close()
        del stream
        gc.collect()
    flush()
    assert end_by_name(sdk, "chat")["status"] == "cancelled"


def test_error_event_mid_stream_is_recorded(sdk: InMemoryExporter) -> None:
    with pytest.raises(anthropic.APIStatusError), aobs.run("answer"):
        for _ in create(sync_client(), model="mid-stream-error", stream=True):
            pass
    flush()
    event = end_by_name(sdk, "chat")
    assert event["status"] == "error"
    assert event["error"]["code"] == "overloaded_error" and event["error"]["retryable"] is True
    assert event["llm"]["streaming"]["completed"] is False
    assert event["llm"]["streaming"]["chunk_count"] == 3  # message_start, content_block_start, one delta
    assert "output_tokens" not in event["usage"]


def test_async_streaming(sdk: InMemoryExporter) -> None:
    client = async_client()

    async def main() -> int:
        count = 0
        with aobs.run("answer"):
            stream = await create(client, stream=True)
            async for _ in stream:
                count += 1
        return count

    assert asyncio.run(main()) == 11
    flush()
    event = end_by_name(sdk, "chat")
    assert event["llm"]["streaming"]["completed"] is True
    assert event["usage"]["output_tokens"] == 15


def test_stream_helper_text_stream_and_final_message(make_sdk: Callable[..., InMemoryExporter]) -> None:
    sdk = make_sdk(capture="full")
    client = sync_client()
    with aobs.run("answer"), client.messages.stream(model="claude-test", max_tokens=64, messages=MESSAGES) as stream:
        text = "".join(stream.text_stream)
        final = stream.get_final_message()
    assert text == "Hello" and final.usage.output_tokens == 15
    assert final.content[1].input == {"order_id": "7"}
    flush()
    event = end_by_name(sdk, "chat")
    assert event["status"] == "ok"
    assert event["llm"]["request"] == {"max_tokens": 64, "stream": True}
    assert event["llm"]["streaming"]["completed"] is True and event["llm"]["streaming"]["chunk_count"] == 11
    assert event["llm"]["provider_request_id"] == "req_claude-test"
    assert event["usage"]["input_tokens"] == 12 and event["usage"]["output_tokens"] == 15
    assert event["content"]["output"]["value"][0]["parts"][0] == {"type": "text", "content": "Hello"}


def test_stream_helper_left_early_is_cancelled(sdk: InMemoryExporter) -> None:
    client = sync_client()
    with aobs.run("answer"), client.messages.stream(model="claude-test", max_tokens=64, messages=MESSAGES) as stream:
        for event in stream:
            if event.type == "text":
                break
    flush()
    event = end_by_name(sdk, "chat")
    assert event["status"] == "cancelled"
    assert event["llm"]["streaming"]["completed"] is False
    assert "output_tokens" not in event["usage"]


def test_exception_inside_stream_helper_block_is_an_error(sdk: InMemoryExporter) -> None:
    client = sync_client()
    with (
        pytest.raises(KeyError),
        aobs.run("answer"),
        client.messages.stream(model="claude-test", max_tokens=64, messages=MESSAGES) as stream,
    ):
        next(iter(stream))
        raise KeyError("application bug")
    flush()
    event = end_by_name(sdk, "chat")
    assert event["status"] == "error" and event["error"]["type"] == "KeyError"
    assert event["llm"]["streaming"]["completed"] is False


def test_stream_helper_request_error(sdk: InMemoryExporter) -> None:
    client = sync_client()
    manager = client.messages.stream(model="fail-500", max_tokens=64, messages=MESSAGES)
    assert sdk.events() == []  # no request (and no span) until the manager is entered
    with pytest.raises(anthropic.InternalServerError), manager:
        pass
    flush()
    error = end_by_name(sdk, "chat")["error"]
    assert error["code"] == "500" and error["retryable"] is True


def test_async_stream_helper(sdk: InMemoryExporter) -> None:
    client = async_client()

    async def main() -> str:
        with aobs.run("answer"):
            async with client.messages.stream(model="claude-test", max_tokens=64, messages=MESSAGES) as stream:
                return "".join([text async for text in stream.text_stream])

    assert asyncio.run(main()) == "Hello"
    flush()
    event = end_by_name(sdk, "chat")
    assert event["status"] == "ok" and event["llm"]["streaming"]["completed"] is True
    assert event["usage"]["output_tokens"] == 15


def test_async_stream_helper_left_early_is_cancelled(sdk: InMemoryExporter) -> None:
    client = async_client()

    async def main() -> None:
        with aobs.run("answer"):
            async with client.messages.stream(model="claude-test", max_tokens=64, messages=MESSAGES) as stream:
                async for _ in stream:
                    break

    asyncio.run(main())
    flush()
    assert end_by_name(sdk, "chat")["status"] == "cancelled"


def test_parse_is_instrumented(sdk: InMemoryExporter) -> None:
    class Answer(pydantic.BaseModel):
        answer: int

    with aobs.run("answer"):
        parsed = sync_client().messages.parse(model="json-out", max_tokens=64, messages=MESSAGES, output_format=Answer)
    assert parsed.parsed_output == Answer(answer=42)
    flush()
    event = end_by_name(sdk, "chat")
    assert event["status"] == "ok" and event["usage"]["output_tokens"] == 20


def test_with_options_stays_instrumented_and_passthrough_works(sdk: InMemoryExporter) -> None:
    client = sync_client()
    create(client.with_options(timeout=5))
    assert str(client.base_url).startswith("http://mock.invalid")  # other attributes pass through
    assert client.messages.batches is not None  # uninstrumented endpoints pass through
    flush()
    assert end_by_name(sdk, "chat")["llm"]["provider"] == "anthropic"


def test_provider_inference(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    assert aobs_anthropic._infer_provider(Anthropic(api_key="x")) == "anthropic"
    raw = Anthropic(api_key="x", base_url="http://localhost:8000")
    assert aobs_anthropic._infer_provider(raw) == "anthropic_compatible"


def test_wrapped_client_works_when_sdk_not_initialized() -> None:
    response = create(sync_client())
    assert response.usage.input_tokens == 10
    with sync_client().messages.stream(model="claude-test", max_tokens=64, messages=MESSAGES) as stream:
        assert stream.get_final_text() == "Hello"


def test_all_integration_output_is_schema_valid(make_sdk: Callable[..., InMemoryExporter]) -> None:
    sdk = make_sdk(capture="full")
    client = sync_client()
    with aobs.run("answer"):
        create(client, system=SYSTEM)
        create(client, model="no-usage")
        create(client, model="tool-use", tools=TOOLS)
        list(create(client, stream=True))
        with client.messages.stream(model="claude-test", max_tokens=64, messages=MESSAGES) as stream:
            next(iter(stream))
        with pytest.raises(anthropic.APIStatusError):
            list(create(client, model="mid-stream-error", stream=True))
        with pytest.raises(anthropic.RateLimitError):
            create(client, model="fail-429")
    flush()
    check_bodies(sdk.bodies)
