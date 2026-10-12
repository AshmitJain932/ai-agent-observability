"""openai_compat against the real ``openai`` client (3.29.0) with a mock HTTP transport.

No network, no credentials: the transport answers requests the way the API does, so request
building, response parsing, streaming and error classes are the library's own.
"""

from __future__ import annotations

import asyncio
import gc
import json
from collections.abc import Callable
from typing import Any

import httpx2
import openai
import pytest
from openai import AsyncOpenAI, OpenAI

import aobs
from aobs.export import InMemoryExporter
from aobs.integrations import openai_compat
from sdk_support import check_bodies, end_by_name, flush, only_trace

FULL_USAGE = {
    "prompt_tokens": 100,
    "completion_tokens": 20,
    "total_tokens": 120,
    "prompt_tokens_details": {"cached_tokens": 64, "cache_write_tokens": 8},
    "completion_tokens_details": {"reasoning_tokens": 5},
}
MESSAGES = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Hello jane@example.com"}]


def completion(usage: dict[str, Any] | None = FULL_USAGE, content: str = "Hi!") -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "gpt-x-2026-01",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
    }
    if usage is not None:
        body["usage"] = usage
    return body


def sse(chunks: list[dict[str, Any]]) -> bytes:
    return ("".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n").encode()


def chunk(
    content: str | None = None, finish: str | None = None, usage: dict[str, Any] | None = None, choices: bool = True
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": "chatcmpl-s",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "gpt-x-2026-01",
        "choices": [{"index": 0, "delta": {"content": content} if content else {}, "finish_reason": finish}]
        if choices
        else [],
    }
    if usage:
        out["usage"] = usage
    return out


STREAM = [
    chunk("Hel"),
    chunk("lo"),
    chunk(finish="stop"),
    chunk(choices=False, usage={"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11}),
]


def handler(request: httpx2.Request) -> httpx2.Response:
    body = json.loads(request.content)
    model = body["model"]
    headers = {"x-request-id": f"req_{model}"}
    if model == "fail-500":
        return httpx2.Response(500, json={"error": {"message": "boom", "type": "server_error"}}, headers=headers)
    if model == "fail-400":
        return httpx2.Response(400, json={"error": {"message": "bad request", "type": "invalid"}}, headers=headers)
    if body.get("stream"):
        return httpx2.Response(200, content=sse(STREAM), headers={**headers, "content-type": "text/event-stream"})
    if model == "no-usage":
        return httpx2.Response(200, json=completion(usage=None), headers=headers)
    return httpx2.Response(200, json=completion(), headers=headers)


def sync_client(transport: Any = None) -> Any:
    client = OpenAI(
        api_key="sk-test-not-real-0000000000",
        base_url="http://mock.invalid/v1",
        max_retries=0,
        http_client=httpx2.Client(transport=transport or httpx2.MockTransport(handler)),
    )
    return openai_compat.wrap(client, provider="openai")


def async_client() -> Any:
    client = AsyncOpenAI(
        api_key="sk-test-not-real-0000000000",
        base_url="http://mock.invalid/v1",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    return openai_compat.wrap(client, provider="openai")


def test_tested_library_version() -> None:
    assert openai.__version__ == "3.29.0"


def test_sync_completion_maps_response_and_usage(sdk: InMemoryExporter) -> None:
    client = sync_client()
    with aobs.run("answer"):
        response = client.chat.completions.create(model="gpt-x", messages=MESSAGES, temperature=0.2, max_tokens=50)
    assert response.choices[0].message.content == "Hi!"  # the application gets the real response object
    flush()
    event = end_by_name(sdk, "chat")
    assert event["kind"] == "llm" and event["status"] == "ok"
    assert event["llm"] == {
        "provider": "openai",
        "operation": "chat",
        "request_model": "gpt-x",
        "response_model": "gpt-x-2026-01",
        "provider_request_id": "req_gpt-x",
        "request": {"temperature": 0.2, "max_tokens": 50},
        "response": {"id": "chatcmpl-1", "finish_reasons": ["stop"]},
    }
    assert event["usage"] == {
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
        "cache_read_input_tokens": 64,
        "cache_write_input_tokens": 8,
        "reasoning_tokens": 5,
        "source": "provider_reported",
    }
    assert "cost" not in event  # Chat Completions does not report cost; nothing is invented
    assert event["content"] == {"input": {"status": "not_captured"}, "output": {"status": "not_captured"}}
    assert event["input_fingerprint"].startswith("hmac-sha256:")
    resource = sdk.batches[0]["resource"]
    assert resource["integrations"][0]["name"] == "openai_compat"
    assert "llm.usage" in resource["integrations"][0]["capabilities"]


def test_missing_usage_stays_unknown(sdk: InMemoryExporter) -> None:
    with aobs.run("answer"):
        sync_client().chat.completions.create(model="no-usage", messages=MESSAGES)
    flush()
    assert "usage" not in end_by_name(sdk, "chat")
    assert only_trace(sdk).tokens["input_tokens"].value is None


def test_content_capture_uses_otel_message_shape(make_sdk: Callable[..., InMemoryExporter]) -> None:
    sdk = make_sdk(capture="redacted")
    with aobs.run("answer"):
        sync_client().chat.completions.create(model="gpt-x", messages=MESSAGES)
    flush()
    content = end_by_name(sdk, "chat")["content"]
    assert content["input"]["format"] == "messages"
    assert content["input"]["value"] == [
        {"role": "system", "parts": [{"type": "text", "content": "Be brief."}]},
        {"role": "user", "parts": [{"type": "text", "content": "Hello [REDACTED:email]"}]},
    ]
    assert content["output"]["value"] == [
        {"role": "assistant", "parts": [{"type": "text", "content": "Hi!"}], "finish_reason": "stop"}
    ]


def test_tool_call_messages_are_mapped() -> None:
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "lookup", "arguments": '{"a":1}'}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": '{"ok":true}'},
        {
            "role": "user",
            "content": [{"type": "text", "text": "see"}, {"type": "image_url", "image_url": {"url": "x"}}],
        },
    ]
    assert openai_compat.to_otel_messages(messages) == [
        {"role": "assistant", "parts": [{"type": "tool_call", "id": "c1", "name": "lookup", "arguments": '{"a":1}'}]},
        {"role": "tool", "parts": [{"type": "tool_call_response", "id": "c1", "response": '{"ok":true}'}]},
        {"role": "user", "parts": [{"type": "text", "content": "see"}, {"type": "image_url"}]},  # image data not kept
    ]


def test_identical_requests_share_a_fingerprint(sdk: InMemoryExporter) -> None:
    client = sync_client()
    with aobs.run("answer"):
        for _ in range(2):
            client.chat.completions.create(model="gpt-x", messages=MESSAGES)
        client.chat.completions.create(model="gpt-x", messages=MESSAGES, timeout=30)  # transport option ignored
        client.chat.completions.create(model="gpt-x", messages=MESSAGES[:1])
    flush()
    fps = [e["input_fingerprint"] for e in sdk.events() if e["type"] == "span_end" and e["kind"] == "llm"]
    assert fps[0] == fps[1] == fps[2] != fps[3]


def test_provider_error_is_recorded_and_reraised(sdk: InMemoryExporter) -> None:
    with pytest.raises(openai.InternalServerError) as caught, aobs.run("answer"):
        sync_client().chat.completions.create(model="fail-500", messages=MESSAGES)
    assert caught.value.status_code == 500
    flush()
    event = end_by_name(sdk, "chat")
    assert event["status"] == "error"
    assert event["error"]["type"] == "openai.InternalServerError"
    assert event["error"]["origin"] == "provider" and event["error"]["code"] == "500"
    assert event["error"]["retryable"] is True
    assert event["llm"]["provider_request_id"] == "req_fail-500"


def test_client_error_is_not_marked_retryable(sdk: InMemoryExporter) -> None:
    with pytest.raises(openai.BadRequestError):
        sync_client().chat.completions.create(model="fail-400", messages=MESSAGES)
    flush()
    error = end_by_name(sdk, "chat")["error"]
    assert error["code"] == "400" and "retryable" not in error


def test_connection_error(sdk: InMemoryExporter) -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused")

    with pytest.raises(openai.APIConnectionError):
        sync_client(httpx2.MockTransport(refuse)).chat.completions.create(model="gpt-x", messages=MESSAGES)
    flush()
    error = end_by_name(sdk, "chat")["error"]
    assert error["type"] == "openai.APIConnectionError" and error["retryable"] is True


def test_async_client(sdk: InMemoryExporter) -> None:
    client = async_client()

    async def main() -> str:
        with aobs.run("answer"):
            response = await client.chat.completions.create(model="gpt-x", messages=MESSAGES)
        return str(response.choices[0].message.content)

    assert asyncio.run(main()) == "Hi!"
    flush()
    view = only_trace(sdk)
    llm = end_by_name(sdk, "chat")
    assert llm["usage"]["input_tokens"] == 100
    assert llm["parent_span_id"] == view.roots[0]


def test_async_error(sdk: InMemoryExporter) -> None:
    client = async_client()

    async def main() -> None:
        await client.chat.completions.create(model="fail-500", messages=MESSAGES)

    with pytest.raises(openai.InternalServerError):
        asyncio.run(main())
    flush()
    assert end_by_name(sdk, "chat")["error"]["code"] == "500"


def test_streaming_records_lifecycle_and_usage(make_sdk: Callable[..., InMemoryExporter]) -> None:
    sdk = make_sdk(capture="full")
    with aobs.run("answer"):
        stream = sync_client().chat.completions.create(
            model="gpt-x", messages=MESSAGES, stream=True, stream_options={"include_usage": True}
        )
        text = "".join(c.choices[0].delta.content or "" for c in stream if c.choices)
    assert text == "Hello"
    flush()
    event = end_by_name(sdk, "chat")
    assert event["status"] == "ok"
    streaming = event["llm"]["streaming"]
    assert streaming["chunk_count"] == 4 and streaming["completed"] is True
    assert streaming["time_to_first_token_ms"] >= 0
    assert event["llm"]["request"] == {"stream": True}
    assert event["llm"]["response"]["finish_reasons"] == ["stop"]
    assert event["usage"] == {"input_tokens": 9, "output_tokens": 2, "total_tokens": 11, "source": "provider_reported"}
    assert event["content"]["output"]["value"] == [
        {"role": "assistant", "parts": [{"type": "text", "content": "Hello"}]}
    ]


def test_stream_closed_early_is_cancelled(sdk: InMemoryExporter) -> None:
    with aobs.run("answer"):
        stream = sync_client().chat.completions.create(model="gpt-x", messages=MESSAGES, stream=True)
        with stream:
            next(iter(stream))
    flush()
    event = end_by_name(sdk, "chat")
    assert event["status"] == "cancelled"
    assert event["llm"]["streaming"] == {
        "chunk_count": 1,
        "completed": False,
        "time_to_first_token_ms": event["llm"]["streaming"]["time_to_first_token_ms"],
    }
    assert "usage" not in event  # usage chunk never arrived: unknown, not zero


def test_abandoned_stream_is_recorded_as_cancelled_via_finalizer(sdk: InMemoryExporter) -> None:
    with aobs.run("answer"):
        stream = sync_client().chat.completions.create(model="gpt-x", messages=MESSAGES, stream=True)
        for _ in stream:
            break  # abandon without close()
        del stream
        gc.collect()
    flush()
    assert end_by_name(sdk, "chat")["status"] == "cancelled"


def test_async_streaming(sdk: InMemoryExporter) -> None:
    client = async_client()

    async def main() -> int:
        count = 0
        with aobs.run("answer"):
            stream = await client.chat.completions.create(model="gpt-x", messages=MESSAGES, stream=True)
            async for _ in stream:
                count += 1
        return count

    assert asyncio.run(main()) == 4
    flush()
    assert end_by_name(sdk, "chat")["llm"]["streaming"]["completed"] is True


def test_with_options_stays_instrumented_and_passthrough_works(sdk: InMemoryExporter) -> None:
    client = sync_client()
    client.with_options(timeout=5).chat.completions.create(model="gpt-x", messages=MESSAGES)
    assert str(client.base_url).startswith("http://mock.invalid")  # other attributes pass through
    flush()
    assert end_by_name(sdk, "chat")["llm"]["provider"] == "openai"


def test_provider_inference() -> None:
    raw = OpenAI(api_key="x", base_url="http://localhost:8000/v1")
    assert openai_compat._infer_provider(raw) == "openai_compatible"
    assert openai_compat._infer_provider(OpenAI(api_key="x")) == "openai"


def test_wrapped_client_works_when_sdk_not_initialized() -> None:
    response = sync_client().chat.completions.create(model="gpt-x", messages=MESSAGES)
    assert response.usage.prompt_tokens == 100


def test_all_integration_output_is_schema_valid(make_sdk: Callable[..., InMemoryExporter]) -> None:
    sdk = make_sdk(capture="full")
    client = sync_client()
    with aobs.run("answer"):
        client.chat.completions.create(model="gpt-x", messages=MESSAGES)
        client.chat.completions.create(model="no-usage", messages=MESSAGES)
        list(client.chat.completions.create(model="gpt-x", messages=MESSAGES, stream=True))
        with pytest.raises(openai.InternalServerError):
            client.chat.completions.create(model="fail-500", messages=MESSAGES)
    flush()
    check_bodies(sdk.bodies)
