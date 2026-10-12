"""Identifier generation (W3C/OTel-compatible trace and span ids; UUIDv7 event ids)."""

from __future__ import annotations

import os
import secrets
import time

_ZERO_TRACE = "0" * 32
_ZERO_SPAN = "0" * 16


def new_trace_id() -> str:
    """32 lowercase hex chars from the OS CSPRNG, never all zero."""
    while True:
        value = secrets.token_hex(16)
        if value != _ZERO_TRACE:
            return value


def new_span_id() -> str:
    """16 lowercase hex chars from the OS CSPRNG, never all zero."""
    while True:
        value = secrets.token_hex(8)
        if value != _ZERO_SPAN:
            return value


def uuid7() -> str:
    """RFC 9562 UUIDv7: 48-bit Unix milliseconds, version 7, variant 10, 74 random bits.

    (``uuid.uuid7`` only exists from Python 3.14.)
    """
    ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    rand_a = (rand >> 62) & 0xFFF
    rand_b = rand & ((1 << 62) - 1)
    value = (ms & ((1 << 48) - 1)) << 80 | 0x7 << 76 | rand_a << 64 | 0b10 << 62 | rand_b
    h = f"{value:032x}"
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
