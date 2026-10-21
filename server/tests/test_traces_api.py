"""GET /v1/projects/{project_id}/traces/{trace_id}[/events] against real PostgreSQL: retrieval,
tenant isolation, scopes, pagination, privacy at read time and read-time status."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from aobs_server.api import create_app
from aobs_server.clock import FixedClock
from aobs_server.model import assemble, merge_events, validate_batch
from aobs_server.project_settings import ProjectSettings
from aobs_server.storage import tenancy
from ingest_support import NOW, Collector, batch, end, make_collector, point, sid, simple_trace, start, tid, zulu
from tenants import bearer, make_key

TRACE = tid(0xE1)
SECRET_VALUE = "the-model-said-something-private"


def problem(response: Any) -> dict[str, Any]:
    assert response.headers["content-type"].startswith("application/problem+json"), response.text
    body: dict[str, Any] = response.json()
    return body


def reader(c: Collector, **kwargs: Any) -> dict[str, str]:
    return bearer(make_key(c.engine, c.project_id, scopes=("read",), **kwargs)[1])


def get(c: Collector, path: str, headers: dict[str, str], **params: Any) -> Any:
    return c.client.get(path, headers=headers, params=params)


def url(c: Collector, trace_id: str = TRACE, project: Any = None, suffix: str = "") -> str:
    return f"/v1/projects/{project or c.project_id}/traces/{trace_id}{suffix}"


def content_trace(trace: str = TRACE) -> list[dict[str, Any]]:
    content = {"input": {"status": "captured", "format": "text", "value": SECRET_VALUE},
               "output": {"status": "not_captured"}}  # fmt: skip
    error = {"type": "ValueError", "message": "upstream timed out", "stack": "Traceback: frame 1"}
    return [start(trace, sid(1), name="run"), end(trace, sid(1), name="run", status="error", error=error,
                                                  content=content)]  # fmt: skip


@pytest.fixture
def collector(engine: Engine) -> Collector:
    c = make_collector(engine)
    assert c.post(batch(*simple_trace(TRACE))).status_code == 200
    return c


# --------------------------------------------------------------------------- retrieval


def test_a_stored_trace_reads_back_as_it_was_assembled(collector: Collector) -> None:
    sent = batch(*simple_trace(TRACE))
    response = get(collector, url(collector), reader(collector))
    assert response.status_code == 200, response.text
    body = response.json()
    expected = assemble(merge_events(validate_batch(sent).accepted), NOW)[TRACE].to_dict()
    assert body["roots"] == expected["roots"] and body["orphans"] == expected["orphans"]
    spans = {s["span_id"]: {k: v for k, v in s.items() if k != "event_count"} for s in body["spans"]}
    assert spans == zulu(expected["spans"])
    assert all(s["start_time"].endswith("Z") and s["end_time"].endswith("Z") for s in body["spans"])
    assert [s["event_count"] for s in body["spans"]] == [0, 1, 0]
    assert body["summary"] | {"first_received_at": None, "last_received_at": None} == {
        "status": "ok", "started_at": expected["started_at"].replace("+00:00", "Z"),
        "ended_at": expected["ended_at"].replace("+00:00", "Z"), "root_name": "answer_ticket",
        "agent_name": "triage", "session_id": "s-1", "span_count": 3, "open_span_count": 0, "error_count": 0,
        "span_event_count": 1, "kind_counts": {"agent": 1, "llm": 1, "tool": 1}, "tokens": expected["tokens"],
        "conflicts": 0, "orphan_count": 0, "truncated": False, "environments": ["dev"],
        "first_received_at": None, "last_received_at": None,
    }  # fmt: skip
    assert body["summary"]["first_received_at"] == NOW.isoformat().replace("+00:00", "Z")
    assert (body["project_id"], body["trace_id"], body["content"]) == (str(collector.project_id), TRACE, "omitted")
    assert body["page"] == {"limit": 500, "next_cursor": None} and body["orphan_event_count"] == 0


def test_span_events_are_listed_chronologically(collector: Collector) -> None:
    extra = [point(50 + i, TRACE, sid(3), name=f"chunk-{i}", at=0.9 - i * 0.1, attributes={"i": i}) for i in range(4)]
    collector.post(batch(*extra, point(99, TRACE, sid(9), name="lost", at=0.05)))  # sid(9): no such span
    body = get(collector, url(collector, suffix="/events"), reader(collector)).json()
    assert [e["name"] for e in body["events"]] == ["lost", "retry", "chunk-3", "chunk-2", "chunk-1", "chunk-0"]
    assert body["events"][2] == {
        "event_id": "00000000-0000-4000-8000-000000000035",
        "span_id": sid(3),
        "name": "chunk-3",
        "time": "2026-10-12T10:00:00.600000Z",
        "attributes": {"i": 3},
    }
    only = get(collector, url(collector, suffix="/events"), reader(collector), span_id=sid(3)).json()
    assert [e["name"] for e in only["events"]] == ["chunk-3", "chunk-2", "chunk-1", "chunk-0"]
    trace = get(collector, url(collector), reader(collector)).json()
    assert trace["orphan_event_count"] == 1 and trace["summary"]["span_event_count"] == 6


# --------------------------------------------------------------------------- tenant isolation and auth


def _not_found(response: Any) -> dict[str, Any]:
    assert response.status_code == 404, response.text
    return problem(response)


@pytest.mark.parametrize("suffix", ["", "/events"])
def test_another_projects_trace_is_indistinguishable_from_a_missing_one(engine: Engine, suffix: str) -> None:
    a = make_collector(engine, name="a")
    b = make_collector(engine, settings=a.settings, name="b")
    b_trace = tid(0xB0B)
    assert b.post(batch(*simple_trace(b_trace))).status_code == 200
    a.post(batch(*simple_trace(TRACE)))
    key_a = reader(a)
    answers = [
        get(a, url(a, b_trace, project=b.project_id, suffix=suffix), key_a),  # B's trace under B's id
        get(a, url(a, TRACE, project=b.project_id, suffix=suffix), key_a),  # A's own trace id under B
        get(a, url(a, b_trace, suffix=suffix), key_a),  # B's trace id under A's own project
        get(a, url(a, tid(0x404), suffix=suffix), key_a),  # nothing anywhere
        get(a, url(a, b_trace, project=uuid4(), suffix=suffix), key_a),  # a project that does not exist
        get(a, url(a, b_trace, project="not-a-uuid", suffix=suffix), key_a),
        get(a, url(a, "NOT-A-TRACE-ID", suffix=suffix), key_a),
        get(a, url(a, "0" * 32, suffix=suffix), key_a),
    ]
    bodies = [_not_found(r) for r in answers]
    assert all(body == bodies[0] for body in bodies)
    assert bodies[0] == {"type": "urn:aobs:problem:not-found", "title": "Not found", "status": 404,
                         "detail": "The requested resource was not found"}  # fmt: skip
    for r in answers:  # nothing of B leaks into any answer
        assert b_trace not in r.text and str(b.project_id) not in r.text
    # B's own read key sees B's trace, and only its own spans
    own = get(b, url(b, b_trace, suffix=suffix), reader(b))
    assert own.status_code == 200


def test_same_trace_id_in_two_projects_reads_back_separately(engine: Engine) -> None:
    a = make_collector(engine, name="a")
    b = make_collector(engine, settings=a.settings, name="b")
    a.post(batch(*simple_trace(TRACE)))
    b_events = [point(70, TRACE, sid(7), name="b-event"), point(71, TRACE, sid(2), name="b-event-on-a-span-id")]
    b.post(batch(start(TRACE, sid(7), name="b-only"), end(TRACE, sid(7), name="b-only"), *b_events))
    in_a = get(a, url(a), reader(a)).json()
    in_b = get(b, url(b), reader(b)).json()
    assert {s["span_id"] for s in in_a["spans"]} == {sid(1), sid(2), sid(3)}
    assert [s["name"] for s in in_b["spans"]] == ["b-only"] and in_b["summary"]["span_count"] == 1
    events_a = get(a, url(a, suffix="/events"), reader(a)).json()["events"]
    events_b = get(b, url(b, suffix="/events"), reader(b)).json()["events"]
    assert [e["name"] for e in events_a] == ["retry"]
    assert sorted(e["name"] for e in events_b) == ["b-event", "b-event-on-a-span-id"]
    only_a = get(a, url(a, suffix="/events"), reader(a), span_id=sid(2)).json()["events"]
    assert [e["name"] for e in only_a] == ["retry"]  # B's event on the same span id stays in B
    assert in_a["spans"][1]["event_count"] == 1 and in_a["orphan_event_count"] == 0


@pytest.mark.parametrize("suffix", ["", "/events"])
def test_reading_needs_the_read_scope_on_the_keys_own_project(engine: Engine, suffix: str) -> None:
    a = make_collector(engine, name="a")
    b = make_collector(engine, settings=a.settings, name="b")
    a.post(batch(*simple_trace(TRACE)))
    ingest_only = bearer(a.key)
    forbidden = get(a, url(a, suffix=suffix), ingest_only)
    assert forbidden.status_code == 403 and "'read' scope" in problem(forbidden)["detail"]
    # without the scope on another project: still 404, so scopes reveal nothing about B either
    assert get(a, url(a, project=b.project_id, suffix=suffix), ingest_only).status_code == 404
    assert get(a, url(a, suffix=suffix), bearer(a.key_for(scopes=("admin",)))).status_code == 403
    assert get(a, url(a, suffix=suffix), bearer(a.key_for(scopes=("ingest", "read")))).status_code == 200


@pytest.mark.parametrize("suffix", ["", "/events"])
def test_missing_and_revoked_keys_get_401(collector: Collector, suffix: str) -> None:
    key_id, secret = make_key(collector.engine, collector.project_id, scopes=("read",))
    with collector.engine.begin() as conn:
        tenancy.revoke_api_key(conn, key_id, NOW)
    for headers in ({}, {"Authorization": "Bearer nope"}, bearer(secret)):
        response = get(collector, url(collector, suffix=suffix), headers)
        assert response.status_code == 401 and problem(response)["type"] == "urn:aobs:problem:unauthorized"


def test_environment_bound_read_keys_still_read_their_project(collector: Collector) -> None:
    # environment binding restricts writes; a read key bound to "prod" may read the project's traces
    assert get(collector, url(collector), reader(collector, environment="prod")).status_code == 200


def test_deleted_traces_are_not_found(collector: Collector) -> None:
    with collector.engine.begin() as conn:
        conn.execute(text("INSERT INTO trace_tombstones (project_id, trace_id) VALUES (:p, :t)"),
                     {"p": collector.project_id, "t": TRACE})  # fmt: skip
    assert get(collector, url(collector), reader(collector)).status_code == 404
    assert get(collector, url(collector, suffix="/events"), reader(collector)).status_code == 404


# --------------------------------------------------------------------------- pagination and input validation


def test_spans_are_paginated_by_span_id_without_gaps_or_repeats(engine: Engine) -> None:
    c = make_collector(engine)
    children = [e for i in range(7) for e in (start(TRACE, sid(20 + i), sid(1), name=f"s{i}", at=0.1 * i),
                                             end(TRACE, sid(20 + i), sid(1), name=f"s{i}", at=0.1 * i))]  # fmt: skip
    c.post(batch(start(TRACE, sid(1)), end(TRACE, sid(1)), *children))
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        params = {"limit": 3} | ({"cursor": cursor} if cursor else {})
        body = get(c, url(c), reader(c), **params).json()
        pages += 1
        seen += [s["span_id"] for s in body["spans"]]
        assert body["summary"]["span_count"] == 8 and body["roots"] == [sid(1)]  # whole-trace data on every page
        cursor = body["page"]["next_cursor"]
        if cursor is None:
            break
    assert pages == 3 and seen == sorted(seen) and len(seen) == len(set(seen)) == 8


def test_span_events_are_paginated_with_an_opaque_cursor(engine: Engine) -> None:
    c = make_collector(engine)
    c.post(batch(start(TRACE, sid(1)), *(point(i + 1, TRACE, sid(1), at=0.5) for i in range(5))))  # equal times
    names: list[str] = []
    cursor = None
    while True:
        body = get(c, url(c, suffix="/events"), reader(c), limit=2, **({"cursor": cursor} if cursor else {})).json()
        names += [e["event_id"] for e in body["events"]]
        cursor = body["page"]["next_cursor"]
        if cursor is None:
            break
    assert names == sorted(names) and len(names) == 5  # ties on time are broken by event id


@pytest.mark.parametrize(
    ("suffix", "params", "message"),
    [
        ("", {"limit": 0}, "invalid parameter limit"),
        ("", {"limit": 1001}, "invalid parameter limit"),
        ("", {"limit": "many"}, "invalid parameter limit"),
        ("", {"cursor": "zz"}, "cursor must be a span id"),
        ("", {"include_content": "maybe"}, "invalid parameter include_content"),
        ("/events", {"cursor": "not base64 !"}, "cursor must be a value returned"),
        ("/events", {"cursor": "aGVsbG8"}, "cursor must be a value returned"),  # base64 of "hello"
        ("/events", {"span_id": "XYZ"}, "span_id must be 16 lowercase hex"),
        ("/events", {"limit": 5000}, "invalid parameter limit"),
    ],
)
def test_invalid_parameters_are_400(collector: Collector, suffix: str, params: dict[str, Any], message: str) -> None:
    response = get(collector, url(collector, suffix=suffix), reader(collector), **params)
    assert response.status_code == 400 and message in problem(response)["detail"], response.text


# --------------------------------------------------------------------------- privacy at read time


def _content_of(body: dict[str, Any]) -> dict[str, Any]:
    content: dict[str, Any] = body["spans"][0]["fields"]["content"]
    return content


def test_content_values_are_omitted_unless_asked_for(engine: Engine) -> None:
    c = make_collector(engine, capture_ceiling="full")
    c.post(batch(*content_trace()))
    default = get(c, url(c), reader(c))
    assert SECRET_VALUE not in default.text
    assert _content_of(default.json()) == {"input": {"status": "captured", "format": "text"},
                                           "output": {"status": "not_captured"}}  # fmt: skip
    included = get(c, url(c), reader(c), include_content="true").json()
    assert included["content"] == "included" and _content_of(included)["input"]["value"] == SECRET_VALUE


FUTURE_VALUE = "a-value-in-a-field-from-a-newer-schema-minor"


def _newer_minor_trace() -> dict[str, Any]:
    """A schema-1.1 batch whose span end carries a field this server does not know. Under a full
    ceiling it is stored as an extra, because the server cannot tell whether it holds content."""
    body = batch(start(TRACE, sid(1)), end(TRACE, sid(1)) | {"future_field": FUTURE_VALUE})
    body["schema_version"] = "1.1"
    return body


def test_fields_from_newer_schema_minors_are_omitted_unless_content_is_asked_for(engine: Engine) -> None:
    c = make_collector(engine, capture_ceiling="full")
    assert c.post(_newer_minor_trace()).json()["counts"]["accepted"] == 2
    default = get(c, url(c), reader(c))
    assert default.status_code == 200 and FUTURE_VALUE not in default.text
    assert "__extra__" not in default.json()["spans"][0]["fields"]
    included = get(c, url(c), reader(c), include_content="true").json()
    assert included["spans"][0]["fields"]["__extra__"] == {"future_field": FUTURE_VALUE}


def test_fields_from_newer_schema_minors_follow_the_current_ceiling_even_when_asked_for(engine: Engine) -> None:
    c = make_collector(engine, capture_ceiling="full")
    c.post(_newer_minor_trace())
    with engine.begin() as conn:
        redacted = json.dumps(ProjectSettings(capture_ceiling="redacted").to_db())
        conn.execute(
            text("UPDATE projects SET settings = CAST(:s AS jsonb) WHERE id = :p"), {"s": redacted, "p": c.project_id}
        )
    response = get(c, url(c), reader(c), include_content="true")
    assert FUTURE_VALUE not in response.text and "__extra__" not in response.json()["spans"][0]["fields"]


def test_lowering_the_projects_policy_also_hides_data_stored_earlier(engine: Engine) -> None:
    c = make_collector(engine, capture_ceiling="full", stack_traces=True)
    c.post(batch(*content_trace()))
    with engine.begin() as conn:
        stricter = ProjectSettings(capture_ceiling="metadata_only", error_messages=False, stack_traces=False)
        conn.execute(text("UPDATE projects SET settings = CAST(:s AS jsonb) WHERE id = :p"),
                     {"s": json.dumps(stricter.to_db()), "p": c.project_id})  # fmt: skip
    response = get(c, url(c), reader(c), include_content="true")
    body = response.json()
    assert SECRET_VALUE not in response.text and "upstream timed out" not in response.text
    assert "Traceback" not in response.text
    assert _content_of(body) == {"input": {"status": "not_captured"}, "output": {"status": "not_captured"}}
    assert body["spans"][0]["fields"]["error"] == {"type": "ValueError"}


def test_secrets_stored_before_a_scrubber_existed_are_scrubbed_on_read(engine: Engine) -> None:
    c = make_collector(engine)
    c.post(batch(start(TRACE, sid(1)), point(1, TRACE, sid(1), attributes={"note": "ok"})))
    leaked = "sk-proj-" + "Q" * 30
    with engine.begin() as conn:  # simulate a row written by an older server without that scrubber
        update = "UPDATE span_events SET event = jsonb_set(event, '{attributes,note}', to_jsonb(CAST(:v AS text)))"
        conn.execute(text(update), {"v": f"key {leaked}"})
    response = get(c, url(c, suffix="/events"), reader(c))
    assert leaked not in response.text and "[REDACTED:api_key]" in response.text


# --------------------------------------------------------------------------- read-time status and failures


def test_an_unfinished_trace_is_in_progress_then_incomplete(engine: Engine) -> None:
    c = make_collector(engine, stale_after_s=60)
    c.post(batch(start(TRACE, sid(1), at=0)))  # started at NOW - 30 s
    assert get(c, url(c), reader(c)).json()["summary"]["status"] == "in_progress"
    later = TestClient(create_app(c.settings, engine, FixedClock(NOW + timedelta(seconds=45))))
    body = later.get(url(c), headers=reader(c)).json()
    assert body["summary"]["status"] == "incomplete"
    assert body["spans"][0]["lifecycle"] == "incomplete"  # the assembled spans agree with the summary


def test_storage_errors_on_read_are_problem_responses_without_internals(collector: Collector) -> None:
    with collector.engine.begin() as conn:
        conn.execute(text("ALTER TABLE span_content RENAME TO span_content_moved"))  # a real query failure
    try:
        response = get(collector, url(collector), reader(collector))
    finally:
        with collector.engine.begin() as conn:
            conn.execute(text("ALTER TABLE span_content_moved RENAME TO span_content"))
    assert response.status_code == 500
    assert problem(response) == {"type": "urn:aobs:problem:internal-error", "title": "Internal server error",
                                 "status": 500, "detail": "The trace could not be read"}  # fmt: skip
    for internal in ("span_content", "SELECT", "psycopg", "sqlalchemy", "Traceback"):
        assert internal not in response.text


def test_reads_are_read_only(collector: Collector) -> None:
    before = get(collector, url(collector), reader(collector)).json()
    for _ in range(3):
        get(collector, url(collector), reader(collector))
        get(collector, url(collector, suffix="/events"), reader(collector))
    with collector.engine.connect() as conn:
        diagnostics = conn.execute(text("SELECT count(*) FROM ingest_diagnostics")).scalar_one()
    assert get(collector, url(collector), reader(collector)).json() == before and diagnostics == 0


def test_the_documented_responses_match_real_ones(collector: Collector) -> None:
    import re

    from support import REPO_ROOT

    doc = (REPO_ROOT / "docs" / "traces-api.md").read_text()
    trace_doc, events_doc = (json.loads(b) for b in re.findall(r"```json\n(.*?)```", doc, re.DOTALL))
    trace = get(collector, url(collector), reader(collector)).json()
    events = get(collector, url(collector, suffix="/events"), reader(collector)).json()
    assert set(trace_doc) == set(trace) and set(trace_doc["summary"]) == set(trace["summary"])
    assert set(trace_doc["spans"][0]) == set(trace["spans"][0]) and set(trace_doc["page"]) == set(trace["page"])
    assert set(events_doc) == set(events) and set(events_doc["events"][0]) <= set(events["events"][0]) | {"attributes"}


# --------------------------------------------------------------------------- read transaction guarantees
# Each test replaces the endpoint's query function with a wrapper that acts inside the very
# transaction the endpoint opened, then checks what the real request observed.


def test_a_read_sees_one_snapshot_while_another_transaction_commits(
    collector: Collector, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aobs_server.api import app as api_app
    from aobs_server.auth.service import authenticate
    from aobs_server.config import Settings
    from aobs_server.ingest.pipeline import ingest

    original = api_app.get_trace
    with collector.engine.connect() as conn:
        writer_principal = authenticate(conn, bearer(collector.key)["Authorization"])
    observed: dict[str, Any] = {}

    def racing(conn: Any, project: Any, trace_id: str, **kwargs: Any) -> dict[str, Any]:
        first = original(conn, project, trace_id, **kwargs)
        late = [start(TRACE, sid(8), sid(1), name="late", at=0.5), end(TRACE, sid(8), sid(1), name="late", at=0.5)]
        with collector.engine.begin() as writer:  # committed while the read transaction is open
            ingest(writer, writer_principal, batch(*late), NOW, Settings())
        observed["second"] = original(conn, project, trace_id, **kwargs)
        return first

    monkeypatch.setattr(api_app, "get_trace", racing)
    body = get(collector, url(collector), reader(collector)).json()
    assert body["summary"]["span_count"] == 3 and len(body["spans"]) == 3
    assert observed["second"] == body  # the same snapshot: the committed span is not visible yet
    monkeypatch.undo()
    after = get(collector, url(collector), reader(collector)).json()  # a new request sees it
    assert after["summary"]["span_count"] == 4 and sid(8) in {s["span_id"] for s in after["spans"]}


@pytest.mark.parametrize("target", ["get_trace", "get_span_events"])
def test_a_read_cannot_write(
    collector: Collector, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, target: str
) -> None:
    from aobs_server.api import app as api_app

    original = getattr(api_app, target)

    def tampering(conn: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        conn.execute(text("UPDATE traces SET root_name = 'tampered'"))
        result: dict[str, Any] = original(conn, *args, **kwargs)
        return result

    caplog.set_level(logging.WARNING, logger="aobs")
    monkeypatch.setattr(api_app, target, tampering)
    suffix = "" if target == "get_trace" else "/events"
    response = get(collector, url(collector, suffix=suffix), reader(collector))
    assert response.status_code == 500 and problem(response)["detail"] == "The trace could not be read"
    assert "sqlstate=25006" in caplog.text  # read_only_sql_transaction
    with collector.engine.connect() as conn:
        assert conn.execute(text("SELECT root_name FROM traces")).scalar_one() == "answer_ticket"


def test_read_logs_carry_only_project_key_id_and_endpoint(engine: Engine, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)  # every logger, SQLAlchemy's included
    c = make_collector(engine, capture_ceiling="full", stack_traces=True)
    leaked = "sk-proj-" + "L" * 30
    c.post(batch(*content_trace(), point(1, TRACE, sid(1), attributes={"note": "private-attribute-value"}),
                 point(2, TRACE, sid(1), attributes={"auth": f"token={leaked}"})))  # fmt: skip
    read_key_id, read_secret = make_key(engine, c.project_id, scopes=("read",))
    caplog.clear()
    assert get(c, url(c), bearer(read_secret), include_content="true").status_code == 200
    assert get(c, url(c, suffix="/events"), bearer(read_secret)).status_code == 200
    server_side = [r for r in caplog.records if not r.name.startswith("httpx")]  # httpx logs the test client's URLs
    text_logged = "\n".join(r.getMessage() for r in server_side)
    for sensitive in (SECRET_VALUE, "upstream timed out", "Traceback: frame 1", "private-attribute-value", leaked,
                      read_secret, c.key, TRACE):  # fmt: skip
        assert sensitive not in text_logged, sensitive
    reads = [r.getMessage() for r in server_side if r.name == "aobs.api" and r.getMessage().startswith("read ")]
    assert reads == [f"read project={c.project_id} key={read_key_id} trace",
                     f"read project={c.project_id} key={read_key_id} span_events"]  # fmt: skip
