"""Content capture policy and redaction.

Modes (``aobs.init(capture=...)``):

* ``metadata_only`` (default) — no prompt, response, argument or result content leaves the
  process. Content slots are reported as ``{"status": "not_captured"}``.
* ``redacted`` — content is captured after every redactor runs over every string in it. The
  status is ``redacted`` (meaning "the redaction policy was applied"), with ``redaction_count``
  when something was replaced.
* ``full`` — content is captured as is (size-capped). Secret scrubbing is *not* applied to full
  content; choose ``redacted`` if content may contain credentials.

Independently of the mode, error messages and stack traces are always secret-scrubbed, and can
be turned off (``capture_error_messages=False``) or on (``capture_stack_traces=True``).
"""

from __future__ import annotations

import json
import math
import re
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from . import _sanitize as S

CaptureMode = Literal["metadata_only", "redacted", "full"]
ContentFormat = Literal["text", "messages", "json"]
CAPTURE_MODES: tuple[CaptureMode, ...] = ("metadata_only", "redacted", "full")

# A redactor takes text and returns (new_text, number_of_replacements).
Redactor = Callable[[str], tuple[str, int]]

MAX_CONTENT_DEPTH = 32


@dataclass(frozen=True)
class RegexRedactor:
    """Replace every match of ``pattern`` with ``[REDACTED:<name>]``.

    ``validate`` can reject matches (e.g. a Luhn check for card numbers) to cut false positives.
    """

    name: str
    pattern: re.Pattern[str]
    validate: Callable[[str], bool] | None = None

    def __call__(self, text: str) -> tuple[str, int]:
        count = 0

        def repl(m: re.Match[str]) -> str:
            nonlocal count
            if self.validate is not None and not self.validate(m.group(0)):
                return m.group(0)
            count += 1
            return f"[REDACTED:{self.name}]"

        return self.pattern.sub(repl, text), count


def _luhn(candidate: str) -> bool:
    digits = [int(c) for c in candidate if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def secrets_redactor(text: str) -> tuple[str, int]:
    return S.scrub_secrets(text)


EMAIL = RegexRedactor("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"))
CARD_NUMBER = RegexRedactor("card_number", re.compile(r"\b(?:\d[ -]?){12,18}\d\b"), validate=_luhn)

#: Applied in ``redacted`` mode unless ``use_default_redactors=False``.
DEFAULT_REDACTORS: tuple[Redactor, ...] = (secrets_redactor, EMAIL, CARD_NUMBER)


@dataclass(frozen=True)
class CapturePolicy:
    mode: CaptureMode = "metadata_only"
    error_messages: bool = True
    stack_traces: bool = False
    max_content_bytes: int = 32768
    redactors: Sequence[Redactor] = field(default_factory=lambda: DEFAULT_REDACTORS)

    @property
    def captures_content(self) -> bool:
        return self.mode != "metadata_only"

    # ---------------------------------------------------------------- content

    def content_part(self, value: Any, fmt: ContentFormat | None) -> tuple[dict[str, Any], bool]:
        """Build a schema ``contentPart``. Returns (part, truncated)."""
        if not self.captures_content:
            return {"status": "not_captured"}, False
        fmt = fmt or ("text" if isinstance(value, str) else "json")
        data = jsonable(value)
        count = 0
        if self.mode == "redacted":
            data, count = _redact(data, self.redactors)
        data, fmt, truncated = _fit(data, fmt, self.max_content_bytes)
        part: dict[str, Any] = {
            "status": "redacted" if self.mode == "redacted" else "captured",
            "format": fmt,
            "value": data,
        }
        if truncated:
            part["truncated"] = True
        if count:
            part["redaction_count"] = min(count, 1_000_000)
        return part, truncated

    # ---------------------------------------------------------------- errors

    def error_message(self, exc: BaseException) -> str | None:
        if not self.error_messages:
            return None
        try:
            text = str(exc)
        except Exception:
            return None
        text, _ = S.scrub_secrets(S.clean_text(text))
        return text[: S.MAX_ERROR_MESSAGE] or None

    def stack(self, exc: BaseException) -> str | None:
        if not self.stack_traces:
            return None
        try:
            text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        except Exception:
            return None
        text, _ = S.scrub_secrets(S.clean_text(text))
        return text[-S.MAX_STACK :]  # keep the end: the frames closest to the failure


def jsonable(value: Any, depth: int = 0) -> Any:
    """Convert to plain JSON-compatible data (a copy, so later mutation by the caller is not seen)."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return S.clean_text(value)
    if depth >= MAX_CONTENT_DEPTH:
        return "[aobs: nesting too deep]"
    if isinstance(value, dict):
        return {S.clean_text(str(k)): jsonable(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(v, depth + 1) for v in value]
    dump = getattr(value, "model_dump", None)  # pydantic v2 objects (e.g. provider SDK responses)
    if callable(dump):
        try:
            return jsonable(dump(mode="json", exclude_none=True), depth + 1)
        except Exception:
            pass
    return S.clean_text(str(value))


def _redact(data: Any, redactors: Sequence[Redactor]) -> tuple[Any, int]:
    if isinstance(data, str):
        total = 0
        for redactor in redactors:
            data, n = redactor(data)
            total += n
        return data, total
    if isinstance(data, dict):
        total = 0
        out = {}
        for k, v in data.items():
            out[k], n = _redact(v, redactors)
            total += n
        return out, total
    if isinstance(data, list):
        total = 0
        items = []
        for v in data:
            item, n = _redact(v, redactors)
            items.append(item)
            total += n
        return items, total
    return data, 0


def _fit(data: Any, fmt: ContentFormat, max_bytes: int) -> tuple[Any, ContentFormat, bool]:
    """Enforce the per-part size cap. Structured content that does not fit becomes truncated
    JSON *text* (format ``text``) rather than an invalid partial structure."""
    if isinstance(data, str):
        encoded = data.encode("utf-8")
        if len(encoded) <= max_bytes:
            return data, fmt, False
        return encoded[:max_bytes].decode("utf-8", "ignore"), "text" if fmt != "text" else fmt, True
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return data, fmt, False
    return encoded[:max_bytes].decode("utf-8", "ignore"), "text", True
