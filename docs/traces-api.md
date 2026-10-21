# Reading stored traces: `GET /v1/projects/{project_id}/traces/{trace_id}`

A read-only API that returns one trace stored by the collector ([ingestion.md](ingestion.md)).
There are two endpoints:
- the trace itself: summary, tree structure and its spans, paginated;
- its span events, paginated in time order.

Both are served by `aobs-server serve`, next to `POST /v1/ingest`.

**Not here yet:**
- a run list or search;
- deletion;
- the web dashboard over this data. The dashboard still reads JSONL files.

These are next on the [roadmap](roadmap.md).

## Quick start

Create a key with the `read` scope for the project (an `ingest` key cannot read):

```bash
uv run aobs-server key create --project <project_id> --name trace-reader --scopes read
```

Then read a trace whose id you know. Trace ids appear in each ingest response's `trace_ids`, and
in the SDK's spans:

```bash
curl -s "http://127.0.0.1:8000/v1/projects/<project_id>/traces/<trace_id>" \
  -H "Authorization: Bearer $AOBS_READ_KEY"
curl -s "http://127.0.0.1:8000/v1/projects/<project_id>/traces/<trace_id>/events?limit=100" \
  -H "Authorization: Bearer $AOBS_READ_KEY"
```

## `GET /v1/projects/{project_id}/traces/{trace_id}`

| Query parameter | Default | Meaning |
|---|---|---|
| `limit` | 500 | Spans per page, 1 – 1000 |
| `cursor` | none | The `page.next_cursor` of the previous page (a span id) |
| `include_content` | `false` | `true` adds content *values*, as far as the project's privacy settings allow (see below) |

Response (200) for the recorded `retry_storm` sample, with `limit=1` (token totals other than
`input_tokens` are cut for space):

```json
{
  "project_id": "1575c6fe-bc0c-450c-b49b-10da76b58fb7",
  "trace_id": "501fc29c3f1bf956230907f096a84610",
  "summary": {
    "status": "error",
    "started_at": "2026-10-13T07:59:05.821709Z",
    "ended_at": "2026-10-13T07:59:05.943875Z",
    "root_name": "restock_check",
    "agent_name": "inventory_agent",
    "session_id": null,
    "span_count": 5,
    "open_span_count": 0,
    "error_count": 4,
    "span_event_count": 0,
    "kind_counts": {"agent": 1, "llm": 1, "tool": 3},
    "tokens": {
      "input_tokens": {"value": null, "reported_spans": 0, "expected_spans": 1, "estimated": false, "complete": false}
    },
    "conflicts": 0,
    "orphan_count": 0,
    "truncated": false,
    "environments": ["staging"],
    "first_received_at": "2026-10-12T10:00:30Z",
    "last_received_at": "2026-10-12T10:00:30Z"
  },
  "roots": ["2b3a965163f20f67"],
  "orphans": [],
  "orphan_event_count": 0,
  "spans": [
    {
      "span_id": "2b3a965163f20f67",
      "parent_span_id": null,
      "kind": "agent",
      "name": "restock_check",
      "start_time": "2026-10-13T07:59:05.821709Z",
      "end_time": "2026-10-13T07:59:05.943875Z",
      "duration_ms": 122.16661,
      "status": "error",
      "lifecycle": "complete",
      "children": ["819499666f1004f0", "cd4121b3911fdb5c", "96722b833007c842", "ab6091696c288ad2"],
      "orphan_reason": null,
      "flags": [],
      "fields": {
        "trace_id": "501fc29c3f1bf956230907f096a84610",
        "span_id": "2b3a965163f20f67",
        "parent_span_id": null,
        "kind": "agent",
        "name": "restock_check",
        "start_time": "2026-10-13T07:59:05.821709Z",
        "agent": {"name": "inventory_agent", "version": "0.3.1"},
        "end_time": "2026-10-13T07:59:05.943875Z",
        "duration_ms": 122.16661,
        "status": "error",
        "error": {"type": "RuntimeError", "message": "inventory lookup failed after 3 attempts", "origin": "application"}
      },
      "event_count": 0
    }
  ],
  "page": {"limit": 1, "next_cursor": "2b3a965163f20f67"},
  "content": "omitted"
}
```

**`summary`:** the stored trace summary.
- `status` is one of `ok`, `ok_with_errors`, `error`, `cancelled`, `in_progress` or `incomplete`.
- An unfinished trace is `in_progress` while it is younger than the project's `stale_after_s`
  (default 15 min), then `incomplete`. This is decided at read time with the server's clock.
- Token totals never count a missing value as zero ([event-model.md](event-model.md) §6).

**`spans`:** the trace's spans, assembled by the same code the SDK tests and the offline tools use.
- **Fields:** each span has its tree links (`parent_span_id`, `children`, `orphan_reason`),
  `lifecycle` (`complete`, `in_progress` or `incomplete`) and `flags` (for example
  `clock_skew_suspected` or `conflicting_duplicates`). `fields` holds every merged field of the
  span's start and end.
- **Order:** spans come in span-id order. `roots` and `orphans` describe the whole trace on every
  page, so a client can fetch all pages and build the tree.
- **Event counts:** `event_count` counts the span's events. `orphan_event_count` counts events
  whose span was never stored.

**Pagination:**
- `next_cursor` is `null` on the last page.
- Pages never overlap or skip a span, as long as the trace does not change between requests.
- Each request reads one consistent snapshot. Data that another transaction commits while the
  request runs is not visible to it, so a page's summary and spans always agree. Separate requests
  (different pages) can see different snapshots if the trace changes in between.

## `GET /v1/projects/{project_id}/traces/{trace_id}/events`

| Query parameter | Default | Meaning |
|---|---|---|
| `limit` | 500 | Events per page, 1 – 1000 |
| `cursor` | none | `page.next_cursor` of the previous page (opaque; pass it back unchanged) |
| `span_id` | none | Only this span's events |

```json
{
  "project_id": "1575c6fe-bc0c-450c-b49b-10da76b58fb7",
  "trace_id": "501fc29c3f1bf956230907f096a84610",
  "span_id": null,
  "events": [
    {
      "event_id": "00000000-0000-4000-8000-000000000001",
      "span_id": "2b3a965163f20f67",
      "name": "retry",
      "time": "2026-10-12T10:00:00.200000Z",
      "attributes": {"attempt": 2}
    }
  ],
  "page": {"limit": 500, "next_cursor": null}
}
```

Events are ordered by time, with ties broken by event id.

## Errors

The same problem+json format as ingestion ([ingestion.md](ingestion.md#errors)):

| Status | When |
|---|---|
| 400 `bad-request` | `limit` out of range, `include_content` not a boolean, or a malformed `cursor` or `span_id`. `detail` names the parameter. |
| 401 `unauthorized` | Key missing, malformed, unknown or revoked |
| 403 `forbidden` | The key belongs to this project but lacks the `read` scope |
| 404 `not-found` | Anything that is not a stored trace in **the key's own project** (see below) |
| 500 / 503 | A database error. 503 is transient and carries `Retry-After`. The body never includes SQL, driver messages or table names. |

## Tenant isolation

- **A key reads only its own project.** The project in the URL must be the key's project; this is
  checked before the database is queried for any trace. Every query also filters on that project
  id, so a trace id that exists in two projects is two unrelated traces.
- **One identical 404 for everything not found.** These all get the same body and status:
  - another project's id, whether or not that project or trace exists;
  - a project id that is not a UUID;
  - a trace id that is malformed or unknown in the key's project;
  - a deleted (tombstoned) trace.

  A response never says whether something exists elsewhere. A key without the `read` scope still
  gets 404, not 403, for other projects.
- **Environment-bound keys** restrict what a key may *write*. A read key bound to `prod` can still
  read all of its project's traces.

## Privacy

- **Content values are omitted by default.** Without `include_content=true`, a span's `content`
  shows only metadata: `status`, `format`, `truncated` and `redaction_count`. For example
  `{"input": {"status": "captured", "format": "text"}}` says a prompt exists without showing it.
- **Fields from newer schema versions are treated as content.** A span's `fields.__extra__` holds
  fields the server does not know (from a newer schema minor). It is stored only under a `full`
  ceiling, because such fields may hold content. Like content values, it is returned only with
  `include_content=true`, and only while the project's ceiling is still `full`.
- **The project's current privacy settings apply to every read.** Stored events go through the
  same privacy step as ingestion, using the project's settings *as they are now*. Lowering the
  capture ceiling, or turning off error messages or stack traces, therefore also hides data
  stored before the change: content above the ceiling reads as `not_captured`. Secrets are
  scrubbed again on the way out.
- **Nothing is stored above the ceiling.** Content above the project's ceiling at ingest time was
  never stored ([ingestion.md](ingestion.md#what-is-stored-redacted-or-omitted)), so
  `include_content=true` can only show what the project allowed.
- **Reads cannot write.** Each request runs in a `READ ONLY` transaction; a write attempted inside
  it fails, and the request returns 500 without changing anything.
- **Logs carry no trace data or keys.** The collector logs one line per read:
  `read project=<project id> key=<key id> trace|span_events`. No log record contains content,
  error messages, stack traces, attribute values, trace ids or API keys, and SQL parameters are
  hidden. When run with `aobs-server serve`, the HTTP server's access log additionally records
  each request's path and query string. That includes the project id and trace id (identifiers,
  not content); keys are sent in a header and are never logged.

## Limits and performance

- Pages hold at most 1000 spans or 1000 events.
- To build a page of spans, the server assembles the whole trace (at most 10,000 spans by
  default). Reading a very large trace page by page therefore repeats that work for every page.
  Nothing has been benchmarked yet.
- There is no rate limiting.

## Tests

```bash
uv run pytest server/tests/test_traces_api.py              # isolation, scopes, pagination, privacy, status
uv run pytest server/tests/test_ingest_sdk_contract.py     # SDK run -> HTTP ingest -> HTTP read equals the SDK-side trace
```

Both need PostgreSQL 16; see [ingestion.md](ingestion.md#running-the-tests).
