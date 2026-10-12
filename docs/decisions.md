# Decision Log

Short architecture decision records. Status: **Accepted** (in force), **Proposed** (default we
will use unless overruled), **Pending** (needs the founder's input).

---

## ADR-001 — Own canonical schema, OpenTelemetry-compatible at the edges · Proposed
**Context.** OTel is the standard for distributed tracing, and its GenAI conventions cover models,
tools and agents. But those conventions are still marked *Development*, have renamed attributes
before, and OTLP exports spans only on completion (crashed or still-running work is invisible).
**Decision.** Native JSON event schema with `span_start`/`span_end`/`span_event`; trace/span IDs in
W3C/OTel format; a mapping module to and from `gen_ai.*`; an OTLP ingestion endpoint in Phase 7.
**Consequence.** One more format to maintain, in exchange for in-flight visibility, explicit
provenance/unknown semantics, and insulation from upstream renames.

## ADR-002 — PostgreSQL as the initial store · Proposed
**Context.** Pilot-scale volume (up to tens of millions of spans), strong need for tenant isolation,
transactional idempotent merges, and flexible attributes.
**Decision.** PostgreSQL 16 with JSONB attributes, project-first keys, repositories as the only DB
access path.
**Consequence.** Simple operations now; columnar analytics store later if measured volume demands.

## ADR-003 — Content capture off by default · Proposed
**Decision.** Default `metadata_only`; `redacted` and `full` are explicit opt-ins; project policy on
the server is a ceiling; content stored in its own table.
**Consequence.** Less debugging detail out of the box; users enable content per environment.

## ADR-004 — One run = one trace · Proposed
**Decision.** A run's root span is the trace root; sessions group runs via `session_id`; detached
background work gets its own trace plus a link.
**Consequence.** Simple queries and direct OTel mapping. Very long-lived agents (hours) are one large
trace; revisit if that becomes common.

## ADR-005 — stdlib-only Python SDK core · Proposed
**Decision.** No third-party runtime dependencies in `aobs` core; integrations are optional extras.
**Consequence.** Zero dependency conflicts in user apps; we maintain a small HTTP transport ourselves.

## ADR-006 — Explicit wrappers, not global monkey-patching · Proposed
**Decision.** Provider integrations wrap a client instance the user passes in
(`wrap(OpenAI())`) and only touch documented public methods.
**Consequence.** One line of user code per client, in exchange for predictable behaviour across
provider SDK upgrades.

## ADR-007 — First provider integration: OpenAI-compatible Chat Completions · Proposed
**Context.** It is the most widely implemented request shape (many hosted providers and local
servers expose it), so one adapter gives broad early reach and a free local test path.
**Decision.** First adapter targets that shape, plus a deterministic fake model for tests. The second
adapter (Phase 7) is deliberately a *different* shape to prove neutrality.

## ADR-008 — Synchronous ingest pipeline, no queue · Proposed
**Decision.** Validate-merge-store inside the request. Introduce a Postgres-backed work queue only
when ingest latency measurements require it.

## ADR-009 — Order-independent conflict resolution · Accepted (2026-10-12)
**Context.** Retries, parallel exporters and out-of-order delivery mean "the first event received"
is not a meaningful notion, yet the stored trace must not depend on arrival order.
**Decision.** Identical duplicates are no-ops. When two events of the same type for the same span
(or the same `event_id`) differ, the one whose canonical JSON (sorted keys, no whitespace) has the
lowest SHA-256 wins, and the conflict is counted. Between a span's start and end, end wins field by
field (deep merge of objects; arrays replaced).
**Consequence.** Fully deterministic and testable by permutation. The winner of a genuine conflict
is arbitrary-but-stable — acceptable because conflicts indicate a bug upstream and are surfaced.

## ADR-010 — Strict validation for known versions, lenient only for newer minors · Accepted (2026-10-12)
**Context.** Rejecting all unknown fields breaks older servers when SDKs upgrade; accepting all of
them hides typos.
**Decision.** The server knows a maximum minor per major. Payload minor ≤ known: unknown fields are
errors. Payload minor > known: unknown fields are moved into `extra` (with their paths) and
counted. Unknown major: batch rejected. Fields that future minors may extend (`kind`,
`llm.operation`, `tool.type`, `error.origin`, link `relation`, capability names) are open,
pattern-checked vocabularies; closed enums (`status`, content `status`, usage `source`, capture
`mode`) change only in a major version.
**Consequence.** Typos fail fast in tests; an old server keeps accepting a new SDK.

## ADR-011 — Keyed (HMAC) input fingerprints · Accepted (2026-10-12)
**Context.** Waste and loop detection need to know that two calls had identical inputs without
storing the inputs. Unkeyed hashes of low-entropy inputs are reversible by enumeration.
**Decision.** `input_fingerprint = "hmac-sha256:<key_id>:<hex>"` over a canonical JSON encoding of
the input. Key held by the SDK: random per process by default, or a configured stable key for
cross-process/cross-run comparison. Never sent to the server.
**Consequence.** Default fingerprints only compare within one process (enough for in-run repeats).
Cross-run waste analysis requires users to configure a stable key — documented in Phase 10.

## ADR-012 — SDK requires Python ≥ 3.11 · Accepted (2026-10-12)
**Context.** Phase 0 planned 3.10. Python 3.10 reaches end of life in October 2026 (PEP 619), and the
uv workspace resolves one `requires-python` for all members (the server needs 3.11).
**Decision.** `aobs` requires Python ≥ 3.11; CI tests 3.11 and 3.13.
**Consequence.** Users on 3.10 cannot install the SDK; they are on an unsupported Python anyway.

## ADR-013 — Exporters receive the encoded wire batch · Accepted (2026-10-12)
**Context.** The SDK must not be tied to one transport, and tests must see exactly what a collector
would receive.
**Decision.** `Exporter.export(EncodedBatch, timeout_s) -> ExportResult` where `EncodedBatch.body`
is the complete schema-v1.0 envelope bytes, rebuilt per attempt (fresh `sent_at`, same
`batch_id`). Outcomes: `success`, `retry` (with optional `retry_after_s`), `failure`. Exceptions
count as `retry`.
**Consequence.** Every transport (HTTP, file, in-memory, future ones) is conformance-testable by
validating `body`; encoding happens once, on the worker thread.

## ADR-014 — Ingest response contract the SDK relies on · Accepted (2026-10-12)
**Context.** The SDK's retry behaviour depends on how the collector answers; Phase 3 must honour it.
**Decision.** 2xx = batch processed; the body may be a JSON object whose `rejected` array lists
events rejected individually (counted by the SDK, never retried). 408/429/5xx = retry
(`Retry-After` in seconds honoured). Other 4xx = permanent failure. 3xx is never followed.
**Consequence.** Phase 3 adds a contract test running the SDK's `HttpExporter` against the real API.

---

## Pending decisions

Not yet answered; the defaults are in use as of Phase 2 (2026-10-12). None of them affected Phase 1 or 2
code. P1 and P4 should be settled before the SDK is published to a package index (not done
in Phase 2); P2 before Phase 4.

| # | Question | Default if unanswered | Why it matters |
|---|---|---|---|
| P1 | Where should the repository live? (GitHub repo to push to) | Keep building in this session workspace and hand over files | Work in this workspace is not a durable home |
| P2 | First deployment target: self-hosted for one team, or hosted multi-tenant SaaS? | Self-hosted single-organization; org/project model still built in | Decides whether real user login (OIDC/password) is needed by Phase 4 or can wait until Phase 8 |
| P3 | Which LLM provider/API key do you have for the live example? | OpenAI-compatible adapter; tests use the fake model; example can also run against a local OpenAI-compatible server | Determines the first real-provider test |
| P4 | Product/package name | `aobs` placeholder | Renaming a published Python package later is disruptive; cheap to settle before Phase 2 |
