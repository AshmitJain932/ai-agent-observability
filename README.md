# aobs — vendor-neutral observability for AI agents

> Working name. Status: **Phase 1 complete** — canonical event model, validation and trace
> assembly. No SDK, server API, storage or UI yet.

Records what an AI agent did — model calls, tool calls, sub-agents, errors, latency, tokens and
cost — as nested traces, regardless of framework or model provider.

## What works today

| Component | Location | Status |
|---|---|---|
| Wire schema v1.0 (normative JSON Schema) | `schema/v1/ingest.schema.json` | Done |
| Conformance fixtures (10 valid, 43 invalid, 4 versioning) | `schema/fixtures/` | Done |
| Strict parsing, validation, version policy | `server/src/aobs_server/model/` | Done (library only) |
| Deterministic merge + trace assembly | `server/src/aobs_server/model/` | Done (library only) |
| Python SDK | — | Phase 2 |
| Ingestion API, storage, auth | — | Phase 3 |
| Query API, dashboard | — | Phase 4 |

## Development

Requires [uv](https://docs.astral.sh/uv/) and Python ≥ 3.11.

```bash
uv sync                                    # create .venv with server + dev dependencies
uv run pytest                              # full suite (~70 s; the differential sweep dominates)
uv run pytest --deselect server/tests/test_differential.py   # fast loop (~5 s)
uv run ruff check server && uv run ruff format --check server
uv run mypy server/src                     # strict
uv run python -m aobs_server.model.schema_lock   # schema file matches its lock
```

Using the model library:

```python
from datetime import UTC, datetime
from aobs_server.model import validate_batch, assemble_events

result = validate_batch(raw_request_body)        # bytes; strict JSON + schema + version policy
traces = assemble_events(result.accepted, datetime.now(UTC))
for trace_id, view in traces.items():
    print(trace_id, view.status, view.tokens["input_tokens"].to_dict())
```

## Documentation

| Doc | Contents |
|---|---|
| [docs/architecture.md](docs/architecture.md) | System shape, stack, ingestion pipeline, storage, SDK, privacy, risks, differentiator readiness |
| [docs/event-model.md](docs/event-model.md) | Normative event semantics: fields, unknown-value rules, merge, derived state, versioning |
| [docs/roadmap.md](docs/roadmap.md) | Phases 1–11 with acceptance criteria; test strategy |
| [docs/decisions.md](docs/decisions.md) | Architecture decision records and pending decisions |
| [docs/reviews/](docs/reviews/) | Design reviews and what they changed |
| [schema/CHANGELOG.md](schema/CHANGELOG.md) | Wire schema versions |

## Support matrix (honest)

| Integration | Status |
|---|---|
| Manual instrumentation (Python) | Planned — Phase 2 |
| OpenAI-compatible Chat Completions (Python) | Planned — Phase 2 |
| HTTP ingestion API (any language) | Planned — Phase 3 (the wire schema already exists) |
| OpenTelemetry GenAI (OTLP) ingestion | Planned — Phase 7 |
| Everything else | Not supported |
