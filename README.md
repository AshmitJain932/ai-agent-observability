# aobs — vendor-neutral observability for AI agents

> Working name. Status: **Phase 2 complete; Phase 3 in progress** — canonical event model, a
> working Python SDK with an example agent, offline diagnosis and regression tools, a first
> read-only dashboard over saved trace files, and an authenticated ingestion API that stores runs
> in PostgreSQL. There is no API to read stored runs yet, and the dashboard does not show them
> (Phase 3/4). Not production-hardened: no rate limiting, retention job or TLS of its own.

Records what an AI agent did — model calls, tool calls, sub-agents, errors, latency, tokens and
cost — as nested traces, regardless of framework or model provider.

## What works today

| Component | Location | Status |
|---|---|---|
| Wire schema v1.0 (normative JSON Schema) + 57 conformance fixtures | `schema/` | Done |
| Strict parsing, validation, version policy, merge, trace assembly | `server/src/aobs_server/model/` | Done (library only) |
| Python SDK `aobs` (stdlib only) | `sdk/python/` | Done — see [docs/sdk.md](docs/sdk.md) |
| OpenAI-compatible integration (sync/async/streaming) | `aobs.integrations.openai_compat` | Done — tested with `openai` 3.29.0 |
| Example tool-calling agent, mock and live modes | `examples/support_agent/` | Done |
| Overhead benchmark + recorded baseline | `sdk/python/benchmarks/`, [docs/benchmarks.md](docs/benchmarks.md) | Done |
| Offline trace diagnosis (`aobs-server analyze`): deterministic rules, evidence, suggestions | `server/src/aobs_server/diagnosis/`, [examples/diagnose](examples/diagnose/README.md) | Demo (no server or database needed) |
| Cross-run regression detection (`aobs-server compare`): new failures, slowdowns, retries, tool/model/token/cost changes | `server/src/aobs_server/diagnosis/compare.py`, [examples/diagnose](examples/diagnose/README.md#demo-compare-runs-and-find-regressions) | Demo (no server or database needed) |
| Web dashboard (`aobs-server dashboard`): overview, runs explorer, run details with diagnosis, two-run and group comparison, over sample data or local trace files | `server/src/aobs_server/dashboard/`, [docs/dashboard.md](docs/dashboard.md) | First version (reads files; no live ingestion yet) |
| Repeated-run regression analysis (`aobs-server compare-groups`): medians, variation, exact rank / Fisher tests with Holm correction, honest "insufficient data" | `server/src/aobs_server/diagnosis/repeated.py`, `stats.py`, [examples/diagnose](examples/diagnose/README.md#demo-repeated-runs-with-statistics) | Demo (no server or database needed) |
| Server foundations: PostgreSQL schema + migrations, API keys, authorization, ingest privacy step | `server/src/aobs_server/` | Done (Phase 3) |
| Ingestion API (`aobs-server serve`, `POST /v1/ingest`): API-key auth, privacy before storage, retry-safe idempotent merge, per-trace locking, trace summaries | `server/src/aobs_server/api/`, `ingest/`, `storage/traces.py`, [docs/ingestion.md](docs/ingestion.md) | First version (Phase 3; no read API, rate limiting or retention job yet) |
| Query API and dashboard over runs stored by the collector | — | Phase 4 |

## Try it

Requires [uv](https://docs.astral.sh/uv/) and Python ≥ 3.11.

```bash
uv sync
uv run python examples/support_agent/run.py --scenario refund-outage    # no API key needed
```

**Collector:** store runs in PostgreSQL with `uv run aobs-server serve` and an API key; see
[docs/ingestion.md](docs/ingestion.md).

**Dashboard:** `uv run aobs-server dashboard`, then open <http://localhost:8800/> (in a Codespace:
Ports tab → port 8800 → *Open in Browser*). It starts with clearly labelled sample data; see
[docs/dashboard.md](docs/dashboard.md).

The example command above prints the agent's answer, then the trace the collector will reconstruct (validated with the
Phase 1 model library). See [examples/support_agent/README.md](examples/support_agent/README.md).

Diagnose a recorded run (errors, repeated failures, wasted calls, bottlenecks, with evidence and
suggestions; content is never shown):

```bash
uv run aobs-server analyze examples/diagnose/traces/retry_storm.jsonl
```

Compare a baseline run with a later one (what got slower, what newly fails, what costs more):

```bash
uv run aobs-server compare examples/diagnose/compare/inventory_v1.jsonl examples/diagnose/compare/inventory_v2.jsonl
```

With several recordings per side, test whether the change is beyond normal run-to-run variation:

```bash
uv run aobs-server compare-groups --baseline examples/diagnose/groups/baseline.jsonl --current examples/diagnose/groups/slow.jsonl
```

See [examples/diagnose/README.md](examples/diagnose/README.md).

## Development

```bash
uv run pytest                                   # everything: server + SDK (~95 s)
uv run pytest sdk/python/tests                  # SDK only (~25 s)
uv run pytest --deselect server/tests/test_differential.py   # fast loop
uv run ruff check server sdk examples && uv run ruff format --check server sdk examples
uv run mypy server/src sdk/python/src           # strict
uv run python -m aobs_server.model.schema_lock  # schema file matches its lock
uv run python sdk/python/benchmarks/overhead.py # overhead benchmark (~1 min)
```

Python 3.11 run: `UV_PROJECT_ENVIRONMENT=.venv-311 uv run --python 3.11 pytest`.

## Documentation

| Doc | Contents |
|---|---|
| [docs/ingestion.md](docs/ingestion.md) | Collector: PostgreSQL setup, API keys, `POST /v1/ingest` request/response, limits, duplicates, retries, errors |
| [docs/dashboard.md](docs/dashboard.md) | Web dashboard: start, use, local files mode, security, limits |
| [docs/sdk.md](docs/sdk.md) | SDK usage, configuration, privacy, delivery guarantees, integrations, limitations |
| [docs/benchmarks.md](docs/benchmarks.md) | Overhead method and recorded baseline |
| [docs/architecture.md](docs/architecture.md) | System shape, stack, ingestion pipeline, storage, privacy, risks, differentiator readiness |
| [docs/event-model.md](docs/event-model.md) | Normative event semantics: fields, unknown-value rules, merge, derived state, versioning |
| [docs/roadmap.md](docs/roadmap.md) | Phases 1–11 with acceptance criteria and status |
| [docs/decisions.md](docs/decisions.md) | Architecture decision records and pending decisions |
| [docs/reviews/](docs/reviews/) | Design reviews and what they changed |
| [schema/CHANGELOG.md](schema/CHANGELOG.md) | Wire schema versions |

## Support matrix (honest)

| Integration | Status |
|---|---|
| Manual instrumentation (Python ≥ 3.11) | Supported (Phase 2) |
| OpenAI-compatible Chat Completions client (Python) | Supported; tested with `openai` 3.29.0 |
| HTTP ingestion API (any language) | First version — `POST /v1/ingest`, see [docs/ingestion.md](docs/ingestion.md) |
| OpenTelemetry GenAI (OTLP) ingestion | Planned — Phase 7 |
| Everything else | Not supported |
