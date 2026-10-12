"""HTTP exporter for ``POST {endpoint}/v1/ingest`` (standard library ``urllib``).

Status handling:

* 2xx -> success. If the body is a JSON object with a ``rejected`` list (the per-event result the
  Phase 3 collector returns), its length is reported as ``rejected_events``.
* 408, 429, 5xx, network errors, timeouts -> retry (``Retry-After`` seconds honoured, capped by the
  processor's maximum backoff).
* Other 4xx -> permanent failure (bad key, malformed batch): retrying cannot help.

The API key is sent only in the ``Authorization`` header and never logged.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping

from .._version import __version__
from .base import EncodedBatch, ExportResult

RETRYABLE_STATUS = frozenset({408, 429}) | frozenset(range(500, 600))
MAX_RESPONSE_BYTES = 1_048_576


class HttpExporter:
    def __init__(
        self,
        endpoint: str,
        api_key: str | None = None,
        headers: Mapping[str, str] | None = None,
        path: str = "/v1/ingest",
    ) -> None:
        self.url = endpoint.rstrip("/") + path
        self._headers = {
            "Content-Type": "application/json",
            "User-Agent": f"aobs-python/{__version__}",
            **dict(headers or {}),
        }
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"
        # Do not follow redirects: a redirect could forward the Authorization header elsewhere.
        self._opener = urllib.request.build_opener(_NoRedirect)

    def __repr__(self) -> str:
        return f"HttpExporter(url={self.url!r})"

    def export(self, batch: EncodedBatch, timeout_s: float) -> ExportResult:
        request = urllib.request.Request(self.url, data=batch.body, headers=self._headers, method="POST")
        try:
            with self._opener.open(request, timeout=timeout_s) as response:
                body = response.read(MAX_RESPONSE_BYTES)
                return ExportResult("success", rejected_events=_rejected_count(body))
        except urllib.error.HTTPError as exc:
            status = exc.code
            retry_after = _retry_after(exc.headers.get("Retry-After") if exc.headers else None)
            exc.close()
            if status in RETRYABLE_STATUS:
                return ExportResult("retry", retry_after_s=retry_after, message=f"HTTP {status}")
            return ExportResult("failure", message=f"HTTP {status}")
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            return ExportResult("retry", message=f"{type(exc).__name__}: {reason}"[:200])

    def shutdown(self) -> None:
        pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None  # HTTP-date form not supported; fall back to exponential backoff
    return seconds if seconds >= 0 else None


def _rejected_count(body: bytes) -> int:
    if not body:
        return 0
    try:
        data = json.loads(body)
    except ValueError:
        return 0
    rejected = data.get("rejected") if isinstance(data, dict) else None
    return len(rejected) if isinstance(rejected, list) else 0
