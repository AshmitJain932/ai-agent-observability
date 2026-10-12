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

_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")),
    ("api_key", re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}")),
    ("url_credentials", re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)")),
]
_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|password|passwd|authorization)\b(\s*[:=]\s*)(['\"]?)([^\s'\",;]{6,})"
)


def scrub_secrets(text: str) -> tuple[str, int]:
    """Replace credential-shaped substrings with ``[REDACTED:<kind>]``. Returns (text, count)."""
    total = 0
    for name, pattern in _SECRET_PATTERNS:
        text, n = pattern.subn(f"[REDACTED:{name}]", text)
        total += n

    def _assign(m: re.Match[str]) -> str:
        if m.group(4).startswith("[REDACTED"):
            return m.group(0)
        return f"{m.group(1)}{m.group(2)}{m.group(3)}[REDACTED:credential]"

    text, n = _ASSIGNMENT.subn(_assign, text)
    return text, total + n


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
        text, _ = scrub_secrets(clean_text(value))
        return True, text[:MAX_ATTRIBUTE_STRING]
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
