# Anthropic integration (`aobs.integrations.anthropic`)

`wrap(client, provider=None)` instruments the Anthropic Messages API on `anthropic.Anthropic` and
`anthropic.AsyncAnthropic` clients: `client.messages.create` (non-streaming and `stream=True`),
the `client.messages.stream(...)` helper, and `client.messages.parse(...)`. Each call becomes an
`llm` span, a child of the current run or span.

**Tested with `anthropic` 2.1.0**, the real library, against a mocked HTTP transport
(`httpx2.MockTransport`) that returns JSON bodies and SSE streams built from the shapes in the
public Messages API documentation (`sdk/python/tests/test_sdk_anthropic.py`). It has **not** been
run against the real Anthropic API by CI; see [Live smoke tests](../sdk.md#live-smoke-tests) to do
that yourself. Other library versions are likely to work (the integration is duck-typed and imports
nothing from `anthropic`) but are not tested.

```python
import aobs
from aobs.integrations import anthropic as aobs_anthropic
from anthropic import Anthropic

aobs.init(service_name="support-agent", endpoint="http://localhost:8000")  # or AOBS_ENDPOINT
client = aobs_anthropic.wrap(Anthropic())  # provider "anthropic" for api.anthropic.com

with aobs.run("answer_ticket", agent="support_agent"):
    reply = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system="You are a support agent.",
        messages=[{"role": "user", "content": "Where is order 1042?"}],
        tools=TOOLS,
    )

    with client.messages.stream(model=MODEL, max_tokens=1024, messages=messages) as stream:
        for text in stream.text_stream:
            print(text, end="")
        final = stream.get_final_message()
```

`AsyncAnthropic` works the same way (`await client.messages.create(...)`,
`async with client.messages.stream(...)`). `client.with_options(...)` and `client.copy(...)` return
instrumented clients; every other attribute passes through to the real client.

For Anthropic models served through another platform, pass `provider` explicitly (for example
`wrap(AnthropicBedrock(...), provider="aws.bedrock")`); a client whose base URL is not
`api.anthropic.com` otherwise gets `anthropic_compatible`. Only the first-party client is tested.

Capabilities declared in the batch `resource`: `llm.usage`, `llm.streaming`,
`llm.request_params`, `llm.response_metadata`, `llm.content`, `llm.errors`.

## What is recorded

| Recorded | From |
|---|---|
| `request_model`, max_tokens, stream (temperature, top_p when passed) | request arguments |
| `response_model`, response id | the `Message` (or the stream's `message_start`) |
| finish reason | `stop_reason` (`end_turn`, `max_tokens`, `stop_sequence`, `tool_use`, `pause_turn`, `refusal`, …) |
| `provider_request_id` | the `request-id` header (message `_request_id`, stream response headers, error `request_id`) |
| usage (`provider_reported`) | see below |
| streaming | time to the first `content_block_delta` event, number of events, completed or not |
| errors | type, HTTP status as `code`, `retryable` for 408/409/429/5xx (529 overloaded included) and connection/timeout errors |
| content (policy permitting) | `system` and messages → OpenTelemetry GenAI `{role, parts}` (below) |
| `input_fingerprint` | the request arguments, excluding `timeout`, `extra_headers`, `extra_query`, `stream` |

### Usage: nothing invented

| Wire field | Anthropic field |
|---|---|
| `input_tokens` | `input_tokens` + `cache_read_input_tokens` + `cache_creation_input_tokens` |
| `cache_read_input_tokens` | `cache_read_input_tokens` |
| `cache_write_input_tokens` | `cache_creation_input_tokens` |
| `output_tokens` | `output_tokens` |
| `reasoning_tokens` | `output_tokens_details.thinking_tokens` |
| `total_tokens` | not reported by Anthropic: left unknown, never derived |

Anthropic's `input_tokens` counts only the input *after* the last cache breakpoint; the wire's
`input_tokens` means all input the model processed, with cache reads and writes as subsets of it
([event-model.md](../event-model.md)), so the integration adds the two cache counts in. A cache
field the response does not carry (or carries as `null`) adds nothing and stays unknown itself. A
response with no `usage` records no usage.

Streaming: input and cache counts come from `message_delta` when it carries them (cumulative) and
otherwise from `message_start`; output tokens come **only** from `message_delta`, because the
count in `message_start` is a placeholder. A stream closed before `message_delta` therefore records
its input tokens but leaves output tokens unknown.

### Content

Under `capture="redacted"` or `"full"` (the default `metadata_only` sends none of it):

* `system` (string or text blocks) becomes a leading `system` message.
* `text` blocks → `{"type": "text", "content": ...}`.
* `tool_use` blocks → `{"type": "tool_call", "id", "name", "arguments"}` (the `input` object), the
  same part type `openai_compat` uses for tool calls. Tool calls are recorded as content parts of
  the `llm` span; a tool span is created only when your tool function runs (`@aobs.tool`).
* `tool_result` blocks → `{"type": "tool_call_response", "id": tool_use_id, "response": ...}`.
* `thinking` blocks → `{"type": "reasoning", "content": ...}` (the signature is not kept).
* Everything else (images, documents, `redacted_thinking`, server tool blocks, …) is recorded by
  type only; its data is not captured.
* The response becomes one `assistant` message with `finish_reason` set to `stop_reason`. For
  streams, text, thinking and tool-call inputs are accumulated from the deltas (up to 64 KiB); a
  tool input whose JSON is incomplete (stream cut short) is kept as the text received.

### Streams

* Time to first token is measured to the first `content_block_delta` (the `message_start` and
  `ping` events arrive before any generated content). `chunk_count` counts the events the library
  yields (it consumes `ping` itself).
* A stream read to the end is `ok` with `completed: true`. One closed early, left early through
  `with`, or garbage-collected unread is `cancelled` with `completed: false`.
* An exception raised inside the `with` block (yours or the library's) ends the span as `error`.
* An `error` event in the middle of a stream (the HTTP status was 200) is recorded with the event's
  error type as `code` (e.g. `overloaded_error`) and `retryable` for `rate_limit_error`,
  `api_error`, `timeout_error` and `overloaded_error`.
* With `client.messages.stream(...)` the request (and the span) starts when the manager is
  entered, not when `stream(...)` is called. All ways of reading the helper (iteration,
  `text_stream`, `get_final_message`, `until_done`) are observed.

## Not instrumented / not recorded

* `with_raw_response` / `with_streaming_response`, `client.beta.messages`, `messages.batches`,
  `messages.count_tokens`, and other endpoints pass through uninstrumented.
* Cost (the Messages API does not report it), headers, API keys, `server_tool_use` counts,
  `service_tier`, `stop_details`.
