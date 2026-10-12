# Example: instrumented support agent

A small tool-calling agent (`agent.py`) that answers order questions with an OpenAI-compatible
model and two tools, `get_order_status` and `issue_refund`. Instrumentation is 10 statements
(including 4 imports), each marked `# aobs`.

```bash
uv sync
uv run python examples/support_agent/run.py                              # mock model, success
uv run python examples/support_agent/run.py --scenario refund            # two tool calls
uv run python examples/support_agent/run.py --scenario refund-outage     # refund tool times out
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

Telemetry goes to `traces.jsonl` (mode 0600) because the collector arrives in Phase 3
(`--endpoint URL` sends it over HTTP instead). The script then validates the file with the Phase 1
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

Tested by `sdk/python/tests/test_sdk_example.py`.
