# Trace ingestion: `POST /v1/ingest`

The collector accepts schema-v1 batches over authenticated HTTP. It applies the project's privacy
policy and stores the result in PostgreSQL, retry-safely.

**Works now:**
- authenticated ingestion;
- per-event validation results;
- privacy enforcement before storage;
- idempotent, order-independent merging;
- per-trace locking for concurrent writers;
- trace summaries;
- the SDK's `HttpExporter` talking to it.

**Not built yet:**
- a run list or search **over HTTP** (one trace can be read by id: [traces-api.md](traces-api.md)).
  The CLI and the dashboard can list and analyze stored traces: [stored-analysis.md](stored-analysis.md);
- rate limiting, trace deletion and the retention job.

See [What is not supported yet](#what-is-not-supported-yet). This is a working milestone, not a
production-hardened service.

## Quick start

**1. PostgreSQL 16.** Any PostgreSQL 16 server you can create a database on works. For example,
with Docker:

```bash
docker run -d --name aobs-pg -e POSTGRES_USER=aobs -e POSTGRES_PASSWORD=change-me \
  -e POSTGRES_DB=aobs -p 127.0.0.1:5432:5432 postgres:16
export AOBS_DATABASE_URL=postgresql://aobs:change-me@127.0.0.1:5432/aobs
```

`postgres://`, `postgresql://` and `postgresql+psycopg://` URLs are accepted.
`uv run aobs-server config` prints the effective settings with the password masked.

**2. Create the schema, an organization, a project and a key:**

```bash
uv run aobs-server db upgrade
uv run aobs-server org create --name acme                    # prints organization_id
uv run aobs-server project create --org <organization_id> --name support-bot \
  --capture-ceiling metadata_only                            # prints project_id
uv run aobs-server key create --project <project_id> --name support-bot-dev \
  --scopes ingest --environment dev                          # prints api_key ONCE
```

- **Keep the printed `api_key` (`aobs_ik_…`).** Only its SHA-256 hash is stored, so it cannot be
  shown again.
- `--environment dev` binds the key to the `dev` environment. A batch for any other environment
  gets 403. Leave the flag out to allow every environment.
- `aobs-server key list --project …` lists keys without their secrets.
- `aobs-server key revoke --key-id …` revokes a key immediately.

**3. Start the collector:**

```bash
uv run aobs-server serve                 # http://127.0.0.1:8000, --host/--port to change
```

It refuses to start if `AOBS_DATABASE_URL` is unset, the database is unreachable, or the schema is
not at the latest migration.

**4. Send traces.** With the Python SDK:

```python
import aobs

aobs.init(service_name="support-bot", environment="dev", endpoint="http://127.0.0.1:8000", api_key="aobs_ik_…")
```

Or set `AOBS_ENDPOINT` and `AOBS_API_KEY`. To try it with the example agent:

```bash
AOBS_API_KEY=aobs_ik_… uv run python examples/support_agent/run.py --endpoint http://127.0.0.1:8000
```

Any HTTP client works too, for example curl with a recorded batch:

```bash
head -1 examples/diagnose/traces/refund_outage.jsonl | curl -s -X POST http://127.0.0.1:8000/v1/ingest \
  -H "Authorization: Bearer $AOBS_API_KEY" -H "Content-Type: application/json" --data-binary @-
```

`GET /healthz` reports that the process is up. `GET /readyz` additionally checks the database.

## Request

```
POST /v1/ingest
Authorization: Bearer aobs_ik_…
Content-Type: application/json
```

The body is one schema-v1 envelope: `schema_version`, optional `batch_id`, `sent_at`, `resource`
and 1 to 1000 `events`. The format is defined in [event-model.md](event-model.md) and
[`schema/v1/ingest.schema.json`](../schema/v1/ingest.schema.json); this API does not add or
change any field.

**The project is always the API key's project.** The wire format has no project field. An
unknown envelope or resource field such as `project_id` is a validation error (400), so a client
cannot write into another project by editing the payload. A trace id used in two projects is two
unrelated traces.

Compressed bodies (`Content-Encoding: gzip`) are not accepted yet.

## Response

A processed batch always gets **200**, even if some or all events were rejected:

```json
{
  "batch_id": "01a134fb-f7c4-7721-a13f-81d864389d03",
  "received_at": "2026-10-13T11:18:10.668000Z",
  "counts": {"received": 9, "accepted": 6, "duplicate": 1, "dropped": 0, "rejected": 2},
  "conflicts": 0,
  "rejected": [
    {"index": 2, "errors": [{"path": "events/2/span_id", "type": "string_pattern_mismatch",
                             "message": "span_id does not match ^(?!0{16}$)[0-9a-f]{16}$"}]},
    {"index": 4, "errors": [{"path": "events/4/duration_ms", "type": "greater_than_equal",
                             "message": "Input should be greater than or equal to 0"}]}
  ],
  "dropped": [],
  "trace_ids": ["8086b1aaf2235e7c0580b082ca0d8133"],
  "clock_skew_suspected": false,
  "notices": {"content_dropped_policy": 2}
}
```

**`counts`:** every event gets exactly one outcome, and the outcomes add up to `received`.

| Outcome | Meaning |
|---|---|
| `accepted` | New information, stored. |
| `duplicate` | An identical copy was already stored, or appeared earlier in this batch. Nothing changed. |
| `dropped` | Valid, but not stored because of server state. The reason is listed in `dropped`: `trace_deleted`, `trace_span_limit` or `trace_span_event_limit`. |
| `rejected` | Invalid. The errors are listed in `rejected`, each with a slash-separated `path` relative to the batch, an error `type` and a message. |

**Other fields:**
- **Rejections are permanent; do not resend those events.** The SDK counts them and never retries
  them.
- **`conflicts`:** accepted events that differ from an already-stored copy of the same event. See
  [Duplicates, conflicts and retries](#duplicates-conflicts-and-retries).
- **`notices`:** non-zero counters for what the server did, for example:
  - `content_dropped_policy`, `content_dropped_oversize`;
  - `error_messages_dropped_policy`, `stack_traces_dropped_policy`;
  - `secrets_scrubbed`, `personal_data_redacted`, `nul_characters_replaced`, `unknown_fields_dropped_policy`;
  - `clock_skew_suspected`, `newer_schema_minor`.

  The same counters, plus duplicates, conflicts and rejections, are recorded per batch in
  `ingest_diagnostics`.

## Errors

Errors are [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) `application/problem+json`, with
`type`, `title`, `status` and `detail`:

| Status | `type` (`urn:aobs:problem:…`) | When | Client should |
|---|---|---|---|
| 400 | `invalid-batch` | Body is not strict JSON (no `NaN`, duplicate keys or lone surrogates), not an object, or the envelope is invalid; `errors` lists up to 20 field errors | Fix the sender; do not retry |
| 400 | `unsupported-schema-version` | Unknown major version | Upgrade the server or downgrade the SDK |
| 401 | `unauthorized` | Key missing, malformed, unknown or revoked; one identical answer for all four | Fix the key |
| 403 | `forbidden` | Key lacks the `ingest` scope, or is bound to another environment | Use the right key |
| 404 / 405 | `not-found` / `http-error` | Unknown path / wrong method | — |
| 413 | `payload-too-large` | Body over `max` bytes (`"limit": "body_bytes"`) or more than `max` events (`"limit": "events_per_batch"`) | Send smaller batches |
| 415 | `unsupported-media-type` | Not `Content-Type: application/json`, or a `Content-Encoding` | Fix the request |
| 500 | `internal-error` | Unexpected server or database error; nothing was stored | Retry later (safe) |
| 503 | `service-unavailable` | Database unreachable, a lock wait over 10 s, or a deadlock or another transient database error; has `Retry-After` | Retry after the delay (safe) |

These match the SDK's contract (ADR-014): 408, 429 and 5xx are retried, and other 4xx are
permanent. Error bodies never contain stack traces, SQL, driver messages, file paths or the API
key. Client-supplied fragments that appear in field errors, such as an unknown key name, are
secret-scrubbed and cut to 300 characters.

## Semantics

### Batches

- **The envelope is all-or-nothing.** An invalid envelope rejects the whole batch with 400, and
  nothing is stored.
- **Events are individual.** An invalid event is listed in `rejected`; the others are processed.
- **Storage is atomic per batch.** All accepted events of a batch are written in one database
  transaction. If anything fails, nothing from the batch is stored and the client gets 500 or 503,
  so the retry starts from a clean state.
- **A batch may hold events of several traces.** Each trace is merged and summarized on its own.

### Duplicates, conflicts and retries

An event's identity is its **natural key** within the project:
- span start: `(trace_id, span_id, "span_start")`;
- span end: `(trace_id, span_id, "span_end")`;
- span event: `event_id`.

Its **content** is the SHA-256 of its canonical JSON after the privacy policy. `batch_id` and
`sent_at` are not part of an event's identity: the SDK rebuilds `sent_at` on every attempt, and a
retried batch is just the same events again.

- **Duplicate:** same key, same content. Changes nothing and is reported as `duplicate`.
  Resending a batch any number of times leaves the stored data unchanged.
- **Conflict:** same key, different content, for example two SDK versions describing one span
  differently. The copy with the **lowest digest** wins, whatever the arrival order (ADR-009).
  Both copies count as seen, and the trace's `conflicts` counter goes up. Up to 16 distinct
  copies per event are remembered, always including the winner. A 17th or later distinct copy is
  reported as `accepted`, but cannot win unless its digest is lower.
- **A span event id belongs to its first trace.** The first stored copy fixes the event's trace
  and span. A later copy that names a different trace or span is `rejected` with type
  `event_id_conflict`; this is a client bug, because event ids are random UUIDs.

**This is not exactly-once delivery.** The SDK delivers at least once (it retries on timeouts and
5xx), and the collector makes repeats harmless. Exceptions:
- A retry arriving after the project's privacy settings changed can differ from the first copy.
  It is then counted as a conflict, not a duplicate.
- An acknowledgement lost in transit makes the SDK resend data that was already stored, which is
  reported as duplicates.

### Order, late arrivals and concurrency

- **Any order works.** An end may arrive before its start, and children before their parent. A
  span whose parent has not arrived yet is an orphan until it does. Late events are merged
  whenever they come; there is no time window. Every batch recomputes the summary of each trace it
  touched from all stored spans and events, so the stored trace never depends on arrival order or
  batch boundaries. A test feeds the same trace in several orders and compares the stored rows.
- **Unfinished traces** are stored with status `open` and a `stale_reference_time`. At read time
  an open trace is `in_progress` while younger than the project's `stale_after_s` (default 15
  min), then `incomplete`, as in the trace model. The read API applies this
  ([traces-api.md](traces-api.md)).
- **Concurrent requests for the same trace are serialized.** Each batch locks the row of every
  trace it touches, in trace-id order, before writing, so two batches for one trace are applied one
  after the other. Two batches sharing several traces cannot deadlock. A batch that waits more than
  10 s for a lock gets 503 and can retry. Tests force two connections onto one trace, and send about 30
  batches through up to 8 concurrent HTTP clients, both onto one trace and onto overlapping traces.

### What is stored, redacted or omitted

The project's settings are applied **before** anything is written:
- `--capture-ceiling`: `metadata_only` (default), `redacted` or `full`;
- `--error-messages` / `--no-error-messages`;
- `--stack-traces` / `--no-stack-traces`.

| Data | Stored? |
|---|---|
| Span identity, timing, status, kind, names, model/provider/tool names, token counts, provider-reported cost, links, retry info, attributes | Yes. String attributes are secret-scrubbed. |
| Prompts, responses, tool arguments, tool results (`content`) | Only up to the project ceiling. Above it, a part is stored as `{"status": "not_captured"}`. Under `redacted`, SDK-redacted parts are kept and scrubbed again; `captured` parts are dropped because the server cannot verify they were redacted. Parts over 64 KiB are dropped. Content lives in its own table, `span_content`. |
| Error messages | If the project allows them (default yes), with secrets, email addresses and card numbers removed. |
| Stack traces | Only if the project allows them (default no), with secrets, email addresses and card numbers removed. |
| Fields from a newer schema minor | Only under a `full` ceiling (the server cannot tell whether they hold content). |
| NUL characters | Replaced with U+FFFD, because PostgreSQL cannot store them. |
| API key | Never. Only its hash, in `api_keys`. |
| Request headers, raw request body, client IP | Never. |

Secret scrubbing replaces API keys, bearer tokens, private keys, cloud and chat tokens, JWTs,
`password=`-style assignments and URL credentials with `[REDACTED:<kind>]`.

Logs contain project id, key id, batch id and counts. They never contain the key, event content
or SQL parameters: the engine hides parameters, and `serve` keeps SQLAlchemy's logger at WARNING.
Database errors are logged as exception type plus SQLSTATE.

### Clock skew

Client timestamps are never rewritten. If `sent_at` differs from the server's receive time by more
than `AOBS_CLOCK_SKEW_THRESHOLD_S` (default 300 s), the response says `clock_skew_suspected:
true` and the batch is counted. All `*_received_at` columns use server time.

### Deleted traces

Events for a trace with a row in `trace_tombstones` are dropped (`trace_deleted`), so late events
cannot re-create a deleted trace. The deletion API that writes tombstones is not built yet.

## Limits

| Limit | Default | Change with | Exceeded → |
|---|---|---|---|
| Request body | 1 MiB | `AOBS_MAX_BODY_BYTES` (1 KiB – 16 MiB) | 413 `body_bytes`. Checked against `Content-Length` before the key is looked up, and while reading chunked bodies. |
| Events per batch | 1000 | `AOBS_MAX_EVENTS_PER_BATCH` (1 – 1000; the schema allows at most 1000) | 413 `events_per_batch`. Checked right after JSON parsing, before validation. |
| Spans per trace | 10,000 | project `--max-spans-per-trace` | New spans are dropped (`trace_span_limit`) and the trace is flagged `truncated`; existing spans can still be updated. |
| Span events per trace | 100,000 | project setting `max_span_events_per_trace` | Dropped (`trace_span_event_limit`), trace flagged `truncated`. |
| Content part | 64 KiB | — | Part stored as `not_captured` (`content_dropped_oversize`). |
| Field sizes (names, attributes, error message 8 KiB, stack 32 KiB, …) | wire schema | — | Event rejected. |
| Lock wait / statement | 10 s / 30 s | — | 503 with `Retry-After`. |

## Running the tests

The ingestion tests use a real PostgreSQL 16, never SQLite. By default the test harness
(`server/tests/pg.py`) starts a **throwaway cluster** with `initdb`. It listens on a Unix socket in
a temporary directory only (no TCP port) and is deleted afterwards. It needs the PostgreSQL 16
server binaries (on Debian/Ubuntu: `apt-get install postgresql-16`). Alternatively, set
`AOBS_TEST_DATABASE_URL` to a server where the user may create databases; only `aobs_test_*`
databases are created and dropped there.

```bash
uv run pytest -m pg                                   # every PostgreSQL test
uv run pytest server/tests/test_ingest_api.py         # auth, isolation, validation, limits, privacy, retries, order
uv run pytest server/tests/test_ingest_concurrency.py # lock contention, concurrent clients, no deadlocks
uv run pytest server/tests/test_ingest_sdk_contract.py # SDK HttpExporter against the real API
uv run pytest server/tests/test_ingest_cli.py         # `aobs-server serve` start-up checks
```

## What is not supported yet

- **Listing and searching stored traces over HTTP.** One trace can be read by id
  ([traces-api.md](traces-api.md)); there is no HTTP run list or search yet (Phase 4). The CLI
  (`traces list`, `db:` selections) and the dashboard (`--project`) read stored traces directly
  from the database: [stored-analysis.md](stored-analysis.md).
- **Rate limiting:** no per-key limits, so the collector never answers 429.
- **Trace deletion API and the retention job.** Tombstones are honoured, but nothing creates them
  yet, and nothing deletes data older than `retention_days`.
- **Compressed bodies**, OTLP/OpenTelemetry ingestion, and SDKs other than Python (any language
  can POST the JSON format).
- **Performance work.**
  - Each batch recomputes the summary of every trace it touches from all of that trace's stored
    spans, so the cost grows with trace size (bounded by the 10,000-span cap).
  - Every request does one indexed key lookup; there is no key cache.
  - No throughput or latency benchmark exists yet.
- **TLS:** `serve` speaks plain HTTP and listens on 127.0.0.1 by default. Put a TLS-terminating
  proxy in front before exposing it. The SDK warns when a key is sent in plain text to a
  non-local host.
- **Several collector replicas** are safe for correctness, because the locks are in PostgreSQL.
  This has not been load-tested.
