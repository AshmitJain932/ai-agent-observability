"""HTTP exporter against a real local HTTP server: delivery, outages, bounded retries."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

import aobs
from aobs.config import ConfigError
from aobs.export import HttpExporter, InMemoryExporter
from sdk_support import check_bodies

Responder = Callable[[int], tuple[int, dict[str, str], bytes]]


class Collector:
    """A scripted collector. ``responder(n)`` -> (status, headers, body) for the n-th request."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.responder: Responder = lambda n: (200, {}, b'{"accepted": 1}')
        self.delay_s = 0.0
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                collector.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                if collector.delay_s:
                    time.sleep(collector.delay_s)
                status, headers, payload = collector.responder(len(collector.requests))
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def collector(monkeypatch: pytest.MonkeyPatch) -> Iterator[Collector]:
    # urllib honours *_proxy variables; make sure loopback traffic never goes through a proxy.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    c = Collector()
    yield c
    c.close()


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _emit(name: str = "s") -> None:
    with aobs.run(name), aobs.span("child"):
        pass


def test_delivers_valid_batch_with_auth(collector: Collector, make_sdk: Callable[..., Any]) -> None:
    make_sdk(exporter=HttpExporter(collector.url, api_key="aobs_ik_test_secret"))
    _emit()
    assert aobs.flush(5)
    (request,) = collector.requests
    assert request["path"] == "/v1/ingest"
    assert request["headers"]["Authorization"] == "Bearer aobs_ik_test_secret"
    assert request["headers"]["Content-Type"] == "application/json"
    assert request["headers"]["User-Agent"].startswith("aobs-python/")
    check_bodies([request["body"]])
    assert b"aobs_ik_test_secret" not in request["body"]


def test_endpoint_from_environment(collector: Collector, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AOBS_ENDPOINT", collector.url)
    monkeypatch.setenv("AOBS_API_KEY", "k")
    aobs.init(service_name="env-test", schedule_delay_s=0.02)
    _emit()
    assert aobs.flush(5)
    assert collector.requests[0]["headers"]["Authorization"] == "Bearer k"


def test_transient_errors_are_retried(collector: Collector, make_sdk: Callable[..., Any]) -> None:
    collector.responder = lambda n: (503, {}, b"") if n < 3 else (200, {}, b"{}")
    make_sdk(exporter=HttpExporter(collector.url))
    _emit()
    assert aobs.flush(5)
    assert len(collector.requests) == 3
    assert collector.requests[0]["body"] != collector.requests[2]["body"]  # sent_at refreshed
    diag = aobs.diagnostics()
    assert diag["export_retries"] == 2 and diag["events_exported"] == 2


def test_retry_after_is_honoured(collector: Collector, make_sdk: Callable[..., Any]) -> None:
    collector.responder = lambda n: (429, {"Retry-After": "0.3"}, b"") if n == 1 else (200, {}, b"{}")
    make_sdk(exporter=HttpExporter(collector.url), retry_backoff_max_s=1.0)
    _emit()
    started = time.perf_counter()
    assert aobs.flush(5)
    assert time.perf_counter() - started >= 0.29
    assert len(collector.requests) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413])
def test_permanent_errors_are_not_retried(collector: Collector, make_sdk: Callable[..., Any], status: int) -> None:
    collector.responder = lambda n: (status, {}, b"")
    make_sdk(exporter=HttpExporter(collector.url))
    _emit()
    aobs.flush(5)
    assert len(collector.requests) == 1
    assert aobs.diagnostics()["events_dropped_export_failed"] == 2


def test_collector_down_bounded_retries_and_app_unaffected(make_sdk: Callable[..., Any]) -> None:
    make_sdk(exporter=HttpExporter(f"http://127.0.0.1:{_closed_port()}"), max_export_attempts=3)
    durations = []
    for _ in range(20):
        started = time.perf_counter()
        _emit()
        durations.append(time.perf_counter() - started)
    assert max(durations) < 0.05  # recording never waits for the network
    aobs.flush(5)
    diag = aobs.diagnostics()
    assert diag["batches_failed"] >= 1
    assert diag["export_attempts"] == diag["batches_failed"] * 3  # exactly max attempts each, no more
    assert diag["events_dropped_export_failed"] == 40
    assert "Connection refused" in diag["last_error"] or "URLError" in diag["last_error"]


def test_slow_collector_times_out_and_is_retried(collector: Collector, make_sdk: Callable[..., Any]) -> None:
    collector.delay_s = 0.5
    make_sdk(exporter=HttpExporter(collector.url), export_timeout_s=0.1, max_export_attempts=2)
    _emit()
    aobs.flush(5)
    diag = aobs.diagnostics()
    assert diag["export_attempts"] == 2 and diag["events_dropped_export_failed"] == 2


def test_redirects_are_not_followed(collector: Collector, make_sdk: Callable[..., Any]) -> None:
    other = Collector()
    try:
        collector.responder = lambda n: (307, {"Location": other.url + "/steal"}, b"")
        make_sdk(exporter=HttpExporter(collector.url, api_key="secret-key"))
        _emit()
        aobs.flush(5)
        assert other.requests == []  # the Authorization header was never forwarded
        assert aobs.diagnostics()["batches_failed"] == 1
    finally:
        other.close()


def test_collector_rejections_are_counted(collector: Collector, make_sdk: Callable[..., Any]) -> None:
    collector.responder = lambda n: (200, {}, json.dumps({"rejected": [{"index": 0}]}).encode())
    make_sdk(exporter=HttpExporter(collector.url))
    _emit()
    assert aobs.flush(5)
    assert aobs.diagnostics()["events_rejected_by_collector"] == 1


def test_plaintext_key_to_remote_host_is_flagged() -> None:
    with pytest.raises(ConfigError, match="plaintext"):
        aobs.init(endpoint="http://collector.example.com", api_key="k", strict=True)
    aobs.init(endpoint="http://localhost:9", api_key="k", strict=True)  # local plaintext is fine


def test_no_exporter_disables_with_explanation() -> None:
    aobs.init(service_name="x")
    diag = aobs.diagnostics()
    assert diag["enabled"] is False and "no exporter" in diag["disabled_reason"]
    with aobs.span("s") as sp:
        assert not sp.is_recording
    with pytest.raises(ConfigError):
        aobs.init(service_name="x", strict=True)


def test_exporter_protocol_is_open(make_sdk: Callable[..., Any]) -> None:
    from aobs.export import Exporter

    assert isinstance(InMemoryExporter(), Exporter)
    assert isinstance(HttpExporter("http://localhost"), Exporter)
