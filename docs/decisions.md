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

---

## Pending decisions

| # | Question | Default if unanswered | Why it matters |
|---|---|---|---|
| P1 | Where should the repository live? (GitHub repo to push to) | Keep building in this session workspace and hand over files | Work in this workspace is not a durable home |
| P2 | First deployment target: self-hosted for one team, or hosted multi-tenant SaaS? | Self-hosted single-organization; org/project model still built in | Decides whether real user login (OIDC/password) is needed by Phase 4 or can wait until Phase 8 |
| P3 | Which LLM provider/API key do you have for the live example? | OpenAI-compatible adapter; tests use the fake model; example can also run against a local OpenAI-compatible server | Determines the first real-provider test |
| P4 | Product/package name | `aobs` placeholder | Renaming a published Python package later is disruptive; cheap to settle before Phase 2 |
