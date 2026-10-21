"""Content capture policy and redaction.

Modes (``aobs.init(capture=...)``):

* ``metadata_only`` (default) — no prompt, response, argument or result content leaves the
  process. Content slots are reported as ``{"status": "not_captured"}``.
* ``redacted`` — content is captured after every redactor runs over every string in it. The
  status is ``redacted`` (meaning "the redaction policy was applied"), with ``redaction_count``
  when something was replaced.
* ``full`` — content is captured as is (size-capped). Secret scrubbing is *not* applied to full
  content; choose ``redacted`` if content may contain credentials.

Independently of the mode, credentials, email addresses and card numbers are always removed from
error messages and stack traces (``scrub_error_text``), which can be turned off
(``capture_error_messages=False``) or on (``capture_stack_traces=True``).
"""

from __future__ import annotations

import json
import math
import re
import string
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


_EMAIL_LOCAL_CHARS = string.ascii_letters + string.digits + "._%+-"
_EMAIL_DOMAIN_RUN = re.compile(r"[A-Za-z0-9.-]*")
_ASCII_LETTERS = frozenset(string.ascii_letters)
_ASCII_LETTER_RUN = re.compile(r"[A-Za-z]*")


def _email_end(text: str, at: int) -> int | None:
    """Where an address whose ``@`` is at ``at`` ends, or None if the domain is not valid. The domain
    ends after the rightmost ``.`` that has a domain character before it and two letters after it
    (as the greedy regex ``[A-Za-z0-9.-]+\\.[A-Za-z]{2,}`` decides)."""
    run_end = _EMAIL_DOMAIN_RUN.match(text, at + 1).end()  # type: ignore[union-attr]  # "*" always matches
    dot = text.rfind(".", at + 2, run_end)
    while dot != -1:
        if dot + 2 < len(text) and text[dot + 1] in _ASCII_LETTERS and text[dot + 2] in _ASCII_LETTERS:
            return _ASCII_LETTER_RUN.match(text, dot + 1).end()  # type: ignore[union-attr]
        dot = text.rfind(".", at + 2, dot)
    return None


@dataclass(frozen=True)
class EmailRedactor:
    """Replace e-mail addresses with ``[REDACTED:email]``, in time linear in the text's length.

    Finds exactly what ``re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}", ...)`` finds, but
    that regex tries every start inside a long run of address characters and scans to the run's
    end each time: quadratic (a 64 KiB token took over 5 s). Here candidates are found from their
    ``@``, and every character is looked at a bounded number of times.
    """

    name: str = "email"

    def __call__(self, text: str) -> tuple[str, int]:
        at = text.find("@")
        if at == -1:
            return text, 0
        parts: list[str] = []
        count = 0
        done = 0  # end of the previous replacement: the regex would resume its search here
        previous_at = -1
        while at != -1:
            floor = max(done, previous_at + 1)  # a local part never spans an "@" or a replacement
            start = floor + len(text[floor:at].rstrip(_EMAIL_LOCAL_CHARS))
            end = _email_end(text, at) if start < at else None
            previous_at = at
            if end is None:
                at = text.find("@", at + 1)
                continue
            parts.append(text[done:start])
            parts.append(f"[REDACTED:{self.name}]")
            count += 1
            done = end
            at = text.find("@", end)
        parts.append(text[done:])
        return "".join(parts), count


EMAIL = EmailRedactor()
CARD_NUMBER = RegexRedactor("card_number", re.compile(r"\b(?:\d[ -]?){12,18}\d\b"), validate=_luhn)

#: Applied in ``redacted`` mode unless ``use_default_redactors=False``.
DEFAULT_REDACTORS: tuple[Redactor, ...] = (secrets_redactor, EMAIL, CARD_NUMBER)

#: Always applied to error messages and stack traces, in every capture mode. They are not content,
#: so neither the capture mode nor ``use_default_redactors`` turns this off.
ERROR_TEXT_REDACTORS: tuple[Redactor, ...] = (secrets_redactor, EMAIL, CARD_NUMBER)


def scrub_error_text(text: str) -> str:
    """Remove credentials, email addresses and card numbers (Luhn-checked) from error text."""
    text = S.clean_text(text)
    for redactor in ERROR_TEXT_REDACTORS:
        text, _ = redactor(text)
    return text


# Error text can be arbitrarily long (an exception carrying a whole response body), and redaction
# costs time on the application's thread: text longer than twice its output limit is cut *before*
# redaction, and what the cut split is dropped (``_sanitize.redact_head`` / ``redact_tail``).
MESSAGE_REDACTION_INPUT = 2 * S.MAX_ERROR_MESSAGE
STACK_REDACTION_INPUT = 2 * S.MAX_STACK


def redact_error_message(text: str) -> str:
    """The redacted error message, at most ``MAX_ERROR_MESSAGE`` characters from its start."""
    return S.redact_head(text, S.MAX_ERROR_MESSAGE, scrub_error_text)


def redact_stack(text: str) -> str:
    """The redacted stack trace, at most ``MAX_STACK`` characters from its end (the frames closest
    to the failure)."""
    return S.redact_tail(text, S.MAX_STACK, scrub_error_text)


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
        return redact_error_message(text) or None

    def stack(self, exc: BaseException) -> str | None:
        if not self.stack_traces:
            return None
        try:
            text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        except Exception:
            return None
        return redact_stack(text)


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
