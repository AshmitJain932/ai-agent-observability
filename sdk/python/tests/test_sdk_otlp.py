"""OTLP/HTTP JSON exporter against a real local HTTP server: structure, mapping, conformance with
the OTLP protobuf definitions, cross-batch span events, privacy, retries, fan-out."""

from __future__ import annotations

import base64
import gzip
import json
import re
import socket
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from google.protobuf import json_format
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

import aobs
from aobs.export import FanOutExporter, HttpExporter, InMemoryExporter, OtlpHttpExporter
from aobs.export.base import SUCCESS, EncodedBatch, ExportResult
from aobs.export.otlp import _unix_nanos
from sdk_support import check_bodies

Responder = Callable[[int], tuple[int, dict[str, str], bytes]]
SECRET = "Customer jane.doe@example.com paid with 4111 1111 1111 1111"


class Collector:
    """A scripted OTLP receiver. ``responder(n)`` -> (status, headers, body) for the n-th request."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.responder: Responder = lambda n: (200, {"Content-Type": "application/json"}, b"{}")
        self.delay_s = 0.0
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                body = gzip.decompress(raw) if self.headers.get("Content-Encoding") == "gzip" else raw
                collector.requests.append(
                    {
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "raw": raw,
                        "body": body,
                    }
                )
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
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.url = self.base + "/v1/traces"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def payloads(self) -> list[dict[str, Any]]:
        return [json.loads(r["body"]) for r in self.requests]

    def spans(self) -> list[dict[str, Any]]:
        """All spans of all requests (each retry counted once: identical bodies are collapsed)."""
        unique = {r["body"]: json.loads(r["body"]) for r in self.requests}
        return [
            span
            for payload in unique.values()
            for rs in payload["resourceSpans"]
            for ss in rs["scopeSpans"]
            for span in ss["spans"]
        ]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def otlp(monkeypatch: pytest.MonkeyPatch) -> Iterator[Collector]:
    # urllib honours *_proxy variables; make sure loopback traffic never goes through a proxy.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    for name in (
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
    ):
        monkeypatch.delenv(name, raising=False)
    c = Collector()
    yield c
    c.close()


def attrs(item: dict[str, Any]) -> dict[str, Any]:
    """OTLP attribute list -> {key: plain value}."""

    def plain(value: dict[str, Any]) -> Any:
        ((kind, inner),) = value.items()
        if kind == "intValue":
            return int(inner)
        if kind == "arrayValue":
            return [plain(v) for v in inner.get("values", [])]
        return inner

    keys = [a["key"] for a in item.get("attributes", [])]
    assert len(keys) == len(set(keys)), f"duplicate attribute keys: {keys}"
    return {a["key"]: plain(a["value"]) for a in item.get("attributes", [])}


def by_name(spans: list[dict[str, Any]], name: str) -> dict[str, Any]:
    matches = [s for s in spans if s["name"] == name]
    assert len(matches) == 1, [s["name"] for s in spans]
    return matches[0]


def to_proto(payload: dict[str, Any]) -> ExportTraceServiceRequest:
    """Parse with the official OTLP protobuf definitions (unknown fields are errors). OTLP/JSON
    encodes ids as hex where proto3 JSON uses base64, so ids are converted first, exactly as an
    OTLP receiver does."""
    copy = json.loads(json.dumps(payload))
    for rs in copy["resourceSpans"]:
        for ss in rs["scopeSpans"]:
            for span in ss["spans"]:
                for key in ("traceId", "spanId", "parentSpanId"):
                    if key in span:
                        span[key] = base64.b64encode(bytes.fromhex(span[key])).decode()
                for link in span.get("links", []):
                    for key in ("traceId", "spanId"):
                        link[key] = base64.b64encode(bytes.fromhex(link[key])).decode()
    return json_format.Parse(json.dumps(copy), ExportTraceServiceRequest())


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _agent_run() -> None:
    with aobs.run("answer_ticket", agent="triage", agent_version="1.4.0", session_id="ticket-8812") as root:
        root.set_attribute("app.tier", "gold")
        with aobs.llm_span(provider="openai", request_model="model-x", name="chat") as llm:
            llm.set_llm(
                response_model="model-x-2026",
                request={"temperature": 0.2, "max_tokens": 512},
                response={"id": "resp_1", "finish_reasons": ["tool_calls"]},
                provider_request_id="req_1",
            )
            llm.record_usage(input_tokens=812, output_tokens=95, cache_read_input_tokens=512)
        with aobs.tool_span("get_order_status", call_id="call_abc") as tool:
            tool.fingerprint_input({"order_id": "1042"})
            tool.event("cache.miss", {"app.key": "order"})


# ------------------------------------------------------------------------------ structure and mapping


def test_exports_otlp_json_with_resource_scope_and_spans(otlp: Collector, make_sdk: Callable[..., Any]) -> None:
    memory = InMemoryExporter()
    make_sdk(exporter=FanOutExporter(memory, OtlpHttpExporter(otlp.url)), service_version="git:3f9c2e1")
    _agent_run()
    assert aobs.flush(5)
    check_bodies(memory.bodies)
    aobs_ends = {e["name"]: e for e in memory.events() if e["type"] == "span_end"}

    (request,) = otlp.requests
    assert request["path"] == "/v1/traces"
    assert request["headers"]["content-type"] == "application/json"
    assert request["headers"]["user-agent"].startswith("aobs-python/")
    (resource_spans,) = json.loads(request["body"])["resourceSpans"]
    resource = attrs(resource_spans["resource"])
    assert resource["service.name"] == "sdk-tests"
    assert resource["service.version"] == "git:3f9c2e1"
    assert resource["deployment.environment.name"] == "test"
    assert resource["telemetry.sdk.name"] == "aobs-python"
    assert resource["telemetry.sdk.language"] == "python"
    assert resource["aobs.capture.mode"] == "metadata_only"
    (scope_spans,) = resource_spans["scopeSpans"]
    assert scope_spans["scope"]["name"] == "aobs"
    spans = scope_spans["spans"]
    assert sorted(s["name"] for s in spans) == ["answer_ticket", "chat", "get_order_status"]

    for span in spans:
        source = aobs_ends[span["name"]]
        assert span["traceId"] == source["trace_id"] and len(span["traceId"]) == 32
        assert span["spanId"] == source["span_id"] and len(span["spanId"]) == 16
        assert int(span["startTimeUnixNano"]) == _unix_nanos(source["start_time"])
        assert int(span["endTimeUnixNano"]) == _unix_nanos(source["end_time"])
        assert int(span["endTimeUnixNano"]) >= int(span["startTimeUnixNano"])
        assert isinstance(span["startTimeUnixNano"], str)  # int64 is a string in OTLP/JSON
        assert span["status"] == {"code": 0}

    root, llm, tool = by_name(spans, "answer_ticket"), by_name(spans, "chat"), by_name(spans, "get_order_status")
    assert "parentSpanId" not in root
    assert llm["parentSpanId"] == tool["parentSpanId"] == root["spanId"]
    assert len({s["traceId"] for s in spans}) == 1
    assert (root["kind"], llm["kind"], tool["kind"]) == (1, 3, 1)  # INTERNAL, CLIENT, INTERNAL

    assert attrs(root) == {
        "app.tier": "gold",
        "aobs.span.kind": "agent",
        "aobs.status": "ok",
        "gen_ai.conversation.id": "ticket-8812",
        "gen_ai.agent.name": "triage",
        "gen_ai.agent.version": "1.4.0",
        "gen_ai.operation.name": "invoke_agent",
    }
    llm_attrs = attrs(llm)
    assert {k: v for k, v in llm_attrs.items() if k.startswith("gen_ai.")} == {
        "gen_ai.provider.name": "openai",
        "gen_ai.system": "openai",
        "gen_ai.operation.name": "chat",
        "gen_ai.request.model": "model-x",
        "gen_ai.response.model": "model-x-2026",
        "gen_ai.request.temperature": 0.2,
        "gen_ai.request.max_tokens": 512,
        "gen_ai.response.id": "resp_1",
        "gen_ai.response.finish_reasons": ["tool_calls"],
        "gen_ai.usage.input_tokens": 812,
        "gen_ai.usage.output_tokens": 95,
        "gen_ai.usage.cache_read.input_tokens": 512,
    }
    assert llm_attrs["aobs.llm.provider_request_id"] == "req_1"
    assert llm_attrs["aobs.usage.source"] == "provider_reported"
    assert "aobs.usage.total_tokens" not in llm_attrs  # unknown stays absent, never zero
    tool_attrs = attrs(tool)
    assert tool_attrs["gen_ai.tool.name"] == "get_order_status"
    assert tool_attrs["gen_ai.tool.call.id"] == "call_abc"
    assert tool_attrs["gen_ai.operation.name"] == "execute_tool"
    assert tool_attrs["aobs.tool.type"] == "function"
    assert tool_attrs["aobs.input_fingerprint"] == aobs_ends["get_order_status"]["input_fingerprint"]
    (event,) = tool["events"]
    assert event["name"] == "cache.miss" and attrs(event) == {"app.key": "order"}
    assert int(tool["startTimeUnixNano"]) <= int(event["timeUnixNano"]) <= int(tool["endTimeUnixNano"])


def test_payload_conforms_to_the_otlp_protobuf_definitions(otlp: Collector, make_sdk: Callable[..., Any]) -> None:
    make_sdk(exporter=OtlpHttpExporter(otlp.url), capture="full")
    _agent_run()
    with aobs.run("linked") as linked:
        linked.add_link("4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7", "handoff_from")
        with aobs.tool_span("fails") as tool:
            tool.set_input({"q": "x"})
            tool.set_error("ValueError", message="bad input", code="E1", retryable=False)
    assert aobs.flush(5)
    spans = {}
    for payload in otlp.payloads():
        for rs in to_proto(payload).resource_spans:
            for ss in rs.scope_spans:
                for span in ss.spans:
                    assert len(span.trace_id) == 16 and len(span.span_id) == 8
                    assert len(span.parent_span_id) in (0, 8)
                    assert span.end_time_unix_nano >= span.start_time_unix_nano > 1_600_000_000 * 10**9
                    spans[span.name] = span
    assert len(spans) == 5
    assert spans["fails"].status.code == 2 and spans["fails"].status.message == "bad input"
    assert spans["chat"].kind == 3
    (link,) = spans["linked"].links
    assert link.trace_id.hex() == "4bf92f3577b34da6a3ce929d0e0e4736" and link.span_id.hex() == "00f067aa0ba902b7"
    assert link.attributes[0].key == "aobs.link.relation" and link.attributes[0].value.string_value == "handoff_from"


def test_error_status_message_and_exception_event(otlp: Collector, make_sdk: Callable[..., Any]) -> None:
    make_sdk(exporter=OtlpHttpExporter(otlp.url), capture_stack_traces=True)
    with pytest.raises(TimeoutError), aobs.run("r"), aobs.tool_span("issue_refund"):
        raise TimeoutError("refund API did not respond")
    assert aobs.flush(5)
    spans = otlp.spans()
    tool, root = by_name(spans, "issue_refund"), by_name(spans, "r")
    assert tool["status"] == {"code": 2, "message": "refund API did not respond"}
    tool_attrs = attrs(tool)
    assert tool_attrs["error.type"] == "TimeoutError"
    assert tool_attrs["aobs.error.origin"] == "timeout"
    assert tool_attrs["aobs.status"] == "error"
    (exception,) = tool["events"]
    assert exception["name"] == "exception" and exception["timeUnixNano"] == tool["endTimeUnixNano"]
    exc_attrs = attrs(exception)
    assert exc_attrs["exception.type"] == "TimeoutError"
    assert exc_attrs["exception.message"] == "refund API did not respond"
    assert "raise TimeoutError" in exc_attrs["exception.stacktrace"]
    assert root["status"]["code"] == 2


def test_cancelled_spans_keep_unset_status(otlp: Collector, make_sdk: Callable[..., Any]) -> None:
    make_sdk(exporter=OtlpHttpExporter(otlp.url))
    with aobs.run("r") as root:
        root.set_status("cancelled")
    assert aobs.flush(5)
    (span,) = otlp.spans()
    assert span["status"] == {"code": 0} and attrs(span)["aobs.status"] == "cancelled"


def test_error_message_and_stack_stay_out_when_policy_disables_them(
    otlp: Collector, make_sdk: Callable[..., Any]
) -> None:
    make_sdk(exporter=OtlpHttpExporter(otlp.url), capture_error_messages=False, capture_stack_traces=False)
    with pytest.raises(RuntimeError), aobs.run("r"):
        raise RuntimeError("secret detail sk-proj-abcdefghijklmnopqrst")
    assert aobs.flush(5)
    body = otlp.requests[0]["body"].decode()
    assert "secret detail" not in body and "sk-proj" not in body and "Traceback" not in body
    (span,) = otlp.spans()
    assert span["status"] == {"code": 2}
    assert attrs(span["events"][0]) == {"exception.type": "RuntimeError"}


# ------------------------------------------------------------------------------ privacy


def _tool_and_llm_with_content() -> None:
    @aobs.tool
    def lookup(note: str) -> str:
        return note

    with aobs.run("r"):
        lookup(SECRET)
        with aobs.llm_span(provider="acme", name="chat") as sp:
            sp.set_input([{"role": "user", "parts": [{"type": "text", "content": SECRET}]}], "messages")
            sp.set_output(SECRET)


def test_no_content_leaves_when_capture_is_metadata_only(otlp: Collector, make_sdk: Callable[..., Any]) -> None:
    make_sdk(exporter=OtlpHttpExporter(otlp.url))  # capture defaults to metadata_only
    _tool_and_llm_with_content()
    assert aobs.flush(5)
    body = b"".join(r["body"] for r in otlp.requests).decode()
    # Each fragment has a character no hex id, hash or fingerprint can contain, so a match is a leak.
    for fragment in ("jane.doe", "4111 1111", "Customer"):
        assert fragment not in body
    spans = otlp.spans()
    for name in ("lookup", "chat"):
        span_attrs = attrs(by_name(spans, name))
        assert span_attrs["aobs.content.input.status"] == "not_captured"
        assert not any(k.endswith((".messages", ".arguments", ".result", ".value")) for k in span_attrs)


def test_redacted_content_is_exported_as_redacted_only(otlp: Collector, make_sdk: Callable[..., Any]) -> None:
    memory = InMemoryExporter()
    make_sdk(exporter=FanOutExporter(memory, OtlpHttpExporter(otlp.url)), capture="redacted")
    _tool_and_llm_with_content()
    assert aobs.flush(5)
    body = b"".join(r["body"] for r in otlp.requests).decode()
    assert "jane.doe@example.com" not in body and "4111 1111 1111 1111" not in body
    spans = otlp.spans()
    chat = attrs(by_name(spans, "chat"))
    source = next(e for e in memory.events() if e.get("name") == "chat" and e["type"] == "span_end")["content"]
    assert chat["aobs.content.output.status"] == "redacted"
    assert chat["aobs.content.output.redaction_count"] == source["output"]["redaction_count"]
    assert source["output"]["format"] == "text"
    assert chat["aobs.content.output.value"] == source["output"]["value"]  # text is exported verbatim
    assert json.loads(chat["gen_ai.input.messages"]) == source["input"]["value"]
    lookup = attrs(by_name(spans, "lookup"))
    assert "[REDACTED:email]" in lookup["gen_ai.tool.call.arguments"]
    assert "[REDACTED:email]" in lookup["gen_ai.tool.call.result"]


def test_include_content_false_keeps_captured_content_out(otlp: Collector, make_sdk: Callable[..., Any]) -> None:
    make_sdk(exporter=OtlpHttpExporter(otlp.url, include_content=False), capture="full")
    _tool_and_llm_with_content()
    assert aobs.flush(5)
    body = b"".join(r["body"] for r in otlp.requests).decode()
    assert "jane.doe" not in body and "Customer" not in body
    chat = attrs(by_name(otlp.spans(), "chat"))
    assert chat["aobs.content.input.status"] == chat["aobs.content.output.status"] == "not_exported"


def test_full_capture_text_output_is_exported_verbatim(otlp: Collector, make_sdk: Callable[..., Any]) -> None:
    make_sdk(exporter=OtlpHttpExporter(otlp.url), capture="full")
    with aobs.run("r") as root:
        root.set_output("plain answer", "text")
    assert aobs.flush(5)
    (span,) = otlp.spans()
    assert attrs(span)["aobs.content.output.value"] == "plain answer"


# ------------------------------------------------------------------------------ span lifecycle across batches


def test_span_event_and_end_in_different_batches_become_one_span(otlp: Collector, make_sdk: Callable[..., Any]) -> None:
    exporter = OtlpHttpExporter(otlp.url)
    memory = InMemoryExporter()
    make_sdk(exporter=FanOutExporter(memory, exporter))
    with aobs.run("long") as root:
        root.event("stage", {"app.n": 1})
        assert aobs.flush(5)  # exports the span_start and the span_event; the span is still open
        assert otlp.requests == []  # nothing complete yet, so nothing is sent
        assert exporter.diagnostics()["pending_events"] == 1
        root.event("stage", {"app.n": 2})
    assert aobs.flush(5)
    types = [e["type"] for batch in memory.batches for e in batch["events"]]
    assert types.count("span_start") == 1 and len(memory.batches) == 2  # it really spanned batches
    (span,) = otlp.spans()
    assert [attrs(e)["app.n"] for e in span["events"]] == [1, 2]
    diag = exporter.diagnostics()
    assert diag["starts_skipped"] == 1 and diag["events_attached"] == 2 and diag["pending_events"] == 0
    assert diag["spans_exported"] == 1


def test_event_after_its_span_was_exported_is_counted_not_buffered(
    otlp: Collector, make_sdk: Callable[..., Any]
) -> None:
    exporter = OtlpHttpExporter(otlp.url)
    make_sdk(exporter=exporter)
    with aobs.run("r") as root:
        pass
    assert aobs.flush(5)
    root.event("late")
    assert aobs.flush(5)
    assert len(otlp.requests) == 1
    diag = exporter.diagnostics()
    assert diag["events_dropped_late"] == 1 and diag["pending_events"] == 0


def test_events_of_spans_that_never_end_are_dropped_and_counted_at_shutdown(
    otlp: Collector, make_sdk: Callable[..., Any]
) -> None:
    exporter = OtlpHttpExporter(otlp.url)
    make_sdk(exporter=exporter)
    root = aobs.run("never_ends")
    root.event("e1")
    assert aobs.shutdown(5)
    assert otlp.requests == []
    diag = exporter.diagnostics()
    assert diag["events_dropped_unended"] == 1 and diag["starts_skipped"] == 1 and diag["pending_events"] == 0


def _batch(events: list[dict[str, Any]], batch_id: str = "b1") -> EncodedBatch:
    resource = {"service_name": "svc", "environment": "test", "sdk": {"name": "x", "version": "1", "language": "py"}}
    body = json.dumps(
        {"schema_version": "1.0", "sent_at": "2026-10-12T12:00:00Z", "resource": resource, "events": events}
    )
    return EncodedBatch(body.encode(), len(events), batch_id, 1)


def _event(span_id: str, n: int) -> dict[str, Any]:
    return {
        "type": "span_event",
        "event_id": f"0192f4a1-7c3e-7b10-9a2d-5f6e1c0b8d{n:02d}",
        "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
        "span_id": span_id,
        "name": f"e{n}",
        "time": "2026-10-12T12:15:00.123456789Z",
    }


def _end(span_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "type": "span_end",
        "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
        "span_id": span_id,
        "parent_span_id": None,
        "kind": "step",
        "name": "s",
        "start_time": "2026-10-12T12:15:00.000000001Z",
        "end_time": "2026-10-12T12:15:01.5Z",
        "duration_ms": 1500.0,
        "status": "ok",
        **extra,
    }


def test_pending_events_are_bounded(otlp: Collector) -> None:
    exporter = OtlpHttpExporter(otlp.url, max_pending_events=3)
    events = [_event(f"{i + 1:016x}", i) for i in range(5)]
    assert exporter.export(_batch(events), 5).outcome == "success"
    diag = exporter.diagnostics()
    assert diag["pending_events"] == 3 and diag["events_dropped_buffer_full"] == 2
    assert exporter.export(_batch([_end(f"{5:016x}")], "b2"), 5).outcome == "success"
    (span,) = otlp.spans()
    assert [e["name"] for e in span["events"]] == ["e4"]  # the newest events were kept


def test_timestamps_convert_to_exact_unix_nanos(otlp: Collector) -> None:
    exporter = OtlpHttpExporter(otlp.url)
    assert exporter.export(_batch([_event("00f067aa0ba902b7", 1), _end("00f067aa0ba902b7")]), 5) == SUCCESS
    (span,) = otlp.spans()
    assert span["startTimeUnixNano"] == "1791807300000000001"
    assert span["endTimeUnixNano"] == "1791807301500000000"
    assert span["events"][0]["timeUnixNano"] == "1791807300123456789"
    assert _unix_nanos("1970-01-01T00:00:00Z") == 0
    for bad in ("2026-10-12T12:15:00+00:00", "2026-13-01T00:00:00Z", "2026-10-12 12:15:00Z", 5):
        with pytest.raises(ValueError):
            _unix_nanos(bad)


def test_invalid_events_are_skipped_and_counted(otlp: Collector) -> None:
    exporter = OtlpHttpExporter(otlp.url)
    events = [
        _end("00f067aa0ba902b7"),
        _end("0000000000000000"),
        _end("b7ad6b7169203331", start_time="yesterday"),
        {"type": "span_end"},
        {"type": "mystery"},
    ]
    assert exporter.export(_batch(events), 5).outcome == "success"
    assert len(otlp.spans()) == 1
    assert exporter.diagnostics()["spans_invalid"] == 3
    assert exporter.export(EncodedBatch(b"not json", 1, "b9", 1), 5).outcome == "failure"


# ------------------------------------------------------------------------------ transport


def test_transient_errors_are_retried_with_identical_bodies(otlp: Collector, make_sdk: Callable[..., Any]) -> None:
    otlp.responder = lambda n: (503, {}, b"") if n < 3 else (200, {}, b"{}")
    exporter = OtlpHttpExporter(otlp.url)
    make_sdk(exporter=exporter)
    with aobs.run("r"):
        pass
    assert aobs.flush(5)
    assert len(otlp.requests) == 3
    assert len({r["body"] for r in otlp.requests}) == 1  # converted once, re-sent as is
    assert aobs.diagnostics()["export_retries"] == 2
    assert exporter.diagnostics()["spans_exported"] == 1


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_retryable_statuses(otlp: Collector, status: int) -> None:
    otlp.responder = lambda n: (status, {"Retry-After": "2"}, b"")
    result = OtlpHttpExporter(otlp.url).export(_batch([_end("00f067aa0ba902b7")]), 5)
    assert result.outcome == "retry" and result.retry_after_s == 2.0 and str(status) in result.message


def test_retry_after_is_honoured(otlp: Collector, make_sdk: Callable[..., Any]) -> None:
    otlp.responder = lambda n: (429, {"Retry-After": "0.3"}, b"") if n == 1 else (200, {}, b"{}")
    make_sdk(exporter=OtlpHttpExporter(otlp.url), retry_backoff_max_s=1.0)
    with aobs.run("r"):
        pass
    started = time.perf_counter()
    assert aobs.flush(5)
    assert time.perf_counter() - started >= 0.29
    assert len(otlp.requests) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413])
def test_permanent_errors_are_not_retried(otlp: Collector, make_sdk: Callable[..., Any], status: int) -> None:
    otlp.responder = lambda n: (status, {}, b"")
    exporter = OtlpHttpExporter(otlp.url)
    make_sdk(exporter=exporter)
    with aobs.run("r"):
        pass
    aobs.flush(5)
    assert len(otlp.requests) == 1
    assert aobs.diagnostics()["events_dropped_export_failed"] == 1
    assert exporter.diagnostics()["spans_failed"] == 1


def test_receiver_down_is_retried_then_dropped(make_sdk: Callable[..., Any]) -> None:
    make_sdk(exporter=OtlpHttpExporter(f"http://127.0.0.1:{_closed_port()}/v1/traces"), max_export_attempts=3)
    with aobs.run("r"):
        pass
    aobs.flush(5)
    diag = aobs.diagnostics()
    assert diag["export_attempts"] == 3 and diag["events_dropped_export_failed"] == 1
    assert "OTLP" in diag["last_error"]


def test_slow_receiver_times_out(otlp: Collector) -> None:
    otlp.delay_s = 0.5
    result = OtlpHttpExporter(otlp.url).export(_batch([_end("00f067aa0ba902b7")]), 0.1)
    assert result.outcome == "retry"


def test_partial_success_rejections_are_counted(otlp: Collector, make_sdk: Callable[..., Any]) -> None:
    otlp.responder = lambda n: (200, {}, b'{"partialSuccess":{"rejectedSpans":"1","errorMessage":"too old"}}')
    make_sdk(exporter=OtlpHttpExporter(otlp.url))
    with aobs.run("r"):
        pass
    assert aobs.flush(5)
    assert aobs.diagnostics()["events_rejected_by_collector"] == 1


def test_gzip_and_custom_headers(otlp: Collector) -> None:
    exporter = OtlpHttpExporter(otlp.url, headers={"x-honeycomb-team": "test-key"}, compression="gzip")
    assert exporter.export(_batch([_end("00f067aa0ba902b7")]), 5) == SUCCESS
    (request,) = otlp.requests
    assert request["headers"]["content-encoding"] == "gzip"
    assert request["headers"]["x-honeycomb-team"] == "test-key"
    assert request["raw"][:2] == b"\x1f\x8b" and b"test-key" not in request["body"]
    to_proto(json.loads(request["body"]))
    assert "test-key" not in repr(exporter)


def test_endpoint_and_headers_from_otel_environment(otlp: Collector, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", otlp.base + "/")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "api-key=a%3Db, x-other = 1 ,broken")
    exporter = OtlpHttpExporter()
    assert exporter.url == otlp.url
    assert exporter.export(_batch([_end("00f067aa0ba902b7")]), 5) == SUCCESS
    headers = otlp.requests[0]["headers"]
    assert headers["api-key"] == "a=b" and headers["x-other"] == "1"
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://127.0.0.1:1/custom")
    assert OtlpHttpExporter().url == "http://127.0.0.1:1/custom"
    assert OtlpHttpExporter(otlp.url).url == otlp.url  # explicit argument wins


def test_default_endpoint_and_invalid_arguments(otlp: Collector) -> None:
    assert OtlpHttpExporter().url == "http://localhost:4318/v1/traces"
    with pytest.raises(ValueError):
        OtlpHttpExporter("localhost:4318")
    with pytest.raises(ValueError):
        OtlpHttpExporter(otlp.url, compression="zstd")  # type: ignore[arg-type]


def test_redirects_are_not_followed(otlp: Collector) -> None:
    other = Collector()
    try:
        otlp.responder = lambda n: (307, {"Location": other.url}, b"")
        result = OtlpHttpExporter(otlp.url, headers={"authorization": "secret"}).export(
            _batch([_end("00f067aa0ba902b7")]), 5
        )
        assert result.outcome == "failure" and other.requests == []
    finally:
        other.close()


def test_resource_attributes_extend_and_override(otlp: Collector) -> None:
    exporter = OtlpHttpExporter(otlp.url, resource_attributes={"service.namespace": "support", "service.name": "x2"})
    assert exporter.export(_batch([_end("00f067aa0ba902b7")]), 5) == SUCCESS
    resource = attrs(otlp.payloads()[0]["resourceSpans"][0]["resource"])
    assert resource["service.namespace"] == "support" and resource["service.name"] == "x2"


# ------------------------------------------------------------------------------ fan-out


class Scripted:
    def __init__(self, *outcomes: ExportResult | Exception) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, int]] = []
        self.shut_down = False

    def export(self, batch: EncodedBatch, timeout_s: float) -> ExportResult:
        self.calls.append((batch.batch_id, batch.attempt))
        outcome = self.outcomes.pop(0) if self.outcomes else SUCCESS
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def shutdown(self) -> None:
        self.shut_down = True
        raise RuntimeError("boom")


def test_fan_out_resends_only_to_destinations_that_have_not_finished(
    otlp: Collector, make_sdk: Callable[..., Any]
) -> None:
    aobs_collector = Collector()
    try:
        otlp.responder = lambda n: (503, {}, b"") if n == 1 else (200, {}, b"{}")
        make_sdk(exporter=FanOutExporter(HttpExporter(aobs_collector.base), OtlpHttpExporter(otlp.url)))
        with aobs.run("r"):
            pass
        assert aobs.flush(5)
        assert len(aobs_collector.requests) == 1  # not re-sent because the other destination retried
        assert len(otlp.requests) == 2
        check_bodies([aobs_collector.requests[0]["body"]])
        diag = aobs.diagnostics()
        assert diag["export_retries"] == 1 and diag["events_exported"] == 1
    finally:
        aobs_collector.close()


def test_fan_out_combines_outcomes() -> None:
    batch = _batch([_end("00f067aa0ba902b7")])
    a = Scripted(ExportResult("retry", retry_after_s=1.0), SUCCESS)
    b = Scripted(ExportResult("retry", retry_after_s=3.0), ExportResult("failure", message="HTTP 400"))
    c = Scripted(ExportResult("success", rejected_events=2))
    fan = FanOutExporter(a, b, c)
    first = fan.export(batch, 1)
    assert first.outcome == "retry" and first.retry_after_s == 3.0
    second = fan.export(batch, 1)
    assert second.outcome == "failure" and "Scripted[1]: HTTP 400" in second.message
    assert len(c.calls) == 1  # finished on the first attempt, never called again
    both = FanOutExporter(Scripted(ExportResult("success", rejected_events=2)), Scripted(SUCCESS))
    assert both.export(batch, 1) == ExportResult("success", rejected_events=2)  # the largest count


def test_fan_out_treats_exceptions_as_retry_and_shuts_everything_down() -> None:
    a, b = Scripted(ValueError("bad")), Scripted()
    fan = FanOutExporter(a, b)
    result = fan.export(_batch([]), 1)
    assert result.outcome == "retry" and "ValueError" in result.message
    with pytest.raises(RuntimeError):
        fan.shutdown()
    assert a.shut_down and b.shut_down
    with pytest.raises(ValueError):
        FanOutExporter()


def test_new_exporters_satisfy_the_protocol(otlp: Collector) -> None:
    from aobs.export import Exporter

    assert isinstance(OtlpHttpExporter(otlp.url), Exporter)
    assert isinstance(FanOutExporter(InMemoryExporter()), Exporter)


def test_event_model_example_converts_and_conforms(otlp: Collector) -> None:
    doc = (Path(__file__).resolve().parents[3] / "docs" / "event-model.md").read_text()
    blocks = re.findall(r"```json\n(.*?)```", doc, re.DOTALL)
    events = json.loads(next(b for b in reversed(blocks) if b.lstrip().startswith("[")))
    exporter = OtlpHttpExporter(otlp.url)
    assert exporter.export(_batch(events), 5) == SUCCESS
    to_proto(otlp.payloads()[0])
    spans = otlp.spans()
    assert sorted(s["name"] for s in spans) == ["get_order_status", "issue_refund"]  # the start is skipped
    refund = by_name(spans, "issue_refund")
    assert refund["parentSpanId"] == "5fb397be34d26b51"
    assert refund["status"] == {"code": 2, "message": "refund API did not respond within 500 ms"}
    assert attrs(refund)["aobs.error.retryable"] is True
    assert int(refund["endTimeUnixNano"]) - int(refund["startTimeUnixNano"]) == 500_000_000
    assert exporter.diagnostics()["starts_skipped"] == 1
