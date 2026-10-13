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
- [ ] Pending decisions answered (decisions.md → Pending) — defaults in use until answered

A design review before Phase 1 corrected several contradictions and gaps; see
[reviews/2026-10-12-pre-phase-1-review.md](reviews/2026-10-12-pre-phase-1-review.md).

## Phase 1 — Canonical events & core data model ✅ (2026-10-12)

Build: `schema/v1/ingest.schema.json` + `SCHEMA_LOCK`; `schema/CHANGELOG.md`; strict JSON parsing;
server-side Pydantic models and version policy in the pure `aobs_server.model` package; fixtures
(success, failed tool, nested multi-agent, incomplete, out-of-order, duplicate/conflicting,
newer-minor unknown fields, ~20 invalid cases); deterministic merge; trace assembler (events → tree,
lifecycles, derived trace status, orphans/cycles, clock-skew flags, token totals with completeness).
*Moved to Phase 2:* SDK serialization (the SDK package does not exist yet).

Acceptance (all verified by tests in `server/tests/`; 146 passed on Python 3.13 and 3.11):
- [x] Every valid fixture passes both the JSON Schema and the Pydantic models; every invalid fixture
      fails both, at the expected field path (`test_conformance.py`; 10 valid, 43 invalid).
- [x] Differential testing: JSON Schema and Pydantic agree on accept/reject for every single-field
      mutation of every valid fixture, plus random multi-mutations (`test_differential.py`).
      Shown to catch four planted drifts (null accepted, negative counts, length limit, extra fields).
- [x] Trace assembler produces identical results for 200 random permutations (with injected
      duplicates) of each fixture's events (`test_order_independence.py`). Shown to catch the
      Phase 0 "first end wins" rule.
- [x] Conflicting duplicates resolve to the same winner in every order, and are counted (`test_merge.py`).
- [x] Missing usage yields `null` + `complete=false` in aggregates; no zero-filling (`test_assembly.py`).
- [x] Unknown fields on a newer-minor payload are preserved in `extra`; the same fields on a `1.0`
      payload are rejected; an unknown major is rejected (`test_versioning.py`).
- [x] Cycles (incl. 2,000-span and self-parent) and 5,000-deep chains terminate (`test_assembly.py`).
- [x] A test fails if the schema file changes without a `SCHEMA_LOCK` + CHANGELOG update
      (`test_schema_lock.py`).
- [x] JSON examples in `event-model.md` are validated (`test_docs_examples.py`).

Known gaps carried forward: the SDK serializer joins the conformance suite in Phase 2; size limits
that JSON Schema cannot express (content parts) are enforced at ingest in Phase 3.

## Phase 2 — Python SDK & example agent ✅ (2026-10-12, one criterion partly met)

Build: `aobs` package (stdlib-only core): `init`, `run`, `span`, `@tool`, `@agent`, `event`,
`flush`, `shutdown`, `diagnostics`; contextvars propagation; exporter (bounded queue, batching,
size-aware batch splitting, backoff, start coalescing); capture modes, redactors and HMAC input
fingerprints; file/console exporter for offline use; `integrations/openai_compat` explicit wrapper
(sync, async, streaming); deterministic fake model for tests; `examples/support_agent` (LLM + 2
tools, a forced-failure mode); SDK serializer added to the Phase 1 conformance suite;
**overhead micro-benchmark harness** (per-span CPU time, allocations, bytes per event) kept in the
repo because Phase 9 depends on it.

Acceptance (tests in `sdk/python/tests/`; full repository suite 294 passed on Python 3.13 and 3.11):
- [x] Instrumenting the example takes ≤ 10 statements — exactly 10 including 4 imports
      (`test_sdk_example.py`).
- [x] Example produces a valid success trace and a valid failed trace, validated by the Phase 1
      model: tool and LLM spans parented to the run, error on the failing tool and the run
      (`test_sdk_example.py`).
- [x] Nesting is correct under `asyncio.gather` of 50 concurrent runs (`test_sdk_relationships.py`).
- [x] With the collector unreachable, the app's results and exceptions are unchanged; retries are
      bounded at `max_export_attempts`; `diagnostics()` reports drops; the queue never exceeds its
      bound and producers are never blocked (`test_sdk_http.py`, `test_sdk_processor.py`).
- [x] `metadata_only` mode emits zero content bytes (`test_sdk_privacy.py`).
- [x] Fingerprints of the same input match within a process and differ across keys; the key never
      appears in any payload, repr or diagnostics, in raw/hex/base64 form (`test_sdk_fingerprint.py`).
- [x] Every SDK-emitted batch passes the JSON Schema and the collector model — including 300
      property-generated scenarios of arbitrary user input (`test_sdk_conformance.py`).
- [~] Overhead < 50 µs median per span: **met for an empty span (~26 µs), not met for a decorated
      tool call (~57 µs)** on the reference VM. Baseline and method recorded in
      [benchmarks.md](benchmarks.md); profiled candidates listed there, to be evaluated before Phase 9.

Changes from the plan: SDK floor raised to Python 3.11 (architecture §4); the `openai_compat`
integration is tested against `openai` 3.29.0 only; no console exporter (JSONL file + in-memory
cover offline use).

## Phase 3 — Ingestion API & storage (in progress: ingestion and trace reads done 2026-10-13)

Build: FastAPI app; Alembic migrations for tables in architecture.md §6; API keys with scopes and
optional environment binding (create via CLI); `POST /v1/ingest`; idempotent merge reusing
`aobs_server.model.merge`; trace summary maintenance under a per-trace lock; tombstones; per-trace
span cap; size and rate limits, including limits JSON Schema cannot express (64 KiB per content
part); error format (RFC 9457 problem+json); trace deletion endpoint;
retention job on `received_at`; `GET /v1/projects/{id}/traces/{trace_id}`.

Status: everything up to `POST /v1/ingest`, merge, summaries, tombstone checks, the span cap,
size and event-count limits and problem+json errors is built ([ingestion.md](ingestion.md),
ADR-015), and so is `GET /v1/projects/{id}/traces/{trace_id}` with a span-event listing
([traces-api.md](traces-api.md), ADR-016). Not built yet: rate limits, the trace deletion
endpoint and the retention job.

Acceptance:
- [x] SDK example → server → stored; retrieved trace equals the SDK-side assembled trace
      (`test_ingest_sdk_contract.py`: SDK → HTTP ingest → HTTP `GET` of the trace).
- [ ] Restarting the server and Postgres loses nothing that was acknowledged. (By design the
      transaction commits before the 200 is sent; no restart test exists yet.)
- [x] Re-sending the same batch 5× leaves row counts unchanged; response reports duplicates
      (trace data rows; each batch adds one `ingest_diagnostics` row).
- [x] Key for project A gets 404 (not 403, to avoid existence leaks) on every read of project B
      (both read endpoints; also without the `read` scope; `test_traces_api.py`).
- [x] Bodies > limit → 413; malformed JSON → 400; invalid event → listed in `rejected`, others stored.
- [x] Content sent to a `metadata_only` project is not persisted (asserted at the DB level).
- [x] Contract test: the SDK's `HttpExporter` against the real ingest API — retry/permanent/
      `rejected` semantics match ADR-014.
- [x] Two concurrent batches for the same trace produce a summary equal to a from-scratch
      recomputation (concurrency test with two connections).
- [x] Events for a deleted trace arriving after deletion do not re-create it (tombstone written
      directly in the test; the deletion endpoint is not built yet).
- [x] An environment-bound key cannot write another environment.
- [x] Tests run against real PostgreSQL 16, not SQLite.

## Phase 4 — Query API & dashboard

Build: run list endpoint (filters: environment, status, agent, time range, has error, model, tool,
text search on name; keyset pagination); React SPA: project/env picker, run list, trace view (tree
+ timeline), span detail panel with error, usage, cost, capture-state badges; loading/empty/error
states; generated TS client. Dashboard authentication: read-scoped project keys (self-hosted
default from decision P2); user login is added in Phase 8, or earlier if P2 is answered "SaaS".

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
ingest throughput and query latency baselines; **end-to-end overhead harness** (an agent workload
run with instrumentation off vs on, measuring added latency, CPU, memory, bytes sent and bytes
stored per run); server health/readiness endpoints and metrics; docs: quickstart, SDK reference,
API reference, configuration, architecture, known limitations.

Acceptance (= Section 1 Definition of Done):
- [ ] Someone new follows the quickstart on a clean machine and reaches a trace in the UI.
- [ ] Full test suite green in CI on the supported matrix (Python 3.10–3.13, PostgreSQL 16).
- [ ] Baselines recorded: spans/s ingested per replica, p95 run-list and trace-load latency at 1M spans.
- [ ] Overhead baseline recorded for the example workload with the harness (repeatable command,
      results with variance, committed).

---

# After Section 1: strategic differentiators

These start only once Phases 1–8 work end to end. Each one is an **experiment with a measurable
hypothesis**, evaluated with the Phase 8 harness on recorded workloads, and is kept only if the
measurement supports it. Dependency order: measurement exists before anything claims to reduce
overhead.

## Phase 9 — Prototype: adaptive telemetry + smart failure capture

Plan before code: workloads (example agent plus at least one realistic multi-step agent), the
baseline (Phase 8 numbers), the policy to test (e.g. metadata-only by default; escalate to content
capture for a run after an error, a retry, or a duration/cost outlier), and success thresholds.

Build: SDK capture policy hook; minor schema version adding sampling/capture-decision metadata with
defined defaults for old data; decide (by measurement) between SDK-side run buffering and a
content-attach event.

Acceptance:
- [ ] Measured: bytes sent and stored per *normal* run drop by a stated target vs. always-on
      content capture, with added latency within a stated budget.
- [ ] Measured: for injected failures, the evidence needed to diagnose them (inputs of the failing
      span and its predecessors) is present in ≥ a stated percentage of failed runs.
- [ ] Aggregates remain correct when sampling metadata is present or absent.

## Phase 10 — Agent waste detection

Rules over stored spans, each transparent and individually testable: repeated identical calls
(same kind + name/model + `input_fingerprint` within a run), tool-call loops, retries that never
succeed, sequential calls that could have run in parallel (non-overlapping independent siblings),
cache-eligible repeated prompts. Every finding links to the spans that prove it and an estimated
saving labelled as an estimate.

Acceptance:
- [ ] Seeded traces with known waste produce exactly the expected findings (no false positives on
      the clean fixtures).
- [ ] Each finding's estimated saving is reproducible from the linked spans.

## Phase 11 — Monitoring-overhead measurement (user-facing)

Build: SDK self-measurement events (minor schema version); per-project overhead view: added latency
per span, SDK CPU and memory, bytes sent, bytes stored, dropped events.

Acceptance:
- [ ] Reported overhead matches the Phase 8 harness within a stated tolerance on the same workload.

## Later — Evidence-based optimization

Recommendations derived from Phase 10 findings, applied by the user, then verified by comparing
before/after windows (same agent, `service_version` or `agent.version` change) with a stated
statistical method. Planned after Phases 9–11 produce real data.

---

## Test strategy

| Layer | What | Tools |
|---|---|---|
| Contract | Shared fixtures validated by JSON Schema, server models, and SDK serializer; differential fuzzing between schema and models | pytest, `jsonschema`, Hypothesis |
| Correctness | Order-independence, merge rules, conflict resolution, derived statuses, cycles | Hypothesis property tests |
| SDK | Context propagation (async, threads), never-raise guarantee, queue bounds, flush/shutdown, fork, capture modes | pytest, fake transport, fake model |
| Server | Ingest, idempotency, limits, authz isolation, retention, privacy ceiling | pytest + real Postgres (native `pg_ctl` here; container in CI) |
| End-to-end | Example agent → server → query API → UI | pytest + Playwright |
| Security | XSS fixtures, secret scrubbing, cross-project access matrix | pytest, Playwright |
| Performance | SDK per-span overhead; end-to-end overhead harness; ingest throughput; query latency on seeded data | pytest-benchmark, scripted load |

No test calls a paid LLM API by default. Real-provider tests are opt-in via an environment variable.
