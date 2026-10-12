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
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from typing import Any, NoReturn

from . import _sanitize as S
from .privacy import jsonable

MIN_KEY_BYTES = 32


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
        canonical = json.dumps(jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hmac.new(self.__key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"hmac-sha256:{self.key_id}:{digest}"

    def __repr__(self) -> str:
        return f"Fingerprinter(key_id={self.key_id!r}, key=<hidden>)"

    def __reduce__(self) -> NoReturn:
        raise TypeError("Fingerprinter holds a secret key and cannot be pickled")
