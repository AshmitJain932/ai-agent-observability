"""Parent-child relationships, context propagation and identifiers."""

from __future__ import annotations

import asyncio
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import aobs
from aobs._ids import new_span_id, new_trace_id, uuid7
from aobs.export import InMemoryExporter
from sdk_support import end_by_name, ends, flush, only_trace, traces


def test_id_formats_and_uniqueness() -> None:
    trace_ids = {new_trace_id() for _ in range(5000)}
    span_ids = {new_span_id() for _ in range(5000)}
    assert len(trace_ids) == 5000 and len(span_ids) == 5000
    assert all(re.fullmatch(r"[0-9a-f]{32}", t) and t != "0" * 32 for t in trace_ids)
    assert all(re.fullmatch(r"[0-9a-f]{16}", s) and s != "0" * 16 for s in span_ids)


def test_uuid7_layout_and_time_order() -> None:
    first = uuid7()
    time.sleep(0.002)
    second = uuid7()
    pattern = r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
    assert re.fullmatch(pattern, first) and re.fullmatch(pattern, second)
    assert first[:13] < second[:13]  # millisecond timestamp prefix increases


def test_nested_spans_form_one_tree(sdk: InMemoryExporter) -> None:
    @aobs.tool
    def lookup(order_id: str) -> str:
        with aobs.span("db_query", kind="retrieval"):
            return "ok"

    with aobs.run("answer", agent="triage", session_id="s-1") as run:
        lookup("1")
        with aobs.llm_span(provider="acme", request_model="m"):
            pass
    flush()

    view = only_trace(sdk)
    assert view.roots == (run.span_id,)
    assert view.trace_id == run.trace_id
    root = view.spans[run.span_id]
    names = [view.spans[c].name for c in root.children]
    assert names == ["lookup", "chat"]
    lookup_id = root.children[0]
    assert [view.spans[c].name for c in view.spans[lookup_id].children] == ["db_query"]
    assert view.status == "ok"
    assert end_by_name(sdk, "answer")["session_id"] == "s-1"
    assert end_by_name(sdk, "answer")["agent"] == {"name": "triage"}


def test_span_outside_any_run_is_its_own_root(sdk: InMemoryExporter) -> None:
    with aobs.span("standalone") as sp:
        pass
    flush()
    assert only_trace(sdk).roots == (sp.span_id,)


def test_run_inside_a_span_starts_a_new_linked_trace(sdk: InMemoryExporter) -> None:
    with aobs.span("http_request") as outer, aobs.run("answer") as run:
        pass
    flush()
    assert run.trace_id != outer.trace_id
    assert end_by_name(sdk, "answer")["links"] == [
        {"trace_id": outer.trace_id, "span_id": outer.span_id, "relation": "triggered_by"}
    ]
    assert len(traces(sdk)) == 2


def test_sub_agent_is_a_child_in_the_same_trace(sdk: InMemoryExporter) -> None:
    @aobs.agent(version="2")
    def refund_specialist() -> None:
        aobs.event("handoff", {"aobs.handoff.from_agent": "triage"})

    with aobs.run("answer") as run:
        refund_specialist()
    flush()
    view = only_trace(sdk)
    child = view.spans[view.spans[run.span_id].children[0]]
    assert child.kind == "agent" and child.name == "refund_specialist"
    assert end_by_name(sdk, "refund_specialist")["agent"] == {"name": "refund_specialist", "version": "2"}
    assert [e.name for e in view.events_by_span[child.span_id]] == ["handoff"]


def test_concurrent_async_runs_do_not_leak_parents(sdk: InMemoryExporter) -> None:
    @aobs.tool
    async def slow_tool(i: int) -> int:
        await asyncio.sleep(0.001 * (i % 5))
        return i

    async def one_run(i: int) -> str:
        with aobs.run(f"run-{i}") as run:
            await asyncio.gather(slow_tool(i), slow_tool(i + 1000))
            with aobs.span(f"step-{i}"):
                await asyncio.sleep(0)
        return run.trace_id

    async def main() -> list[str]:
        return await asyncio.gather(*(one_run(i) for i in range(50)))

    trace_ids = asyncio.run(main())
    flush()
    views = traces(sdk)
    assert sorted(views) == sorted(trace_ids)
    for view in views.values():
        assert len(view.roots) == 1 and not view.orphans
        root = view.spans[view.roots[0]]
        assert view.span_count == 4
        assert {view.spans[c].kind for c in root.children} == {"tool", "step"}
        i = root.name.split("-")[1]
        assert {view.spans[c].name for c in root.children} == {"slow_tool", f"step-{i}"}


def test_threads_need_explicit_context_and_get_it_with_helper(sdk: InMemoryExporter) -> None:
    def work(name: str) -> None:
        with aobs.span(name):
            pass

    with aobs.run("answer") as run:
        bare = threading.Thread(target=work, args=("unbound",))
        bare.start()
        bare.join()
        with ThreadPoolExecutor(2) as pool:
            pool.submit(aobs.with_current_context(work), "bound").result()
    flush()
    bound = end_by_name(sdk, "bound")
    unbound = end_by_name(sdk, "unbound")
    assert bound["trace_id"] == run.trace_id and bound["parent_span_id"] == run.span_id
    assert unbound["trace_id"] != run.trace_id and unbound["parent_span_id"] is None


def test_explicit_parent(sdk: InMemoryExporter) -> None:
    root = aobs.span("root")
    with aobs.span("child", parent=root):  # root is not entered (not current)
        pass
    root.end()
    flush()
    child = end_by_name(sdk, "child")
    assert child["parent_span_id"] == root.span_id and child["trace_id"] == root.trace_id


def test_context_is_restored_after_spans(sdk: InMemoryExporter) -> None:
    assert not aobs.current_span().is_recording
    with aobs.run("a") as a:
        assert aobs.current_span() is a
        with aobs.span("b") as b:
            assert aobs.current_span() is b
        assert aobs.current_span() is a
    assert not aobs.current_span().is_recording


def test_durations_are_monotonic_and_consistent(sdk: InMemoryExporter) -> None:
    with aobs.span("sleepy"):
        time.sleep(0.02)
    flush()
    event = end_by_name(sdk, "sleepy")
    assert 15 <= event["duration_ms"] < 1000
    from datetime import datetime

    start = datetime.fromisoformat(event["start_time"])
    end = datetime.fromisoformat(event["end_time"])
    assert abs((end - start).total_seconds() * 1000 - event["duration_ms"]) < 0.01


def test_unended_span_appears_as_in_progress_after_flush(sdk: InMemoryExporter) -> None:
    sp = aobs.span("long_running")
    flush()  # flush exports starts of open spans
    assert [e["type"] for e in sdk.events()] == ["span_start"]
    assert only_trace(sdk).status == "in_progress"
    sp.end()
    flush()
    assert len(ends(sdk)) == 1
    assert only_trace(sdk).status == "ok"
