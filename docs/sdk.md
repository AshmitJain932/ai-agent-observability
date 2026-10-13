# Python SDK (`aobs`)

Status: **Phase 2** (2026-10-12). Version `0.1.0.dev0`, not yet published to PyPI. Python ≥ 3.11.
No third-party dependencies. Emits wire schema v1.0 ([event-model.md](event-model.md)).

## Install

From a checkout of this repository:

```bash
uv sync                              # installs aobs (editable) + the server model + dev tools
# or, into another environment:
pip install ./sdk/python
```

## Quick start

```python
import aobs
from aobs.integrations import openai_compat
from openai import OpenAI

aobs.init(service_name="support-agent", endpoint="http://localhost:8000")  # or AOBS_ENDPOINT
client = openai_compat.wrap(OpenAI(), provider="openai")

@aobs.tool
def get_order_status(order_id: str) -> dict: ...

with aobs.run("answer_ticket", agent="support_agent", session_id=ticket_id):
    reply = client.chat.completions.create(model=MODEL, messages=messages, tools=TOOLS)
    get_order_status("1042")
```

Every model call and tool call inside the `with` block becomes a child span of the run. A
`flush` happens automatically at interpreter exit; call `aobs.shutdown()` yourself in scripts
and short-lived jobs (serverless) so you control the wait.

To store runs in the collector, pass `endpoint=` and `api_key=` (or set `AOBS_ENDPOINT` and
`AOBS_API_KEY`); see [ingestion.md](ingestion.md). Without a collector, use
`exporter=JsonlFileExporter("traces.jsonl")` (see the example agent) or your own exporter.

## API

| Call | Creates | Notes |
|---|---|---|
| `aobs.run(name, *, agent=, agent_version=, agent_id=, session_id=, attributes=)` | `agent` span, **new trace** | One run = one trace. If a span is already active, the new trace is linked to it (`triggered_by`). |
| `aobs.span(name, *, kind="step", attributes=, links=, parent=)` | child span | Generic operation; `kind` is normalized (`"Planner Step"` → `planner_step`). |
| `aobs.agent_span(name, *, version=, agent_id=)` / `@aobs.agent` | `agent` child span | Sub-agent within the current trace. |
| `aobs.tool_span(name, *, tool_type=, call_id=)` / `@aobs.tool` | `tool` span | The decorator fingerprints the bound arguments (receiver `self`/`cls` excluded, defaults applied) and records arguments/result as content per the capture policy (`capture=False` disables). Sync and async functions; generator functions are passed through uninstrumented (counted as `uninstrumented_calls`). |
| `aobs.llm_span(*, provider, request_model=, operation="chat")` | `llm` span | For providers without an integration. |
| `aobs.event(name, attributes)` | span event | On the current span. |
| `aobs.current_span()` | — | A non-recording span when there is none. |
| `aobs.with_current_context(fn)` | — | Carry the current span into threads / thread pools. `asyncio` needs nothing. |
| `aobs.flush(timeout_s=5)` → bool | — | Export everything queued, including starts of still-open spans. False on timeout. |
| `aobs.shutdown(timeout_s=None)` → bool | — | Flush (bounded), stop the worker, shut the exporter down. |
| `aobs.diagnostics()` → dict | — | Counters below plus `enabled`, `disabled_reason`, `queued`, `capture`, `fingerprint_key_id`. |

Span methods (all safe in any state; invalid values are dropped and counted, never raised):
`set_attribute(s)`, `add_link(trace_id, span_id, relation)`, `set_retry(attempt, max_attempts)`,
`set_tool(name=, type=, call_id=)`, `set_llm(provider=, operation=, request_model=,
response_model=, provider_request_id=, request={…}, response={…}, streaming={…})`,
`fingerprint_input(value)`, `set_input(value, format)`, `set_output(value, format)`,
`set_content_unavailable(direction)`, `record_usage(input_tokens=, output_tokens=, total_tokens=,
cache_read_input_tokens=, cache_write_input_tokens=, reasoning_tokens=, source=)`,
`record_cost(amount, currency)` (provider-reported only), `set_status(status)`,
`set_error(exc_or_type, message=, origin=, code=, retryable=)`, `record_exception(exc)`,
`event(name, attributes)`, `end(status=None)`.

### Statuses and errors

* Leaving a span with an `Exception` → `error`, with `error.type` (module-qualified class name),
  `message` (scrubbed), `origin`: `tool` for tool spans, `provider` for LLM spans, `timeout` for
  `TimeoutError`, `application` otherwise. The exception is re-raised unchanged.
* `asyncio.CancelledError`, `KeyboardInterrupt`, `SystemExit`, `GeneratorExit` → `cancelled`.
* `span.set_error(...)` marks a failure without raising (e.g. a tool that returns an error value).
* Durations come from the monotonic clock; `end_time = start_time + duration`, so wall-clock
  adjustments during a span cannot produce negative or inconsistent durations.

### Usage and cost: nothing is invented

* `record_usage` records only counts you pass; missing or invalid counts (negative, non-integer,
  boolean) stay **unknown** — they are omitted, never zero-filled. If no valid count remains, no
  usage is recorded. `source` must be `provider_reported` or `sdk_estimated`.
* `record_cost` is for cost **reported by the provider itself**. The SDK never computes cost;
  platform estimates are server-side (Phase 6) from usage and versioned prices.

## Configuration

Explicit arguments to `aobs.init()` override environment variables, which override defaults.

| Argument | Environment | Default | |
|---|---|---|---|
| `service_name` | `AOBS_SERVICE_NAME` | script name | ≤ 128 chars |
| `environment` | `AOBS_ENVIRONMENT` | `development` | normalized to `[a-z0-9_.-]` |
| `service_version` | `AOBS_SERVICE_VERSION` | — | e.g. git SHA |
| `endpoint` | `AOBS_ENDPOINT` | — | collector base URL; `/v1/ingest` is appended |
| `api_key` | `AOBS_API_KEY` | — | sent only as `Authorization: Bearer`; never logged |
| `exporter` | — | HTTP if `endpoint` set, else telemetry disabled (with a warning) | any `Exporter` |
| `enabled` | `AOBS_ENABLED` | `true` | `false` turns every call into a no-op |
| `capture` | `AOBS_CAPTURE` | `metadata_only` | `metadata_only` \| `redacted` \| `full` |
| `capture_error_messages` | `AOBS_CAPTURE_ERROR_MESSAGES` | `true` | scrubbed |
| `capture_stack_traces` | `AOBS_CAPTURE_STACK_TRACES` | `false` | scrubbed, last 32 KiB |
| `max_content_bytes` | — | 32768 | per content part (256–65536) |
| `redactors`, `use_default_redactors` | — | defaults on | extra `Callable[[str], tuple[str, int]]` |
| `fingerprint_inputs` | — | `true` | |
| `fingerprint_key`, `fingerprint_key_id` | `AOBS_FINGERPRINT_KEY` (hex), `AOBS_FINGERPRINT_KEY_ID` | random per process | key ≥ 32 bytes |
| `strict` | — | `false` | raise `ConfigError` instead of warning and falling back |
| `max_queue_size` | — | 10000 | events held in memory (100–1,000,000) |
| `max_batch_events` | — | 500 | ≤ 1000 (schema limit) |
| `max_batch_bytes` | — | 900000 | per request (16 KiB – 1 MiB) |
| `schedule_delay_s` | — | 1.0 | export interval |
| `start_delay_s` | — | 1.0 | start coalescing window |
| `export_timeout_s` | — | 10 | per HTTP request |
| `max_export_attempts` | — | 4 | then the batch is dropped and counted |
| `retry_backoff_initial_s`, `retry_backoff_max_s` | — | 0.5, 30 | jittered exponential backoff |
| `shutdown_timeout_s` | — | 5 | used at exit and by `shutdown()` |

Out-of-range tuning values are clamped (and rejected with `strict=True`). Unknown argument names
raise `TypeError` like any Python function.

## Privacy

| Mode | Prompt / response / tool arguments / results | Content status sent |
|---|---|---|
| `metadata_only` (default) | **not sent** | `not_captured` |
| `redacted` | sent after redaction (secrets, e-mail addresses, card numbers that pass a Luhn check, plus your redactors) | `redacted` (+ `redaction_count`) |
| `full` | sent as is, size-capped (`truncated: true` when cut) | `captured` |

Always, in every mode: error messages, stack traces and string attributes are secret-scrubbed
(bearer tokens, `sk-…`/`pk-…` keys, AWS access keys, GitHub/Slack tokens, JWTs, private-key
blocks, URL credentials, `password=…`-style assignments). Full-mode *content* is not scrubbed —
use `redacted` if content may contain credentials. The SDK cannot stop code from putting personal
data into span **names** or **attributes**; don't.

Structured content larger than `max_content_bytes` is sent as truncated JSON **text**
(`format: "text"`, `truncated: true`) rather than a broken structure. An event too large for one
batch has its content and attributes removed (content reported as `not_captured`) and is counted
as `events_degraded_oversize`.

### Input fingerprints

`input_fingerprint = hmac-sha256:<key_id>:<hex>` over canonical JSON of the input (tool
arguments; LLM request arguments excluding transport options such as `timeout`, `stream`,
`extra_headers`). It lets the platform spot repeated identical calls without seeing the inputs.

* The key never leaves the process: not in events, not in `repr`, not in diagnostics; the
  `Fingerprinter` refuses to be pickled.
* Default key: random per process — fingerprints compare within one process (and its forked
  children). For comparison across processes and runs, configure a stable key (≥ 32 bytes, e.g.
  `python -c "import secrets; print(secrets.token_hex(32))"`) and a key id; rotate both together.

## Delivery guarantees

* **Never blocks, never raises.** Recording a span takes tens of microseconds (see
  [benchmarks.md](benchmarks.md)) and does no I/O. JSON encoding and network calls happen on one
  background thread (`aobs-exporter`).
* **Bounded memory.** At most `max_queue_size` events (about 1.7 KB each) are held; beyond that new
  events are dropped and counted.
* **Start coalescing.** A span's `span_start` is only exported if the span is still running after
  `start_delay_s` (or at a flush). Short spans cost one event; long or hung spans stay visible.
* **Bounded retries.** Network errors, timeouts, 408/429/5xx are retried up to
  `max_export_attempts` with jittered backoff (`Retry-After` honoured up to the maximum backoff);
  other 4xx are not retried. Retries are safe because ingest is idempotent.
* **Collector unavailable:** the application is unaffected; each batch is attempted
  `max_export_attempts` times and then dropped (`events_dropped_export_failed`); the queue fills
  and further events are dropped (`events_dropped_queue_full`); `flush()` returns False after its
  timeout. Nothing is persisted to disk in this version.
* **Redirects are not followed** (they could forward the `Authorization` header).
* **Fork-safe** (gunicorn, celery, multiprocessing with fork): the child starts with an empty
  queue and its own worker thread.
* **Abandoned streams** (an LLM stream neither exhausted nor closed) are recorded as `cancelled`
  from the finalizer without taking locks there; the span is ended by the worker thread.

## Diagnostics counters

`spans_started`, `spans_ended`, `span_double_end`, `events_enqueued`, `starts_coalesced`,
`events_exported`, `batches_exported`, `export_attempts`, `export_retries`, `batches_failed`,
`events_dropped_queue_full`, `events_dropped_after_shutdown`, `events_dropped_export_failed`,
`events_dropped_oversize`, `events_degraded_oversize`, `events_rejected_by_collector`,
`attributes_dropped`, `values_sanitized`, `invalid_usage_values`, `invalid_cost_values`,
`content_truncated`, `uninstrumented_calls`, `internal_errors`, `config_warnings`, plus
`last_error` / `last_error_at`. Problems are also logged on the `aobs` logger at WARNING, at most
once a minute per category.

## Exporters

`HttpExporter(endpoint, api_key=None, headers=None)`, `JsonlFileExporter(path)` (one batch per
line, file mode 0600), `InMemoryExporter()` (tests). To add a transport, implement:

```python
class MyExporter:
    def export(self, batch: aobs.export.EncodedBatch, timeout_s: float) -> aobs.export.ExportResult: ...
    def shutdown(self) -> None: ...
```

`batch.body` is the exact wire-format envelope; return `ExportResult("success")`,
`ExportResult("retry", retry_after_s=…)` or `ExportResult("failure", message=…)`. Exceptions
raised by `export` are treated as retryable.

## Integrations

### `aobs.integrations.openai_compat`

`wrap(client, provider=None)` instruments `client.chat.completions.create` on OpenAI-compatible
clients (sync and async, streaming and non-streaming). **Tested with `openai` 3.29.0** through its
own HTTP mock transport. Other versions are likely to work (the integration is duck-typed and
imports nothing from `openai`) but are not tested.

Capabilities declared in the batch `resource`: `llm.usage`, `llm.streaming`,
`llm.request_params`, `llm.response_metadata`, `llm.content`, `llm.errors`.

| Recorded | From |
|---|---|
| `request_model`, temperature, top_p, max_tokens (or max_completion_tokens), stream | request arguments |
| `response_model`, response id, finish reasons | response |
| `provider_request_id` | `x-request-id` (response `_request_id`, stream response headers, error `request_id`) |
| usage (`provider_reported`) | `prompt_tokens`→`input_tokens`, `completion_tokens`→`output_tokens`, `total_tokens`, `prompt_tokens_details.cached_tokens`→`cache_read_input_tokens`, `prompt_tokens_details.cache_write_tokens`→`cache_write_input_tokens`, `completion_tokens_details.reasoning_tokens`→`reasoning_tokens` |
| streaming | time to first chunk, chunk count, completed; usage only if the request set `stream_options={"include_usage": True}` |
| errors | type, HTTP status as `code`, `retryable` for 408/409/429/5xx and connection/timeout errors |
| content (policy permitting) | messages → OpenTelemetry GenAI `{role, parts}`; image/audio/file parts recorded by type only |

Not instrumented: `with_raw_response`, `with_streaming_response`, other endpoints (Responses API,
embeddings). Not recorded: cost (not reported by Chat Completions), headers, API keys.

## Known limitations (Phase 2)

* Python only; no cross-process trace propagation helpers (W3C `traceparent`) yet.
* Generator / async-generator functions decorated with `@aobs.tool`/`@aobs.agent` are not
  instrumented (counted as `uninstrumented_calls`).
* No on-disk buffering: telemetry produced while the collector is down beyond the retry budget is
  dropped (and counted).
* No sampling or adaptive capture yet (Phase 9).
* Overhead per instrumented tool call (~57 µs median on the reference VM) is above the 50 µs
  roadmap target; see [benchmarks.md](benchmarks.md) for the measured profile and candidates.
