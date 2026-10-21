"""POST /v1/ingest against real PostgreSQL: auth, isolation, validation, limits, privacy,
idempotency, ordering, rollback and the stored result."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, text

from aobs_server.api import app as api_app
from aobs_server.config import Settings
from aobs_server.model import assemble, merge_events, validate_batch
from aobs_server.model.canonical import content_digest
from aobs_server.storage import tenancy
from aobs_server.storage.traces import load_trace_events
from ingest_support import (
    NOW,
    batch,
    count_rows,
    database_text,
    end,
    make_collector,
    point,
    sid,
    simple_trace,
    start,
    stored_state,
    tid,
)
from tenants import make_key

TRACE = tid(0xA1)


def problem(response: Any) -> dict[str, Any]:
    assert response.headers["content-type"].startswith("application/problem+json"), response.text
    body: dict[str, Any] = response.json()
    return body


def trace_row(engine: Engine, project_id: Any, trace_id: str = TRACE) -> dict[str, Any]:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT * FROM traces WHERE project_id = :p AND trace_id = :t"), {"p": project_id, "t": trace_id}
        ).one()
    return dict(row._mapping)


# --------------------------------------------------------------------------- success and read-back


def test_authenticated_batch_is_stored_and_summarized(engine: Engine) -> None:
    c = make_collector(engine)
    response = c.post(batch(*simple_trace(TRACE), batch_id="0b0e8f6e-4a9b-4c1e-9b7a-2f9d8c7b6a51"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["counts"] == {"received": 7, "accepted": 7, "duplicate": 0, "dropped": 0, "rejected": 0}
    assert body["rejected"] == [] and body["dropped"] == [] and body["trace_ids"] == [TRACE]
    assert body["batch_id"] == "0b0e8f6e-4a9b-4c1e-9b7a-2f9d8c7b6a51"
    assert body["received_at"] == NOW.isoformat().replace("+00:00", "Z")
    assert body["clock_skew_suspected"] is False

    row = trace_row(engine, c.project_id)
    assert row["status"] == "ok" and row["root_name"] == "answer_ticket" and row["agent_name"] == "triage"
    assert (row["span_count"], row["open_span_count"], row["error_count"], row["span_event_count"]) == (3, 0, 0, 1)
    assert row["kind_counts"] == {"agent": 1, "llm": 1, "tool": 1}
    assert row["tokens"]["input_tokens"] == {"value": 10, "reported_spans": 1, "expected_spans": 1,
                                             "estimated": False, "complete": True}  # fmt: skip
    assert row["environments"] == ["dev"] and row["session_id"] == "s-1"
    assert count_rows(engine, c.project_id) | {"ingest_diagnostics": 0} == {
        "traces": 1, "spans": 3, "span_events": 1, "span_content": 0, "resources": 1, "environments": 1,
        "ingest_diagnostics": 0,
    }  # fmt: skip
    with engine.connect() as conn:
        span = conn.execute(text("SELECT * FROM spans WHERE span_id = :s"), {"s": sid(3)}).one()._mapping
    assert (span["kind"], span["provider"], span["status"], span["duration_ms"]) == ("llm", "openai", "ok", 300.0)


def test_stored_trace_reads_back_equal_to_the_assembled_batch(engine: Engine) -> None:
    c = make_collector(engine)
    sent = batch(*simple_trace(TRACE))
    assert c.post(sent).status_code == 200
    with engine.connect() as conn:
        stored = load_trace_events(conn, c.project_id, TRACE)
    expected = assemble(merge_events(validate_batch(sent).accepted), NOW)[TRACE].to_dict()
    assert assemble(merge_events(stored), NOW)[TRACE].to_dict() == expected
    with engine.connect() as conn:  # stored digests can be recomputed from the stored bodies
        for row in conn.execute(text("SELECT start_event, start_digests FROM spans WHERE start_event IS NOT NULL")):
            assert content_digest(row.start_event) in row.start_digests


# --------------------------------------------------------------------------- authentication


@pytest.mark.parametrize(
    "authorization",
    [None, "", "Basic abc", "Bearer", "Bearer not-a-key", "Bearer aobs_ik_" + "A" * 43, "revoked"],
    ids=["missing", "empty", "basic", "no-token", "malformed", "unknown", "revoked"],
)
def test_missing_malformed_unknown_and_revoked_keys_get_one_401(engine: Engine, authorization: str | None) -> None:
    c = make_collector(engine)
    headers = {"Content-Type": "application/json"}
    if authorization == "revoked":
        key_id, secret = make_key(engine, c.project_id)
        with engine.begin() as conn:
            assert tenancy.revoke_api_key(conn, key_id, NOW)
        authorization = f"Bearer {secret}"
    if authorization is not None:
        headers["Authorization"] = authorization
    response = c.client.post("/v1/ingest", content=json.dumps(batch(*simple_trace(TRACE))), headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Bearer realm="aobs"'
    assert problem(response) == {"type": "urn:aobs:problem:unauthorized", "title": "Missing or invalid API key",
                                 "status": 401, "detail": "Missing or invalid API key"}  # fmt: skip
    assert count_rows(engine)["traces"] == 0


def test_a_key_without_the_ingest_scope_is_forbidden(engine: Engine) -> None:
    c = make_collector(engine)
    response = c.post(batch(*simple_trace(TRACE)), key=c.key_for(scopes=("read",)))
    assert response.status_code == 403 and "'ingest' scope" in problem(response)["detail"]
    assert count_rows(engine)["traces"] == 0


def test_an_environment_bound_key_writes_only_its_environment(engine: Engine) -> None:
    c = make_collector(engine)
    dev_key = c.key_for(environment="dev")
    response = c.post(batch(*simple_trace(TRACE), environment="production"), key=dev_key)
    assert response.status_code == 403 and "'dev' environment" in problem(response)["detail"]
    assert count_rows(engine)["traces"] == 0
    assert c.post(batch(*simple_trace(TRACE), environment="dev"), key=dev_key).status_code == 200


# --------------------------------------------------------------------------- tenant isolation


def test_projects_are_isolated_even_with_identical_trace_ids(engine: Engine) -> None:
    a = make_collector(engine, name="a")
    b = make_collector(engine, settings=a.settings, name="b")
    assert a.post(batch(*simple_trace(TRACE))).status_code == 200
    late_child = [start(TRACE, sid(9), sid(1), name="other", at=0.2), end(TRACE, sid(9), sid(1), name="other", at=0.2,
                                                                          status="error")]  # fmt: skip
    assert b.post(batch(*late_child)).status_code == 200  # b's key, same trace id: lands in b only
    assert trace_row(engine, a.project_id)["span_count"] == 3
    assert trace_row(engine, a.project_id)["status"] == "ok"
    assert trace_row(engine, b.project_id)["span_count"] == 1
    with engine.connect() as conn:
        assert {e.event.span_id for e in load_trace_events(conn, b.project_id, TRACE)} == {sid(9)}
        assert sid(9) not in {e.event.span_id for e in load_trace_events(conn, a.project_id, TRACE)}


@pytest.mark.parametrize("field", ["project_id", "organization_id", "project"])
def test_the_payload_cannot_name_a_project(engine: Engine, field: str) -> None:
    a = make_collector(engine, name="a")
    b = make_collector(engine, settings=a.settings, name="b")
    body = batch(*simple_trace(TRACE)) | {field: str(b.project_id)}
    response = a.post(body)
    assert response.status_code == 400
    assert any(e["type"] == "extra_forbidden" for e in problem(response)["errors"])
    body = batch(*simple_trace(TRACE))
    body["resource"][field] = str(b.project_id)
    assert a.post(body).status_code == 400
    assert count_rows(engine)["traces"] == 0


# --------------------------------------------------------------------------- request validation


@pytest.mark.parametrize(
    ("raw", "slug", "error_type"),
    [
        (b"{not json", "invalid-batch", "invalid_json"),
        (b'{"a": 1, "a": 2}', "invalid-batch", "invalid_json"),
        (b'{"x": NaN}', "invalid-batch", "invalid_json"),
        (b'"a string"', "invalid-batch", "dict_type"),
        (b"[]", "invalid-batch", "dict_type"),
        (b"", "invalid-batch", "invalid_json"),
    ],
)
def test_malformed_bodies_are_rejected_whole(engine: Engine, raw: bytes, slug: str, error_type: str) -> None:
    c = make_collector(engine)
    response = c.post(raw)
    assert response.status_code == 400
    body = problem(response)
    assert body["type"] == f"urn:aobs:problem:{slug}" and body["errors"][0]["type"] == error_type
    assert count_rows(engine)["traces"] == 0


def test_invalid_envelope_and_unsupported_major_version(engine: Engine) -> None:
    c = make_collector(engine)
    no_resource = batch(*simple_trace(TRACE))
    del no_resource["resource"]
    body = problem(c.post(no_resource))
    assert (
        body["status"] == 400 and {"path": "resource", "type": "missing", "message": "Field required"} in body["errors"]
    )
    future = batch(*simple_trace(TRACE)) | {"schema_version": "2.0"}
    body = problem(c.post(future))
    assert body["type"] == "urn:aobs:problem:unsupported-schema-version" and "major version 2" in body["detail"]
    assert count_rows(engine)["traces"] == 0


def test_invalid_events_are_listed_and_the_rest_are_stored(engine: Engine) -> None:
    c = make_collector(engine)
    events = simple_trace(TRACE)
    events.insert(2, {**start(TRACE, sid(7)), "span_id": "not-hex"})
    events.insert(4, {**end(TRACE, sid(8)), "duration_ms": -1})
    response = c.post(batch(*events))
    assert response.status_code == 200
    body = response.json()
    assert body["counts"] == {"received": 9, "accepted": 7, "duplicate": 0, "dropped": 0, "rejected": 2}
    assert [r["index"] for r in body["rejected"]] == [2, 4]
    assert body["rejected"][0]["errors"][0]["path"] == "events/2/span_id"
    assert body["rejected"][1]["errors"][0]["path"] == "events/4/duration_ms"
    assert trace_row(engine, c.project_id)["span_count"] == 3


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({"Content-Type": "text/plain"}, 415),
        ({"Content-Type": "application/x-www-form-urlencoded"}, 415),
        ({"Content-Encoding": "gzip"}, 415),
        ({"Content-Type": "application/json; charset=utf-8"}, 200),
    ],
)
def test_media_types(engine: Engine, headers: dict[str, str], status: int) -> None:
    c = make_collector(engine)
    response = c.post(batch(*simple_trace(TRACE)), headers=headers)
    assert response.status_code == status, response.text
    if status != 200:
        assert problem(response)["type"] == "urn:aobs:problem:unsupported-media-type"


def test_other_methods_and_paths_are_problem_responses(engine: Engine) -> None:
    c = make_collector(engine)
    assert problem(c.client.get("/v1/ingest"))["status"] == 405
    assert problem(c.client.post("/v1/nope"))["status"] == 404
    assert c.client.get("/healthz").json() == {"status": "ok"}
    assert c.client.get("/readyz").json() == {"status": "ready"}


# --------------------------------------------------------------------------- limits


def test_bodies_over_the_limit_get_413_before_parsing(engine: Engine) -> None:
    c = make_collector(engine, settings=Settings(max_body_bytes=2048))
    big = batch(*simple_trace(TRACE), *[point(100 + i, TRACE, sid(1)) for i in range(20)])
    raw = json.dumps(big).encode()
    assert len(raw) > 2048
    body = problem(c.post(raw))
    assert body["status"] == 413 and body["limit"] == "body_bytes" and body["max"] == 2048

    def chunks() -> Any:  # no Content-Length: the streamed body is cut off at the limit
        for i in range(0, len(raw), 512):
            yield raw[i : i + 512]

    response = c.client.post("/v1/ingest", content=chunks(), headers={"Content-Type": "application/json",
                                                                       "Authorization": f"Bearer {c.key}"})  # fmt: skip
    assert response.status_code == 413 and problem(response)["limit"] == "body_bytes"
    assert count_rows(engine)["traces"] == 0


def test_declared_length_is_checked_before_the_key_and_the_body_is_never_parsed(engine: Engine) -> None:
    c = make_collector(engine, settings=Settings(max_body_bytes=2048))
    oversized = c.client.post("/v1/ingest", content=b"x" * 4096, headers={"Content-Type": "application/json"})
    assert oversized.status_code == 413 and problem(oversized)["limit"] == "body_bytes"
    unauthenticated = c.client.post("/v1/ingest", content=b"{not json", headers={"Content-Type": "application/json"})
    assert unauthenticated.status_code == 401  # the key is checked before the body is read


def test_too_many_events_get_413(engine: Engine) -> None:
    c = make_collector(engine, settings=Settings(max_events_per_batch=5))
    body = problem(c.post(batch(*simple_trace(TRACE))))
    assert body["status"] == 413 and body["limit"] == "events_per_batch" and body["max"] == 5
    assert "7 events" in body["detail"]
    assert count_rows(engine)["traces"] == 0


def test_per_trace_span_cap_drops_new_spans_and_flags_truncation(engine: Engine) -> None:
    c = make_collector(engine, max_spans_per_trace=2)
    body = c.post(batch(*simple_trace(TRACE))).json()
    assert body["counts"]["dropped"] == 2  # the llm span's start and end (third span)
    assert {d["reason"] for d in body["dropped"]} == {"trace_span_limit"}
    row = trace_row(engine, c.project_id)
    assert row["truncated"] is True and row["span_count"] == 2
    again = c.post(batch(end(TRACE, sid(1), name="answer_ticket", duration_s=2.0))).json()  # existing span: fine
    assert again["counts"]["accepted"] == 1


def test_per_trace_span_event_cap(engine: Engine) -> None:
    c = make_collector(engine, max_span_events_per_trace=1)
    body = c.post(batch(start(TRACE, sid(1)), point(1, TRACE, sid(1)), point(2, TRACE, sid(1)))).json()
    assert body["dropped"] == [{"index": 2, "reason": "trace_span_event_limit"}]
    assert trace_row(engine, c.project_id)["truncated"] is True


# --------------------------------------------------------------------------- privacy


SECRET = "sk-proj-" + "Z" * 30
MARKER = "the-customer-said-something-private"


def _private_trace() -> list[dict[str, Any]]:
    content = {"input": {"status": "captured", "format": "text", "value": MARKER},
               "output": {"status": "redacted", "format": "text", "value": f"{MARKER} with {SECRET}",
                          "redaction_count": 1}}  # fmt: skip
    error = {"type": "ValueError", "message": f"failed with api_key={SECRET}", "stack": f"Traceback {MARKER}"}
    return [
        start(TRACE, sid(1), attributes={"note": f"Bearer {SECRET}"}),
        end(TRACE, sid(1), status="error", error=error, content=content, attributes={"note": f"Bearer {SECRET}"}),
        point(1, TRACE, sid(1), attributes={"auth": f"token={SECRET}"}),
    ]


def test_metadata_only_project_stores_no_content_and_scrubs_secrets(engine: Engine) -> None:
    c = make_collector(engine, capture_ceiling="metadata_only", stack_traces=False)
    body = c.post(batch(*_private_trace())).json()
    assert body["counts"]["accepted"] == 3
    assert body["notices"]["content_dropped_policy"] == 2 and body["notices"]["stack_traces_dropped_policy"] == 1
    assert body["notices"]["secrets_scrubbed"] >= 3
    stored = database_text(engine)
    assert MARKER not in stored and SECRET not in stored
    assert "[REDACTED:" in stored and "ValueError" in stored  # metadata and the scrubbed message stay
    with engine.connect() as conn:
        content = conn.execute(text("SELECT content FROM span_content")).scalar_one()
    assert content == {"input": {"status": "not_captured"}, "output": {"status": "not_captured"}}


@pytest.mark.parametrize("ceiling", ["metadata_only", "redacted", "full"])
def test_emails_and_card_numbers_in_error_text_are_stored_nowhere(engine: Engine, ceiling: str) -> None:
    """Error text from an emitter that did not redact it (an older SDK, another language)."""
    c = make_collector(engine, capture_ceiling=ceiling, stack_traces=True)
    personal = "customer jane.doe@example.com paid with 4111 1111 1111 1111"
    error = {"type": "LookupError", "message": personal, "stack": f"Traceback ...\nLookupError: {personal}"}
    body = c.post(batch(start(TRACE, sid(1)), end(TRACE, sid(1), status="error", error=error))).json()
    assert body["counts"]["accepted"] == 2
    assert body["notices"]["personal_data_redacted"] == 4
    stored = database_text(engine)
    assert "jane.doe@example.com" not in stored and "4111 1111 1111 1111" not in stored
    assert "[REDACTED:email]" in stored and "[REDACTED:card_number]" in stored


def test_redacted_ceiling_keeps_only_redacted_content_scrubbed_again(engine: Engine) -> None:
    c = make_collector(engine, capture_ceiling="redacted")
    c.post(batch(*_private_trace()))
    with engine.connect() as conn:
        content = conn.execute(text("SELECT content FROM span_content")).scalar_one()
    assert content["input"] == {"status": "not_captured"}
    assert content["output"]["status"] == "redacted" and SECRET not in content["output"]["value"]
    assert SECRET not in database_text(engine)


def test_full_ceiling_keeps_content_and_returns_it_on_read(engine: Engine) -> None:
    c = make_collector(engine, capture_ceiling="full")
    c.post(batch(*_private_trace()))
    with engine.connect() as conn:
        events = load_trace_events(conn, c.project_id, TRACE)
    ends = [e.event for e in events if e.event.type == "span_end"]
    assert ends[0].content is not None and ends[0].content.input is not None
    assert ends[0].content.input.value == MARKER
    assert SECRET not in database_text(engine)  # secrets are scrubbed whatever the ceiling


def test_nul_characters_are_stored_safely(engine: Engine) -> None:
    c = make_collector(engine)
    response = c.post(batch(start(TRACE, sid(1), name="bad\u0000name")))
    assert response.status_code == 200 and response.json()["notices"]["nul_characters_replaced"] == 1
    with engine.connect() as conn:
        assert conn.execute(text("SELECT name FROM spans")).scalar_one() == "bad�name"


def test_clock_skew_is_flagged_not_rewritten(engine: Engine) -> None:
    c = make_collector(engine)
    body = c.post(batch(*simple_trace(TRACE), sent_at=30 - 3600)).json()
    assert body["clock_skew_suspected"] is True and body["notices"]["clock_skew_suspected"] == 1
    assert trace_row(engine, c.project_id)["started_at"] == NOW - timedelta(seconds=30)


# --------------------------------------------------------------------------- duplicates and retries


def test_resending_a_batch_changes_nothing_and_reports_duplicates(engine: Engine) -> None:
    c = make_collector(engine)
    sent = batch(*simple_trace(TRACE), batch_id="7d2f4a51-9c3e-4b8a-8f1d-6e5c4b3a2918")
    assert c.post(sent).json()["counts"]["accepted"] == 7
    before = count_rows(engine, c.project_id), stored_state(engine, c.project_id)
    for attempt in range(5):
        retry = sent | {"sent_at": f"2026-10-12T10:00:2{attempt}Z"}  # the SDK rebuilds sent_at per attempt
        body = c.post(retry).json()
        assert body["counts"] == {"received": 7, "accepted": 0, "duplicate": 7, "dropped": 0, "rejected": 0}
    after = count_rows(engine, c.project_id), stored_state(engine, c.project_id)
    assert after[0] | {"ingest_diagnostics": 0} == before[0] | {"ingest_diagnostics": 0}
    assert after[1] == before[1]
    assert after[0]["ingest_diagnostics"] == 5  # each retry is recorded with its duplicate count


def test_duplicates_inside_one_batch(engine: Engine) -> None:
    c = make_collector(engine)
    events = simple_trace(TRACE)
    body = c.post(batch(*events, *events)).json()
    assert body["counts"] == {"received": 14, "accepted": 7, "duplicate": 7, "dropped": 0, "rejected": 0}


def test_conflicting_copies_resolve_the_same_way_in_any_order(engine: Engine) -> None:
    first = start(TRACE, sid(1), attributes={"v": 1})
    second = start(TRACE, sid(1), attributes={"v": 2})
    results = []
    for order, name in (((first, second), "ab"), ((second, first), "ba")):
        c = make_collector(engine, name=name)
        replies = [c.post(batch(e)).json() for e in order]
        assert replies[1]["counts"]["accepted"] == 1 and replies[1]["conflicts"] == 1
        results.append(stored_state(engine, c.project_id))
        assert trace_row(engine, c.project_id)["conflicts"] == 1
    assert results[0] == results[1]
    winner = min((first, second), key=lambda e: content_digest(validate_batch(batch(e)).accepted[0].canonical()))
    assert results[0]["spans"][0]["start_event"]["attributes"] == winner["attributes"]


def test_a_span_event_id_stays_with_its_first_trace(engine: Engine) -> None:
    c = make_collector(engine)
    c.post(batch(start(TRACE, sid(1)), point(1, TRACE, sid(1))))
    other = tid(0xB2)
    body = c.post(batch(start(other, sid(1)), point(1, other, sid(1)))).json()
    assert body["counts"]["rejected"] == 1
    assert body["rejected"] == [{"index": 1, "errors": [{"path": "events/1/event_id", "type": "event_id_conflict",
                                 "message": "this event_id is already stored for another trace or span"}]}]  # fmt: skip
    assert trace_row(engine, c.project_id, other)["span_event_count"] == 0
    lone = c.post(batch(point(1, tid(0xB3), sid(1)))).json()  # every event rejected: no empty trace is left
    assert (lone["counts"]["rejected"], lone["trace_ids"]) == (1, [])
    assert count_rows(engine, c.project_id)["traces"] == 2


# --------------------------------------------------------------------------- order and late arrival


def test_out_of_order_and_late_events_converge_to_the_same_stored_trace(engine: Engine) -> None:
    events = simple_trace(TRACE)
    splits = [
        [events],
        [[e] for e in events],
        [[e] for e in reversed(events)],
        [events[4:], events[:2], events[2:4]],
    ]
    states = []
    for n, batches in enumerate(splits):
        c = make_collector(engine, name=f"order-{n}")
        for chunk in batches:
            assert c.post(batch(*chunk)).status_code == 200
        states.append(stored_state(engine, c.project_id))
    assert all(state == states[0] for state in states[1:])
    assert states[0]["traces"][0]["status"] == "ok"


def test_trace_status_follows_late_arrivals(engine: Engine) -> None:
    c = make_collector(engine)
    c.post(batch(start(TRACE, sid(1), name="run")))
    row = trace_row(engine, c.project_id)
    assert row["status"] == "open" and row["stale_reference_time"] == NOW - timedelta(seconds=30)
    c.post(batch(end(TRACE, sid(2), sid(1), name="child", at=0.2, duration_s=0.1, status="error")))  # end, no start
    c.post(batch(end(TRACE, sid(1), name="run")))
    row = trace_row(engine, c.project_id)
    assert (row["status"], row["error_count"], row["stale_reference_time"]) == ("ok_with_errors", 1, None)
    c.post(batch(start(TRACE, sid(3), sid(9), name="orphan", at=0.5)))  # parent never arrives
    row = trace_row(engine, c.project_id)
    assert row["orphan_count"] == 1 and row["open_span_count"] == 1 and row["status"] == "ok_with_errors"


def test_span_events_without_spans_make_an_incomplete_trace(engine: Engine) -> None:
    c = make_collector(engine)
    c.post(batch(point(1, TRACE, sid(1))))
    assert trace_row(engine, c.project_id)["status"] == "incomplete"
    c.post(batch(start(TRACE, sid(1)), end(TRACE, sid(1))))
    row = trace_row(engine, c.project_id)
    assert (row["status"], row["span_event_count"]) == ("ok", 1)


def test_deleted_traces_are_not_recreated(engine: Engine) -> None:
    c = make_collector(engine)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO trace_tombstones (project_id, trace_id) VALUES (:p, :t)"),
                     {"p": c.project_id, "t": TRACE})  # fmt: skip
    body = c.post(batch(*simple_trace(TRACE))).json()
    assert body["counts"]["dropped"] == 7 and body["trace_ids"] == []
    assert {d["reason"] for d in body["dropped"]} == {"trace_deleted"}
    assert count_rows(engine, c.project_id)["spans"] == 0 and count_rows(engine, c.project_id)["traces"] == 0


# --------------------------------------------------------------------------- failures and safe errors


def test_a_database_failure_rolls_the_whole_batch_back(engine: Engine) -> None:
    c = make_collector(engine)
    with engine.begin() as conn:  # a real failure late in the transaction, after spans were written
        conn.execute(text("CREATE FUNCTION fail_insert() RETURNS trigger AS $$ BEGIN "
                          "RAISE EXCEPTION 'injected failure SELECT secret'; END $$ LANGUAGE plpgsql"))  # fmt: skip
        conn.execute(
            text("CREATE TRIGGER fail BEFORE INSERT ON span_events FOR EACH ROW EXECUTE FUNCTION fail_insert()")
        )
    response = c.post(batch(*simple_trace(TRACE)))
    assert response.status_code == 500
    body = problem(response)
    assert body == {"type": "urn:aobs:problem:internal-error", "title": "Internal server error", "status": 500,
                    "detail": "The batch could not be stored"}  # fmt: skip
    assert count_rows(engine) == {"traces": 0, "spans": 0, "span_events": 0, "span_content": 0, "resources": 0,
                                  "environments": 0, "ingest_diagnostics": 0}  # fmt: skip
    with engine.begin() as conn:
        conn.execute(text("DROP TRIGGER fail ON span_events"))
    assert c.post(batch(*simple_trace(TRACE))).json()["counts"]["accepted"] == 7


def test_a_lock_timeout_asks_the_client_to_retry(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_app, "LOCK_TIMEOUT", "200ms")
    c = make_collector(engine)
    c.post(batch(start(TRACE, sid(1))))
    with engine.connect() as holder:  # another transaction holds the trace row lock
        holder.execute(text("SELECT 1 FROM traces WHERE trace_id = :t FOR UPDATE"), {"t": TRACE})
        response = c.post(batch(end(TRACE, sid(1))))
        holder.rollback()
    assert response.status_code == 503 and response.headers["retry-after"] == "5"
    assert problem(response)["type"] == "urn:aobs:problem:service-unavailable"
    assert c.post(batch(end(TRACE, sid(1)))).json()["counts"]["accepted"] == 1  # the retry succeeds


def test_error_responses_and_logs_never_carry_secrets_or_internals(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)  # every logger, SQLAlchemy's included
    c = make_collector(engine)
    bad = start(TRACE, sid(1), attributes={f"Bearer {SECRET}": 1})
    unknown = start(TRACE, sid(2)) | {f"token={SECRET}": "x"}
    body = c.post(batch(bad, unknown, *_private_trace())).json()
    assert body["counts"]["rejected"] == 2
    duplicate_key = f'{{"password={SECRET}": 1, "password={SECRET}": 2}}'.encode()
    replies = [json.dumps(body), c.post(duplicate_key).text, c.post(b"[").text]
    with engine.begin() as conn:
        conn.execute(text("CREATE FUNCTION fail_insert() RETURNS trigger AS $$ BEGIN "
                          "RAISE EXCEPTION 'injected failure'; END $$ LANGUAGE plpgsql"))  # fmt: skip
        conn.execute(text("CREATE TRIGGER fail BEFORE INSERT ON spans FOR EACH ROW EXECUTE FUNCTION fail_insert()"))
    replies.append(c.post(batch(start(tid(0xC3), sid(1), name=MARKER))).text)
    own_logs = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("aobs"))
    for blob in (*replies, caplog.text):
        assert SECRET not in blob and MARKER not in blob and c.key not in blob
    for blob in (*replies, own_logs):
        for internal in ("Traceback", "sqlalchemy", "psycopg", "/home/", "SELECT ", "INSERT ", "injected"):
            assert internal not in blob, internal
    assert f"key={c.key_id}" in own_logs and "sqlstate=P0001" in own_logs  # key by id; errors by type only


def test_ingest_diagnostics_record_what_was_dropped(engine: Engine) -> None:
    c = make_collector(engine, capture_ceiling="metadata_only")
    events = _private_trace()
    c.post(batch(*events, {"type": "span_start"}))
    c.post(batch(*events))
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT api_key_id, counters FROM ingest_diagnostics ORDER BY id")).all()
    assert [r.api_key_id for r in rows] == [c.key_id, c.key_id]
    assert rows[0].counters["rejected"] == 1 and rows[0].counters["content_dropped_policy"] == 2
    assert rows[1].counters["duplicates"] == 3


def test_the_documented_response_matches_a_real_one(engine: Engine) -> None:
    import re

    from support import REPO_ROOT

    doc = (REPO_ROOT / "docs" / "ingestion.md").read_text()
    documented = json.loads(re.findall(r"```json\n(.*?)```", doc, re.DOTALL)[0])
    events = simple_trace(TRACE)
    events.insert(2, {**start(TRACE, sid(7)), "span_id": "not-hex"})
    real = make_collector(engine).post(batch(*events)).json()
    assert set(documented) == set(real) and set(documented["counts"]) == set(real["counts"])
    assert documented["rejected"][0]["errors"][0]["type"] == real["rejected"][0]["errors"][0]["type"]
    assert documented["rejected"][0]["errors"][0]["message"] == real["rejected"][0]["errors"][0]["message"]
    counts = documented["counts"]
    assert counts["received"] == sum(counts[k] for k in ("accepted", "duplicate", "dropped", "rejected"))
