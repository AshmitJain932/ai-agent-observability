"""Exceptions, failed operations and cancellation."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable

import pytest

import aobs
from aobs.export import InMemoryExporter
from sdk_support import end_by_name, flush, only_trace


class PaymentDeclined(Exception):
    pass


def test_exception_is_recorded_and_reraised_unchanged(sdk: InMemoryExporter) -> None:
    original = PaymentDeclined("card declined")

    @aobs.tool
    def charge() -> None:
        raise original

    with pytest.raises(PaymentDeclined) as caught, aobs.run("checkout"):
        charge()
    assert caught.value is original  # same object, traceback intact
    assert caught.value.__traceback__ is not None
    flush()
    tool = end_by_name(sdk, "charge")
    assert tool["status"] == "error"
    assert tool["error"]["type"].endswith("PaymentDeclined")
    assert tool["error"]["origin"] == "tool"
    assert tool["error"]["message"] == "card declined"
    run = end_by_name(sdk, "checkout")
    assert run["status"] == "error" and run["error"]["origin"] == "application"
    assert only_trace(sdk).status == "error"


def test_handled_failure_inside_run_gives_ok_with_errors(sdk: InMemoryExporter) -> None:
    @aobs.tool
    def flaky() -> None:
        raise ConnectionError("reset")

    with aobs.run("answer"), contextlib.suppress(ConnectionError):
        flaky()
    flush()
    assert end_by_name(sdk, "answer")["status"] == "ok"
    assert only_trace(sdk).status == "ok_with_errors"


def test_timeout_errors_have_timeout_origin(sdk: InMemoryExporter) -> None:
    with pytest.raises(TimeoutError), aobs.span("call_api"):
        raise TimeoutError("slow")
    flush()
    assert end_by_name(sdk, "call_api")["error"]["origin"] == "timeout"


def test_set_error_marks_failure_without_raising(sdk: InMemoryExporter) -> None:
    with aobs.tool_span("search") as sp:
        sp.set_error("ToolReturnedError", message="no results; token=abcdefghijkl", code="E_EMPTY", retryable=False)
    flush()
    event = end_by_name(sdk, "search")
    assert event["status"] == "error"
    assert event["error"] == {
        "type": "ToolReturnedError",
        "origin": "tool",
        "message": "no results; token=[REDACTED:credential]",
        "code": "E_EMPTY",
        "retryable": False,
    }


def test_asyncio_cancellation_is_cancelled_not_error(sdk: InMemoryExporter) -> None:
    @aobs.tool
    async def wait_forever() -> None:
        await asyncio.sleep(60)

    async def main() -> None:
        task = asyncio.create_task(wait_forever())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    flush()
    event = end_by_name(sdk, "wait_forever")
    assert event["status"] == "cancelled" and "error" not in event


@pytest.mark.parametrize("exc_type", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_base_exceptions_mean_cancelled(sdk: InMemoryExporter, exc_type: type[BaseException]) -> None:
    with pytest.raises(exc_type), aobs.span("interrupted"):
        raise exc_type()
    flush()
    assert end_by_name(sdk, "interrupted")["status"] == "cancelled"


def test_exception_overrides_earlier_set_error(sdk: InMemoryExporter) -> None:
    with pytest.raises(ValueError), aobs.span("s") as sp:
        sp.set_error("Soft")
        raise ValueError("hard")
    flush()
    assert end_by_name(sdk, "s")["error"]["type"] == "ValueError"


def test_error_messages_can_be_disabled(make_sdk: Callable[..., InMemoryExporter]) -> None:
    sdk = make_sdk(capture_error_messages=False)
    with pytest.raises(ValueError), aobs.span("s"):
        raise ValueError("customer jane@example.com not found")
    flush()
    error = end_by_name(sdk, "s")["error"]
    assert "message" not in error
    assert "jane@example.com" not in sdk.bodies[0].decode()


def test_stack_traces_are_opt_in_and_scrubbed(make_sdk: Callable[..., InMemoryExporter]) -> None:
    sdk = make_sdk(capture_stack_traces=True)

    def inner() -> None:
        raise RuntimeError("Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123")

    with pytest.raises(RuntimeError), aobs.span("s"):
        inner()
    flush()
    error = end_by_name(sdk, "s")["error"]
    assert "Traceback" in error["stack"] and "in inner" in error["stack"]
    assert "abcdefghijklmnopqrstuvwxyz0123" not in sdk.bodies[0].decode()


def test_double_end_is_ignored_and_counted(sdk: InMemoryExporter) -> None:
    sp = aobs.span("once")
    sp.end()
    sp.end("error")
    flush()
    assert end_by_name(sdk, "once")["status"] == "ok"
    assert aobs.diagnostics()["span_double_end"] == 1


def test_user_exception_survives_sdk_internal_failure(sdk: InMemoryExporter, monkeypatch: pytest.MonkeyPatch) -> None:
    tracer = aobs.get_tracer()
    assert tracer is not None

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("sdk bug")

    monkeypatch.setattr(tracer.processor, "on_end", broken)
    original = KeyError("user error")
    with pytest.raises(KeyError) as caught, aobs.span("s"):
        raise original
    assert caught.value is original
    assert aobs.diagnostics()["internal_errors"] >= 1
