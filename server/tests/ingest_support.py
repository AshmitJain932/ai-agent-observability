"""Helpers for ingest tests: schema-v1 batch builders, a collector client and database dumps."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Engine, text

from aobs_server.api import create_app
from aobs_server.clock import FixedClock
from aobs_server.config import Settings
from aobs_server.storage.tables import metadata
from tenants import bearer, make_key, make_project

T0 = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)
NOW = T0 + timedelta(seconds=30)


def ts(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def tid(n: int) -> str:
    return f"{n:032x}"


def sid(n: int) -> str:
    return f"{n:016x}"


def eid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012x}"


def start(trace: str, span: str, parent: str | None = None, *, name: str = "run", kind: str = "agent",
          at: float = 0.0, **fields: Any) -> dict[str, Any]:  # fmt: skip
    return {"type": "span_start", "trace_id": trace, "span_id": span, "parent_span_id": parent, "kind": kind,
            "name": name, "start_time": ts(at), **fields}  # fmt: skip


def end(trace: str, span: str, parent: str | None = None, *, name: str = "run", kind: str = "agent",
        at: float = 0.0, duration_s: float = 1.0, status: str = "ok", **fields: Any) -> dict[str, Any]:  # fmt: skip
    body = start(trace, span, parent, name=name, kind=kind, at=at, **fields)
    if kind == "tool" and "tool" not in body:
        body["tool"] = {"name": name}
    return {**body, "type": "span_end", "end_time": ts(at + duration_s), "duration_ms": duration_s * 1000,
            "status": status}  # fmt: skip


def point(n: int, trace: str, span: str, *, name: str = "retry", at: float = 0.5, **fields: Any) -> dict[str, Any]:
    return {"type": "span_event", "event_id": eid(n), "trace_id": trace, "span_id": span, "name": name,
            "time": ts(at), **fields}  # fmt: skip


def batch(*events: dict[str, Any], environment: str = "dev", batch_id: str | None = None,
          sent_at: float = 29.0, capture: str = "full") -> dict[str, Any]:  # fmt: skip
    body: dict[str, Any] = {
        "schema_version": "1.0",
        "sent_at": ts(sent_at),
        "resource": {
            "service_name": "test-agent",
            "environment": environment,
            "sdk": {"name": "aobs-python", "version": "0.1.0", "language": "python"},
            "capture": {"mode": capture, "error_messages": True, "stack_traces": True},
        },
        "events": list(events),
    }
    if batch_id is not None:
        body["batch_id"] = batch_id
    return body


def simple_trace(trace: str) -> list[dict[str, Any]]:
    """A finished run: root agent span, one tool call and one LLM call, and a retry event."""
    root, tool, llm = sid(1), sid(2), sid(3)
    usage = {"input_tokens": 10, "output_tokens": 5, "source": "provider_reported"}
    return [
        start(trace, root, name="answer_ticket", agent={"name": "triage"}, session_id="s-1"),
        start(trace, tool, root, name="lookup", kind="tool", at=0.1, tool={"name": "lookup"}),
        end(trace, tool, root, name="lookup", kind="tool", at=0.1, duration_s=0.2, tool={"name": "lookup"}),
        point(1, trace, tool, at=0.15),
        start(trace, llm, root, name="chat", kind="llm", at=0.4, llm={"provider": "openai"}),
        end(trace, llm, root, name="chat", kind="llm", at=0.4, duration_s=0.3, llm={"provider": "openai"}, usage=usage),
        end(trace, root, name="answer_ticket", duration_s=1.0, agent={"name": "triage"}, session_id="s-1"),
    ]


@dataclass
class Collector:
    engine: Engine
    project_id: UUID
    key: str
    key_id: UUID
    settings: Settings
    client: TestClient = field(repr=False)

    def post(self, body: Any, *, key: str | None = None, headers: dict[str, str] | None = None) -> Response:
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        return self.client.post(
            "/v1/ingest",
            content=raw,
            headers={"Content-Type": "application/json", **bearer(key or self.key), **(headers or {})},
        )

    def key_for(self, **kwargs: Any) -> str:
        return make_key(self.engine, self.project_id, **kwargs)[1]


def make_collector(engine: Engine, settings: Settings | None = None, name: str = "proj", **project: Any) -> Collector:
    cfg = settings or Settings()
    project_id = make_project(engine, name, **{"capture_ceiling": "full", "stack_traces": True, **project})
    key_id, key = make_key(engine, project_id)
    client = TestClient(create_app(cfg, engine, FixedClock(NOW)), raise_server_exceptions=False)
    return Collector(engine, project_id, key, key_id, cfg, client)


def count_rows(engine: Engine, project_id: UUID | None = None) -> dict[str, int]:
    tables = ["traces", "spans", "span_events", "span_content", "resources", "environments", "ingest_diagnostics"]
    out = {}
    with engine.connect() as conn:
        for name in tables:
            where = "" if project_id is None else " WHERE project_id = :p"
            out[name] = conn.execute(text(f"SELECT count(*) FROM {name}{where}"), {"p": project_id}).scalar_one()
    return out


def stored_state(engine: Engine, project_id: UUID) -> dict[str, list[dict[str, Any]]]:
    """Every stored span, span event, content row and trace summary of a project, minus receive
    times and the project id, so two projects fed the same data in different orders compare equal."""
    skip = {"project_id", "first_received_at", "last_received_at", "received_at", "resource_hash"}
    queries = {
        "spans": "SELECT * FROM spans WHERE project_id = :p ORDER BY trace_id, span_id",
        "span_events": "SELECT * FROM span_events WHERE project_id = :p ORDER BY event_id",
        "span_content": "SELECT * FROM span_content WHERE project_id = :p ORDER BY trace_id, span_id",
        "traces": "SELECT * FROM traces WHERE project_id = :p ORDER BY trace_id",
    }
    out: dict[str, list[dict[str, Any]]] = {}
    with engine.connect() as conn:
        for name, query in queries.items():
            rows = conn.execute(text(query), {"p": project_id})
            out[name] = [{k: v for k, v in row._mapping.items() if k not in skip} for row in rows]
    return out


def database_text(engine: Engine) -> str:
    """Every row of every table as JSON text (for 'this string is stored nowhere' assertions)."""
    with engine.connect() as conn:
        return "\n".join(
            conn.execute(text(f"SELECT coalesce(string_agg(row_to_json(t)::text, E'\\n'), '') FROM {table.name} t"))
            .scalar_one()
            for table in metadata.sorted_tables
        )  # fmt: skip


class LiveServer:
    """The collector app on a real local port (uvicorn in a thread), for HTTP clients like the SDK."""

    def __init__(self, app: Any) -> None:
        import socket

        import uvicorn

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = int(s.getsockname()[1])
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.url = f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> LiveServer:
        import threading
        import time

        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("collector did not start")
            time.sleep(0.02)
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


def post_json(url: str, key: str, body: Any) -> tuple[int, dict[str, Any]]:
    """POST a batch with urllib (no proxy), returning (status, parsed body)."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        url + "/v1/ingest",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def zulu(spans: dict[str, Any]) -> dict[str, Any]:
    """Assembled spans with their times as the API writes them (RFC 3339, "Z")."""
    out = {}
    for span_id, span in spans.items():
        times = {k: span[k].replace("+00:00", "Z") for k in ("start_time", "end_time") if span[k] is not None}
        out[span_id] = span | times
    return out
