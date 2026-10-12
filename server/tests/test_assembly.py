from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_merge import RESOURCE, T, accepted, end, start

from aobs_server.model import AssemblyConfig, assemble_events, merge_events, validate_batch
from support import assembly_config, fixture_files, fixture_id, fixture_path, load_fixture, parse_ts

NOW = datetime(2026, 10, 12, 12, 20, tzinfo=UTC)


def _assemble_fixture(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    fixture = load_fixture(path)
    expect = fixture["expect"]
    result = validate_batch(fixture["payload"])
    traces = assemble_events(result.accepted, parse_ts(expect["assemble_at"]), assembly_config(expect))
    return expect, {tid: view.to_dict() for tid, view in traces.items()}


@pytest.mark.parametrize("path", fixture_files("valid"), ids=fixture_id)
def test_fixture_expectations(path: Path) -> None:
    expect, traces = _assemble_fixture(path)
    assert sorted(traces) == sorted(expect["traces"])
    for trace_id, want in expect["traces"].items():
        got = traces[trace_id]
        for key in ("status", "roots", "orphans", "span_count", "error_count", "conflicts", "orphan_events"):
            if key in want:
                assert got[key] == want[key], (trace_id, key)
        for span_id, children in want.get("children", {}).items():
            assert got["spans"][span_id]["children"] == children, (trace_id, span_id)
        for span_id, lifecycle in want.get("lifecycles", {}).items():
            assert got["spans"][span_id]["lifecycle"] == lifecycle, (trace_id, span_id)
        for span_id, flags in want.get("flags", {}).items():
            assert got["spans"][span_id]["flags"] == flags, (trace_id, span_id)
        for field, total in want.get("tokens", {}).items():
            for key, value in total.items():
                assert got["tokens"][field][key] == value, (trace_id, field, key)


def test_merge_counters_from_fixtures() -> None:
    for path in fixture_files("valid"):
        fixture = load_fixture(path)
        if "merge" not in fixture["expect"]:
            continue
        merged = merge_events(validate_batch(fixture["payload"]).accepted)
        assert merged.duplicates == fixture["expect"]["merge"]["duplicates"], path.stem
        assert merged.conflicts == fixture["expect"]["merge"]["conflicts"], path.stem


def test_out_of_order_equals_in_order() -> None:
    _, ordered = _assemble_fixture(fixture_path("valid", "success_run"))
    _, shuffled = _assemble_fixture(fixture_path("valid", "out_of_order"))
    assert ordered == shuffled


def test_missing_usage_is_never_zero_filled() -> None:
    view = assemble_events(accepted(end(kind="llm")), NOW)[T]
    total = view.tokens["input_tokens"]
    assert total.value is None
    assert (total.reported_spans, total.expected_spans, total.complete) == (0, 1, False)


def test_no_llm_spans_means_no_token_data_not_zero() -> None:
    view = assemble_events(accepted(end(kind="step")), NOW)[T]
    assert view.tokens["input_tokens"].value is None
    assert view.tokens["input_tokens"].complete is False


def test_usage_on_non_llm_kinds_is_not_summed() -> None:
    # An agent span reporting aggregate usage would otherwise double count its children.
    view = assemble_events(
        accepted(
            end(kind="agent", usage={"input_tokens": 100, "source": "provider_reported"}),
            end(
                span_id="2222222222222222",
                parent_span_id="00f067aa0ba902b7",
                kind="llm",
                usage={"input_tokens": 100, "source": "provider_reported"},
            ),
        ),
        NOW,
    )[T]
    assert view.tokens["input_tokens"].value == 100


def test_ok_root_with_unended_child_reports_open_spans() -> None:
    view = assemble_events(
        accepted(
            end(kind="agent"),
            start(
                span_id="2222222222222222",
                parent_span_id="00f067aa0ba902b7",
                kind="tool",
                start_time="2026-10-12T12:00:00.5Z",
                tool={"name": "t"},
            ),
        ),
        NOW,
    )[T]
    assert view.status == "ok"
    assert view.open_span_count == 1
    assert view.spans["2222222222222222"].lifecycle == "incomplete"


def test_no_root_recent_activity_is_in_progress_then_incomplete() -> None:
    child = end(span_id="2222222222222222", parent_span_id="00f067aa0ba902b7")
    events = accepted(child)
    assert assemble_events(events, datetime(2026, 10, 12, 12, 0, 30, tzinfo=UTC))[T].status == "in_progress"
    assert assemble_events(events, datetime(2026, 10, 12, 13, 0, tzinfo=UTC))[T].status == "incomplete"


def test_trace_with_only_span_events() -> None:
    ev = {
        "type": "span_event",
        "event_id": "0192f4a1-7c3e-7b10-9a2d-5f6e1c0b8d41",
        "trace_id": T,
        "span_id": "00f067aa0ba902b7",
        "name": "log",
        "time": "2026-10-12T12:00:00Z",
    }
    view = assemble_events(accepted(ev), NOW)[T]
    assert view.status == "incomplete"
    assert view.span_count == 0
    assert view.orphan_events == ("0192f4a1-7c3e-7b10-9a2d-5f6e1c0b8d41",)


def test_lifecycle_threshold_is_configurable() -> None:
    events = accepted(start(start_time="2026-10-12T12:19:00Z"))
    assert assemble_events(events, NOW, AssemblyConfig(stale_after=timedelta(minutes=5)))[T].status == "in_progress"
    assert assemble_events(events, NOW, AssemblyConfig(stale_after=timedelta(seconds=30)))[T].status == "incomplete"


def test_naive_now_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone"):
        assemble_events(accepted(end()), datetime(2026, 10, 12, 12, 20))


def _span_id(i: int) -> str:
    return f"{i + 1:016x}"


def _chain_batch(n: int) -> list[Any]:
    events = []
    for i in range(n):
        events.append(end(span_id=_span_id(i), parent_span_id=None if i == 0 else _span_id(i - 1), kind="step"))
    out = []
    for i in range(0, n, 1000):  # respect the 1,000-events-per-batch limit
        result = validate_batch(
            {
                "schema_version": "1.0",
                "sent_at": "2026-10-12T12:00:00Z",
                "resource": RESOURCE,
                "events": events[i : i + 1000],
            }
        )
        out.extend(result.accepted)
    return out


def test_deep_chain_does_not_recurse() -> None:
    view = assemble_events(_chain_batch(5000), NOW)[T]
    assert view.span_count == 5000
    assert view.roots == (_span_id(0),)
    assert view.spans[_span_id(4998)].children == (_span_id(4999),)


def test_long_cycle_is_detected() -> None:
    n = 2000
    events = [end(span_id=_span_id(i), parent_span_id=_span_id((i + 1) % n), kind="step") for i in range(n)]
    acc = []
    for i in range(0, n, 1000):
        acc.extend(
            validate_batch(
                {
                    "schema_version": "1.0",
                    "sent_at": "2026-10-12T12:00:00Z",
                    "resource": RESOURCE,
                    "events": events[i : i + 1000],
                }
            ).accepted
        )
    view = assemble_events(acc, NOW)[T]
    assert len(view.orphans) == n
    assert {reason for _, reason in view.orphans} == {"cycle"}
    assert view.status == "incomplete"  # no root at all
