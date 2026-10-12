"""Content capture modes, redaction, truncation and secret scrubbing."""

from __future__ import annotations

from collections.abc import Callable

import pytest

import aobs
from aobs.export import InMemoryExporter
from aobs.privacy import RegexRedactor
from sdk_support import end_by_name, flush

SECRET_PROMPT = "Customer jane.doe@example.com paid with 4111 1111 1111 1111, key sk-proj-abcdefghijklmnopqrst"


def _run_tool_and_llm() -> None:
    @aobs.tool
    def lookup(note: str) -> dict[str, str]:
        return {"echo": note}

    with aobs.run("answer"):
        lookup(SECRET_PROMPT)
        with aobs.llm_span(provider="acme") as sp:
            sp.set_input([{"role": "user", "parts": [{"type": "text", "content": SECRET_PROMPT}]}], "messages")
            sp.set_output(SECRET_PROMPT)


def test_metadata_only_sends_no_content_bytes(sdk: InMemoryExporter) -> None:
    _run_tool_and_llm()
    flush()
    wire = b"".join(sdk.bodies).decode()
    for fragment in ("jane.doe", "4111", "sk-proj", "Customer"):
        assert fragment not in wire
    tool = end_by_name(sdk, "lookup")
    assert tool["content"] == {"input": {"status": "not_captured"}, "output": {"status": "not_captured"}}
    assert "input_fingerprint" in tool  # repeat detection still works without content


def test_redacted_mode_replaces_pii_and_secrets(make_sdk: Callable[..., InMemoryExporter]) -> None:
    sdk = make_sdk(capture="redacted")
    _run_tool_and_llm()
    flush()
    wire = b"".join(sdk.bodies).decode()
    for fragment in ("jane.doe@example.com", "4111 1111 1111 1111", "sk-proj-abcdefghijklmnopqrst"):
        assert fragment not in wire
    output = end_by_name(sdk, "chat")["content"]["output"]
    assert output["status"] == "redacted"
    assert output["redaction_count"] == 3
    assert output["value"] == ("Customer [REDACTED:email] paid with [REDACTED:card_number], key [REDACTED:api_key]")
    tool_input = end_by_name(sdk, "lookup")["content"]["input"]
    assert tool_input["format"] == "json" and tool_input["status"] == "redacted"


def test_card_redaction_requires_luhn(make_sdk: Callable[..., InMemoryExporter]) -> None:
    sdk = make_sdk(capture="redacted")
    with aobs.span("s") as sp:
        sp.set_output("order 1234 5678 9012 3456 shipped")  # fails the Luhn check
    flush()
    output = end_by_name(sdk, "s")["content"]["output"]
    assert output["value"] == "order 1234 5678 9012 3456 shipped" and "redaction_count" not in output


def test_custom_redactor(make_sdk: Callable[..., InMemoryExporter]) -> None:
    import re

    sdk = make_sdk(capture="redacted", redactors=[RegexRedactor("ticket", re.compile(r"TCK-\d+"))])
    with aobs.span("s") as sp:
        sp.set_output("see TCK-991 and TCK-992")
    flush()
    output = end_by_name(sdk, "s")["content"]["output"]
    assert output["value"] == "see [REDACTED:ticket] and [REDACTED:ticket]" and output["redaction_count"] == 2


def test_full_mode_captures_content_as_is(make_sdk: Callable[..., InMemoryExporter]) -> None:
    sdk = make_sdk(capture="full")
    _run_tool_and_llm()
    flush()
    output = end_by_name(sdk, "chat")["content"]["output"]
    assert output == {"status": "captured", "format": "text", "value": SECRET_PROMPT}
    tool = end_by_name(sdk, "lookup")["content"]
    assert tool["input"]["value"] == {"note": SECRET_PROMPT}
    assert tool["output"]["value"] == {"echo": SECRET_PROMPT}


def test_oversized_content_is_truncated_and_flagged(make_sdk: Callable[..., InMemoryExporter]) -> None:
    sdk = make_sdk(capture="full", max_content_bytes=1024)
    with aobs.span("s") as sp:
        sp.set_input("é" * 5000)  # multi-byte: truncation must not split a character
        sp.set_output({"rows": list(range(2000))})
    flush()
    content = end_by_name(sdk, "s")["content"]
    assert content["input"]["truncated"] is True
    assert len(content["input"]["value"].encode()) <= 1024
    # Structured content that does not fit becomes truncated JSON text, never a broken structure.
    assert content["output"]["format"] == "text" and content["output"]["truncated"] is True
    assert content["output"]["value"].startswith('{"rows":[0,1,2')
    assert aobs.diagnostics()["content_truncated"] == 2


def test_attributes_are_scrubbed_and_invalid_ones_dropped(sdk: InMemoryExporter) -> None:
    with aobs.span(
        "s",
        attributes={
            "app.note": "password=hunter2hunter2",
            "app.ok": 1,
            "bad key": 1,
            "app.nested": {"a": 1},
            "app.nan": float("nan"),
            "app.list": ["a", 2, True],
            "app.long": "x" * 5000,
        },
    ):
        pass
    flush()
    attributes = end_by_name(sdk, "s")["attributes"]
    assert attributes["app.note"] == "password=[REDACTED:credential]"
    assert attributes["app.list"] == ["a", 2, True]
    assert len(attributes["app.long"]) == 4096
    assert set(attributes) == {"app.note", "app.ok", "app.list", "app.long"}
    assert aobs.diagnostics()["attributes_dropped"] == 3


def test_lone_surrogates_are_cleaned(make_sdk: Callable[..., InMemoryExporter]) -> None:
    sdk = make_sdk(capture="full")
    with aobs.span("bad\udc80name") as sp:
        sp.set_output("bad\ud800text")
    flush()  # check_bodies (via end_by_name) would fail on unencodable text
    assert "�" not in end_by_name(sdk, "bad?name")["name"]


@pytest.mark.parametrize("mode", ["metadata_only", "redacted", "full"])
def test_capture_mode_is_declared_in_resource(make_sdk: Callable[..., InMemoryExporter], mode: str) -> None:
    sdk = make_sdk(capture=mode)
    with aobs.span("s"):
        pass
    flush()
    assert sdk.batches[0]["resource"]["capture"] == {"mode": mode, "error_messages": True, "stack_traces": False}
