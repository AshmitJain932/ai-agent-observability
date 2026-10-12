"""The SDK must not change the application's behaviour, whatever state it is in."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

import aobs
from aobs.export import EncodedBatch, ExportResult, InMemoryExporter


@aobs.tool
def add(a: int, b: int = 1) -> int:
    return a + b


@aobs.tool
async def async_add(a: int) -> int:
    await asyncio.sleep(0)
    return a + 1


def test_decorated_functions_work_without_init() -> None:
    assert add(1, 2) == 3
    assert asyncio.run(async_add(1)) == 2
    with aobs.run("x") as run, aobs.span("y") as sp:
        assert not run.is_recording and not sp.is_recording
        sp.set_attribute("a", 1)
        sp.record_usage(input_tokens=1)
        sp.set_error(ValueError("x"))
        sp.anything_else(1, 2)  # unknown methods are no-ops on non-recording spans
    assert aobs.flush() is True and aobs.shutdown() is True


def test_decorators_preserve_metadata_and_behaviour(sdk: InMemoryExporter) -> None:
    assert add.__name__ == "add" and add.__wrapped__ is not None  # type: ignore[attr-defined]
    assert add(2) == 3
    assert asyncio.iscoroutinefunction(async_add)
    with pytest.raises(TypeError):
        add()  # type: ignore[call-arg]  # wrong call signatures still raise the normal TypeError


def test_generators_are_passed_through_uninstrumented(sdk: InMemoryExporter) -> None:
    @aobs.tool
    def gen(n: int):  # type: ignore[no-untyped-def]
        yield from range(n)

    assert list(gen(3)) == [0, 1, 2]
    assert aobs.diagnostics()["uninstrumented_calls"] == 1


def test_disabled_by_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AOBS_ENABLED", "false")
    aobs.init(exporter=InMemoryExporter())
    assert aobs.diagnostics()["enabled"] is False
    assert add(1) == 2


def test_raising_exporter_does_not_reach_application(make_sdk: Callable[..., InMemoryExporter]) -> None:
    class Exploding(InMemoryExporter):
        def export(self, batch: EncodedBatch, timeout_s: float) -> ExportResult:
            raise MemoryError("exporter bug")

    make_sdk(exporter=Exploding(), max_export_attempts=1)
    for _ in range(10):
        assert add(1) == 2
    aobs.flush(2)
    assert aobs.diagnostics()["events_dropped_export_failed"] == 10


def test_internal_failure_at_span_start_falls_back_to_non_recording(
    sdk: InMemoryExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracer = aobs.get_tracer()
    assert tracer is not None

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(tracer, "start_span", broken)
    assert add(1) == 2
    assert aobs.diagnostics()["internal_errors"] == 1


def test_unserialisable_values_do_not_break(make_sdk: Callable[..., InMemoryExporter]) -> None:
    sdk = make_sdk(capture="full")

    class Opaque:
        def __repr__(self) -> str:
            return "<Opaque>"

    @aobs.tool
    def takes_object(x: object) -> object:
        return x

    obj = Opaque()
    assert takes_object(obj) is obj  # the very same object is returned
    aobs.flush(2)
    from sdk_support import end_by_name

    assert end_by_name(sdk, "takes_object")["content"]["input"]["value"] == {"x": "<Opaque>"}


def test_reinit_replaces_and_flushes_previous(make_sdk: Callable[..., InMemoryExporter]) -> None:
    first = make_sdk()
    with aobs.span("before"):
        pass
    second = make_sdk()
    with aobs.span("after"):
        pass
    aobs.flush(2)
    assert [e["name"] for e in first.events()] == ["before"] and first.shut_down
    assert [e["name"] for e in second.events()] == ["after"]


def test_unknown_init_argument_is_a_programming_error() -> None:
    with pytest.raises(TypeError, match=r"unknown aobs\.init"):
        aobs.init(exporter=InMemoryExporter(), max_batch_event=10)


def test_out_of_range_tuning_is_clamped(make_sdk: Callable[..., InMemoryExporter]) -> None:
    make_sdk(max_batch_events=5000)  # the schema allows at most 1,000 events per batch
    tracer = aobs.get_tracer()
    assert tracer is not None and tracer.config.max_batch_events == 1000
    with pytest.raises(aobs.ConfigError):
        make_sdk(max_batch_events=5000, strict=True)


def test_environment_name_is_normalized(make_sdk: Callable[..., InMemoryExporter]) -> None:
    sdk = make_sdk(environment="Production EU")
    with aobs.span("s"):
        pass
    aobs.flush(2)
    assert sdk.batches[0]["resource"]["environment"] == "production_eu"
