"""The LangChain callback handler against real ``langchain-core`` (1.6.13) and ``langgraph`` (1.2.16).

No network, no credentials: models are langchain-core's fake chat models (or small subclasses of
``BaseChatModel``), graphs are real LangGraph ``StateGraph``s with a real ``ToolNode``.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import uuid
from collections.abc import Callable
from typing import Any

import langchain_core
import langgraph
import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.documents import Document
from langchain_core.language_models import BaseChatModel, LangSmithParams
from langchain_core.language_models.fake_chat_models import (
    FakeListChatModel,
    FakeListChatModelError,
    FakeMessagesListChatModel,
    GenericFakeChatModel,
)
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.retrievers import BaseRetriever
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import StructuredTool, ToolException, tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.types import Command, interrupt

import aobs
import aobs.integrations.langchain
from aobs.export import InMemoryExporter
from aobs.integrations.langchain import CallbackHandler
from sdk_support import check_bodies, end_by_name, ends, flush, only_trace, traces

USAGE = {
    "input_tokens": 120,
    "output_tokens": 15,
    "total_tokens": 135,
    "input_token_details": {"cache_read": 100, "cache_creation": 4},
    "output_token_details": {"reasoning": 7},
}
QUESTION = "Weather in Paris? Reply to jane@example.com"


@pytest.fixture(autouse=True)
def _no_langsmith(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGSMITH_API_KEY", "LANGCHAIN_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@tool
def get_weather(city: str) -> str:
    """Look up the weather for a city."""
    if city == "Atlantis":
        raise ValueError("unknown city Atlantis")
    return f"sunny in {city}"


def tool_call(city: str = "Paris", call_id: str = "call_1", usage: dict[str, Any] | None = USAGE) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": "get_weather", "args": {"city": city}, "id": call_id}],
        usage_metadata=usage,  # type: ignore[arg-type]
        response_metadata={"finish_reason": "tool_calls", "model_name": "fake-model-2026", "id": "resp_1"},
    )


def scripted(*messages: AIMessage | str) -> GenericFakeChatModel:
    return GenericFakeChatModel(messages=iter(messages))


def weather_graph(model: BaseChatModel, **tool_node: Any) -> Any:
    def agent(state: MessagesState) -> dict[str, Any]:
        return {"messages": [model.invoke(state["messages"])]}

    builder = StateGraph(MessagesState)
    builder.add_node("agent", agent)
    builder.add_node("tools", ToolNode([get_weather], **tool_node))
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", tools_condition)
    builder.add_edge("tools", "agent")
    return builder.compile()


def ask(graph: Any, handler: Any, **config: Any) -> Any:
    return graph.invoke({"messages": [HumanMessage(QUESTION)]}, config={"callbacks": [handler], **config})


def children(view: Any, span_id: str) -> list[tuple[str, str]]:
    return [(view.spans[c].kind, view.spans[c].name) for c in view.spans[span_id].children]


def by_kind(sdk: InMemoryExporter, kind: str) -> list[dict[str, Any]]:
    return sorted((e for e in ends(sdk) if e["kind"] == kind), key=lambda e: e["start_time"])


def test_tested_library_versions() -> None:
    assert langchain_core.__version__ == "1.6.13"
    from importlib.metadata import version

    assert version("langgraph") == "1.2.16" and langgraph is not None


def test_handler_is_a_langchain_callback_handler() -> None:
    handler = aobs.integrations.langchain.CallbackHandler()
    assert isinstance(handler, BaseCallbackHandler)
    assert type(handler) is CallbackHandler and CallbackHandler.__name__ == "CallbackHandler"
    assert handler.run_inline is True and handler.raise_error is False
    with pytest.raises(AttributeError):
        aobs.integrations.langchain.NotAThing  # noqa: B018


def test_langgraph_run_nests_nodes_models_and_tools(sdk: InMemoryExporter) -> None:
    graph = weather_graph(scripted(tool_call(), "It is sunny in Paris."))
    result = ask(graph, CallbackHandler(), configurable={"thread_id": "ticket-7"})
    assert result["messages"][-1].content == "It is sunny in Paris."  # the application's result is untouched
    flush()

    view = only_trace(sdk)
    assert view.status == "ok" and view.open_span_count == 0 and not view.orphans
    (root_id,) = view.roots
    assert children(view, root_id) == [("step", "agent"), ("step", "tools"), ("step", "agent")]
    first_agent, tools_node, second_agent = view.spans[root_id].children
    assert children(view, first_agent) == [("llm", "chat"), ("step", "tools_condition")]
    assert children(view, tools_node) == [("tool", "get_weather")]
    assert children(view, second_agent) == [("llm", "chat"), ("step", "tools_condition")]

    root = end_by_name(sdk, "LangGraph")
    assert root["kind"] == "agent" and root["agent"] == {"name": "LangGraph"}
    assert root["session_id"] == "ticket-7"  # from LangGraph's thread_id
    assert root["parent_span_id"] is None
    assert root["attributes"]["langchain.run_id"]
    node = next(e for e in ends(sdk) if e["span_id"] == tools_node)
    assert node["attributes"]["langgraph.node"] == "tools" and node["attributes"]["langgraph.step"] == 2

    tool_end = end_by_name(sdk, "get_weather")
    assert tool_end["status"] == "ok"
    assert tool_end["tool"] == {"name": "get_weather", "type": "function", "call_id": "call_1"}
    assert tool_end["input_fingerprint"].startswith("hmac-sha256:")
    assert tool_end["content"] == {"input": {"status": "not_captured"}, "output": {"status": "not_captured"}}

    resource = sdk.batches[0]["resource"]
    integration = next(i for i in resource["integrations"] if i["name"] == "langchain")
    assert "llm.usage" in integration["capabilities"] and "tool.errors" in integration["capabilities"]


def test_usage_is_recorded_only_when_the_model_reports_it(sdk: InMemoryExporter) -> None:
    ask(weather_graph(scripted(tool_call(), "It is sunny.")), CallbackHandler())
    flush()
    first, second = by_kind(sdk, "llm")
    assert first["usage"] == {
        "input_tokens": 120,
        "output_tokens": 15,
        "total_tokens": 135,
        "cache_read_input_tokens": 100,
        "cache_write_input_tokens": 4,
        "reasoning_tokens": 7,
        "source": "provider_reported",
    }
    assert first["llm"] == {
        "provider": "genericfakechatmodel",  # LangChain's ls_provider for this class
        "operation": "chat",
        "response_model": "fake-model-2026",
        "response": {"id": "resp_1", "finish_reasons": ["tool_calls"]},
    }
    assert "usage" not in second  # not reported: unknown, never zero
    assert "response" not in second["llm"] and "response_model" not in second["llm"]
    assert "cost" not in first and "cost" not in second
    tokens = only_trace(sdk).tokens["input_tokens"]
    assert (tokens.value, tokens.reported_spans, tokens.expected_spans) == (120, 1, 2)


def test_model_without_usage_records_none(sdk: InMemoryExporter) -> None:
    model = FakeMessagesListChatModel(responses=[AIMessage("hello")])
    assert model.invoke("hi", config={"callbacks": [CallbackHandler()]}).content == "hello"
    flush()
    event = end_by_name(sdk, "chat")
    assert event["status"] == "ok" and "usage" not in event
    assert event["parent_span_id"] is None  # a bare model call is its own trace


class ProviderLikeChat(BaseChatModel):
    """A chat model that reports what provider integrations report: ``ls_*`` params and an
    OpenAI-shaped ``llm_output`` (no ``usage_metadata``), or raises a provider error."""

    fail_status: int | None = None

    @property
    def _llm_type(self) -> str:
        return "provider-like"

    def _get_ls_params(self, stop: list[str] | None = None, **kwargs: Any) -> LangSmithParams:
        return LangSmithParams(
            ls_provider="acme", ls_model_name="acme-large", ls_model_type="chat", ls_temperature=0.2, ls_max_tokens=64
        )

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kw: Any
    ) -> ChatResult:
        if self.fail_status is not None:
            raise ProviderError(self.fail_status)
        generation = ChatGeneration(message=AIMessage("ok"), generation_info={"finish_reason": "stop"})
        usage = {"prompt_tokens": 30, "completion_tokens": 3, "total_tokens": 33}
        return ChatResult(generations=[generation], llm_output={"token_usage": usage, "model_name": "acme-large-v2"})

    def _combine_llm_outputs(self, llm_outputs: list[dict[str, Any] | None]) -> dict[str, Any]:
        return next((o for o in llm_outputs if o), {})


class ProviderError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"provider returned HTTP {status_code}")
        self.status_code = status_code


def test_request_params_and_llm_output_usage(sdk: InMemoryExporter) -> None:
    ProviderLikeChat().invoke(
        [SystemMessage("Be brief."), HumanMessage("hi")], config={"callbacks": [CallbackHandler()]}
    )
    flush()
    event = end_by_name(sdk, "chat")
    assert event["llm"] == {
        "provider": "acme",
        "operation": "chat",
        "request_model": "acme-large",
        "response_model": "acme-large-v2",
        "request": {"temperature": 0.2, "max_tokens": 64},
        "response": {"finish_reasons": ["stop"]},
    }
    assert event["usage"] == {"input_tokens": 30, "output_tokens": 3, "total_tokens": 33, "source": "provider_reported"}
    assert event["input_fingerprint"].startswith("hmac-sha256:")


def test_model_error_is_recorded_and_reraised(sdk: InMemoryExporter) -> None:
    with pytest.raises(ProviderError):
        ProviderLikeChat(fail_status=429).invoke("hi", config={"callbacks": [CallbackHandler()]})
    flush()
    event = end_by_name(sdk, "chat")
    assert event["status"] == "error"
    assert event["error"]["type"].endswith("ProviderError")
    assert event["error"]["origin"] == "provider" and event["error"]["code"] == "429"
    assert event["error"]["retryable"] is True
    assert "usage" not in event


def test_model_error_inside_a_graph_fails_node_and_run(sdk: InMemoryExporter) -> None:
    with pytest.raises(ProviderError):
        ask(weather_graph(ProviderLikeChat(fail_status=400)), CallbackHandler())
    flush()
    view = only_trace(sdk)
    assert view.status == "error"
    llm = end_by_name(sdk, "chat")
    assert llm["error"]["code"] == "400" and "retryable" not in llm["error"]
    assert end_by_name(sdk, "agent")["status"] == "error"
    assert end_by_name(sdk, "LangGraph")["error"]["origin"] == "application"


def test_tool_exception_fails_tool_node_and_run(sdk: InMemoryExporter) -> None:
    with pytest.raises(ValueError, match="unknown city Atlantis"):
        ask(weather_graph(scripted(tool_call("Atlantis"))), CallbackHandler())
    flush()
    tool_end = end_by_name(sdk, "get_weather")
    assert tool_end["status"] == "error"
    assert tool_end["error"]["type"] == "ValueError" and tool_end["error"]["origin"] == "tool"
    assert tool_end["error"]["message"] == "unknown city Atlantis"
    assert end_by_name(sdk, "tools")["status"] == "error"
    assert only_trace(sdk).status == "error"


def test_tool_error_handled_by_tool_node_keeps_run_ok(sdk: InMemoryExporter) -> None:
    graph = weather_graph(scripted(tool_call("Atlantis"), "Sorry, I don't know."), handle_tool_errors=True)
    assert ask(graph, CallbackHandler())["messages"][-1].content == "Sorry, I don't know."
    flush()
    assert end_by_name(sdk, "get_weather")["status"] == "error"
    assert end_by_name(sdk, "LangGraph")["status"] == "ok"
    assert only_trace(sdk).status == "ok_with_errors"


def test_tool_that_returns_an_error_message_is_an_error(sdk: InMemoryExporter) -> None:
    def _charge(amount: int) -> str:
        raise ToolException("card declined for jane@example.com")

    charge = StructuredTool.from_function(_charge, name="charge", description="Charge.", handle_tool_error=True)

    output = charge.invoke(
        {"type": "tool_call", "name": "charge", "args": {"amount": 5}, "id": "call_9"},
        config={"callbacks": [CallbackHandler()]},
    )
    assert output.status == "error"
    flush()
    event = end_by_name(sdk, "charge")
    assert event["status"] == "error"
    assert event["error"] == {
        "type": "ToolReturnedError",
        "origin": "tool",
        "message": "card declined for [REDACTED:email]",
    }
    assert event["tool"]["call_id"] == "call_9"


def test_async_ainvoke_nests_under_the_current_aobs_run(sdk: InMemoryExporter) -> None:
    graph = weather_graph(scripted(tool_call(), "It is sunny."))
    handler = CallbackHandler()

    async def main() -> Any:
        with aobs.run("support_ticket", session_id="s-1") as run:
            result = await graph.ainvoke({"messages": [HumanMessage(QUESTION)]}, config={"callbacks": [handler]})
        return run, result

    run, result = asyncio.run(main())
    assert result["messages"][-1].content == "It is sunny."
    flush()
    view = only_trace(sdk)
    assert view.roots == (run.span_id,)
    assert children(view, run.span_id) == [("agent", "LangGraph")]
    graph_span = view.spans[run.span_id].children[0]
    assert children(view, graph_span) == [("step", "agent"), ("step", "tools"), ("step", "agent")]
    assert children(view, view.spans[graph_span].children[1]) == [("tool", "get_weather")]
    assert view.status == "ok" and view.open_span_count == 0
    assert handler._runs == {}  # every run that started also ended


def test_concurrent_async_runs_share_one_handler(sdk: InMemoryExporter) -> None:
    handler = CallbackHandler()

    async def one(city: str) -> Any:
        graph = weather_graph(scripted(tool_call(city, call_id=f"call_{city}"), f"Done {city}."))
        return await graph.ainvoke({"messages": [HumanMessage(city)]}, config={"callbacks": [handler]})

    async def main() -> list[Any]:
        return await asyncio.gather(*(one(c) for c in ("Paris", "Rome", "Oslo", "Lima")))

    asyncio.run(main())
    flush()
    views = traces(sdk)
    assert len(views) == 4
    for view in views.values():
        (root_id,) = view.roots
        tools_node = view.spans[root_id].children[1]
        (tool_id,) = view.spans[tools_node].children
        call_id = next(e for e in ends(sdk) if e["span_id"] == tool_id)["tool"]["call_id"]
        assert view.kind_counts == {"agent": 1, "step": 5, "llm": 2, "tool": 1}
        assert call_id.startswith("call_") and view.status == "ok"
    assert handler._runs == {}


def test_parallel_branches_and_batch_in_threads(sdk: InMemoryExporter) -> None:
    def make(name: str) -> Callable[[MessagesState], dict[str, Any]]:
        model = FakeListChatModel(responses=[f"from {name}"])
        return lambda state: {"messages": [model.invoke(state["messages"])]}

    builder = StateGraph(MessagesState)
    builder.add_node("left", make("left"))
    builder.add_node("right", make("right"))
    builder.add_edge(START, "left")
    builder.add_edge(START, "right")
    builder.add_edge("left", END)
    builder.add_edge("right", END)
    graph = builder.compile()
    handler = CallbackHandler()
    graph.batch([{"messages": [HumanMessage(str(i))]} for i in range(3)], config={"callbacks": [handler]})
    flush()
    views = traces(sdk)
    assert len(views) == 3
    for view in views.values():
        (root_id,) = view.roots
        assert sorted(children(view, root_id)) == [("step", "left"), ("step", "right")]
        for node in view.spans[root_id].children:
            assert children(view, node) == [("llm", "chat")]
    assert handler._runs == {}


def test_streaming_records_tokens_and_errors(sdk: InMemoryExporter) -> None:
    handler = CallbackHandler()
    chunks = list(FakeListChatModel(responses=["hello"]).stream("hi", config={"callbacks": [handler]}))
    assert "".join(str(c.content) for c in chunks) == "hello"
    broken = FakeListChatModel(responses=["hello"], error_on_chunk_number=2)
    with pytest.raises(FakeListChatModelError):
        list(broken.stream("hi", config={"callbacks": [handler]}))
    flush()
    ok, failed = by_kind(sdk, "llm")
    assert ok["status"] == "ok"
    assert ok["llm"]["streaming"]["chunk_count"] == 5 and ok["llm"]["streaming"]["completed"] is True
    assert ok["llm"]["streaming"]["time_to_first_token_ms"] >= 0
    assert failed["status"] == "error" and failed["error"]["type"].endswith("FakeListChatModelError")
    assert failed["llm"]["streaming"]["chunk_count"] == 2 and failed["llm"]["streaming"]["completed"] is False


def test_graph_stream_mode_messages(sdk: InMemoryExporter) -> None:
    # (GenericFakeChatModel cannot stream a tool-call-only message, so this graph answers directly.)
    graph = weather_graph(scripted("It is sunny in Paris."))
    config = {"callbacks": [CallbackHandler()]}
    streamed = list(graph.stream({"messages": [HumanMessage("q")]}, config=config, stream_mode="messages"))
    assert "".join(str(chunk.content) for chunk, _ in streamed) == "It is sunny in Paris."
    flush()
    assert only_trace(sdk).status == "ok"
    (llm,) = by_kind(sdk, "llm")
    assert llm["llm"]["streaming"]["completed"] is True and llm["llm"]["streaming"]["chunk_count"] > 1


def test_metadata_only_sends_no_content(sdk: InMemoryExporter) -> None:
    ask(weather_graph(scripted(tool_call(), "It is sunny in Paris.")), CallbackHandler())
    flush()
    body = b"".join(sdk.bodies)
    for secret in (b"jane@example.com", b"Weather in Paris", b"sunny in Paris", b"It is sunny"):
        assert secret not in body
    for event in ends(sdk):
        assert event["content"] == {"input": {"status": "not_captured"}, "output": {"status": "not_captured"}}


def test_redacted_content_uses_otel_message_shape(make_sdk: Callable[..., InMemoryExporter]) -> None:
    sdk = make_sdk(capture="redacted")
    ask(weather_graph(scripted(tool_call(), "It is sunny in Paris.")), CallbackHandler())
    flush()
    first, second = by_kind(sdk, "llm")
    assert first["content"]["input"]["format"] == "messages"
    assert first["content"]["input"]["value"] == [
        {"role": "user", "parts": [{"type": "text", "content": "Weather in Paris? Reply to [REDACTED:email]"}]}
    ]
    assert first["content"]["output"]["value"] == [
        {
            "role": "assistant",
            "parts": [{"type": "tool_call", "id": "call_1", "name": "get_weather", "arguments": {"city": "Paris"}}],
            "finish_reason": "tool_calls",
        }
    ]
    assert second["content"]["input"]["value"][-1] == {
        "role": "tool",
        "parts": [{"type": "tool_call_response", "id": "call_1", "response": "sunny in Paris"}],
    }
    tool_end = end_by_name(sdk, "get_weather")
    assert tool_end["content"]["input"]["value"] == {"city": "Paris"}
    assert tool_end["content"]["output"]["value"] == "sunny in Paris"
    root = end_by_name(sdk, "LangGraph")
    assert root["content"]["output"]["value"]["messages"][-1] == {
        "role": "assistant",
        "parts": [{"type": "text", "content": "It is sunny in Paris."}],
    }


def test_identical_tool_calls_share_a_fingerprint(sdk: InMemoryExporter) -> None:
    handler = CallbackHandler()
    for city in ("Paris", "Paris", "Rome"):
        get_weather.invoke({"city": city}, config={"callbacks": [handler]})
    flush()
    fps = [e["input_fingerprint"] for e in by_kind(sdk, "tool")]
    assert fps[0] == fps[1] != fps[2]


def test_interrupt_is_not_an_error(sdk: InMemoryExporter) -> None:
    def approve(state: MessagesState) -> dict[str, Any]:
        answer = interrupt("approve?")
        return {"messages": [AIMessage(f"approved: {answer}")]}

    builder = StateGraph(MessagesState)
    builder.add_node("approve", approve)
    builder.add_edge(START, "approve")
    builder.add_edge("approve", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    result = ask(graph, CallbackHandler(), configurable={"thread_id": "t-1"})
    assert "__interrupt__" in result
    flush()
    node = end_by_name(sdk, "approve")
    assert node["status"] == "cancelled" and "error" not in node
    assert node["attributes"]["langgraph.control_flow"] == "GraphInterrupt"
    assert end_by_name(sdk, "LangGraph")["status"] == "ok"


def test_parent_command_is_not_an_error(sdk: InMemoryExporter) -> None:
    def hand_back(state: MessagesState) -> Command[Any]:
        return Command(graph=Command.PARENT, goto="done", update={"messages": [AIMessage("handing back")]})

    child = StateGraph(MessagesState)
    child.add_node("hand_back", hand_back)
    child.add_edge(START, "hand_back")
    parent = StateGraph(MessagesState)
    parent.add_node("child", child.compile(), destinations=("done",))
    parent.add_node("done", lambda state: {"messages": [AIMessage("done")]})
    parent.add_edge(START, "child")
    parent.add_edge("done", END)
    assert ask(parent.compile(), CallbackHandler())["messages"][-1].content == "done"
    flush()
    node = end_by_name(sdk, "hand_back")
    assert node["status"] == "ok" and node["attributes"]["langgraph.control_flow"] == "ParentCommand"
    assert only_trace(sdk).status == "ok"


class Shelf(BaseRetriever):
    def _get_relevant_documents(self, query: str, *, run_manager: Any) -> list[Document]:
        if query == "fail":
            raise RuntimeError("index offline")
        return [Document(page_content=f"about {query}", metadata={"id": 1}), Document(page_content="more")]


def test_retriever_calls(make_sdk: Callable[..., InMemoryExporter]) -> None:
    sdk = make_sdk(capture="full")
    handler = CallbackHandler()
    Shelf().invoke("refunds", config={"callbacks": [handler]})
    with pytest.raises(RuntimeError):
        Shelf().invoke("fail", config={"callbacks": [handler]})
    flush()
    ok, failed = by_kind(sdk, "retrieval")
    assert ok["name"] == "Shelf" and ok["status"] == "ok"
    assert ok["attributes"]["retrieval.document_count"] == 2
    assert ok["content"]["input"] == {"status": "captured", "format": "text", "value": "refunds"}
    assert ok["content"]["output"]["value"][0] == {"page_content": "about refunds", "metadata": {"id": 1}}
    assert ok["input_fingerprint"].startswith("hmac-sha256:")
    assert failed["status"] == "error" and failed["error"]["type"] == "RuntimeError"


def test_hidden_chains_are_skipped_but_their_model_calls_are_kept(sdk: InMemoryExporter) -> None:
    model = FakeListChatModel(responses=["hi"])
    plumbing = RunnableLambda(lambda x: model.invoke(x)).with_config(tags=["langsmith:hidden"], run_name="plumbing")
    outer = RunnableLambda(lambda x: plumbing.invoke(x)).with_config(run_name="outer")
    outer.invoke("q", config={"callbacks": [CallbackHandler()]})
    flush()
    view = only_trace(sdk)
    (root_id,) = view.roots
    assert view.spans[root_id].name == "outer"
    assert children(view, root_id) == [("llm", "chat")]


def test_handler_without_sdk_initialized_is_a_noop() -> None:
    handler = CallbackHandler()
    result = ask(weather_graph(scripted(tool_call(), "It is sunny.")), handler)
    assert result["messages"][-1].content == "It is sunny."
    assert handler._runs == {}


def test_handler_never_raises_into_langchain(sdk: InMemoryExporter) -> None:
    handler = CallbackHandler()
    # Malformed callbacks (unknown run ids, wrong types) are ignored rather than raised.
    handler.on_chain_end({}, run_id=_uuid())
    handler.on_llm_end(object(), run_id=_uuid())
    handler.on_tool_error(ValueError("x"), run_id=_uuid())
    handler.on_llm_new_token("t", run_id=_uuid())
    run_id = _uuid()
    handler.on_chat_model_start(None, [[object()]], run_id=run_id, metadata={"ls_provider": 3})
    handler.on_llm_end(object(), run_id=run_id)
    flush()
    assert end_by_name(sdk, "chat")["status"] == "ok"
    assert aobs.diagnostics()["internal_errors"] == 0


def test_all_integration_output_is_schema_valid(make_sdk: Callable[..., InMemoryExporter]) -> None:
    sdk = make_sdk(capture="full")
    handler = CallbackHandler()
    ask(weather_graph(scripted(tool_call(), "It is sunny.")), handler, configurable={"thread_id": "t"})
    ask(weather_graph(scripted(tool_call("Atlantis"), "Sorry."), handle_tool_errors=True), handler)
    with pytest.raises(ProviderError):
        ProviderLikeChat(fail_status=503).invoke("hi", config={"callbacks": [handler]})
    Shelf().invoke("q", config={"callbacks": [handler]})
    flush()
    accepted = check_bodies(sdk.bodies)
    assert len(accepted) == len(sdk.events())


def test_aobs_spans_inside_langchain_code_attach_to_the_current_aobs_span(sdk: InMemoryExporter) -> None:
    # Documented limitation: the handler does not change aobs's current span, so a span opened by
    # aobs code inside a LangChain tool is a child of the aobs span that was current at invoke time.
    @aobs.tool
    def lookup(city: str) -> str:
        return f"sunny in {city}"

    @tool
    def get_weather(city: str) -> str:
        """Look up the weather for a city."""
        return lookup(city)

    model = scripted(tool_call(), "Sunny.")

    def agent(state: MessagesState) -> dict[str, Any]:
        return {"messages": [model.invoke(state["messages"])]}

    builder = StateGraph(MessagesState)
    builder.add_node("agent", agent)
    builder.add_node("tools", ToolNode([get_weather]))
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", tools_condition)
    builder.add_edge("tools", "agent")
    with aobs.run("ticket") as run:
        ask(builder.compile(), CallbackHandler())
    flush()
    assert end_by_name(sdk, "lookup")["parent_span_id"] == run.span_id
    assert end_by_name(sdk, "get_weather")["parent_span_id"] != run.span_id
    assert only_trace(sdk).status == "ok"


def test_missing_langchain_core_gives_a_clear_error() -> None:
    code = (
        "import sys; sys.modules['langchain_core'] = None\n"
        "import aobs.integrations.langchain as lc\n"
        "try:\n    lc.CallbackHandler\nexcept ImportError as e:\n    print(e)\n"
    )
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True, timeout=60)
    assert "needs langchain-core" in out.stdout


def _uuid() -> uuid.UUID:
    return uuid.uuid4()
