# aobs — vendor-neutral observability for AI agents

> Working name. Status: **Phase 0 complete — architecture only, no runnable code yet.**

Records what an AI agent did — model calls, tool calls, sub-agents, errors, latency, tokens and
cost — as nested traces, regardless of framework or model provider, and shows them in a dashboard.

## What works today

Nothing runnable yet. The design is in `docs/`:

| Doc | Contents |
|---|---|
| [docs/architecture.md](docs/architecture.md) | System shape, stack, ingestion pipeline, storage, SDK guarantees, privacy, risks |
| [docs/event-model.md](docs/event-model.md) | Canonical event model v1 draft, value semantics, example trace |
| [docs/roadmap.md](docs/roadmap.md) | Phases 1–8 with measurable acceptance criteria; test strategy |
| [docs/decisions.md](docs/decisions.md) | Architecture decision records and pending decisions |

## Support matrix (honest)

| Integration | Status |
|---|---|
| Manual instrumentation (Python) | Planned — Phase 2 |
| OpenAI-compatible Chat Completions (Python) | Planned — Phase 2 |
| HTTP ingestion API (any language) | Planned — Phase 3 |
| OpenTelemetry GenAI (OTLP) ingestion | Planned — Phase 7 |
| Everything else | Not supported |
