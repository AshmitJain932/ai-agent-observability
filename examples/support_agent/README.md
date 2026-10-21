# Example: instrumented support agent

A small tool-calling agent (`agent.py`) that answers order questions with an OpenAI-compatible
model and two tools, `get_order_status` and `issue_refund`. Instrumentation is 10 statements
(including 4 imports), each marked `# aobs`.

```bash
uv sync
uv run python examples/support_agent/run.py                              # mock model, success
uv run python examples/support_agent/run.py --scenario refund            # two tool calls
uv run python examples/support_agent/run.py --scenario refund-outage     # refund tool times out
uv run python examples/support_agent/run.py --scenario model-outage      # model call fails (simulated HTTP 503)
uv run python examples/support_agent/run.py --no-usage                   # model reports no usage
uv run python examples/support_agent/run.py --capture redacted           # capture redacted content
```

**Mock mode (default)** needs no credentials and no network: `mock_llm.py` is a deterministic
scripted model behind the *real* `openai` client's HTTP mock transport, so the same instrumentation
path as live mode runs.

**Live mode** (not run by the test suite; needs your credentials):

```bash
AOBS_EXAMPLE_MODE=live OPENAI_API_KEY=... AOBS_EXAMPLE_MODEL=<model name> \
  uv run python examples/support_agent/run.py
# any OpenAI-compatible server: also set OPENAI_BASE_URL
```

Telemetry goes to `traces.jsonl` (mode 0600) by default. `--endpoint URL` with `AOBS_API_KEY` set
sends it to the collector instead ([docs/ingestion.md](../../docs/ingestion.md)). Each run answers
one ticket (`--ticket`, a fresh id by default), recorded as its session id, so
`db:session=<ticket>` selects that run in the collector's database. The full walkthrough runs
the agent, then stores, lists, analyzes, compares, gates and views its runs:
[docs/agent-workflow.md](../../docs/agent-workflow.md). The script then validates the file with the Phase 1
model library and prints the trace as the collector will reconstruct it:

```
trace 26e5a181…  status=error  spans=5
input tokens: 121 (reported by 2 of 2 LLM calls)
├─ agent answer_ticket     36.4 ms  error    TimeoutError: payments API did not respond within 2s
  ├─ llm   chat            10.6 ms  ok       usage=51/12
  ├─ tool  get_order_status 0.1 ms  ok       input=hmac-sha256:p80e912b4:42ba50…
  ├─ llm   chat             1.9 ms  ok       usage=70/12
  ├─ tool  issue_refund     0.1 ms  error    input=hmac-sha256:p80e912b4:ce5590…  TimeoutError: …
```

Tested by `sdk/python/tests/test_sdk_example.py`. The collector workflow, with every scenario sent
over HTTP to a real collector and PostgreSQL, is tested by `server/tests/test_agent_workflow.py`.

## Guarding the refund (`aobs.safety`)

`safety_demo.py` runs the refund behind `aobs.safety` in twelve local scenarios (denials, approval,
rejections, timeouts, duplicates, a restart); `protect_example.py` wraps the existing tool in a few
lines. Everything is simulated (`payments.py`); no refund or network call is real.

What protects against a duplicate refund, and what does not:

* The guard decides before the call and records the action in a store before executing it. With
  `SqliteActionStore`, processes on one host coordinate through the file: one attempt starts per
  action, an unknown outcome is never retried automatically, and only a recorded `FAILED` (by the
  adapter, a status lookup, or a person) lets another attempt start.
* The store cannot stop a request a paused process has already prepared. Only the payments
  provider can: it must honour the idempotency key the guard derives (`IdempotencyKeys`, keyed by a
  separate secret and a service/environment namespace). Without that, an action whose outcome is
  unknown stays unknown until a person resolves it, and resolving a stale one as failed requires
  stating that the original process has stopped.
* Nothing here is exactly-once execution.
