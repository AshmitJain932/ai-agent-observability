"""Exceptions, failed operations and cancellation."""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import sys
from collections.abc import Callable
from typing import Any

import pytest

import aobs
from aobs import _sanitize as S
from aobs.export import InMemoryExporter
from aobs.privacy import (
    MESSAGE_REDACTION_INPUT,
    STACK_REDACTION_INPUT,
    redact_error_message,
    redact_stack,
    scrub_error_text,
)
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


PERSONAL = "customer jane.doe@example.com paid with 4111 1111 1111 1111 (backup 5500-0000-0000-0004)"
PERSONAL_FRAGMENTS = ("jane.doe@example.com", "4111 1111 1111 1111", "5500-0000-0000-0004")


@pytest.mark.parametrize("mode", ["metadata_only", "redacted", "full"])
@pytest.mark.parametrize("default_redactors", [True, False])
def test_emails_and_card_numbers_never_leave_in_error_text(
    make_sdk: Callable[..., InMemoryExporter], mode: str, default_redactors: bool
) -> None:
    sdk = make_sdk(capture=mode, capture_stack_traces=True, use_default_redactors=default_redactors)

    def inner() -> None:
        raise LookupError(PERSONAL)

    with pytest.raises(LookupError), aobs.span("raised"):
        inner()
    with aobs.span("set_error") as sp:
        sp.set_error("PaymentFailed", message=PERSONAL)
    with aobs.span("set_error_exc") as sp:
        sp.set_error(ValueError(PERSONAL))
    flush()
    wire = b"".join(sdk.bodies).decode()
    for fragment in PERSONAL_FRAGMENTS:
        assert fragment not in wire, (mode, fragment)
    raised = end_by_name(sdk, "raised")["error"]
    assert raised["message"] == (
        "customer [REDACTED:email] paid with [REDACTED:card_number] (backup [REDACTED:card_number])"
    )
    assert "[REDACTED:email]" in raised["stack"] and "in inner" in raised["stack"]
    assert end_by_name(sdk, "set_error")["error"]["message"] == raised["message"]
    assert end_by_name(sdk, "set_error_exc")["error"]["message"] == raised["message"]


def test_error_text_keeps_numbers_that_are_not_card_numbers(sdk: InMemoryExporter) -> None:
    with pytest.raises(ValueError), aobs.span("s"):
        raise ValueError("order 1234 5678 9012 3456 for user 42 failed")  # fails the Luhn check
    flush()
    assert end_by_name(sdk, "s")["error"]["message"] == "order 1234 5678 9012 3456 for user 42 failed"


# --------------------------------------------------------------------------- long error text


# A frozen redaction cannot be timed out from another thread (a regex holds the interpreter lock for
# the whole match), so these run in a child process that is killed after a generous timeout.
_RECORD_HUGE_ERROR = """
import json, sys
import aobs
from aobs.export import InMemoryExporter
exporter = InMemoryExporter()
aobs.init(service_name="t", exporter=exporter, capture_stack_traces=sys.argv[2] == "stacks")
if sys.argv[1] == "raise":
    try:
        with aobs.span("s"):
            raise ValueError("payload " + "a" * 2**20 + " for jane.doe@example.com")
    except ValueError:
        pass
else:
    with aobs.span("s") as sp:
        sp.set_error("Failed", message="reason: " + "x@" * 2**19)
assert aobs.flush(10)
(end,) = [e for e in exporter.events() if e["type"] == "span_end"]
print(json.dumps({"error": end["error"], "wire": b"".join(exporter.bodies).decode()}))
"""


def _record_huge_error(how: str, stacks: bool, timeout_s: float = 60.0) -> dict[str, Any]:
    try:
        out = subprocess.run(
            [sys.executable, "-c", _RECORD_HUGE_ERROR, how, "stacks" if stacks else "no-stacks"],
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"recording a 1 MiB error took longer than {timeout_s} s: the application would freeze")
    result: dict[str, Any] = json.loads(out.stdout)
    return result


@pytest.mark.parametrize("stacks", [False, True])
def test_a_1_mib_unbroken_exception_message_does_not_freeze_the_application(stacks: bool) -> None:
    result = _record_huge_error("raise", stacks)
    error = result["error"]
    assert error["message"] == "payload"  # the start is kept; the 1 MiB token the cut went through is not
    assert len(error.get("stack", "")) <= S.MAX_STACK
    assert "jane.doe" not in result["wire"]


def test_a_1_mib_message_passed_to_set_error_is_bounded_too() -> None:
    assert _record_huge_error("set_error", stacks=False)["error"]["message"] == "reason:"


# Many addresses shrink to "[REDACTED:email]" during redaction, so the place where long text was cut
# ends up inside what is kept: whatever the cut split must still never appear.
_SHRINKING = ("x" * 40 + "@example.com ") * 1400  # 74 KiB, longer than both input limits
_SPLIT_AT_END = [  # (text around the cut, the cut position within it, a fragment that must not leak)
    ("jane.doe@example.com", 12, "jane.doe"),
    ("4111 1111 1111 1111", 12, "4111"),
    ("Bearer abcdefghijklmnop", 10, "abc"),
    ("password=hunter2secret", 15, "hunter"),
    ("-----BEGIN RSA PRIVATE KEY-----\nSECRETKEYBODY\nMORE\n-----END RSA PRIVATE KEY-----", 45, "SECRETKEY"),
]


@pytest.mark.parametrize(("secret", "cut", "fragment"), _SPLIT_AT_END)
def test_a_secret_split_by_the_message_cut_never_leaks(secret: str, cut: int, fragment: str) -> None:
    filler = _SHRINKING[: MESSAGE_REDACTION_INPUT - cut - 1] + " "
    text = filler + secret + " and more text after it"
    assert len(filler) + cut == MESSAGE_REDACTION_INPUT  # the cut goes through the secret
    message = redact_error_message(text)
    assert fragment not in message
    assert message.startswith("[REDACTED:email]") and len(message) <= S.MAX_ERROR_MESSAGE
    assert len(scrub_error_text(text[:MESSAGE_REDACTION_INPUT])) < S.MAX_ERROR_MESSAGE  # the cut is in view


_SPLIT_AT_START = [  # (text around the cut, the cut position within it, a fragment that must not leak)
    ("4111 1111 1111 1111", 7, "1111 1111"),
    ("Bearer abcdefghijklmnop", 6, "abcdefghijk"),
    ("password=hunter2secret", 12, "er2secret"),
    ("-----BEGIN RSA PRIVATE KEY-----\nSECRETKEY\nBODYPART\n-----END RSA PRIVATE KEY-----", 42, "BODYPART"),
    # The keyword is cut off and its value sits further than the margin after it.
    ("Bearer" + " " * 100 + "abcdefghijklmnop", 6, "abcdefghij"),
    ("Bearer" + " " * 100 + "abcdefghijklmnop", 3, "abcdefghij"),
    ("password =" + " " * 100 + "hunter2secret", 8, "hunter2"),
    ("password=" + "\t" * 80 + '"hunter2secret"', 9, "hunter2"),
    ("token:" + "\n" * 70 + "  hunter2secret", 6, "hunter2"),
]


@pytest.mark.parametrize(("secret", "cut", "fragment"), _SPLIT_AT_START)
def test_a_secret_split_by_the_stack_cut_never_leaks(secret: str, cut: int, fragment: str) -> None:
    filler = " " + _SHRINKING[: STACK_REDACTION_INPUT - (len(secret) - cut) - 1]
    text = "Traceback (most recent call last):\n" + secret + filler
    assert len(text) - STACK_REDACTION_INPUT == len("Traceback (most recent call last):\n") + cut
    stack = redact_stack(text)
    assert fragment not in stack
    assert "[REDACTED:email]" in stack and len(stack) <= S.MAX_STACK
    assert len(scrub_error_text(text[-STACK_REDACTION_INPUT:])) < S.MAX_STACK  # the cut is in view


def test_error_text_within_the_limits_is_redacted_exactly_as_before() -> None:
    text = "customer jane.doe@example.com, card 4111 1111 1111 1111, key sk-proj-abcdefghijklmnopqrst"
    expected = "customer [REDACTED:email], card [REDACTED:card_number], key [REDACTED:api_key]"
    assert redact_error_message(text) == scrub_error_text(text) == expected
    assert redact_stack(text) == expected
    long_message = "y " * (MESSAGE_REDACTION_INPUT // 2)  # at the limit: not cut before redaction
    assert redact_error_message(long_message) == long_message[: S.MAX_ERROR_MESSAGE]


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
