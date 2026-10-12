import copy
import itertools
from typing import Any

from aobs_server.model import AcceptedEvent, merge_events, validate_batch
from aobs_server.model.merge import deep_merge

T = "4bf92f3577b34da6a3ce929d0e0e4736"
S = "00f067aa0ba902b7"
RESOURCE = {
    "service_name": "svc",
    "environment": "dev",
    "sdk": {"name": "test", "version": "0", "language": "python"},
    "capture": {"mode": "metadata_only", "error_messages": True, "stack_traces": False},
}


def accepted(*events: dict[str, Any]) -> list[AcceptedEvent]:
    result = validate_batch(
        {"schema_version": "1.0", "sent_at": "2026-10-12T12:00:00Z", "resource": RESOURCE, "events": list(events)}
    )
    assert result.ok and not result.rejected, (result.batch_errors, result.rejected)
    return list(result.accepted)


def start(**kw: Any) -> dict[str, Any]:
    base = {
        "type": "span_start",
        "trace_id": T,
        "span_id": S,
        "parent_span_id": None,
        "kind": "llm",
        "name": "chat",
        "start_time": "2026-10-12T12:00:00Z",
    }
    return {**base, **kw}


def end(**kw: Any) -> dict[str, Any]:
    base = {**start(), "type": "span_end", "end_time": "2026-10-12T12:00:01Z", "duration_ms": 1000.0, "status": "ok"}
    return {**base, **kw}


def test_identical_duplicates_are_noops() -> None:
    result = merge_events(accepted(end(), end(), end()))
    assert result.duplicates == 2
    assert result.conflicts == 0
    assert result.spans[(T, S)].conflicts == 0


def test_conflict_winner_is_independent_of_order_and_is_lowest_digest() -> None:
    variants = accepted(end(duration_ms=1.0), end(duration_ms=2.0), end(duration_ms=3.0))
    expected = min(variants, key=lambda a: a.digest())
    winners = set()
    for perm in itertools.permutations(variants):
        result = merge_events(perm)
        record = result.spans[(T, S)]
        assert record.conflicts == 2
        assert result.conflicts == 2
        winners.add(record.end.digest() if record.end else None)
    assert winners == {expected.digest()}


def test_equivalent_timestamp_spellings_are_duplicates_not_conflicts() -> None:
    # Canonicalization goes through the parsed model, so equal instants compare equal.
    result = merge_events(accepted(end(), end(end_time="2026-10-12T12:00:01.000000Z")))
    assert result.duplicates == 1
    assert result.conflicts == 0


def test_start_and_end_deep_merge_end_wins() -> None:
    result = merge_events(
        accepted(
            start(
                attributes={"a": 1, "b": 1},
                llm={"provider": "openai", "request_model": "m"},
                links=[{"trace_id": T, "span_id": "1111111111111111", "relation": "follows_from"}],
            ),
            end(attributes={"b": 2}, llm={"response_model": "m-2026"}),
        )
    )
    merged = result.spans[(T, S)].merged
    assert merged["attributes"] == {"a": 1, "b": 2}
    assert merged["llm"] == {"provider": "openai", "request_model": "m", "response_model": "m-2026"}
    assert merged["links"] == [{"trace_id": T, "span_id": "1111111111111111", "relation": "follows_from"}]
    assert merged["status"] == "ok"
    assert "type" not in merged


def test_arrays_are_replaced_not_concatenated() -> None:
    link = {"trace_id": T, "span_id": "1111111111111111", "relation": "follows_from"}
    other = {**link, "span_id": "2222222222222222"}
    merged = merge_events(accepted(start(links=[link]), end(links=[other]))).spans[(T, S)].merged
    assert merged["links"] == [other]


def test_identity_mismatch_between_start_and_end_is_reported_and_end_wins() -> None:
    record = merge_events(accepted(start(name="draft"), end(name="final"))).spans[(T, S)]
    assert record.mismatches == ("name",)
    assert record.identity.name == "final"


def test_start_only_and_end_only_records() -> None:
    other = "2222222222222222"
    result = merge_events(accepted(start(), end(span_id=other)))
    assert result.spans[(T, S)].has_start and not result.spans[(T, S)].has_end
    assert result.spans[(T, other)].has_end and not result.spans[(T, other)].has_start


def test_span_events_deduplicate_by_event_id() -> None:
    ev = {
        "type": "span_event",
        "event_id": "0192f4a1-7c3e-7b10-9a2d-5f6e1c0b8d41",
        "trace_id": T,
        "span_id": S,
        "name": "log",
        "time": "2026-10-12T12:00:00.5Z",
    }
    result = merge_events(accepted(ev, copy.deepcopy(ev), {**ev, "name": "different"}))
    assert result.duplicates == 1
    assert result.conflicts == 1
    assert len(result.span_events) == 1


def test_deep_merge_does_not_mutate_inputs() -> None:
    a = {"x": {"y": 1}}
    b = {"x": {"z": 2}}
    assert deep_merge(a, b) == {"x": {"y": 1, "z": 2}}
    assert a == {"x": {"y": 1}} and b == {"x": {"z": 2}}
