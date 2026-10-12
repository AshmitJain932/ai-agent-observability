"""Strict JSON parsing for untrusted ingest payloads.

Python's ``json`` module is more permissive than RFC 8259 in ways that matter for telemetry:

* it accepts ``NaN``, ``Infinity`` and ``-Infinity`` literals,
* it turns over-large numbers such as ``1e400`` into ``inf``,
* it silently keeps the *last* of duplicate object keys (other parsers keep the first, so a
  validator and a consumer could see different values),
* it accepts lone UTF-16 surrogates (``"\\ud800"``), which cannot be encoded as UTF-8 and would
  fail later at the database,
* it recurses without a depth bound.

``loads_strict`` rejects all of these with :class:`StrictJSONError`.
"""

from __future__ import annotations

import json
import math
from typing import Any

MAX_DEPTH = 64


class StrictJSONError(ValueError):
    """The payload is not acceptable JSON."""


def _reject_constant(name: str) -> Any:
    raise StrictJSONError(f"non-finite number literal {name!r} is not valid JSON")


def _parse_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise StrictJSONError(f"number {text!r} is out of range")
    return value


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StrictJSONError(f"duplicate object key {key!r}")
        result[key] = value
    return result


def _check_strings_and_depth(value: Any, depth: int = 0) -> None:
    # Recursive walk; recursion depth is bounded by MAX_DEPTH.
    if depth > MAX_DEPTH:
        raise StrictJSONError(f"nesting deeper than {MAX_DEPTH} levels")
    if isinstance(value, str):
        _check_text(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_text(key)
            _check_strings_and_depth(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _check_strings_and_depth(item, depth + 1)


def _check_text(text: str) -> None:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise StrictJSONError("string contains an unpaired UTF-16 surrogate") from exc


def loads_strict(data: bytes | str) -> Any:
    """Parse JSON text, rejecting the permissive cases listed in the module docstring."""
    if isinstance(data, bytes):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StrictJSONError("payload is not valid UTF-8") from exc
    else:
        text = data
    try:
        value = json.loads(
            text,
            parse_constant=_reject_constant,
            parse_float=_parse_float,
            object_pairs_hook=_no_duplicate_keys,
        )
    except StrictJSONError:
        raise
    except RecursionError as exc:
        raise StrictJSONError("nesting too deep") from exc
    except ValueError as exc:  # json.JSONDecodeError is a ValueError
        raise StrictJSONError(f"malformed JSON: {exc}") from exc
    _check_strings_and_depth(value)
    return value
