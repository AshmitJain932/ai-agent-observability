"""Turn arbitrary user-supplied values into values that satisfy wire schema v1.0.

The SDK must not raise into the application and must not emit events the collector will reject,
so every value crossing the API boundary goes through one of these functions. Changes are made
visible through diagnostics counters, never silently.

Limits mirror ``schema/v1/ingest.schema.json``; ``test_sdk_conformance.py`` checks the SDK's
output against that schema.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any

from ._diagnostics import Diagnostics

MAX_LABEL = 256
MAX_SHORT_LABEL = 128
MAX_VOCAB = 64
MAX_ATTRIBUTES = 128
MAX_ATTRIBUTE_STRING = 4096
MAX_ATTRIBUTE_ARRAY = 128
MAX_SAFE_INTEGER = 9_007_199_254_740_991
MAX_DURATION_MS = 2_592_000_000.0
MAX_ERROR_MESSAGE = 8192
MAX_STACK = 32768

_VOCAB_RE = re.compile(r"^[a-z][a-z0-9_-]*(\.[a-z0-9_-]+)*\Z")
_VOCAB_BAD_CHARS = re.compile(r"[^a-z0-9_-]")
_ATTR_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}\Z")
_ENVIRONMENT_BAD = re.compile(r"[^a-z0-9_.-]")
_AMOUNT_RE = re.compile(r"^(0|[1-9][0-9]{0,14})(\.[0-9]{1,12})?\Z")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}\Z")
_KEY_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}\Z")


def clean_text(value: str) -> str:
    """Replace unpaired surrogates (not encodable as UTF-8, rejected by the collector)."""
    try:
        value.encode("utf-8")
        return value
    except UnicodeEncodeError:
        return value.encode("utf-8", "replace").decode("utf-8")


def label(value: object, max_len: int = MAX_LABEL, fallback: str = "unnamed") -> str:
    text = clean_text(value if isinstance(value, str) else str(value))
    if not text:
        return fallback
    return text[:max_len]


def vocab(value: object, fallback: str = "unknown") -> str:
    """Normalize to an open-vocabulary identifier: lowercase, dotted, starts with a letter.

    ``"Azure OpenAI"`` -> ``"azure_openai"``; ``"Planner Step"`` -> ``"planner_step"``.
    """
    text = (value if isinstance(value, str) else str(value)).strip().lower()
    if _VOCAB_RE.match(text) and len(text) <= MAX_VOCAB:
        return text
    segments = [_VOCAB_BAD_CHARS.sub("_", seg) for seg in text.split(".")]
    segments = [seg for seg in segments if seg]
    result = ".".join(segments)
    if result and not result[0].isalpha():
        result = "x" + result
    result = result[:MAX_VOCAB].rstrip(".")
    return result if _VOCAB_RE.match(result) else fallback


def environment(value: str) -> str:
    text = _ENVIRONMENT_BAD.sub("_", value.strip().lower())
    if not text or not text[0].isalnum():
        text = "env" + text
    return text[:64]


def valid_key_id(value: str) -> bool:
    return bool(_KEY_ID_RE.match(value))


def token_count(value: object) -> int | None:
    """A provider token count, or None if it is not a valid non-negative integer."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > MAX_SAFE_INTEGER:
        return None
    return value


def duration_ms(elapsed_ns: int) -> float:
    return min(max(elapsed_ns, 0) / 1_000_000, MAX_DURATION_MS)


def money(amount: object) -> str | None:
    """Plain decimal string (no exponent, ≤ 12 fractional digits) or None if invalid/negative."""
    if isinstance(amount, bool):
        return None
    try:
        dec = amount if isinstance(amount, Decimal) else Decimal(str(amount))
        if not dec.is_finite() or dec < 0:
            return None
        if dec.as_tuple().exponent < -12:  # type: ignore[operator]
            dec = dec.quantize(Decimal("1e-12"))  # half-even; documented
        text = format(dec, "f")
    except (InvalidOperation, ValueError, TypeError):
        return None
    if "." in text:
        text = text.rstrip("0").rstrip(".") or "0"
    return text if _AMOUNT_RE.match(text) else None


def currency(value: object) -> str | None:
    text = str(value).upper()
    return text if _CURRENCY_RE.match(text) else None


# --------------------------------------------------------------------------------------------
# Secret scrubbing — always applied to error messages, stack traces and string attributes.

# Private keys and JWTs are found without a regex over the whole secret: as regexes
# (``-----BEGIN …-----[\s\S]*?-----END …-----`` and ``\beyJ…{5,}\.eyJ…{5,}\.…{5,}``) they try
# every BEGIN marker, or every ``eyJ`` inside a long token, and scan to the end of the text from
# each one: quadratic (32 KiB took 0.3 s, 1 MiB minutes). Both scanners below look at every
# character a bounded number of times and find exactly what those regexes found, except that a
# private key with no END marker is removed up to the end of the text instead of left in place.
_PRIVATE_KEY_BEGIN = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_PRIVATE_KEY_END = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")
# A run of token characters with at least two dots in it; the lookbehind starts it only at the
# beginning of a run, so a long run without dots is scanned once, not once per character.
_DOTTED_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]++(?:\.[A-Za-z0-9_-]++){2,}+")


def _scrub_private_keys(text: str, replacement: str) -> tuple[str, int]:
    begin = _PRIVATE_KEY_BEGIN.search(text)
    if begin is None:
        return text, 0
    parts: list[str] = []
    count = 0
    done = 0
    while begin is not None:
        parts.append(text[done : begin.start()])
        parts.append(replacement)
        count += 1
        end = _PRIVATE_KEY_END.search(text, begin.end())
        if end is None:  # cut off, or malformed: fail closed
            done = len(text)
            break
        done = end.end()
        begin = _PRIVATE_KEY_BEGIN.search(text, done)
    parts.append(text[done:])
    return "".join(parts), count


def _jwt_start(segment: str, at_boundary: bool) -> int:
    """Where a JWT's first part starts in ``segment``: the first ``eyJ`` at a word boundary with at
    least five token characters after it, or -1. ``at_boundary``: whether the segment's start is
    one (inside the segment only ``-`` ends a word)."""
    at = segment.find("eyJ")
    while at != -1 and at + 8 <= len(segment):
        if at_boundary if at == 0 else segment[at - 1] == "-":
            return at
        at = segment.find("eyJ", at + 1)
    return -1


def _scrub_jwts(text: str, replacement: str) -> tuple[str, int]:
    if "eyJ" not in text:
        return text, 0
    count = 0

    def scrub_token(match: re.Match[str]) -> str:
        nonlocal count
        token = match.group()
        segments = token.split(".")
        before = text[match.start() - 1] if match.start() else ""
        at_boundary = not (before.isalnum() or before == "_")  # what \b sees before the token
        parts: list[str] = []
        done = 0  # end, in ``token``, of the previous replacement
        start = 0  # start, in ``token``, of segments[i]
        i = 0
        while i + 2 < len(segments):
            at = _jwt_start(segments[i], at_boundary or i > 0)
            payload, signature = segments[i + 1], segments[i + 2]
            if at != -1 and payload.startswith("eyJ") and len(payload) >= 8 and len(signature) >= 5:
                parts.append(token[done : start + at])
                parts.append(replacement)
                count += 1
                done = start + len(segments[i]) + len(payload) + len(signature) + 2
                start = done + 1
                i += 3
            else:
                start += len(segments[i]) + 1
                i += 1
        parts.append(token[done:])
        return "".join(parts)

    return _DOTTED_TOKEN.sub(scrub_token, text), count


_SECRET_PATTERNS: list[tuple[str, re.Pattern[str] | Callable[[str, str], tuple[str, int]]]] = [
    ("private_key", _scrub_private_keys),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")),
    ("api_key", re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("jwt", _scrub_jwts),
    ("url_credentials", re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)")),
]
_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|password|passwd|authorization)\b(\s*[:=]\s*)(['\"]?)([^\s'\",;]{6,})"
)


def scrub_secrets(text: str) -> tuple[str, int]:
    """Replace credential-shaped substrings with ``[REDACTED:<kind>]``. Returns (text, count)."""
    total = 0
    for name, pattern in _SECRET_PATTERNS:
        replacement = f"[REDACTED:{name}]"
        text, n = pattern.subn(replacement, text) if isinstance(pattern, re.Pattern) else pattern(text, replacement)
        total += n

    def _assign(m: re.Match[str]) -> str:
        if m.group(4).startswith("[REDACTED"):
            return m.group(0)
        return f"{m.group(1)}{m.group(2)}{m.group(3)}[REDACTED:credential]"

    text, n = _ASSIGNMENT.subn(_assign, text)
    return text, total + n


# --------------------------------------------------------------------------------------------
# Bounded redaction — text longer than twice its output limit is cut *before* redaction, so the
# time redaction takes on the application's thread does not grow with what the application passed.

_CUT_MARGIN = 64  # longer than any match that can contain whitespace, except private-key blocks
_ASCII_WHITESPACE = " \t\n\r\x0b\x0c"
# A cut through a keyword, or just after it, leaves the keyword's value behind unrecognized, and
# ``Bearer`` and ``password=``-style values may sit any amount of whitespace after their keyword:
# the rest of the cut token, then the separator, then the value that may follow it.
_CUT_KEYWORD_VALUE = re.compile(r"\S*[\s:='\"]*\S*")


def drop_cut_end(redacted: str) -> str:
    """``redacted`` is the redaction of text that was cut at its end. A secret or address that the
    cut split was not recognized, so its first part is still there, verbatim, at the end. Drop it:
    a private key whose END marker was cut off from its BEGIN marker, then the last ``_CUT_MARGIN``
    characters (card numbers contain spaces), then the rest of the whitespace-free token the cut
    went through (addresses, tokens and keys contain no whitespace, however long they are)."""
    key = _PRIVATE_KEY_BEGIN.search(redacted)
    if key:
        redacted = redacted[: key.start()]
    redacted = redacted[: max(len(redacted) - _CUT_MARGIN, 0)]
    boundary = max(redacted.rfind(c) for c in _ASCII_WHITESPACE)
    return redacted[:boundary].rstrip() if boundary >= 0 else ""


def drop_cut_start(redacted: str) -> str:
    """As :func:`drop_cut_end`, for text that was cut at its start (stack traces keep their end).
    A keyword can also lose its value here: the cut may leave ``Bearer``'s or ``password=``'s value
    behind, after any amount of whitespace, so the first value after the cut token goes too."""
    key = _PRIVATE_KEY_END.search(redacted)
    if key:
        redacted = redacted[key.end() :]
    value_end = _CUT_KEYWORD_VALUE.match(redacted).end()  # type: ignore[union-attr]  # "*" always matches
    redacted = redacted[max(_CUT_MARGIN, value_end) :]
    starts = [i for i in (redacted.find(c) for c in _ASCII_WHITESPACE) if i >= 0]
    return redacted[min(starts) :].lstrip() if starts else ""


def redact_head(text: str, max_chars: int, redact: Callable[[str], str]) -> str:
    """``redact(text)``'s first ``max_chars`` characters, redacting at most ``2 * max_chars``."""
    if len(text) <= 2 * max_chars:
        return redact(text)[:max_chars]
    return drop_cut_end(redact(text[: 2 * max_chars]))[:max_chars]


def redact_tail(text: str, max_chars: int, redact: Callable[[str], str]) -> str:
    """``redact(text)``'s last ``max_chars`` characters, redacting at most ``2 * max_chars``."""
    if len(text) <= 2 * max_chars:
        return redact(text)[-max_chars:]
    return drop_cut_start(redact(text[-2 * max_chars :]))[-max_chars:]


def _scrub_attribute(text: str) -> str:
    return scrub_secrets(clean_text(text))[0]


# --------------------------------------------------------------------------------------------
# Attributes


def _attr_scalar(value: object) -> tuple[bool, object]:
    if isinstance(value, bool):
        return True, value
    if isinstance(value, int):
        return True, value
    if isinstance(value, float):
        return (True, value) if math.isfinite(value) else (False, None)
    if isinstance(value, str):
        return True, redact_head(value, MAX_ATTRIBUTE_STRING, _scrub_attribute)
    return False, None


def attributes(values: dict[str, Any] | None, diag: Diagnostics, existing: int = 0) -> dict[str, Any]:
    """Keep valid attributes (scrubbed, truncated); drop and count the rest."""
    out: dict[str, Any] = {}
    if not values:
        return out
    dropped = 0
    for key, value in values.items():
        if not isinstance(key, str) or not _ATTR_KEY_RE.match(key) or existing + len(out) >= MAX_ATTRIBUTES:
            dropped += 1
            continue
        if isinstance(value, (list, tuple)):
            items = []
            ok = len(value) <= MAX_ATTRIBUTE_ARRAY
            for item in value if ok else ():
                good, clean = _attr_scalar(item)
                if not good:
                    ok = False
                    break
                items.append(clean)
            if not ok:
                dropped += 1
                continue
            out[key] = items
            continue
        good, clean = _attr_scalar(value)
        if good:
            out[key] = clean
        else:
            dropped += 1
    if dropped:
        diag.inc("attributes_dropped", dropped)
    return out
