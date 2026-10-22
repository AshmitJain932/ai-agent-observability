# Example: LangGraph agent with the aobs callback handler

A LangGraph tool-calling agent (`run.py`): an `agent` node calls a chat model, a `ToolNode` runs
`get_order_status`, and the graph loops until the model answers. Instrumentation is the
`CallbackHandler` passed in the graph's `config`; the lines are marked `# aobs`.

```bash
uv sync
uv run python examples/langgraph_agent/run.py                        # success
uv run python examples/langgraph_agent/run.py --scenario tool-error  # the tool raises KeyError
uv run python examples/langgraph_agent/run.py --capture redacted     # also record redacted content
```

It runs offline: the model is langchain-core's `GenericFakeChatModel` playing back a scripted
tool call (with `usage_metadata`, as a provider integration would report it) and a final answer
(without usage, so that call's usage is shown as unknown). Swap in a real chat model (e.g.
`ChatOpenAI`) and the graph and instrumentation stay the same; that has not been tested here.

Telemetry goes to `traces.jsonl` (`--out`). The script then validates the file with the
collector's model library and prints the trace:

```
trace 13b6a409…  status=ok  spans=9
├─ agent support_agent      ok
  ├─ step  agent              ok
    ├─ llm   chat               ok        usage=52/11
    ├─ step  tools_condition    ok
  ├─ step  tools              ok
    ├─ tool  get_order_status   ok
  ├─ step  agent              ok
    ├─ llm   chat               ok
    ├─ step  tools_condition    ok
```

What is recorded and why: [docs/integrations/langchain.md](../../docs/integrations/langchain.md).
Tested by `sdk/python/tests/test_sdk_langgraph_example.py`.
