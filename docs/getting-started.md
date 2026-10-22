# Quick start

Instrument your own agent, explain a run and gate it. Everything here runs locally: no server,
no database and no API key, and nothing leaves your machine.

## Install

Requires Python ≥ 3.11. The packages are not on PyPI yet: install them from a clone of the
repository.

```bash
git clone https://github.com/AshmitJain932/ai-agent-observability.git
pip install ./ai-agent-observability/sdk/python ./ai-agent-observability/server
```

`aobs` is the SDK (standard library only). `aobs-server` provides the `aobs-server` command:
diagnosis, the reliability gate, the dashboard and the collector.

## Record a run

`agent.py`:

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

`python agent.py` writes one trace to `traces.jsonl`: an agent span for the run with a tool span
for `lookup_order` inside it. By default only metadata is captured (names, timing, status, a keyed
fingerprint of the arguments), not prompts or tool arguments; see [Privacy](sdk.md#privacy) in the SDK
guide.

## Explain it

```bash
aobs-server analyze traces.jsonl
```

The report shows the span tree with durations and statuses, a table of tool calls, and the
findings of the diagnosis rules (errors, repeated failures, wasted calls, bottlenecks), each with
evidence and a suggested next step. Content is never shown.

## Gate it

Write the requirements the run must meet, `policy.toml`:

```toml
version = 1

[gate]
task = "answer_ticket"           # check runs whose root span has this name

[run]
require_success = true
require_complete = true          # every span ended
max_duration_ms = 10000
max_tool_calls = 4
required_tools = ["lookup_order"]
```

```bash
aobs-server gate --policy policy.toml traces.jsonl
echo "exit code: $?"             # 0 on PASS, 3 on FAIL
```

Every check is printed with its expected and actual value; a failed check also lists the spans
that show it. In CI, add `--junit reports/agent-gate.xml` for a JUnit test report and
`--markdown "$GITHUB_STEP_SUMMARY"` for the GitHub Actions job summary. All policy settings,
missing-data rules and exit codes: [Reliability gate](reliability-gate.md).

## Next steps

- [Python SDK](sdk.md): the full API, OpenAI-compatible client integration, configuration,
  privacy and delivery guarantees.
- [Action safety](action-safety.md): guard refunds, payments and other side effects with policy
  checks, approval and duplicate prevention.
- [End-to-end workflow](agent-workflow.md): the example support agent with a scripted model, sent
  to the collector and PostgreSQL, then analyzed, compared, gated and shown in the dashboard.
- [Ingestion API](ingestion.md): run the collector (`aobs-server serve`) and send traces to it
  with `aobs.init(endpoint=..., api_key=...)`.
