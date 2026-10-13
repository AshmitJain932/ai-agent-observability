"""Concurrent ingestion of the same trace (real PostgreSQL, several connections)."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy import Engine, text

from aobs_server.api import create_app
from aobs_server.auth.service import authenticate
from aobs_server.clock import FixedClock
from aobs_server.config import Settings
from aobs_server.ingest.pipeline import ingest
from ingest_support import (
    NOW,
    LiveServer,
    batch,
    end,
    make_collector,
    point,
    post_json,
    sid,
    simple_trace,
    start,
    stored_state,
    tid,
)
from tenants import bearer

TRACE = tid(0xC0)


def test_a_second_batch_for_a_locked_trace_waits_and_both_are_applied(engine: Engine) -> None:
    c = make_collector(engine)
    reference = make_collector(engine, name="reference")
    events = simple_trace(TRACE)
    existing, first, second = events[:1], events[1:4], events[4:]
    with engine.connect() as conn:
        principal = authenticate(conn, bearer(c.key)["Authorization"])
    with engine.begin() as conn:  # the trace row exists already: only the explicit row lock can serialize
        ingest(conn, principal, batch(*existing), NOW, Settings())

    holder = engine.connect()
    transaction = holder.begin()
    ingest(holder, principal, batch(*first), NOW, Settings())  # holds the trace lock, not committed yet

    outcome: dict[str, Any] = {}

    def second_batch() -> None:
        with engine.begin() as conn:
            outcome["body"] = ingest(conn, principal, batch(*second), NOW, Settings())

    worker = threading.Thread(target=second_batch)
    worker.start()
    worker.join(timeout=1.0)
    assert worker.is_alive(), "the second batch must wait for the trace lock"
    with engine.connect() as probe:
        waiting = probe.execute(
            text(
                "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND datname = current_database()"
            )
        ).scalar_one()
    assert waiting == 1
    transaction.commit()
    holder.close()
    worker.join(timeout=10)
    assert not worker.is_alive() and outcome["body"]["counts"]["accepted"] == 3

    reference.post(batch(*events))  # the same events in one batch, no concurrency
    assert stored_state(engine, c.project_id) == stored_state(engine, reference.project_id)


def test_many_concurrent_clients_on_one_trace_lose_nothing(engine: Engine) -> None:
    c = make_collector(engine)
    reference = make_collector(engine, name="reference")
    root = sid(1)
    children = [(start(TRACE, sid(10 + i), root, name=f"step-{i}", at=i * 0.01),
                 end(TRACE, sid(10 + i), root, name=f"step-{i}", at=i * 0.01, duration_s=0.005),
                 point(100 + i, TRACE, sid(10 + i))) for i in range(24)]  # fmt: skip
    batches = [batch(start(TRACE, root), end(TRACE, root))] + [batch(*child) for child in children]
    batches += batches[:6]  # some clients retry: duplicates arrive concurrently with first copies
    app = create_app(Settings(), engine, FixedClock(NOW))
    with LiveServer(app) as server, ThreadPoolExecutor(max_workers=8) as pool:
        replies = list(pool.map(lambda b: post_json(server.url, c.key, b), batches))
    assert all(status == 200 for status, _ in replies), replies
    accepted = sum(body["counts"]["accepted"] for _, body in replies)
    duplicates = sum(body["counts"]["duplicate"] for _, body in replies)
    assert (accepted, duplicates) == (2 + 24 * 3, 2 + 5 * 3)

    reference.post(batch(start(TRACE, root), end(TRACE, root), *(e for child in children for e in child)))
    state = stored_state(engine, c.project_id)
    assert state == stored_state(engine, reference.project_id)
    assert state["traces"][0]["span_count"] == 25 and state["traces"][0]["span_event_count"] == 24


def test_batches_sharing_traces_in_opposite_orders_do_not_deadlock(engine: Engine) -> None:
    c = make_collector(engine)
    traces = [tid(0xD0 + n) for n in range(4)]
    forward = [batch(*(start(t, sid(i + 1), name=f"s{i}") for t in traces)) for i in range(15)]
    backward = [batch(*(end(t, sid(i + 1), name=f"s{i}") for t in reversed(traces))) for i in range(15)]
    started = time.monotonic()
    app = create_app(Settings(), engine, FixedClock(NOW))
    with LiveServer(app) as server, ThreadPoolExecutor(max_workers=6) as pool:
        replies = list(pool.map(lambda b: post_json(server.url, c.key, b), [*forward, *backward]))
    assert [status for status, _ in replies] == [200] * 30, [b for s, b in replies if s != 200]
    assert time.monotonic() - started < 60
    state = stored_state(engine, c.project_id)
    assert [t["span_count"] for t in state["traces"]] == [15] * 4
    assert all(t["open_span_count"] == 0 for t in state["traces"])
