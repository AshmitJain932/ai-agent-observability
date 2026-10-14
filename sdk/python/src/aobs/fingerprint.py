"""Keyed input fingerprints (ADR-011).

``input_fingerprint = "hmac-sha256:<key_id>:<hex>"`` over the canonical JSON of an input. Identical
inputs produce identical fingerprints *under the same key*, which is enough to detect repeated tool
or model calls without storing the inputs. Because the hash is keyed, low-entropy inputs (an order
id, a short prompt) cannot be recovered by guessing.

The key never leaves the process: it is not part of any event, it is hidden from ``repr``, and the
object refuses to be pickled.

* Default: a random 32-byte key per process with a random key id (``p`` + 8 hex). Fingerprints
  are comparable within one process (and its forked children), which covers repeats inside a run.
* Configured: ``fingerprint_key`` (≥ 32 bytes) + ``fingerprint_key_id`` — or the environment
  variables ``AOBS_FINGERPRINT_KEY`` (hex, ≥ 64 hex chars) and ``AOBS_FINGERPRINT_KEY_ID`` — make
  fingerprints comparable across processes and runs. Rotate by changing both.

The canonical form must be **stable**: equal inputs must give equal bytes, in every process.
:func:`canonical` therefore differs from content capture (``privacy.jsonable``) in two ways:

* a value whose only representation contains a memory address (``<Order object at 0x7f...>``,
  functions, locks, ...) has no stable form and raises :class:`Unfingerprintable`: the span then
  gets no fingerprint (unknown) instead of one that is different for equal inputs, or equal for
  different inputs once the address is reused;
* sets are sorted by the canonical JSON of their elements, so their order does not depend on the
  process's hash seed.

Every other value has exactly the canonical form it had before, so existing fingerprints under a
configured key stay comparable.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
from typing import Any, NoReturn

from . import _sanitize as S
from .privacy import MAX_CONTENT_DEPTH

MIN_KEY_BYTES = 32
_ADDRESS = re.compile(r"\bat 0x[0-9a-fA-F]+")


class Unfingerprintable(ValueError):
    """The input has no stable representation, so no fingerprint is recorded for it."""


def _text(value: Any) -> str:
    """The string form of a value that is not JSON data; refused if it embeds a memory address."""
    try:
        text = S.clean_text(str(value))
    except Exception:
        raise Unfingerprintable(f"{type(value).__name__} cannot be converted to text") from None
    if _ADDRESS.search(text):
        raise Unfingerprintable(f"{type(value).__name__} has no stable representation (memory address)")
    return text


def _sort_key(item: Any) -> str:
    return json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical(value: Any, depth: int = 0) -> Any:
    """The stable, JSON-compatible form of an input (see the module docstring)."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return S.clean_text(value)
    if depth >= MAX_CONTENT_DEPTH:
        raise Unfingerprintable(f"nested deeper than {MAX_CONTENT_DEPTH} levels")
    if isinstance(value, dict):
        return {S.clean_text(k) if isinstance(k, str) else _text(k): canonical(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [canonical(v, depth + 1) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((canonical(v, depth + 1) for v in value), key=_sort_key)
    dump = getattr(value, "model_dump", None)  # pydantic v2 objects (e.g. provider SDK responses)
    if callable(dump):
        try:
            dumped = dump(mode="json", exclude_none=True)
        except Exception:
            pass
        else:
            return canonical(dumped, depth + 1)
    return _text(value)


class Fingerprinter:
    __slots__ = ("__key", "key_id")

    def __init__(self, key: bytes, key_id: str) -> None:
        if not isinstance(key, (bytes, bytearray)) or len(key) < MIN_KEY_BYTES:
            raise ValueError(f"fingerprint key must be at least {MIN_KEY_BYTES} bytes")
        if not S.valid_key_id(key_id):
            raise ValueError("fingerprint key id must match ^[a-z0-9_-]{1,32}$")
        self.__key = bytes(key)
        self.key_id = key_id

    @classmethod
    def random(cls) -> Fingerprinter:
        return cls(os.urandom(MIN_KEY_BYTES), "p" + secrets.token_hex(4))

    def fingerprint(self, value: Any) -> str:
        """Raises :class:`Unfingerprintable` for an input without a stable representation."""
        text = json.dumps(canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hmac.new(self.__key, text.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"hmac-sha256:{self.key_id}:{digest}"

    def __repr__(self) -> str:
        return f"Fingerprinter(key_id={self.key_id!r}, key=<hidden>)"

    def __reduce__(self) -> NoReturn:
        raise TypeError("Fingerprinter holds a secret key and cannot be pickled")
