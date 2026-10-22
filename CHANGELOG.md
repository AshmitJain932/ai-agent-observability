# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) once it makes releases.
**No version has been published yet**: both packages, `aobs` (the SDK) and `aobs-server` (the
collector and tools), are at `0.1.0.dev0` and are installed from a clone of the repository.
Changes to the wire schema are recorded separately in [schema/CHANGELOG.md](schema/CHANGELOG.md).

## [Unreleased]

Everything that exists today. Status per component, and what is planned, is in
[docs/roadmap.md](docs/roadmap.md).

### Added

#### Event model and wire schema

- Canonical event schema v1.0 (`schema/v1/ingest.schema.json`, normative), with a schema lock
  and 57 conformance fixtures (valid, invalid, versioning).
- Event semantics in `docs/event-model.md`: `span_start`, `span_end` and `span_event`; merge
  rules, derived states, unknown-value rules and the version policy.
- Model library (`aobs_server.model`): strict JSON parsing, Pydantic wire models, version
  policy, order-independent merge, and trace assembly with lifecycles, orphans, cycles,
  clock-skew flags and token totals that never count unreported usage as zero.

#### Python SDK (`aobs`)

- Standard library only, Python 3.11 to 3.13: `aobs.run`, `aobs.span`, `@aobs.tool`,
  `@aobs.agent`, `aobs.llm_span` and span events, with context propagation for threads and
  `asyncio`.
- Privacy: capture modes `metadata_only` (default), `redacted` and `full`; secret and
  personal-data redaction of error messages and stacks; keyed HMAC input fingerprints whose key
  never leaves the process.
- Delivery: recording never blocks or raises; bounded queue, start coalescing, size-aware
  batching, bounded jittered retries, flush and shutdown deadlines, fork safety, and diagnostics
  counters for everything dropped.
- Exporters: HTTP (to the collector), JSONL file and in-memory, and a small interface for your
  own.
- `aobs.integrations.openai_compat`: OpenAI-compatible Chat Completions clients, sync and async,
  streaming and non-streaming, with usage, request ids and errors. Tested with `openai` 3.29.0
  through its own mock HTTP transport.
- `py.typed`, so type checkers use the SDK's annotations.

#### Action safety (`aobs.safety`, part of the SDK)

- A guard in front of tools with side effects: a TOML policy checks the request against trusted
  evidence (required and matching fields, amount limits, approval above a threshold, one action
  per key) before anything runs.
- Durable records before the side effect (`MemoryActionStore`, `SqliteActionStore`), duplicate
  prevention, no blind retries of an unknown outcome, recovery by inspection, reconciliation or
  resolution, and stable provider idempotency keys (`IdempotencyKeys`).
- Decisions and outcomes recorded as `guardrail` spans, without evidence values, keys or secrets.
- Not exactly-once, and the SQLite store coordinates one host only; the guarantees and limits are
  in `docs/action-safety.md`.

#### Collector (`aobs-server`)

- PostgreSQL 16 schema with Alembic migrations (`aobs-server db upgrade`).
- Tenancy: organisations, projects, API keys with scopes (only a hash of each key is stored) and
  a single authorization decision point; CLI commands to manage them.
- Ingestion API (`aobs-server serve`, `POST /v1/ingest`): API-key authentication, privacy
  enforcement before storage, retry-safe idempotent merge, per-trace locking for concurrent
  writers, trace summaries and per-event results. No rate limiting, retention job or built-in TLS
  yet.
- Trace read API (`GET /v1/projects/{project_id}/traces/{trace_id}` and `.../events`):
  read-scoped keys, the same 404 for every trace outside the key's project, content only on
  request and under the project's current privacy settings, pagination. No run list or search
  yet.

#### Diagnosis and regression analysis

- `aobs-server analyze`: deterministic rules (root-cause errors, repeated failures, repeated
  identical calls, bottlenecks, unfinished or broken traces, missing usage, clock skew), each
  finding with evidence and a suggested next step. Content is never shown.
- `aobs-server compare`: a known-good run against later runs; new failures, slower runs and
  operations, tool, retry, model, token and provider-reported cost changes.
- `aobs-server compare-groups`: repeated runs before and after a change, with medians and
  variation, exact rank and Fisher tests, Holm correction, and "insufficient data" when there are
  too few runs.
- Example traces and generators in `examples/diagnose/`.

#### Reliability gate

- `aobs-server gate --policy POLICY.toml`: requirements in a strictly validated TOML policy
  (outcome, completeness, duration, model and tool calls, retries, tokens, cost, required and
  forbidden tools, per-tool limits, minimum runs and pass rate, baseline limits). Every check is
  PASS, FAIL or UNKNOWN with expected value, actual value and evidence; exit code 3 on FAIL.
- CI reports: JUnit XML (`--junit`) and Markdown for the GitHub Actions job summary
  (`--markdown`), and a workflow template in `examples/ci/github-actions-agent-gate.yml`.

#### Stored-trace analysis

- `db:` selections of one project's stored traces for `analyze`, `compare`, `compare-groups`
  and `gate` (`--project`, `AOBS_DATABASE_URL`), and `aobs-server traces list`. Stored traces go
  through the same loader, privacy step and analysis as files.

#### Dashboard

- `aobs-server dashboard`: a read-only web UI with an overview, a runs explorer, run details with
  a timeline and diagnosis findings, and two-run and group comparisons, over bundled sample data,
  local trace files or (with `--project`) a project's stored traces. Token-protected local data,
  strict Content Security Policy, no content shown.

#### Examples, project and documentation

- Example support agent (`examples/support_agent/`) with a scripted mock model (no API key) and a
  live mode, plus action-safety demos.
- SDK overhead benchmark and recorded baselines (`docs/benchmarks.md`).
- Documentation in `docs/`: SDK, action safety, reliability gate, stored analysis, dashboard, an
  end-to-end workflow, event model, ingestion and traces API, architecture, decisions, roadmap,
  and a PyPI release procedure (`docs/releasing.md`); a documentation site built with MkDocs
  Material (`mkdocs.yml`) and published to GitHub Pages.
- CI on pull requests and `master`: lint, formatting, strict type checks, schema lock and lock
  file checks, and the full test suite on Python 3.11, 3.12 and 3.13 against PostgreSQL 16; a
  strict documentation build.
- Package metadata for PyPI for both packages (nothing is published).
- Apache License 2.0, a security policy (`SECURITY.md`), contributing guide, code of conduct,
  and issue and pull request templates.

[Unreleased]: https://github.com/AshmitJain932/ai-agent-observability/commits/master
