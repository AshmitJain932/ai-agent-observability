"""Time sources.

A span records its wall-clock start once and measures duration with the monotonic clock. Its end
timestamp is ``start + measured duration``, so ``end_time - start_time`` always equals
``duration_ms`` even if the wall clock is adjusted while the span runs.
"""

from __future__ import annotations

import time

wall_ns = time.time_ns
mono_ns = time.perf_counter_ns


def format_ts(ns: int) -> str:
    """RFC 3339 UTC with microseconds and a 'Z' suffix (schema ``timestamp``)."""
    seconds, rem = divmod(ns, 1_000_000_000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds)) + f".{rem // 1000:06d}Z"
