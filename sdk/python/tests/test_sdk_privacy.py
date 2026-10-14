"""Content capture modes, redaction, truncation and secret scrubbing."""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import aobs
from aobs import _sanitize as S
from aobs.export import InMemoryExporter
from aobs.privacy import EMAIL, RegexRedactor
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


# --------------------------------------------------------------------------- e-mail redaction in linear time

# The regex the e-mail redactor replaced: same results, but quadratic on long runs of address characters.
QUADRATIC_EMAIL_REGEX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


@settings(max_examples=3000, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.text(alphabet="aZ9._%+-@ x:/é", max_size=40))
def test_email_redactor_finds_exactly_what_the_regex_found(text: str) -> None:
    assert EMAIL(text) == QUADRATIC_EMAIL_REGEX.subn("[REDACTED:email]", text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("jane@example.com+bob@example.com", "[REDACTED:email][REDACTED:email]"),  # adjacent addresses
        ("a@b.cc9x@d.ee", "[REDACTED:email][REDACTED:email]"),
        ("mail jane.doe@mail.example.co.uk now", "mail [REDACTED:email] now"),
        ("u@d.com-1 and a@b.co9", "[REDACTED:email]-1 and [REDACTED:email]9"),
        ("not an address: a@b.c, @example.com, jane@", "not an address: a@b.c, @example.com, jane@"),
        (
            "postgresql://[REDACTED:url_credentials]@db.internal/app",
            "postgresql://[REDACTED:url_credentials]@db.internal/app",
        ),
    ],
)
def test_email_redactor_examples(text: str, expected: str) -> None:
    assert EMAIL(text)[0] == expected == QUADRATIC_EMAIL_REGEX.sub("[REDACTED:email]", text)


_EMAIL_ON_1_MIB = """
import sys
from aobs.privacy import EMAIL
texts = {"run": "a" * 2**20, "at_run": "x@" + "a" * 2**20, "dots": "a." * 2**19,
         "at_dots": "x@" + "a." * 2**19, "ats": "@" * 2**20}
EMAIL(texts[sys.argv[1]])
"""


@pytest.mark.parametrize("case", ["run", "at_run", "dots", "at_dots", "ats"])
def test_email_redactor_handles_1_mib_without_quadratic_time(case: str) -> None:
    # In a child process: a quadratic regex holds the interpreter lock, so a thread could not time out.
    try:
        subprocess.run([sys.executable, "-c", _EMAIL_ON_1_MIB, case], check=True, timeout=60)
    except subprocess.TimeoutExpired:
        pytest.fail("redacting 1 MiB took longer than 60 s (the quadratic regex needed minutes)")


# --------------------------------------------------------------------------- JWTs and private keys in linear time

QUADRATIC_JWT_REGEX = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}")
QUADRATIC_PRIVATE_KEY_REGEX = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")
_JWT_PIECES = ["eyJ", "a", "Z", "9", "_", "-", ".", " ", "é", "²", "x"]  # é and ² are word characters for \b
_KEY_PIECES = ["-----BEGIN ", "-----END ", "RSA ", "PRIVATE KEY-----", "-", "A", " ", "\n", "x"]


@settings(max_examples=3000, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(st.sampled_from(_JWT_PIECES), max_size=40).map("".join))
def test_jwt_scanner_finds_exactly_what_the_regex_found(text: str) -> None:
    assert S._scrub_jwts(text, "[REDACTED:jwt]") == QUADRATIC_JWT_REGEX.subn("[REDACTED:jwt]", text)


@settings(max_examples=3000, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(st.sampled_from(_KEY_PIECES), max_size=25).map("".join))
def test_private_key_scanner_finds_what_the_regex_found_and_fails_closed(text: str) -> None:
    expected, count = QUADRATIC_PRIVATE_KEY_REGEX.subn("[R]", text)
    unclosed = S._PRIVATE_KEY_BEGIN.search(expected)  # the regex left it: no END marker follows it
    if unclosed:
        expected, count = expected[: unclosed.start()] + "[R]", count + 1
    assert S._scrub_private_keys(text, "[R]") == (expected, count)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJl", "[REDACTED:jwt]"),
        ("t=eyJhbGciOiJ9.eyJzdWIiOiIx.c2lnbmF0.eyJhbGciOiJ9.eyJzdWIiOiIx.c2lnbmF0", "t=[REDACTED:jwt].[REDACTED:jwt]"),
        ("aeyJhbGciOiJ9.eyJzdWIiOiIx.c2lnbmF0", "aeyJhbGciOiJ9.eyJzdWIiOiIx.c2lnbmF0"),  # no word boundary
        ("a-eyJhbGciOiJ9.eyJzdWIiOiIx.c2lnbmF0", "a-[REDACTED:jwt]"),
        ("-----BEGIN RSA PRIVATE KEY-----\nAAA\n-----END RSA PRIVATE KEY----- ok", "[REDACTED:private_key] ok"),
        ("cfg -----BEGIN EC PRIVATE KEY-----\nMIIEowSECRET\nMORE", "cfg [REDACTED:private_key]"),  # no END
    ],
)
def test_jwt_and_private_key_examples(text: str, expected: str) -> None:
    assert S.scrub_secrets(text)[0] == expected


_SECRETS_ON_1_MIB = """
import sys
from aobs import _sanitize as S
from aobs._diagnostics import Diagnostics
texts = {"jwt_run": "-eyJ" * 2**18, "jwt_dots": "eyJaaaaa." * 2**17,
         "keys": "-----BEGIN RSA PRIVATE KEY-----" * 2**15}
case, where = sys.argv[1:]
if where == "scrub":
    S.scrub_secrets(texts[case])
else:
    assert "app.body" in S.attributes({"app.body": texts[case]}, Diagnostics())
"""


@pytest.mark.parametrize("where", ["scrub", "attribute"])
@pytest.mark.parametrize("case", ["jwt_run", "jwt_dots", "keys"])
def test_secret_scrubbing_handles_1_mib_without_quadratic_time(case: str, where: str) -> None:
    # In a child process: a quadratic regex holds the interpreter lock, so a thread could not time out.
    try:
        subprocess.run([sys.executable, "-c", _SECRETS_ON_1_MIB, case, where], check=True, timeout=60)
    except subprocess.TimeoutExpired:
        pytest.fail("scrubbing 1 MiB took longer than 60 s (the quadratic regexes needed minutes)")


def test_long_attributes_are_cut_before_scrubbing_without_leaking_what_the_cut_split(sdk: InMemoryExporter) -> None:
    shrinking = ("sk-" + "a" * 97 + " ") * 80  # 8080 characters that scrub down to 1520
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJ" + "p" * 200 + ".c2lnbmF0dXJlXzEyMw"  # its signature is past the cut
    key = "-----BEGIN RSA PRIVATE KEY-----\n" + "K" * 10_000 + "\n-----END RSA PRIVATE KEY-----"
    assert len(shrinking) < 2 * S.MAX_ATTRIBUTE_STRING < len(shrinking) + len(jwt)
    with aobs.span("s", attributes={"app.jwt": shrinking + jwt + " tail", "app.key": "id " + key + " tail"}):
        pass
    flush()
    attributes = end_by_name(sdk, "s")["attributes"]
    assert attributes["app.jwt"].startswith("[REDACTED:api_key]") and len(attributes["app.jwt"]) < 2000
    assert "eyJhbGci" not in attributes["app.jwt"] and "ppp" not in attributes["app.jwt"]
    assert "KKKK" not in attributes["app.key"]  # its END marker was past the cut: removed to the end
