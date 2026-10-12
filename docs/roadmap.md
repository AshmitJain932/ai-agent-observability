# Section 1 Roadmap — Phases, Checklists, Acceptance Criteria

Each phase ends with a runnable codebase, updated docs, and a report of commands run and results.
A phase is closed only when its acceptance criteria pass or the gap is written down under
"Known gaps" in that phase's report. Criteria are phrased so a test or a command can check them.

---

## Phase 0 — Inspection & architecture ✅ (this document set)

- [x] Environment inspected (greenfield; toolchain inventory in architecture.md §1)
- [x] Architecture, module boundaries, stack rationale
- [x] Canonical event model v1 draft with example trace
- [x] Risks, future-section mapping, phase plan, test strategy
- [ ] Pending decisions answered (decisions.md → Pending)

## Phase 1 — Canonical events & core data model

Build: `schema/v1/ingest.schema.json`; server-side Pydantic models; SDK dataclasses; fixtures
(`success_run`, `failed_tool_run`, `nested_multi_agent`, `incomplete_run`, `out_of_order`,
`duplicate_batch`, `unknown_fields_minor_bump`, ~15 invalid cases); `schema/CHANGELOG.md`;
compatibility policy; a pure-Python trace assembler (events → tree + derived statuses).

Acceptance:
- [ ] Every valid fixture passes both the JSON Schema and the Pydantic models; every invalid fixture
      fails both, with the same field path reported (single conformance test).
- [ ] Trace assembler produces identical trees for 200 random permutations of each fixture's events
      (property test).
- [ ] Missing usage yields `null` + `complete=false` in aggregates; a test asserts no zero-filling.
- [ ] Unknown top-level fields on a `1.1` payload are preserved in `extra` and do not fail validation.
- [ ] CI check fails if the schema file changes without a version/CHANGELOG change.

## Phase 2 — Python SDK & example agent

Build: `aobs` package (stdlib-only core): `init`, `run`, `span`, `@tool`, `@agent`, `event`,
`flush`, `shutdown`, `diagnostics`; contextvars propagation; exporter (bounded queue, batching,
backoff); capture modes and redactors; file/console exporter for offline use;
`integrations/openai_compat` explicit wrapper (sync, async, streaming); deterministic fake model
for tests; `examples/support_agent` (LLM + 2 tools, a forced-failure mode).

Acceptance:
- [ ] Instrumenting the example takes ≤ 10 added lines (counted in the example's diff).
- [ ] Example produces a valid success trace and a valid failed trace, checked against Phase 1
      fixtures' assembler: correct parent ids for tool spans under the run, error on the failing span.
- [ ] Nesting is correct under `asyncio.gather` of 50 concurrent runs (no cross-run parent leaks).
- [ ] With the server unreachable, the app's results and exceptions are unchanged; `diagnostics()`
      reports drops/failures; memory stays under the queue bound.
- [ ] `metadata_only` mode emits zero content bytes (test scans serialized payloads).
- [ ] Overhead: < 50 µs median per span on the dev machine (micro-benchmark recorded, not a CI gate).

## Phase 3 — Ingestion API & storage

Build: FastAPI app; Alembic migrations for tables in architecture.md §6; API keys (create via CLI);
`POST /v1/ingest`; idempotent merge; trace summary maintenance; size and rate limits; error format
(RFC 9457 problem+json); trace deletion endpoint; retention job; `GET /v1/projects/{id}/traces/{trace_id}`.

Acceptance:
- [ ] SDK example → server → stored; retrieved trace equals the SDK-side assembled trace.
- [ ] Restarting the server and Postgres loses nothing that was acknowledged.
- [ ] Re-sending the same batch 5× leaves row counts unchanged; response reports duplicates.
- [ ] Key for project A gets 404 (not 403, to avoid existence leaks) on every read of project B.
- [ ] Bodies > limit → 413; malformed JSON → 400; invalid event → listed in `rejected`, others stored.
- [ ] Content sent to a `metadata_only` project is not persisted (asserted at the DB level).
- [ ] Tests run against real PostgreSQL 16, not SQLite.

## Phase 4 — Query API & dashboard

Build: run list endpoint (filters: environment, status, agent, time range, has error, model, tool,
text search on name; keyset pagination); React SPA: project/env picker, run list, trace view (tree
+ timeline), span detail panel with error, usage, cost, capture-state badges; loading/empty/error
states; generated TS client.

Acceptance:
- [ ] From a fresh `docker compose up` (or native script), running the example shows the run in
      the UI within 5 s of flush.
- [ ] Failed span reachable in ≤ 2 clicks from the run list ("jump to first error").
- [ ] Filter tests: each filter has an API test comparing results with a direct SQL count.
- [ ] A 5,000-span trace renders without freezing (virtualized list; Playwright timing check).
- [ ] Unknown/estimated/redacted/not-captured values are visibly distinct (component tests).
- [ ] No captured string is rendered as HTML (XSS fixture test).

## Phase 5 — LLM, tool & agent details

Build: richer openai_compat capture (request params, finish reasons, provider request ids,
streaming lifecycle, retries, timeouts); handoff events and links; content capture end-to-end with
truncation and redaction reporting; capability declarations shown in UI.

Acceptance:
- [ ] Each content state (captured/redacted/truncated/not_captured/unavailable) has a fixture and
      renders distinctly.
- [ ] Turning capture off at SDK or project level removes content from storage (DB assertion).
- [ ] Provider-specific attributes round-trip without schema changes.
- [ ] Streaming call abandoned mid-stream ends as `cancelled` with partial chunk count.

## Phase 6 — Cost, latency & analytics

Build: versioned price table (`pricing/*.yaml` with source URL and effective date); per-span cost
estimate with basis; aggregates (p50/p95 latency by kind/model/tool, error rate, token and cost
sums with completeness); time-range charts that answer a diagnostic question; slow/expensive run
flags with documented, configurable thresholds.

Acceptance:
- [ ] Aggregates over seeded data equal independently computed values (test).
- [ ] Every cost shown can be reproduced from stored usage × the named price-table version.
- [ ] Partial data is labelled (e.g. "≥ $0.42, 3 of 10 calls unpriced").

## Phase 7 — Universal integration architecture

Build: written adapter contract + capability vocabulary; integration conformance test kit;
**second, differently-shaped integration** (candidate: Anthropic Messages API or a framework hook,
chosen by demand); OTLP/HTTP ingestion endpoint mapping GenAI semantic conventions; HTTP API docs
for non-Python clients.

Acceptance:
- [ ] Second integration added with zero changes to `schema/` major version or DB tables.
- [ ] Both integrations pass the same conformance kit.
- [ ] A trace emitted by a standard OpenTelemetry GenAI instrumentation appears correctly mapped.
- [ ] Each integration page lists what it cannot capture.

## Phase 8 — Hardening & first release

Build/verify: failure-path tests (network drop mid-batch, server 5xx storms, missing ends,
duplicates, malformed and oversized payloads, 10k-span traces); auth and redaction test sweep;
ingest throughput and query latency baselines; server health/readiness endpoints and metrics;
docs: quickstart, SDK reference, API reference, configuration, architecture, known limitations.

Acceptance (= Section 1 Definition of Done):
- [ ] Someone new follows the quickstart on a clean machine and reaches a trace in the UI.
- [ ] Full test suite green in CI on the supported matrix (Python 3.10–3.13, PostgreSQL 16).
- [ ] Baselines recorded: spans/s ingested per replica, p95 run-list and trace-load latency at 1M spans.

---

## Test strategy

| Layer | What | Tools |
|---|---|---|
| Contract | Shared fixtures validated by JSON Schema, server models, and SDK serializer | pytest, `jsonschema` |
| Correctness | Order-independence, merge rules, derived statuses | Hypothesis property tests |
| SDK | Context propagation (async, threads), never-raise guarantee, queue bounds, flush/shutdown, fork, capture modes | pytest, fake transport, fake model |
| Server | Ingest, idempotency, limits, authz isolation, retention, privacy ceiling | pytest + real Postgres (native `pg_ctl` here; container in CI) |
| End-to-end | Example agent → server → query API → UI | pytest + Playwright |
| Security | XSS fixtures, secret scrubbing, cross-project access matrix | pytest, Playwright |
| Performance | SDK per-span overhead; ingest throughput; query latency on seeded data | pytest-benchmark, scripted load |

No test calls a paid LLM API by default. Real-provider tests are opt-in via an environment variable.
