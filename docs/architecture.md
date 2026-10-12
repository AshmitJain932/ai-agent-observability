# Architecture — Section 1: Agent Observability

Status: **Phase 0 proposal** (2026-10-12). Working name `aobs` ("agent observability") is a placeholder.

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
├─ sdk/python/             # package `aobs` — stdlib-only core, optional extras per integration
│  ├─ src/aobs/core/       # tracer, span, context, ids, clock
│  ├─ src/aobs/privacy/    # capture modes, redactors
│  ├─ src/aobs/export/     # queue, batcher, http transport, diagnostics
│  ├─ src/aobs/integrations/openai_compat/   # first provider adapter
│  └─ tests/
├─ server/                 # package `aobs_server`
│  ├─ src/aobs_server/api/        # HTTP layer only: routing, request/response models
│  ├─ src/aobs_server/auth/       # API keys, principals, authorize()
│  ├─ src/aobs_server/ingest/     # validation, normalization, privacy enforcement, merge
│  ├─ src/aobs_server/query/      # run list, trace assembly, filters, keyset pagination
│  ├─ src/aobs_server/pricing/    # versioned price tables + cost estimation
│  ├─ src/aobs_server/storage/    # SQLAlchemy models, repositories, Alembic migrations
│  └─ tests/
├─ web/                    # React + Vite + TypeScript
├─ examples/               # example agents (custom loop + fake model; real provider)
├─ deploy/docker-compose.yml
└─ docs/
```

**Dependency rule:** `api → (auth, ingest, query) → storage`. Storage never imports api; ingest
and query never import each other. The SDK never imports server code; both depend only on
`schema/`.

## 4. Technology choices

| Concern | Choice | Why | Trade-off accepted |
|---|---|---|---|
| Wire contract | JSON Schema (draft 2020-12) in `schema/` | Language-neutral; future SDKs (TS, Go) and third-party clients validate against the same file. | Must keep server Pydantic models in sync — enforced by a test that validates every fixture with both. |
| First SDK | Python ≥ 3.10, **stdlib-only core** | Most agent code is Python. `contextvars` gives correct parent tracking across `asyncio`. No dependencies = no version conflicts inside customer apps (Pydantic v1/v2 and httpx pins are a real source of breakage). | We hand-write small pieces (UUIDv7, HTTP via `urllib`/`http.client`) instead of importing them. |
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
   so a client cannot write into another project.
2. **Limit** — max body 1 MiB (configurable), max 1,000 events per batch, per-key token-bucket rate
   limit (in-process for one replica; moved to Postgres/Redis only when we run several replicas).
3. **Validate** — envelope and every event against the v1 model. Invalid events are rejected
   individually; the response lists `accepted`, `duplicate` and `rejected` with reasons, so one bad
   span does not drop a whole batch (HTTP 207-style body with 200 status).
4. **Normalize** — clamp/flag clock skew, compute missing duration only when both timestamps
   exist (marked `derived`), attach server `received_at`, preserve unknown fields in `extra`.
5. **Enforce privacy** — the project's capture policy is a *ceiling*: if the project says
   `metadata_only`, content sent by a misconfigured SDK is dropped and counted. Built-in secret
   scrubbers (API-key and bearer-token patterns) run on error messages and string attributes.
6. **Merge** — `span_start` and `span_end` for the same span are merged by natural key
   `(project_id, trace_id, span_id)` with deterministic rules (end wins for final fields, attributes
   union, status never moves backwards). Re-sent events are no-ops. This is what makes SDK retries
   safe.
7. **Derive** — platform cost estimate from the versioned price table (only when model and token
   counts are known), then refresh the per-trace summary row for affected traces in the same
   transaction.

Everything after step 3 is synchronous inside the request in Phases 3–6. If ingest latency or
throughput requires it, steps 4–7 move behind a durable queue (Postgres `SKIP LOCKED` table first,
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
| `ingest_diagnostics` | Per-key counters: rejected, dropped content, skew detected | Lets users see why data is missing |

Retention: a periodic job deletes by `(project_id, started_at < cutoff)`; monthly partitioning of
`spans`/`span_events`/`span_content` is introduced when volume warrants it. Deletion of a single
trace is a first-class API from Phase 3.

## 7. Scaling path (documented, not built)

| Pressure | First response | Later response |
|---|---|---|
| Ingest CPU | More stateless server replicas | — |
| Ingest write latency | Async processing via a Postgres-backed work queue | Dedicated queue (Redis Streams / Kafka) |
| Span table size | Time partitioning + retention | Move span/event store to ClickHouse behind the repository interface |
| Analytics queries | Materialized hourly rollups | Columnar store |

## 8. SDK design

```python
import aobs
from openai import OpenAI

aobs.init(api_key=os.environ["AOBS_API_KEY"], endpoint="http://localhost:8000",
          environment="dev", capture="metadata_only")       # default capture
client = aobs.integrations.openai_compat.wrap(OpenAI())    # explicit wrap, no global patching

@aobs.tool()
def get_order_status(order_id: str) -> dict: ...

with aobs.run("answer_ticket", session_id=ticket_id, agent="triage"):
    reply = client.chat.completions.create(model=..., messages=...)
```

Guarantees:

- **Never breaks the host app.** All exporter and serialization errors are caught; the application's
  own exceptions are recorded and then re-raised unchanged. Misconfiguration raises only at
  `init()` and only with `strict=True`.
- **Correct nesting** via `contextvars` (works across `await`, `asyncio.gather`, task groups).
  Threads and executors need `aobs.context.attach()`/`copy_context()`; documented, and tested.
- **Durations from a monotonic clock** (`perf_counter_ns`); wall-clock timestamps are for placement.
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
  types, tool names and a SHA-256 *fingerprint* of tool arguments (lets us detect repeated identical
  calls later without storing them). No prompt, response, argument or result content.
- `redacted` mode captures content after SDK-side redactors run (built-in patterns + user callables).
- `full` mode captures content subject to size caps (default 32 KiB per field, flagged `truncated`).
- Exception messages and stack traces are treated as content-adjacent: messages are scrubbed for
  secrets always; stack traces are captured only when `capture_stack=True`.
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
| Parent never arrives | Child shown under the root as **orphaned**, labelled; never re-parented by guess |
| `span_start` without `span_end` | `in_progress` until the project's stale threshold (default 15 min), then **incomplete** — never shown as success |
| `span_end` without `span_start` | Accepted; end record is self-contained |
| Duplicate submission | No-op (natural key / `event_id`) |
| Conflicting duplicate (same key, different final values) | First end wins, conflict counted in diagnostics |
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

## 12. How Section 1 prepares later sections (without building them)

| Future section | What Section 1 provides now |
|---|---|
| 2 Evaluation & testing | Stable `(trace_id, span_id)` addresses for future scores/annotations; agent/prompt/model versions on spans; captured content (when enabled) to build datasets |
| 3 Security & guardrails | Separate content path with redaction hook; tool name + argument fingerprint on every tool span; reserved `guardrail` span kind and `policy.*` attribute namespace |
| 4 Reliability | Retry attempts, timeouts and error origins as fields; incomplete-span detection; argument fingerprints for loop detection |
| 5 Cost & performance | Usage with provenance; versioned pricing with calculation basis stored per span |
| 6 Integrations | Versioned schema, adapter contract, capability declarations, OTel-compatible IDs |
| 7 Incident investigation | `service_version` (code), `agent.version`, `prompt.version`, model IDs on every span for correlation; full trace retrieval API |
| 8 Governance | Org → project → environment hierarchy, scoped keys, per-project retention and capture policy, single authorization choke point |

## 13. Open decisions

Tracked in [decisions.md](decisions.md) under "Pending".
