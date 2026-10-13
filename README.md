# aobs — vendor-neutral observability for AI agents

> Working name. Status: **Phase 2 complete** — canonical event model and a working Python SDK
> with an example agent. No collector server, storage or UI yet (Phases 3–4).

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
| Server foundations: PostgreSQL schema + migrations, API keys, authorization, ingest privacy step | `server/src/aobs_server/` | Phase 3 in progress (not yet serving HTTP) |
| Ingestion API, storage, auth | — | Phase 3 |
| Query API, dashboard | — | Phase 4 |

## Try it

Requires [uv](https://docs.astral.sh/uv/) and Python ≥ 3.11.

```bash
uv sync
uv run python examples/support_agent/run.py --scenario refund-outage    # no API key needed
```

Prints the agent's answer, then the trace the collector will reconstruct (validated with the
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
| HTTP ingestion API (any language) | Planned — Phase 3 (the wire schema already exists) |
| OpenTelemetry GenAI (OTLP) ingestion | Planned — Phase 7 |
| Everything else | Not supported |
