# aobs (Python SDK)

Records what an AI agent did (model calls, tool calls, sub-agents, errors, latency, tokens and
cost) as nested traces, in the aobs wire schema v1.0. Standard library only: no dependencies to
conflict with your application. Early-stage software (alpha).

```python
import aobs
from aobs.export import JsonlFileExporter

aobs.init(service_name="my-agent", exporter=JsonlFileExporter("traces.jsonl"))


@aobs.tool
def lookup_order(order_id: str) -> dict:
    return {"order_id": order_id, "status": "delivered"}


with aobs.run("answer_ticket", session_id="ticket-1"):
    lookup_order("1042")

aobs.shutdown()
```

Also included: an OpenAI-compatible client integration (`aobs.integrations.openai_compat`) and
`aobs.safety`, a guard for side-effecting tools (refunds, payments): policy checks against trusted
evidence, approval, duplicate prevention and recovery without blind retries.

- SDK guide: <https://github.com/AshmitJain932/ai-agent-observability/blob/master/docs/sdk.md>
- Action safety: <https://github.com/AshmitJain932/ai-agent-observability/blob/master/docs/action-safety.md>
- Analysis, regression checks and the reliability gate (`aobs-server`):
  <https://github.com/AshmitJain932/ai-agent-observability>

Licensed under the Apache License 2.0.
