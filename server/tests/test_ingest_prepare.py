"""M4: the pure ingest steps (no database) — privacy ceiling, secret scrubbing, content size limit,
NUL handling, newer-minor fields, clock skew — and that their output always stays schema-valid."""

from __future__ import annotations

import copy
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_merge import RESOURCE, accepted, end, start

from aobs._sanitize import scrub_secrets as sdk_scrub_secrets
from aobs.privacy import CapturePolicy
from aobs_server.config import ConfigError, Settings
from aobs_server.ingest.prepare import (
    MAX_ATTRIBUTE_CHARS,
    MAX_CONTENT_PART_BYTES,
    MAX_ERROR_MESSAGE_CHARS,
    clock_skew_suspected,
    content_part_size,
    prepare_event,
    prepare_events,
    prepare_resource,
)
from aobs_server.ingest.scrub import scrub_secrets, scrub_to_fit
from aobs_server.model import validate_batch
from aobs_server.model.versioning import AcceptedEvent
from aobs_server.project_settings import ProjectSettings
from support import SCHEMA_VALIDATOR, fixture_files, fixture_id, fixture_path, load_fixture

FULL = ProjectSettings(capture_ceiling="full", error_messages=True, stack_traces=True)
REDACTED = ProjectSettings(capture_ceiling="redacted")
METADATA = ProjectSettings()  # the default: metadata_only, messages on, stacks off
CEILINGS = {"metadata_only": METADATA, "redacted": REDACTED, "full": FULL}


def prep(event: dict[str, Any], settings: ProjectSettings) -> tuple[dict[str, Any], Counter[str]]:
    counters: Counter[str] = Counter()
    (item,) = accepted(event)
    out = prepare_event(item, settings, counters)
    return out.canonical(), counters


def tool_end(**kw: Any) -> dict[str, Any]:
    return end(**{"kind": "tool", "name": "lookup", "tool": {"name": "lookup"}, **kw})


CAPTURED = {"status": "captured", "format": "text", "value": "order 1042 for jane@example.com"}
REDACTED_PART = {
    "status": "redacted",
    "format": "text",
    "value": "order 1042 for [REDACTED:email]",
    "redaction_count": 1,
}


# --------------------------------------------------------------------------- privacy ceiling


@pytest.mark.parametrize(
    ("ceiling", "part", "kept"),
    [
        ("metadata_only", CAPTURED, False),
        ("metadata_only", REDACTED_PART, False),
        ("redacted", CAPTURED, False),  # cannot verify that captured content was redacted
        ("redacted", REDACTED_PART, True),
        ("full", CAPTURED, True),
        ("full", REDACTED_PART, True),
    ],
)
def test_content_is_capped_by_the_project_ceiling(ceiling: str, part: dict[str, Any], kept: bool) -> None:
    body, counters = prep(tool_end(content={"input": part, "output": part}), CEILINGS[ceiling])
    if kept:
        assert body["content"] == {"input": part, "output": part}
        assert counters["content_dropped_policy"] == 0
    else:
        assert body["content"] == {"input": {"status": "not_captured"}, "output": {"status": "not_captured"}}
        assert counters["content_dropped_policy"] == 2


@pytest.mark.parametrize("ceiling", ["metadata_only", "redacted", "full"])
@pytest.mark.parametrize("status", ["not_captured", "unavailable"])
def test_contentless_parts_pass_through(ceiling: str, status: str) -> None:
    body, counters = prep(tool_end(content={"input": {"status": status}}), CEILINGS[ceiling])
    assert body["content"] == {"input": {"status": status}}
    assert not counters


ERROR = {"type": "ValueError", "message": "bad input", "stack": "Traceback ...\nValueError: bad input"}


@pytest.mark.parametrize(
    ("messages", "stacks", "expected_keys"),
    [
        (True, False, {"type", "message"}),
        (False, False, {"type"}),
        (True, True, {"type", "message", "stack"}),
        (False, True, {"type", "stack"}),
    ],
)
def test_error_messages_and_stacks_follow_project_flags(messages: bool, stacks: bool, expected_keys: set[str]) -> None:
    settings = ProjectSettings(capture_ceiling="full", error_messages=messages, stack_traces=stacks)
    body, counters = prep(end(status="error", error=ERROR), settings)
    assert set(body["error"]) == expected_keys
    assert counters["error_messages_dropped_policy"] == (0 if messages else 1)
    assert counters["stack_traces_dropped_policy"] == (0 if stacks else 1)


def test_policy_never_drops_metadata() -> None:
    event = end(
        usage={"input_tokens": 5, "source": "provider_reported"},
        llm={"provider": "openai", "request_model": "m"},
        attributes={"app.tier": "gold"},
    )
    body, _ = prep(event, METADATA)
    assert body["usage"] == {"input_tokens": 5, "source": "provider_reported"}
    assert body["llm"] == {"provider": "openai", "request_model": "m"}
    assert body["attributes"] == {"app.tier": "gold"}


def test_digest_does_not_encode_content_the_policy_removed() -> None:
    a, _ = prep(tool_end(content={"input": {**CAPTURED, "value": "order 1042"}}), METADATA)
    b, _ = prep(tool_end(content={"input": {**CAPTURED, "value": "order 9999"}}), METADATA)
    assert a == b  # same stored form, so the same digest: nothing to enumerate


# --------------------------------------------------------------------------- content size


def test_content_part_size_matches_the_sdk_measurement() -> None:
    assert content_part_size("é") == 2
    assert content_part_size('a"b\n') == 4  # raw UTF-8 bytes, not the JSON-escaped form
    assert content_part_size({"k": "é", "n": [1, 2.5]}) == len('{"k":"é","n":[1,2.5]}'.encode())


def test_sdk_output_at_its_largest_setting_is_never_dropped() -> None:
    sdk = CapturePolicy(mode="full", max_content_bytes=MAX_CONTENT_PART_BYTES)
    tricky = ('quote " backslash \\ newline \n tab \t é 😀 ' * 4000)[: MAX_CONTENT_PART_BYTES * 2]
    parts = [
        sdk.content_part(tricky, "text")[0],  # truncated string: escaping would push JSON over 64 KiB
        sdk.content_part({"rows": [tricky[:1000]] * 200}, "json")[0],  # truncated structure -> text
        sdk.content_part([{"role": "user", "parts": [{"type": "text", "content": "x" * 60_000}]}], "messages")[0],
    ]
    for part in parts:
        body, counters = prep(tool_end(content={"input": part}), FULL)
        assert body["content"]["input"] == part
        assert counters["content_dropped_oversize"] == 0


@pytest.mark.parametrize("value", ["x" * MAX_CONTENT_PART_BYTES, {"k": "x" * (MAX_CONTENT_PART_BYTES - 8)}])
def test_content_at_the_limit_is_kept(value: Any) -> None:
    assert content_part_size(value) == MAX_CONTENT_PART_BYTES
    fmt = "text" if isinstance(value, str) else "json"
    part = {"status": "captured", "format": fmt, "value": value}
    body, counters = prep(tool_end(content={"output": part}), FULL)
    assert body["content"]["output"] == part and not counters


@pytest.mark.parametrize("value", ["x" * (MAX_CONTENT_PART_BYTES + 1), {"k": "x" * (MAX_CONTENT_PART_BYTES - 7)}])
def test_content_over_the_limit_keeps_the_span_and_drops_the_part(value: Any) -> None:
    fmt = "text" if isinstance(value, str) else "json"
    part = {"status": "captured", "format": fmt, "value": value}
    body, counters = prep(tool_end(content={"input": CAPTURED, "output": part}), FULL)
    assert body["content"] == {"input": CAPTURED, "output": {"status": "not_captured"}}
    assert body["span_id"] == end()["span_id"] and body["status"] == "ok"
    assert counters["content_dropped_oversize"] == 1


# --------------------------------------------------------------------------- secret scrubbing

SECRETS = [
    "Authorization: Bearer abcdefghijklmnop1234",
    "key sk-proj1234567890abcdefghij",
    "AKIAABCDEFGHIJKLMNOP",
    "ghp_" + "a" * 36,
    "xoxb-1234567890-abcdef",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlXzEyMw",
    "postgresql://admin:hunter2pw@db.internal/app",
    "password=correcthorse",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
]
SECRET_FRAGMENTS = [
    "abcdefghijklmnop1234",
    "proj1234567890abcdefghij",
    "AKIAABCDEFGHIJKLMNOP",
    "a" * 36,
    "1234567890-abcdef",
    "c2lnbmF0dXJlXzEyMw",
    "hunter2pw",
    "correcthorse",
    "MIIEow",
]


@pytest.mark.parametrize(("text", "fragment"), list(zip(SECRETS, SECRET_FRAGMENTS, strict=True)))
def test_server_and_sdk_scrubbers_remove_the_same_secrets(text: str, fragment: str) -> None:
    server, n_server = scrub_secrets(f"context {text} trailing")
    sdk, n_sdk = sdk_scrub_secrets(f"context {text} trailing")
    assert fragment not in server and fragment not in sdk
    assert n_server >= 1 and n_sdk >= 1
    assert server == sdk


def test_server_also_scrubs_aobs_api_keys() -> None:
    text, n = scrub_secrets("leaked aobs_ik_" + "Q" * 43)
    assert "Q" * 20 not in text and n == 1


def test_scrubbed_text_is_cut_back_to_its_limit() -> None:
    text = "x" * (MAX_ATTRIBUTE_CHARS - 12) + " password=abcdef"
    out, n = scrub_to_fit(text, MAX_ATTRIBUTE_CHARS)
    assert n == 1 and len(out) == MAX_ATTRIBUTE_CHARS and "abcdef" not in out


def test_attributes_messages_and_stacks_are_scrubbed() -> None:
    event = end(
        status="error",
        attributes={"app.note": "token=supersecret1", "app.list": ["ok", "Bearer abcdefghijklmnop"], "app.n": 3},
        error={
            **ERROR,
            "message": "x" * (MAX_ERROR_MESSAGE_CHARS - 16) + " password=abcdef",
            "stack": "sk-" + "z" * 30,
        },
    )
    body, counters = prep(event, FULL)
    assert "supersecret1" not in body["attributes"]["app.note"]
    assert "abcdefghijklmnop" not in body["attributes"]["app.list"][1]
    assert body["attributes"]["app.n"] == 3
    assert "abcdef" not in body["error"]["message"] and len(body["error"]["message"]) == MAX_ERROR_MESSAGE_CHARS
    assert "z" * 30 not in body["error"]["stack"]
    assert counters["secrets_scrubbed"] == 4


def test_span_event_attributes_are_scrubbed() -> None:
    event = {
        "type": "span_event",
        "event_id": "0192f4a1-7c3e-7b10-9a2d-5f6e1c0b8d41",
        "trace_id": end()["trace_id"],
        "span_id": end()["span_id"],
        "name": "log",
        "time": "2026-10-12T12:00:00.5Z",
        "attributes": {"msg": "api_key: abcdef123456"},
    }
    body, counters = prep(event, METADATA)
    assert "abcdef123456" not in body["attributes"]["msg"] and counters["secrets_scrubbed"] == 1


def test_redacted_content_is_scrubbed_again_and_counted() -> None:
    part = {"status": "redacted", "format": "messages", "value": [{"role": "user", "parts": [{"c": "sk-" + "k" * 24}]}]}
    body, counters = prep(tool_end(content={"input": part}), REDACTED)
    stored = body["content"]["input"]
    assert "k" * 24 not in str(stored["value"])
    assert stored["redaction_count"] == 1 and counters["secrets_scrubbed"] == 1


def test_redaction_count_is_capped_at_the_schema_maximum() -> None:
    part = {"status": "redacted", "format": "text", "value": "password=abcdefgh", "redaction_count": 1_000_000}
    body, _ = prep(tool_end(content={"input": part}), REDACTED)
    assert body["content"]["input"]["redaction_count"] == 1_000_000


def test_full_content_is_stored_as_sent() -> None:
    part = {"status": "captured", "format": "text", "value": "password=abcdefgh"}
    body, counters = prep(tool_end(content={"input": part}), FULL)
    assert body["content"]["input"] == part and not counters


# --------------------------------------------------------------------------- NUL characters


def test_nul_characters_are_replaced_everywhere() -> None:
    event = tool_end(
        name="a\x00b",
        attributes={"app.x": "c\x00d", "app.y": ["\x00"]},
        error={"type": "E\x00", "message": "m\x00"},
        status="error",
        content={"input": {"status": "captured", "format": "json", "value": {"k\x00": ["v\x00"]}}},
    )
    body, counters = prep(event, FULL)
    assert "\x00" not in repr(body)
    assert body["name"] == "a�b" and body["content"]["input"]["value"] == {"k�": ["v�"]}
    assert counters["nul_characters_replaced"] == 7


def test_resource_nul_characters_are_replaced() -> None:
    counters: Counter[str] = Counter()
    stored = prepare_resource({**RESOURCE, "service_name": "s\x00"}, {}, METADATA, counters)
    assert stored["service_name"] == "s�" and counters["nul_characters_replaced"] == 1


# --------------------------------------------------------------------------- newer-minor fields


def _lenient_batch() -> Any:
    payload = load_fixture(fixture_path("versioning", "newer_minor_unknown_fields"))["payload"]
    result = validate_batch(payload)
    assert result.ok and result.lenient and any(a.extra for a in result.accepted)
    return result


@pytest.mark.parametrize(("ceiling", "kept"), [("metadata_only", False), ("redacted", False), ("full", True)])
def test_unknown_fields_from_newer_minors_are_kept_only_under_full(ceiling: str, kept: bool) -> None:
    result = _lenient_batch()
    prepared = prepare_events(result.accepted, CEILINGS[ceiling])
    with_extra = [a for a in result.accepted if a.extra]
    assert len(prepared.accepted) == len(result.accepted) and not prepared.rejected
    by_index = {a.index: a for a in prepared.accepted}
    for original in with_extra:
        assert (by_index[original.index].extra == original.extra) is kept
        assert bool(by_index[original.index].extra) is kept
    assert prepared.counters["unknown_fields_dropped_policy"] == (0 if kept else len(with_extra))

    counters: Counter[str] = Counter()
    resource = prepare_resource(
        result.envelope.resource.model_dump(mode="json", exclude_unset=True),
        dict(result.envelope_extra),
        CEILINGS[ceiling],
        counters,
    )
    assert ("__extra__" in resource) is kept


# --------------------------------------------------------------------------- whole fixtures


@pytest.mark.parametrize("path", fixture_files("valid"), ids=fixture_id)
@pytest.mark.parametrize("ceiling", ["metadata_only", "redacted", "full"])
def test_prepared_fixtures_stay_valid_against_schema_and_model(path: Path, ceiling: str) -> None:
    payload = load_fixture(path)["payload"]
    result = validate_batch(payload)
    prepared = prepare_events(result.accepted, CEILINGS[ceiling])
    assert not prepared.rejected and len(prepared.accepted) == len(result.accepted)
    events = []
    for item in prepared.accepted:
        body = item.canonical()
        body.pop("__extra__", None)
        events.append(body)
    rebuilt = {**copy.deepcopy(payload), "schema_version": "1.0", "events": events}
    rebuilt["resource"] = result.envelope.resource.model_dump(mode="json", exclude_unset=True)
    assert list(SCHEMA_VALIDATOR.iter_errors(rebuilt)) == []
    again = validate_batch(rebuilt)
    assert again.ok and not again.rejected


@pytest.mark.parametrize("path", fixture_files("valid"), ids=fixture_id)
def test_full_ceiling_leaves_clean_fixtures_unchanged(path: Path) -> None:
    result = validate_batch(load_fixture(path)["payload"])
    prepared = prepare_events(result.accepted, FULL)
    assert [a.digest() for a in prepared.accepted] == [a.digest() for a in result.accepted]
    assert sum(prepared.counters.values()) == 0


@pytest.mark.parametrize("path", fixture_files("valid"), ids=fixture_id)
def test_preparation_is_deterministic(path: Path) -> None:
    result = validate_batch(load_fixture(path)["payload"])
    first = prepare_events(result.accepted, METADATA)
    second = prepare_events(tuple(reversed(result.accepted)), METADATA)
    assert sorted(a.digest() for a in first.accepted) == sorted(a.digest() for a in second.accepted)
    assert first.counters == second.counters


def test_original_events_are_not_mutated() -> None:
    (item,) = accepted(tool_end(content={"input": CAPTURED}, attributes={"a.b": "password=abcdefgh"}))
    before = item.canonical()
    prepare_event(item, METADATA, Counter())
    assert item.canonical() == before
    assert isinstance(item, AcceptedEvent)


# --------------------------------------------------------------------------- clock skew


SENT = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("offset_s", "suspected"), [(0, False), (299.9, False), (-299.9, False), (300.1, True), (-300.1, True)]
)
def test_clock_skew_is_flagged_beyond_the_threshold_in_either_direction(offset_s: float, suspected: bool) -> None:
    assert clock_skew_suspected(SENT, SENT + timedelta(seconds=offset_s), 300) is suspected


def test_clock_skew_threshold_setting() -> None:
    assert Settings.from_env({}).clock_skew_threshold_s == 300
    assert Settings.from_env({"AOBS_CLOCK_SKEW_THRESHOLD_S": "60"}).clock_skew_threshold_s == 60
    for bad in ("0", "abc", "nan", "inf", "-5"):
        with pytest.raises(ConfigError, match="AOBS_CLOCK_SKEW_THRESHOLD_S"):
            Settings.from_env({"AOBS_CLOCK_SKEW_THRESHOLD_S": bad})


def test_start_events_are_prepared_too() -> None:
    body, counters = prep(start(attributes={"k": "secret=abcdefgh"}), METADATA)
    assert "abcdefgh" not in body["attributes"]["k"] and counters["secrets_scrubbed"] == 1
