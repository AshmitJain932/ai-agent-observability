"""The SDK's HttpExporter against the real collector and PostgreSQL (ADR-014 contract), and the
stored trace compared with what the SDK sent."""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, text

import aobs
from aobs.export import EncodedBatch, ExportResult, HttpExporter
from aobs_server.api import create_app
from aobs_server.clock import FixedClock
from aobs_server.config import Settings
from aobs_server.model import assemble, merge_events, validate_batch
from aobs_server.storage.traces import load_trace_events
from ingest_support import NOW, Collector, LiveServer, batch, database_text, make_collector, sid, start, tid, zulu
from tenants import make_key


class Recording:
    """Delegates to the HTTP exporter and keeps every body the SDK sent."""

    def __init__(self, inner: HttpExporter) -> None:
        self.inner = inner
        self.bodies: list[bytes] = []

    def export(self, encoded: EncodedBatch, timeout_s: float) -> ExportResult:
        self.bodies.append(encoded.body)
        return self.inner.export(encoded, timeout_s)

    def shutdown(self) -> None:
        self.inner.shutdown()


@pytest.fixture
def live(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Collector, LiveServer]]:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    c = make_collector(engine, settings=Settings(max_body_bytes=4096))
    with LiveServer(create_app(c.settings, engine, FixedClock(NOW))) as server:
        yield c, server
    aobs.shutdown(timeout_s=2)


def encoded(body: dict[str, Any]) -> EncodedBatch:
    return EncodedBatch(body=json.dumps(body).encode(), event_count=len(body["events"]), batch_id="b", attempt=1)


def test_sdk_run_is_stored_and_reads_back_as_the_sdk_assembled_it(live: tuple[Collector, LiveServer]) -> None:
    c, server = live
    exporter = Recording(HttpExporter(server.url, api_key=c.key))
    aobs.init(exporter=exporter, service_name="contract", environment="dev", capture="full",
              schedule_delay_s=0.02)  # fmt: skip

    @aobs.tool
    def lookup(order_id: str) -> dict[str, str]:
        return {"order": order_id, "status": "shipped"}

    with aobs.run("answer_ticket", agent="triage", session_id="s-9"):
        lookup("A-1")
        aobs.event("checked_policy", {"rule": "refunds"})
        with pytest.raises(ValueError), aobs.tool_span("flaky"):
            raise ValueError("upstream timed out")
    assert aobs.flush(5), aobs.diagnostics()
    assert aobs.diagnostics()["events_rejected_by_collector"] == 0

    sent = [e for body in exporter.bodies for e in validate_batch(body).accepted]
    expected = assemble(merge_events(sent), NOW)
    assert len(expected) == 1
    trace_id, view = next(iter(expected.items()))
    with c.engine.connect() as conn:
        stored = load_trace_events(conn, c.project_id, trace_id)
        row = conn.execute(text("SELECT status, span_count, error_count FROM traces")).one()
    assert assemble(merge_events(stored), NOW)[trace_id].to_dict() == view.to_dict()
    assert tuple(row) == ("ok_with_errors", 3, 1)

    # and over HTTP, through GET /v1/projects/{id}/traces/{trace_id} with a read key
    read_key = make_key(c.engine, c.project_id, scopes=("read",))[1]
    request = urllib.request.Request(
        f"{server.url}/v1/projects/{c.project_id}/traces/{trace_id}?include_content=true",
        headers={"Authorization": f"Bearer {read_key}"},
    )
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10) as response:
        fetched = json.loads(response.read())
    expected = view.to_dict()
    assert {sp["span_id"]: {k: v for k, v in sp.items() if k != "event_count"} for sp in fetched["spans"]} == (
        zulu(expected["spans"])
    )
    assert fetched["roots"] == expected["roots"] and fetched["summary"]["status"] == expected["status"]


def test_personal_data_in_errors_reaches_no_table_even_with_the_most_permissive_settings(
    live: tuple[Collector, LiveServer],
) -> None:
    """SDK in full mode without its default redactors, stack traces on; the project allows full
    content, error messages and stack traces. Emails and card numbers in error text still never
    reach storage."""
    c, server = live
    aobs.init(exporter=HttpExporter(server.url, api_key=c.key), service_name="contract", environment="dev",
              capture="full", capture_stack_traces=True, use_default_redactors=False,
              schedule_delay_s=0.02)  # fmt: skip
    personal = "customer jane.doe@example.com paid with 4111 1111 1111 1111"

    @aobs.tool
    def charge(order_id: str) -> None:
        raise LookupError(personal)

    with aobs.run("answer_ticket") as run:
        with pytest.raises(LookupError):
            charge("A-1")
        run.set_error("PaymentFailed", message=personal)
    assert aobs.flush(5), aobs.diagnostics()
    assert aobs.diagnostics()["events_rejected_by_collector"] == 0
    stored = database_text(c.engine)
    assert "LookupError" in stored and "PaymentFailed" in stored  # the errors themselves are stored
    assert "jane.doe@example.com" not in stored and "4111 1111 1111 1111" not in stored
    assert "[REDACTED:email]" in stored and "[REDACTED:card_number]" in stored


def test_exporter_outcomes_match_the_ingest_contract(live: tuple[Collector, LiveServer]) -> None:
    c, server = live
    exporter = HttpExporter(server.url, api_key=c.key)
    good = batch(start(tid(1), sid(1)), {"type": "span_start", "trace_id": "bad"})
    result = exporter.export(encoded(good), 10)
    assert (result.outcome, result.rejected_events) == ("success", 1)  # 200: one event rejected, never retried
    again = exporter.export(encoded(good), 10)
    assert (again.outcome, again.rejected_events) == ("success", 1)  # a retry is a harmless duplicate

    assert HttpExporter(server.url, api_key="aobs_ik_" + "x" * 43).export(encoded(good), 10).outcome == "failure"
    huge = batch(*(start(tid(2), sid(i + 1), name="n" * 200) for i in range(30)))
    assert exporter.export(encoded(huge), 10).outcome == "failure"  # 413 is permanent

    with c.engine.begin() as conn:  # a transient database error: 503 with Retry-After
        conn.execute(text("CREATE FUNCTION fail_insert() RETURNS trigger AS $$ BEGIN "
                          "RAISE EXCEPTION USING ERRCODE = '40P01'; END $$ LANGUAGE plpgsql"))  # fmt: skip
        conn.execute(text("CREATE TRIGGER fail BEFORE INSERT ON spans FOR EACH ROW EXECUTE FUNCTION fail_insert()"))
    retry = exporter.export(encoded(batch(start(tid(3), sid(1)))), 10)
    assert (retry.outcome, retry.retry_after_s) == ("retry", 5.0)
