# Architecture — Section 1: Agent Observability

Status: **Accepted for Phase 1** (2026-10-12; revised after the
[pre-Phase-1 review](reviews/2026-10-12-pre-phase-1-review.md)). Working name `aobs`
("agent observability") is a placeholder.

This document describes what we are building first, how the pieces fit, and why. The canonical
event model is specified separately in [event-model.md](event-model.md); the phase plan and
acceptance criteria are in [roadmap.md](roadmap.md); individual decisions are recorded in
[decisions.md](decisions.md).

---

## 1. Starting point (what Phase 0 inspection found)

| Item | Finding |
|---|---|
| Repository | None. Empty workspace, no git history, no uploaded files. This is a greenfield build. |
| Python | 3.13 available; `uv` 0.11 and Poetry 2.4 installed |
| Node | 22.x with npm 10 and pnpm 10 |
| PostgreSQL | 16.x server binaries (`initdb`, `pg_ctl`, `postgres`) and `psql` available |
| Docker | CLI + Compose plugin present, **daemon not running** in this environment |
| Package registries | PyPI, npm and GitHub reachable |

Consequences: we choose the stack (no existing conventions to respect), we ship a Docker Compose
file for users but test here against a natively started Postgres, and the repo must be pushed
somewhere durable (see "Open decisions").

## 2. Shape of the system

```
 ┌────────────────────────── customer application ──────────────────────────┐
 │  agent code ──▶ aobs SDK (Python)                                        │
 │                  ├─ core: runs, spans, context propagation, events        │
 │                  ├─ integrations/: provider & framework adapters          │
 │                  ├─ privacy: capture mode + redactors (before export)     │
 │                  └─ exporter: bounded queue → background batch HTTP       │
 └───────────────────────────────┬──────────────────────────────────────────┘
                                 │  POST /v1/ingest  (JSON, schema_version)
                                 │  later: POST /v1/otlp/traces (Phase 7)
 ┌───────────────────────────────▼──────────────────────────────────────────┐
 │  aobs server (one FastAPI process, horizontally scalable, stateless)      │
 │   api/ ─▶ auth ─▶ ingest pipeline ─────────────▶ storage (repositories)   │
 │            │      validate → normalize → enforce   │                      │
 │            │      privacy → merge → derive cost    ▼                      │
 │            │                                   PostgreSQL                 │
 │            └─▶ query service (runs, traces, filters, aggregates) ◀─┘      │
 └───────────────────────────────┬──────────────────────────────────────────┘
                                 │  GET /v1/projects/{id}/runs, /traces/{id}
 ┌───────────────────────────────▼──────────────────────────────────────────┐
 │  web dashboard (React + TypeScript SPA): run list → trace tree → span    │
 └──────────────────────────────────────────────────────────────────────────┘
```

One deployable backend, one database, one SPA, one SDK. No queue, no stream processor, no
microservices. Each of those can be introduced later behind an existing boundary if measurements
justify it (see §7).

## 3. Repository layout (monorepo)

```
aobs/
├─ schema/                 # THE contract: JSON Schema for the wire format, versioned
│  ├─ v1/ingest.schema.json
│  ├─ fixtures/valid/*.json, fixtures/invalid/*.json   # shared conformance fixtures
│  └─ CHANGELOG.md
├─ sdk/python/             # package `aobs` — stdlib only (built in Phase 2; see docs/sdk.md)
│  ├─ src/aobs/_api.py, _tracer.py      # public API, spans, contextvars propagation
│  ├─ src/aobs/_sanitize.py             # schema-conformant values at the API boundary, secret scrubbing
│  ├─ src/aobs/privacy.py, fingerprint.py, config.py, _diagnostics.py
│  ├─ src/aobs/export/                  # Exporter protocol; processor (queue, batching, retries); http, file, memory
│  ├─ src/aobs/integrations/openai_compat.py   # first provider integration
│  ├─ benchmarks/                       # overhead harness + recorded baselines
│  └─ tests/
├─ server/                 # package `aobs_server`
│  ├─ src/aobs_server/model/      # PURE (no I/O): wire models, version policy, merge, trace assembly
│  ├─ src/aobs_server/api/        # HTTP layer only: routing, request/response models
│  ├─ src/aobs_server/auth/       # API keys, principals, authorize()
│  ├─ src/aobs_server/ingest/     # validation, normalization, privacy enforcement, merge
│  ├─ src/aobs_server/query/      # run list, trace retrieval, filters, keyset pagination
│  ├─ src/aobs_server/pricing/    # versioned price tables + cost estimation
│  ├─ src/aobs_server/storage/    # SQLAlchemy models, repositories, Alembic migrations
│  └─ tests/
├─ web/                    # React + Vite + TypeScript
├─ examples/               # example agents (custom loop + fake model; real provider)
├─ deploy/docker-compose.yml
└─ docs/
```

**Dependency rule:** `api → (auth, ingest, query) → (model, storage)`. `model` imports nothing
else from the server and performs no I/O, so merge and assembly rules are unit-testable and shared
by ingest (merging) and query (tree building). Storage never imports api; ingest and query never
import each other. The SDK never imports server code at runtime; both depend only on `schema/`.
(SDK *tests* may use `aobs_server.model` to assemble what the SDK emitted.)

The Python packages form a `uv` workspace rooted at the repository root.

## 4. Technology choices

| Concern | Choice | Why | Trade-off accepted |
|---|---|---|---|
| Wire contract | JSON Schema (draft 2020-12) in `schema/` | Language-neutral; future SDKs (TS, Go) and third-party clients validate against the same file. | Must keep server Pydantic models in sync — enforced by a test that validates every fixture with both. |
| First SDK | Python ≥ 3.11 (changed from 3.10 in Phase 2: 3.10 reaches end of life in October 2026 per PEP 619, and the uv workspace needs one floor), **stdlib-only core** | Most agent code is Python. `contextvars` gives correct parent tracking across `asyncio`. No dependencies = no version conflicts inside customer apps (Pydantic v1/v2 and httpx pins are a real source of breakage). | We hand-write small pieces (UUIDv7, HTTP via `urllib`/`http.client`) instead of importing them. |
| Backend | FastAPI + Pydantic v2 | Typed validation at the edge, OpenAPI generated for free (drives the TS client), async-capable. | Pydantic validation cost per event; acceptable at pilot scale, measured in Phase 8. |
| DB access | SQLAlchemy 2 Core + Alembic + psycopg 3 | Explicit SQL for bulk upserts (`INSERT … ON CONFLICT`) and keyset pagination; migrations from day one. | More SQL written by hand than with an ORM-heavy approach. |
| Storage | PostgreSQL 16 | See §6. Relational integrity for tenants/keys, JSONB for open-ended attributes, transactions for idempotent merge, one system to operate. | Not a columnar store; large-scale analytics will eventually want ClickHouse or similar. Storage sits behind repositories so the span store can move without touching ingest/query contracts. |
| Frontend | React 19 + Vite + TypeScript, TanStack Query, generated API client | Plain SPA served as static files; no SSR needed for an authenticated tool. | No server components; fine for this product. |
| Tooling | `uv` (Python), `pnpm` (web), `ruff`, `mypy --strict` on SDK, `pytest`, `vitest`, Playwright (Phase 8) | Fast, reproducible lockfiles. | — |
| Local dev | Docker Compose (postgres + server + web) and a no-Docker path (native Postgres) | Docker is the easy path for users; the native path is how we test here and in lightweight CI. | Two documented paths to keep working. |

Library versions verified available on 2026-10-12: FastAPI 0.143, Pydantic 2.14, SQLAlchemy 2.1,
Alembic 1.20, psycopg 3.3, opentelemetry-api 1.45, React 19.3, Vite 8.3. Exact pins go in lockfiles
when each package is created, and APIs are checked against installed versions, not memory.

## 5. Ingestion pipeline

1. **Authenticate** — `Authorization: Bearer aobs_ik_…`. Keys are random 32-byte secrets, stored as
   SHA-256 hashes with a short visible prefix. Keys carry scopes (`ingest`, `read`, `admin`) and
   belong to exactly one project. **The project is derived from the key, never from the payload**,
   so a client cannot write into another project. A key may also be **bound to one environment**;
   a batch whose `resource.environment` differs is rejected (stops a laptop key writing into
   `production`).
2. **Limit** — max body 1 MiB (configurable), max 1,000 events per batch, per-key token-bucket rate
   limit (in-process for one replica; moved to Postgres/Redis only when we run several replicas).
3. **Parse & validate** — strict JSON parsing (no `NaN`/`Infinity`, no duplicate-key surprises),
   then the envelope and every event against the v1 model (`aobs_server.model`). An invalid
   envelope rejects the batch (400); invalid events are rejected individually and the response
   lists `accepted`, `duplicate` and `rejected` with field paths, so one bad span does not drop a
   whole batch (200 with a per-event result body). Rejections are permanent: the SDK must not retry
   them.
4. **Normalize** — attach server `received_at`; flag (never rewrite) suspected clock skew using
   `sent_at` vs `received_at`; preserve unknown fields from *newer* minor versions in `extra`.
5. **Enforce privacy** — the project's capture policy is a *ceiling*: if the project says
   `metadata_only`, content sent by a misconfigured SDK is dropped and counted; the same applies to
   exception messages and stacks. Built-in secret scrubbers (API-key and bearer-token patterns) run
   on error messages and string attributes.
6. **Guard** — events for tombstoned (deleted) traces are dropped and counted; spans beyond the
   per-trace cap (default 10,000) are dropped and the trace is flagged `truncated`.
7. **Merge** — `span_start` and `span_end` for the same span are merged by natural key
   `(project_id, trace_id, span_id)` using the rules in `aobs_server.model.merge` (event-model §9):
   end wins over start; duplicates of the same type are no-ops; *conflicting* duplicates resolve by
   a content-derived order so the result never depends on arrival order. This is what makes SDK
   retries safe.
8. **Derive** — platform cost estimate from the versioned price table (only when model and token
   counts are known); then, for each affected trace, **lock its summary row** (`INSERT … ON
   CONFLICT DO NOTHING` then `SELECT … FOR UPDATE`) and recompute it from stored spans. The lock
   prevents two concurrent batches for the same trace from each writing a summary that misses the
   other's spans.

The whole batch is one transaction: a database failure rolls everything back and the SDK's retry
re-applies it idempotently.

Everything after step 3 is synchronous inside the request in Phases 3–6. If ingest latency or
throughput requires it, steps 4–8 move behind a durable queue (Postgres `SKIP LOCKED` table first,
not Kafka) without changing the API contract.

## 6. Storage design (initial)

Every tenant-owned table leads with `project_id` in its primary key and indexes — that makes
isolation hard to get wrong and makes future partitioning straightforward.

| Table | Purpose | Key points |
|---|---|---|
| `organizations`, `projects`, `api_keys` | Tenancy and auth | `projects.settings` JSONB holds capture policy and retention days |
| `environments` | Catalog of env names seen per project | upserted on ingest; spans carry the name |
| `traces` | One row per trace (= one agent run): status, times, counts, tokens, cost, completeness flags | Powers the run list without scanning spans |
| `spans` | Merged span records | PK `(project_id, trace_id, span_id)`; **no FK on `parent_span_id`** (parents may arrive later or never); hot fields as columns (`kind`, `status`, `started_at`, `duration_ms`, `model`, `provider`, `tool_name`, `agent_name`), the rest in JSONB |
| `span_events` | Point-in-time events (retry, handoff, stream milestones, custom) | idempotent on `event_id` |
| `span_content` | Captured prompts/responses/tool args/results | **Separate table** so it can have stricter retention, independent deletion and tighter access later |
| `resources` | Deduplicated SDK/service/integration descriptors | spans reference by hash |
| `ingest_diagnostics` | Per-key counters: rejected, dropped content, skew detected, tombstone drops, cap drops, merge conflicts | Lets users see why data is missing |
| `trace_tombstones` | `(project_id, trace_id, deleted_at)` | Stops late events from re-creating a deleted trace |

Retention: a periodic job deletes by `(project_id, received_at < cutoff)` — server time, because
client timestamps can be wrong or manipulated. Monthly partitioning of
`spans`/`span_events`/`span_content` is introduced when volume warrants it. Deletion of a single
trace is a first-class API from Phase 3 and writes a tombstone.

## 7. Scaling path (documented, not built)

| Pressure | First response | Later response |
|---|---|---|
| Ingest CPU | More stateless server replicas | — |
| Ingest write latency | Async processing via a Postgres-backed work queue | Dedicated queue (Redis Streams / Kafka) |
| Span table size | Time partitioning + retention | Move span/event store to ClickHouse behind the repository interface |
| Analytics queries | Materialized hourly rollups | Columnar store |

## 8. SDK design

Implemented in Phase 2; the user-facing reference is [sdk.md](sdk.md), measured overhead is in
[benchmarks.md](benchmarks.md).

```python
import aobs
from aobs.integrations import openai_compat
from openai import OpenAI

aobs.init(service_name="support-agent", endpoint="http://localhost:8000",
          environment="dev", capture="metadata_only")       # default capture; AOBS_* env vars work too
client = openai_compat.wrap(OpenAI(), provider="openai")   # explicit wrap, no global patching

@aobs.tool
def get_order_status(order_id: str) -> dict: ...

with aobs.run("answer_ticket", session_id=ticket_id, agent="triage"):
    reply = client.chat.completions.create(model=..., messages=...)
```

Guarantees (each covered by tests in `sdk/python/tests/`):

- **Never breaks the host app.** All exporter and serialization errors are caught; the application's
  own exceptions are recorded and then re-raised unchanged (same object). Misconfiguration raises
  only at `init()` and only with `strict=True`; otherwise the SDK warns and uses a safe default.
- **Schema-conformant by construction.** Every user-supplied value passes `_sanitize` at the API
  boundary (truncated, normalized or dropped and counted); property tests feed arbitrary input and
  validate every emitted batch against the JSON Schema and the collector model.
- **Correct nesting** via `contextvars` (works across `await`, `asyncio.gather`, task groups).
  Threads and executors need `aobs.with_current_context(fn)`; documented and tested.
- **Durations from a monotonic clock** (`perf_counter_ns`); `end_time = start_time + duration`.
- **Finalizer safety:** an abandoned LLM stream is recorded as `cancelled` from `__del__` without
  taking any lock there (work is queued on a reentrant-safe `SimpleQueue` and done by the worker),
  so cyclic garbage collection can never deadlock the SDK.
- **No redirects** in the HTTP exporter (a redirect could forward the `Authorization` header).
- **Bounded memory:** queue of N events (default 10,000); when full, new events are dropped and
  counted, never blocking the app.
- **Delivery:** background thread batches every 1 s or 500 events; retries with jittered backoff on
  network errors, 429 and 5xx; safe because ingest is idempotent.
- **Shutdown:** `atexit` flush with a timeout; `aobs.flush()` and `aobs.shutdown()` for serverless
  and scripts; exporter re-created after `fork()` (gunicorn/celery workers).
- **Observable telemetry failures:** `aobs.diagnostics()` returns counters (queued, sent, dropped,
  retries, last error) and the `aobs` logger emits rate-limited warnings.
- **Partial runs survive:** spans emit `span_start` so a crash or kill still leaves an
  in-progress/incomplete record on the server instead of nothing.
- **Start coalescing:** a `span_start` becomes eligible for export only after `start_delay_s`
  (default 1 s) or at a flush. If the span ends first, the start is discarded and only the
  self-contained `span_end` is sent. Short spans cost one event; long or hung spans still announce
  themselves about a second after they begin.
- **Batches fit the server limit:** the exporter splits batches so each request stays under the
  body and event-count limits, including captured content.

Integration layering (each is optional and isolated under `integrations/`):

1. Manual API (`run`, `span`, `tool`, `event`) — works for any custom agent, always available.
2. Explicit client wrappers for providers (first: OpenAI-compatible Chat Completions, which also
   covers many hosted and local OpenAI-compatible servers).
3. Framework adapters using the framework's documented callback/tracing hooks (Phase 7+).
4. OTLP ingestion on the server for apps already emitting OpenTelemetry GenAI spans (Phase 7).

Every integration declares **capabilities** (e.g. `llm.usage`, `llm.streaming`, `tool.arguments`)
that travel with its telemetry, so the UI can say "this integration cannot report X" rather than
showing a blank.

## 9. Privacy and security model

- **Default capture mode is `metadata_only`**: names, timing, status, model IDs, token counts, error
  types and messages (scrubbed), tool names, and an **HMAC-SHA256 input fingerprint** (lets us
  detect repeated identical calls without storing them). No prompt, response, argument or result
  content.
- Fingerprints are keyed, not plain hashes: a plain SHA-256 of `{"order_id": "1042"}` can be
  reversed by trying every order id. The SDK holds the key — random per process by default (good
  enough to spot repeats within a run), or a configured stable key (needed to compare across runs
  and processes). The server never sees the key. The value carries a key id so fingerprints made
  with different keys are never compared.
- `redacted` mode captures content after SDK-side redactors run (built-in patterns + user callables).
- `full` mode captures content subject to size caps (default 32 KiB per field, flagged `truncated`).
- Exception messages are captured by default (they are the most useful debugging signal) but they
  can contain personal data, so they are always secret-scrubbed and can be turned off with
  `capture_error_messages=False`. Stack traces are captured only with `capture_stack=True`. The
  server's project ceiling applies to both.
- Instrumentation cannot stop a developer from putting content into span **names** or
  **attributes**; the SDK docs say not to, and attribute values are size-limited and scrubbed.
- Project policy on the server is a ceiling over whatever the SDK sends.
- Dashboard renders all user-supplied strings as text (React escaping, no `dangerouslySetInnerHTML`,
  no markdown rendering of captured content in Phase 4).
- No telemetry leaves the deployment: the server makes no outbound calls (pricing is a file in the
  repo, not fetched).
- Authorization goes through one function, `authorize(principal, project_id, action)`, called by
  every query endpoint; cross-project access tests are part of the suite.

## 10. Trace correctness rules

| Situation | Behaviour |
|---|---|
| Child arrives before parent | Stored immediately; tree resolves when the parent arrives |
| Parent never arrives | Child listed in a separate **missing parent** group, labelled; never re-parented by guess |
| Parent pointers form a cycle (incl. a span naming itself) | Cycle members listed as orphans with reason `cycle`; tree walk always terminates |
| `span_start` without `span_end` | `in_progress` until the project's stale threshold (default 15 min), then **incomplete** — never shown as success |
| `span_end` without `span_start` | Accepted; end record is self-contained |
| Duplicate submission | No-op (natural key / `event_id`) |
| Conflicting duplicate (same key, different content) | Deterministic winner (lowest SHA-256 of canonical JSON), conflict counted in diagnostics — same result in every arrival order |
| Child starts before parent / ends after it (cross-host clocks) | Kept as reported, flagged `clock_skew_suspected` |
| Several roots in one trace | Allowed (e.g. fan-out workers); UI shows a forest |
| Async / background work | Child span with link `triggered_by` to the originating span when it runs outside the parent's lifetime |
| Cross-process agents | W3C `traceparent` propagation helpers so a remote agent continues the same trace |

## 11. Risks

| Risk | Area | Mitigation |
|---|---|---|
| Schema shaped around the first provider | Neutrality | Canonical fields chosen from ≥3 provider APIs; provider-only fields go to namespaced `attributes`; a second, differently-shaped adapter is a Phase 7 exit criterion |
| OTel GenAI conventions still change (status *Development*; spec moved to its own repo; past renames such as `gen_ai.system` → `gen_ai.provider.name`) | Interop | Own canonical names; a single mapping module translates to/from `gen_ai.*`; mapping tested |
| Content captured by accident | Privacy | Off by default; server ceiling; secret scrubbing; separate content table |
| Telemetry overhead or crashes in the host app | Adoption | stdlib-only core, bounded queue, no blocking, overhead benchmark in Phase 8 |
| Missing data read as success | Correctness | Explicit absent/unknown semantics; incomplete/orphaned states; token totals marked partial when any LLM span lacks usage |
| Cost numbers trusted when wrong | Trust | Versioned price table with source and date; estimates labelled; never computed with missing inputs |
| Monkey-patching breaks on SDK upgrades | Maintainability | Explicit wrappers on documented public client methods; version range declared per integration; conformance tests per integration |
| Postgres outgrown | Scale | Repository boundary, project-first keys, partitioning plan, columnar migration path |
| Auth scope creep (multi-tenant SaaS vs self-hosted) | Security | Org/project/key model from day 1; user login strategy decided before Phase 4 (open decision) |
| Reversible "anonymous" hashes | Privacy | HMAC fingerprints with an SDK-held key (ADR-011) |
| Deleted data re-created by late events | Privacy / integrity | Tombstones checked at ingest |
| Wrong trace summaries under concurrent ingest | Integrity | Per-trace row lock before recompute |
| Runaway traces (loops) | Cost / availability | Per-trace span cap with `truncated` flag |

## 12. How Section 1 prepares later sections (without building them)

| Future section | What Section 1 provides now |
|---|---|
| 2 Evaluation & testing | Stable `(trace_id, span_id)` addresses for future scores/annotations; agent/prompt/model versions on spans; captured content (when enabled) to build datasets |
| 3 Security & guardrails | Separate content path with redaction hook; tool name + input fingerprint on every tool span; reserved `guardrail` span kind and `policy.*` attribute namespace |
| 4 Reliability | Retry attempts, timeouts and error origins as fields; incomplete-span detection; input fingerprints for loop detection |
| 5 Cost & performance | Usage with provenance; versioned pricing with calculation basis stored per span |
| 6 Integrations | Versioned schema, adapter contract, capability declarations, OTel-compatible IDs |
| 7 Incident investigation | `service_version` (code), `agent.version`, `aobs.prompt.version` attribute, model IDs for correlation; full trace retrieval API |
| 8 Governance | Org → project → environment hierarchy, scoped and environment-bound keys, per-project retention and capture policy, tombstones, single authorization choke point |

## 13. Readiness for the strategic differentiators

These are explored after Section 1's core works (roadmap Phases 9–11). The question for now is only
whether v1 blocks them or forces a rewrite. It does not; the table shows what each relies on.

| Differentiator | Already in v1 (and why it is needed for Section 1 anyway) | Added later as a backward-compatible minor version | Not needed at all |
|---|---|---|---|
| 1. Adaptive telemetry | Capture mode per resource; content in a separate block and table; start coalescing | Sampling metadata on the root span (e.g. rate and decision reason). Old data defaults to "recorded at rate 1" | Per-span sampling flags |
| 2. Smart failure capture | `span_start` lifecycle (crashed runs visible); per-span `status`/`error`; content only on `span_end`, so a decision made when *that span* ends ("this call failed, keep its input") works today | `capture_reason` on content (e.g. `policy` vs `failure_escalation`). Deciding when the *whole run* ends needs either SDK-side buffering of the run's content or a content-attach event for already-ended spans; Phase 9 measures which costs less | A separate "evidence" store |
| 3. Agent waste detection | `input_fingerprint` (HMAC) for repeated identical tool/LLM calls; monotonic `duration_ms`; token usage with provenance; ordered siblings | Nothing in the wire schema — this is analysis over stored spans | Storing raw arguments to compare them |
| 4. Overhead measurement | SDK `diagnostics()` counters (Phase 2); internal benchmark harness (Phases 2 & 8) | An SDK self-measurement event type (queue depth, bytes sent, export time) | — |
| 5. Evidence-based optimization | Stable trace/span IDs; `service_version`, `agent.version`, prompt version attribute, model IDs to compare before/after | Nothing in the wire schema — this is analysis + recorded experiments | — |

Invariant that keeps this safe: **absence always has a defined meaning**. When sampling metadata
arrives in a later minor version, old data (without it) is interpreted as unsampled, so aggregates
stay correct. Analytics in Phase 6 therefore report over "recorded runs" and say so.

## 14. Open decisions

Tracked in [decisions.md](decisions.md) under "Pending".
