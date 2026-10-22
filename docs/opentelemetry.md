# Exporting to OpenTelemetry backends (OTLP)

`aobs.export.OtlpHttpExporter` sends aobs spans to anything that accepts **OTLP/HTTP with JSON
encoding** on `POST /v1/traces`: an OpenTelemetry Collector (and from there Grafana Tempo,
Datadog, Honeycomb, ...), Jaeger, or a vendor endpoint. It is part of the stdlib-only SDK (uses
`urllib`; no OpenTelemetry packages needed at runtime) and implements the ordinary
[`Exporter`](sdk.md#exporters) protocol, so it sees exactly the batches the aobs collector would
see — nothing more.

What was tested: the exporter's output is posted to a local `http.server` on 127.0.0.1 and
checked for structure, mapping, retries and privacy, and every payload is parsed with the official
protobuf definitions (`opentelemetry-proto`, `ExportTraceServiceRequest`, unknown fields
rejected) (`sdk/python/tests/test_sdk_otlp.py`). It has **not** been run against a real Collector,
Jaeger, Tempo or vendor backend in CI or in this repository's tests; the setup examples below are
provided as a starting point and are not exercised by CI.

## Setup

```python
import aobs
from aobs.export import FanOutExporter, HttpExporter, OtlpHttpExporter

# OpenTelemetry only:
aobs.init(service_name="support-agent", exporter=OtlpHttpExporter("http://localhost:4318/v1/traces"))

# Both the aobs collector and an OpenTelemetry backend:
aobs.init(
    service_name="support-agent",
    exporter=FanOutExporter(
        HttpExporter("http://localhost:8000", api_key=AOBS_API_KEY),
        OtlpHttpExporter("http://localhost:4318/v1/traces", headers={"x-api-key": OTLP_KEY}),
    ),
)
```

`OtlpHttpExporter(endpoint=None, headers=None, *, compression=None, include_content=True,
resource_attributes=None, max_pending_events=4096, max_tracked_spans=16384)`

| Argument | Default | Meaning |
|---|---|---|
| `endpoint` | `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, else `OTEL_EXPORTER_OTLP_ENDPOINT` + `/v1/traces`, else `http://localhost:4318/v1/traces` | Full traces URL (http or https). |
| `headers` | `OTEL_EXPORTER_OTLP_TRACES_HEADERS`, else `OTEL_EXPORTER_OTLP_HEADERS` (`k1=v1,k2=v2`, URL-encoded values) | E.g. a vendor API key. A warning is logged if headers would go over plaintext HTTP to a non-local host. |
| `compression` | `None` | `"gzip"` compresses the body (`Content-Encoding: gzip`). |
| `include_content` | `True` | `False` keeps captured content out of OTLP even when aobs captured it. |
| `resource_attributes` | — | Extra resource attributes (e.g. `service.namespace`); they override derived ones. |
| `max_pending_events` | 4096 | Span events held while waiting for their span to end (see below). |
| `max_tracked_spans` | 16384 | Recently exported spans remembered so late events are recognised. |

`FanOutExporter(*exporters)` delivers each batch to every exporter. `aobs.init` takes one
exporter, so this is how several destinations are combined.

### Jaeger all-in-one (example, not exercised by CI)

```bash
docker run --rm -p 16686:16686 -p 4318:4318 jaegertracing/all-in-one:latest
```

Then use `OtlpHttpExporter("http://localhost:4318/v1/traces")` and open http://localhost:16686.
(Older Jaeger 1.x images may need `-e COLLECTOR_OTLP_ENABLED=true`.)

### OpenTelemetry Collector (example, not exercised by CI)

A Collector receives OTLP/HTTP JSON on port 4318 and forwards it wherever its exporters point
(Tempo, Datadog, Honeycomb, ...):

```yaml
receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:4318
exporters:
  otlphttp:
    endpoint: https://tempo.example.internal:4318   # your backend
processors:
  batch: {}
service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [otlphttp]
```

## Span lifecycle: why only completed spans are exported

The aobs wire model sends a span as up to two events: an optional `span_start` (only for spans
still running after `start_delay_s`, so the collector can show in-progress work) and a
self-contained `span_end` that repeats every start field. OTLP has no partial spans: a span is
sent once, complete, and backends do not merge two halves.

So the exporter converts **each `span_end` into one OTLP span** and **skips `span_start`**
(counted as `starts_skipped`). An end never needs its start, so a start and end in different
batches, or a start that was coalesced away, are both handled without buffering spans.

Span events (`span.event(...)`, `aobs.event(...)`) are separate wire events, normally sent in an
earlier batch than their span's end. OTLP carries them inside the span, so the exporter holds them
in memory until the span ends and attaches them then (same batch or a later one).

Trade-offs:

* Spans that never end — the process was killed, or the span was still open at shutdown — never
  reach the OTLP backend. The aobs collector shows them as in progress / incomplete; OTLP cannot.
  (The alternative, exporting each start as a fake zero-length or "unfinished" span, would put
  wrong durations and duplicate spans into the backend.)
* Held span events are bounded by `max_pending_events`; when full, the events of the oldest
  waiting spans are dropped (`events_dropped_buffer_full`). Events still held at exporter
  shutdown are dropped (`events_dropped_unended`, logged as a warning).
* An event recorded after its span was already exported cannot be attached
  (`events_dropped_late`).

## Mapping

### Identifiers and times

| aobs | OTLP | How |
|---|---|---|
| `trace_id` (32 lowercase hex) | `traceId` | Unchanged. aobs ids are already W3C/OTel ids, and OTLP/JSON encodes `traceId`/`spanId` as hex strings (not base64 as generic proto3 JSON would). 16 bytes. |
| `span_id` (16 hex) | `spanId` | Unchanged, 8 bytes. |
| `parent_span_id` | `parentSpanId` | Omitted for a root (`null`). |
| `links[]` | `links[]` | `traceId`/`spanId` unchanged; `relation` → link attribute `aobs.link.relation`. |
| `start_time`, `end_time` (RFC 3339) | `startTimeUnixNano`, `endTimeUnixNano` | Exact integer nanoseconds (no float rounding), sent as decimal strings (int64 in OTLP/JSON). `end_time` is the SDK's `start + monotonic duration`. |
| `span_event.time` | `events[].timeUnixNano` | Same conversion. |

Spans or events with invalid ids or timestamps are skipped and counted (`spans_invalid`,
`events_invalid`); the SDK never produces them, but the exporter accepts any `EncodedBatch`.

### Kind and status

| aobs | OTLP |
|---|---|
| `kind` `llm`, `embedding` | `SPAN_KIND_CLIENT` (a call to a model service) |
| every other kind (`agent`, `tool`, `retrieval`, `step`, custom) | `SPAN_KIND_INTERNAL` (in-process work; OTel GenAI uses INTERNAL for `execute_tool` and in-process agents) |
| `status: error` | `STATUS_CODE_ERROR`; `status.message` = `error.message` if captured |
| `status: ok`, `cancelled` | `STATUS_CODE_UNSET` (OTel asks instrumentation not to set `OK`); the aobs status is kept in `aobs.status` |
| `error.type` | attribute `error.type`, plus an `exception` span event at the end time with `exception.type`, `exception.message` (if captured), `exception.stacktrace` (if captured) |

The original aobs kind is in `aobs.span.kind`; the span name is the aobs name unchanged.

### Span attributes

[OpenTelemetry GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/)
where they apply (status *Development* upstream, so names may still change), `aobs.*` otherwise.
Absent aobs fields produce no attribute (unknown is never sent as zero).

| aobs field | OTLP attribute |
|---|---|
| `llm.provider` | `gen_ai.provider.name` and `gen_ai.system` (deprecated name, kept for backends that still read it) |
| `llm.operation` | `gen_ai.operation.name` (`completion` → `text_completion`; otherwise unchanged) |
| kind `agent` / `tool` / `embedding` | `gen_ai.operation.name` = `invoke_agent` / `execute_tool` / `embeddings` (if no `llm.operation`) |
| `llm.request_model`, `llm.response_model` | `gen_ai.request.model`, `gen_ai.response.model` |
| `llm.request.temperature`, `top_p`, `max_tokens` | `gen_ai.request.temperature`, `gen_ai.request.top_p`, `gen_ai.request.max_tokens` |
| `llm.request.stream` | `aobs.llm.request.stream` |
| `llm.response.id`, `finish_reasons` | `gen_ai.response.id`, `gen_ai.response.finish_reasons` |
| `llm.provider_request_id` | `aobs.llm.provider_request_id` |
| `llm.streaming.*` | `aobs.llm.streaming.time_to_first_token_ms`, `.chunk_count`, `.completed` |
| `usage.input_tokens`, `output_tokens` | `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` |
| `usage.cache_read_input_tokens` | `gen_ai.usage.cache_read.input_tokens` |
| `usage.cache_write_input_tokens` | `gen_ai.usage.cache_creation.input_tokens` |
| `usage.total_tokens`, `reasoning_tokens`, `source` | `aobs.usage.total_tokens`, `aobs.usage.reasoning_tokens`, `aobs.usage.source` |
| `cost.amount`, `currency`, `source` | `aobs.cost.amount` (decimal string), `aobs.cost.currency`, `aobs.cost.source` |
| `tool.name`, `tool.call_id` | `gen_ai.tool.name`, `gen_ai.tool.call.id` |
| `tool.type` | `aobs.tool.type` (aobs vocabulary — `function`, `mcp`, `http`, ... — differs from `gen_ai.tool.type`) |
| `agent.name`, `id`, `version` | `gen_ai.agent.name`, `gen_ai.agent.id`, `gen_ai.agent.version` |
| `session_id` | `gen_ai.conversation.id` |
| `retry.attempt`, `max_attempts` | `aobs.retry.attempt`, `aobs.retry.max_attempts` |
| `input_fingerprint` | `aobs.input_fingerprint` (keyed HMAC, never the input) |
| `error.code`, `origin`, `retryable` | `aobs.error.code`, `aobs.error.origin`, `aobs.error.retryable` |
| `kind`, `status` | `aobs.span.kind`, `aobs.status` |
| `attributes` (user) | passed through unchanged; a structured field above wins on a key collision |
| content | see below |

### Content

Content is converted only when the batch contains it, i.e. when the SDK's capture policy already
captured it (status `captured` or `redacted`); redaction has already happened in the SDK, so the
OTLP value is the redacted value. In `metadata_only` mode (the default) no prompt, response,
argument or result text is exported. `include_content=False` drops captured values as well
(status becomes `not_exported`).

| aobs | OTLP attribute |
|---|---|
| tool span `content.input` / `content.output` | `gen_ai.tool.call.arguments` / `gen_ai.tool.call.result` |
| other spans, format `messages` | `gen_ai.input.messages` / `gen_ai.output.messages` (JSON string of the `{role, parts}` messages) |
| other spans, format `text` / `json` | `aobs.content.input.value` / `aobs.content.output.value` (the text, or a JSON string) |
| `content.*.status`, `format`, `truncated`, `redaction_count` | `aobs.content.<input/output>.status`, `.format`, `.truncated`, `.redaction_count` |

Error messages and stack traces follow `capture_error_messages` / `capture_stack_traces` the same
way: if the batch does not contain them, neither does OTLP.

### Resource and scope

One request per aobs batch: one `resourceSpans` entry, one `scopeSpans` entry with scope
`{"name": "aobs", "version": <SDK version>}`.

| aobs `resource` | OTLP resource attribute |
|---|---|
| `service_name`, `service_version` | `service.name`, `service.version` |
| `environment` | `deployment.environment.name` |
| `sdk.name`, `sdk.version`, `sdk.language` | `telemetry.sdk.name` (`aobs-python`), `telemetry.sdk.version`, `telemetry.sdk.language` |
| `sdk.runtime` | `aobs.sdk.runtime` |
| `capture.mode`, `error_messages`, `stack_traces` | `aobs.capture.mode`, `aobs.capture.error_messages`, `aobs.capture.stack_traces` |
| `integrations[]` | `aobs.integrations` (`name/version` strings) |
| `resource_attributes=` | added last; override the above |

## Delivery

Same rules as the aobs `HttpExporter`, and the SDK's batch processor does the retrying:

* 2xx → success. OTLP `partialSuccess.rejectedSpans` is reported to the processor as rejected
  events (`events_rejected_by_collector` in `aobs.diagnostics()`; counts spans, not wire events).
* 408, 429, 5xx, connection errors and timeouts → retry with backoff; `Retry-After` (seconds)
  honoured up to `retry_backoff_max_s`. A retried batch is converted once and re-sent byte for byte.
* Other 4xx → permanent failure; the batch is dropped and counted.
* Redirects are not followed (they could forward authentication headers). The request timeout is
  `export_timeout_s`.
* A batch with no completed spans (only starts and events) sends no request.

`FanOutExporter` retries per destination: if the aobs collector accepted a batch and the OTLP
endpoint returned 503, only the OTLP endpoint receives the retry (backends are not necessarily
idempotent). The combined result is `retry` while any destination still retries (largest
`Retry-After`), then `failure` if any destination failed permanently — the processor then counts
the batch as dropped even though other destinations received it, so a failure is never hidden —
else `success`. Destinations are called in order, each with the full `export_timeout_s`.

### Diagnostics

The processor's counters (`aobs.diagnostics()`) cover batches as a whole. Conversion-specific
counters are on the exporter: `OtlpHttpExporter.diagnostics()` returns `spans_exported`,
`spans_failed`, `spans_invalid`, `starts_skipped`, `events_attached`, `events_dropped_late`,
`events_dropped_buffer_full`, `events_dropped_unended`, `events_invalid`, `requests` and the
current `pending_events`.

## Limitations

* OTLP/HTTP **JSON** only: no protobuf encoding and no gRPC. Some vendor endpoints accept only
  protobuf; put an OpenTelemetry Collector in between for those.
* Traces only (no OTLP metrics or logs).
* Spans that never end are not exported (see the trade-off above); span events of such spans are
  held in memory (bounded) and dropped at shutdown.
* No W3C `traceparent` propagation helpers yet, so aobs traces are not joined with traces from
  services instrumented by OpenTelemetry SDKs.
* GenAI semantic conventions are *Development* upstream; attribute names follow them as of this
  writing and may need updating. `gen_ai.system` is emitted alongside `gen_ai.provider.name` for
  older backends.
* Content values are exported as JSON strings, not structured attributes, and are only as small as
  the SDK's `max_content_bytes` makes them; some backends truncate long attribute values.
* Not tested against a real Collector, Jaeger, Tempo, Datadog or Honeycomb (see the top of this
  page for what was tested).
