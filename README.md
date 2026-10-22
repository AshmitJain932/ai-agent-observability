# aobs — vendor-neutral observability for AI agents

> Working name. Status: **Phase 2 complete; Phase 3 in progress** — canonical event model, a
> working Python SDK with an example agent, offline diagnosis and regression tools, a first
> read-only dashboard over saved trace files, and an authenticated ingestion API that stores runs
> in PostgreSQL, with a read API for one stored trace by id. There is no run list yet, and the
> dashboard does not show stored runs (Phase 4). Not production-hardened: no rate limiting, retention job or TLS of its own.

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
| Web dashboard (`aobs-server dashboard`): overview, runs explorer, run details with diagnosis, two-run and group comparison, over sample data, local trace files or a project's stored traces | `server/src/aobs_server/dashboard/`, [docs/dashboard.md](docs/dashboard.md) | First version |
| Repeated-run regression analysis (`aobs-server compare-groups`): medians, variation, exact rank / Fisher tests with Holm correction, honest "insufficient data" | `server/src/aobs_server/diagnosis/repeated.py`, `stats.py`, [examples/diagnose](examples/diagnose/README.md#demo-repeated-runs-with-statistics) | Demo (no server or database needed) |
| Reliability gate (`aobs-server gate`): requirements in a TOML policy (outcome, completeness, duration, calls, retries, tokens, cost, tools, pass rate, baseline limits) → PASS/FAIL with evidence, exit code 3 on FAIL; JUnit XML and Markdown reports for CI (`--junit`, `--markdown`), GitHub Actions template | `server/src/aobs_server/diagnosis/gate.py`, [docs/reliability-gate.md](docs/reliability-gate.md) | First version (no server or database needed) |
| Server foundations: PostgreSQL schema + migrations, API keys, authorization, ingest privacy step | `server/src/aobs_server/` | Done (Phase 3) |
| Ingestion API (`aobs-server serve`, `POST /v1/ingest`): API-key auth, privacy before storage, retry-safe idempotent merge, per-trace locking, trace summaries | `server/src/aobs_server/api/`, `ingest/`, `storage/traces.py`, [docs/ingestion.md](docs/ingestion.md) | First version (Phase 3; no rate limiting or retention job yet) |
| Trace read API (`GET /v1/projects/{id}/traces/{trace_id}`, `…/events`): read-scoped keys, uniform 404 across projects, content opt-in under the project's current privacy settings, pagination | `server/src/aobs_server/query/`, [docs/traces-api.md](docs/traces-api.md) | First version (Phase 3; no run list or search yet) |
| Analysis of stored traces: `db:` selections for `analyze`, `compare`, `compare-groups`, `gate`; `traces list`; a *Stored traces* dashboard dataset (`--project`) | `server/src/aobs_server/diagnosis/stored.py`, [docs/stored-analysis.md](docs/stored-analysis.md) | First version (CLI and dashboard read PostgreSQL directly) |
| Action safety (`aobs.safety`): policy checks against trusted evidence before a side effect, approval, duplicate prevention, durable SQLite store, recovery without blind retries, provider idempotency keys | `sdk/python/src/aobs/safety.py`, [docs/action-safety.md](docs/action-safety.md) | First version (one host; not exactly-once: see its guarantees) |
| HTTP query API (run list, search, analysis over read-scoped keys) | — | Phase 4 |

## Quick start: your own agent

Requires Python ≥ 3.11. Not on PyPI yet: install from a clone of this repository.

```bash
git clone https://github.com/AshmitJain932/ai-agent-observability.git
pip install ./ai-agent-observability/sdk/python ./ai-agent-observability/server
```

Record what your agent does (the SDK has no dependencies; nothing leaves your machine with a file
exporter):

```python
import aobs
from aobs.export import JsonlFileExporter

aobs.init(service_name="my-agent", exporter=JsonlFileExporter("traces.jsonl"))


@aobs.tool
def lookup_order(order_id: str) -> dict:
    return {"order_id": order_id, "status": "delivered"}


with aobs.run("answer_ticket", session_id="ticket-1"):
    lookup_order("1042")  # model calls: wrap your client with aobs.integrations.openai_compat.wrap(...)

aobs.shutdown()
```

Then explain the run, and check it against written requirements (exit code 3 on FAIL, for CI):

```bash
aobs-server analyze traces.jsonl
aobs-server gate --policy ai-agent-observability/examples/diagnose/gate/support_agent.toml traces.jsonl
```

Next: [docs/sdk.md](docs/sdk.md) (SDK and model-client integration),
[docs/reliability-gate.md](docs/reliability-gate.md) (writing a policy) and
[docs/action-safety.md](docs/action-safety.md) (guarding refunds, payments and other side effects).

## Try it

Requires [uv](https://docs.astral.sh/uv/) and Python ≥ 3.11.

```bash
uv sync
uv run python examples/support_agent/run.py --scenario refund-outage    # no API key needed
```

**Collector:** store runs in PostgreSQL with `uv run aobs-server serve` and an API key; see
[docs/ingestion.md](docs/ingestion.md).

**The whole workflow, end to end:** [docs/agent-workflow.md](docs/agent-workflow.md). Run the
example agent (scripted mock model, no API key) and send its runs to the collector. Then list,
analyze and compare the stored runs, gate them (the healthy run passes; the refund outage is
rejected with the failing span as evidence), and view them in the dashboard.

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

Check recorded runs against written requirements, e.g. in CI (exit code 3 on FAIL, every failure
explained with the spans that show it):

```bash
uv run aobs-server gate --policy examples/diagnose/gate/support_agent.toml examples/diagnose/compare/refund_outage.jsonl
```

See [docs/reliability-gate.md](docs/reliability-gate.md).

### Reliability gate demo (no API key, no database)

Record a healthy and a degraded run of the example support agent with the real SDK (a scripted
model stands in for the LLM; the refund tool times out in the degraded run), then gate both:

```bash
uv run python examples/diagnose/make_compare_samples.py --out /tmp/aobs-gate-demo
uv run aobs-server gate --policy examples/diagnose/gate/support_agent.toml /tmp/aobs-gate-demo/refund_ok.jsonl
echo "exit code: $?"   # 0: PASS
uv run aobs-server gate --policy examples/diagnose/gate/support_agent.toml /tmp/aobs-gate-demo/refund_outage.jsonl
echo "exit code: $?"   # 3: FAIL
```

The second report fails two checks and shows each one's expected and actual value:

```
FAIL     run.require_success: the run ended with status error  [expected ok or ok_with_errors, actual error]
           - root cause: tool 'issue_refund' failed with TimeoutError; span … (tool issue_refund) ended with status error …
FAIL     run.tools.issue_refund.max_failures: 1 failure(s) of 'issue_refund', limit 0  [expected at most 0, actual 1]
```

`server/tests/test_gate.py::test_readme_demo_records_fresh_runs_with_the_sdk_and_gates_them` runs
the same steps.

**In CI**, add `--junit reports/agent-gate.xml --markdown "$GITHUB_STEP_SUMMARY"`. The JUnit file
is a test report (one test case per check) for GitLab, Jenkins, Azure DevOps and others; the
Markdown file is the GitHub Actions job summary. A complete GitHub Actions workflow to copy:
[examples/ci/github-actions-agent-gate.yml](examples/ci/github-actions-agent-gate.yml). Details:
[docs/reliability-gate.md#in-ci](docs/reliability-gate.md#in-ci).

### Analyze traces stored by the collector (PostgreSQL)

Every analysis command also reads the traces the collector stored. Set `AOBS_DATABASE_URL`, name
the project, and select traces with `db:` instead of a file (setup: [docs/ingestion.md](docs/ingestion.md#quick-start)):

```bash
uv run aobs-server traces list --project <project_id>                       # newest first, with db:trace=<id> inputs
uv run aobs-server analyze --project <project_id> db:trace=<trace_id>
uv run aobs-server compare --project <project_id> db:trace=<baseline_id> db:trace=<current_id>
uv run aobs-server compare-groups --project <project_id> \
  --baseline 'db:task=restock_check&until=2026-10-08' --current 'db:task=restock_check&since=2026-10-13'
uv run aobs-server gate --project <project_id> --policy examples/diagnose/gate/support_agent.toml \
  'db:task=answer_ticket&since=2026-10-13'
uv run aobs-server dashboard --project <project_id>                         # adds a token-protected "Stored traces" dataset
```

Selections, isolation, privacy, exit codes and limits: [docs/stored-analysis.md](docs/stored-analysis.md).

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
| [docs/traces-api.md](docs/traces-api.md) | Reading a stored trace and its span events: endpoints, pagination, tenant isolation, privacy |
| [docs/agent-workflow.md](docs/agent-workflow.md) | **Start here:** the example agent end to end: run it, send its runs to the collector, then list, analyze, compare, gate them and view them in the dashboard (no model API key needed) |
| [docs/stored-analysis.md](docs/stored-analysis.md) | Analyzing stored traces: `db:` selections, `traces list`, analyze/compare/compare-groups/gate and the dashboard over PostgreSQL |
| [docs/dashboard.md](docs/dashboard.md) | Web dashboard: start, use, local files mode, security, limits |
| [docs/reliability-gate.md](docs/reliability-gate.md) | Reliability gate: policy file, checks, missing-data rules, exit codes, CI use, limits |
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

## License

Apache License 2.0: see [LICENSE](LICENSE).
