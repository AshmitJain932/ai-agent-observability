# LangChain and LangGraph (`aobs.integrations.langchain`)

A LangChain callback handler that records LangChain and LangGraph runs as aobs spans. It is a
`langchain_core.callbacks.BaseCallbackHandler`, so it works anywhere LangChain accepts callbacks:
`invoke`, `ainvoke`, `stream`, `astream`, `batch`, on graphs, chains, chat models, tools and
retrievers.

**What was tested:** `langchain-core` 1.6.13 and `langgraph` 1.2.16, using langchain-core's fake
chat models (`GenericFakeChatModel`, `FakeListChatModel`, `FakeMessagesListChatModel`) and small
`BaseChatModel` subclasses, inside real LangGraph `StateGraph`s with a real `ToolNode`
(`sdk/python/tests/test_sdk_langchain.py`). No real model provider was called. Provider
integrations such as `langchain-openai` or `langchain-anthropic` have not been tested; what this
handler records for them depends on what they report to LangChain (see [Usage](#usage)).

## Install

`aobs` has no dependencies and does not install LangChain. `import aobs.integrations.langchain`
needs only the standard library; `langchain-core` is imported the first time `CallbackHandler`
is used, so install it in your application (LangGraph already depends on it):

```bash
pip install ./sdk/python langchain-core     # from a checkout; `uv sync` in this repo installs both
```

## Quick start

```python
import aobs
from aobs.integrations.langchain import CallbackHandler

aobs.init(service_name="support-agent", endpoint="http://localhost:8000")   # or exporter=...
handler = CallbackHandler()          # one handler can serve every invocation, sync or async

graph = builder.compile(name="support_agent")      # any LangGraph graph or LangChain runnable
result = graph.invoke(
    {"messages": [("user", "Where is my order 1042?")]},
    config={"callbacks": [handler], "configurable": {"thread_id": ticket_id}},
)
```

The graph run becomes one trace:

```
agent support_agent          ok         (session_id = thread_id)
├─ step  agent               ok         langgraph.node=agent, langgraph.step=1
│  ├─ llm   chat             ok         usage=52/11
│  └─ step  tools_condition  ok
├─ step  tools               ok
│  └─ tool  get_order_status ok         call_id=call_1, input=hmac-sha256:…
└─ step  agent               ok
   ├─ llm   chat             ok         usage: not reported
   └─ step  tools_condition  ok
```

[`examples/langgraph_agent/run.py`](https://github.com/AshmitJain932/ai-agent-observability/blob/master/examples/langgraph_agent/run.py) is this graph as a
runnable script; it needs no network or API key (the model is a scripted fake).

To group the graph run with other work, invoke it inside an aobs run; the graph becomes a child
`agent` span of it:

```python
with aobs.run("answer_ticket", session_id=ticket_id):
    await graph.ainvoke(inputs, config={"callbacks": [handler]})
```

## What is recorded

Nesting comes from LangChain's `run_id` / `parent_run_id`. A run whose parent the handler did not
see (the outermost run, or a run started with the handler in its own `config` only) is placed
under the aobs span current at that moment, or starts a new trace.

| LangChain run | aobs span | Recorded |
|---|---|---|
| outermost chain / graph | `agent` | name (`run_name`, the graph's `name`, or `LangGraph`), `agent.name`; `session_id` from the run metadata's `session_id`, `thread_id` (LangGraph's `configurable.thread_id`) or `conversation_id` |
| nested chain, LangGraph node, conditional edge | `step` | nodes also get `langgraph.node` and `langgraph.step` attributes |
| chat model (`on_chat_model_start`) | `llm`, operation `chat` | `provider` = LangChain's `ls_provider`; `request_model` = `ls_model_name` (or invocation params `model` / `model_name`); `temperature` / `max_tokens` from `ls_temperature` / `ls_max_tokens`; `response_model` (`model_name` / `model` in response metadata or `llm_output`); response `id` (response metadata `id` only); finish reasons (`finish_reason` / `stop_reason`); streaming time to first token and chunk count when tokens are streamed |
| completion LLM (`on_llm_start`) | `llm`, operation `completion` | as above |
| tool | `tool` | `tool.name`, `type: function`, `call_id` (the tool call id) |
| retriever | `retrieval` | `retrieval.document_count` attribute |

Every span carries a `langchain.run_id` attribute, for correlating with other LangChain tooling.
Runs LangChain marks `langsmith:hidden` (internal plumbing) get no span if they are chains;
their children attach to the nearest visible span. Model, tool and retriever calls are always
recorded, since tags are inherited.

### Usage

Usage is recorded with `source: provider_reported` **only when LangChain reports it**; otherwise
it is absent (unknown), never zero:

1. `AIMessage.usage_metadata` (LangChain's standard field): `input_tokens`, `output_tokens`,
   `total_tokens`, `input_token_details.cache_read` → `cache_read_input_tokens`,
   `input_token_details.cache_creation` → `cache_write_input_tokens`,
   `output_token_details.reasoning` → `reasoning_tokens`. LangChain's `input_tokens` already
   includes cached tokens, which is what the [event model](../event-model.md#5-span-fields)
   requires. With several generations (n > 1) the first message's usage is taken once.
2. Otherwise an OpenAI-shaped `llm_output["token_usage"]` (or `["usage"]`): `prompt_tokens`,
   `completion_tokens`, `total_tokens`. Other `llm_output` shapes are ignored, because some
   providers report cached tokens separately and the totals would be wrong.

Whether a streamed response has usage depends on the model integration (e.g. some need a
`stream_usage` option). Cost is never recorded (LangChain does not report provider cost).

### Errors and status

| What happened | Span |
|---|---|
| exception in a chain, model, tool or retriever | `error`; `error.type`, scrubbed `message`, `origin` `tool` / `provider` (models) / `application`, `timeout` for `TimeoutError`. Model errors with an integer `status_code` also get `code` and `retryable` (408/409/429/5xx). The exception propagates through every enclosing span, as it does in your code. |
| tool returned an error `ToolMessage` (`handle_tool_error` / `handle_validation_error`) | `error`, `error.type: ToolReturnedError`, the message text as (scrubbed) `message` |
| `ToolNode(handle_tool_errors=True)` caught a tool exception | the tool span is `error`, the node and graph continue `ok`; the trace is `ok_with_errors` |
| `asyncio.CancelledError`, `KeyboardInterrupt` | `cancelled` |
| LangGraph `interrupt()` (`GraphInterrupt`) | node span `cancelled` (not an error), attribute `langgraph.control_flow: GraphInterrupt`; the graph run ends `ok` |
| LangGraph `ParentCommand` (`Command(graph=Command.PARENT)`) | `ok`, attribute `langgraph.control_flow: ParentCommand` |

### Content and fingerprints

Content follows the [capture policy](../sdk.md#privacy) like everything else: nothing in
`metadata_only` (the default; content statuses are `not_captured`), redacted or full otherwise.

* Model input and output messages in the OpenTelemetry GenAI `{role, parts}` shape (`human` →
  `user`, `ai` → `assistant`, tool calls as `tool_call` parts, tool messages as
  `tool_call_response`); image, audio, file and reasoning blocks are recorded by type only.
* Tool arguments (LangChain's filtered `inputs`, without injected arguments) and results (the
  `ToolMessage` content).
* Retriever query and documents (`page_content`, `metadata`).
* Chain and node inputs and outputs (LangGraph state). These can be large; each part is capped at
  `max_content_bytes`.

`input_fingerprint` (keyed HMAC, see [sdk.md](../sdk.md#input-fingerprints)) is recorded for
tool arguments, model requests (messages plus invocation params, without `stream` / timeouts)
and retriever queries.

## Concurrency and async

One handler can be shared by concurrent invocations: runs are tracked by `run_id` under a lock,
so LangGraph's parallel branches (threads), `batch`, `asyncio.gather` over `ainvoke`, and
`ToolNode`'s thread pool all nest correctly (tested). The handler sets `run_inline = True`: it
does no I/O (events are queued for the SDK's exporter thread), so async callback managers call it
directly instead of in a thread pool.

## Limitations

* **The handler does not change aobs's current span.** A span opened by aobs code *inside*
  LangChain code (an `@aobs.tool` function called from a LangChain tool, or an
  `openai_compat`-wrapped client called from a node) is a child of the aobs span that was
  current when the graph was invoked (or its own trace), not of the LangChain node or tool span.
  Use one style per call: instrument it either through LangChain or with aobs directly.
* Not recorded: provider request ids, HTTP status of successful calls, headers, API keys, cost.
* Runs LangChain starts but never ends stay open (shown as in progress, then incomplete); the
  handler tracks at most 10,000 open runs and counts the rest as `uninstrumented_calls`.
* Legacy `AgentExecutor` agent actions, `on_text`, `on_retry` and custom events are not recorded
  as span events.
