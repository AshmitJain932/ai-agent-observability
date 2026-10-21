"""Per-project settings, stored as JSONB in ``projects.settings``.

A missing key always means the default below, so rows written by older servers stay valid and
unknown keys (from newer servers) are ignored.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

CaptureCeiling = Literal["metadata_only", "redacted", "full"]
CAPTURE_LEVELS: dict[str, int] = {"metadata_only": 0, "redacted": 1, "full": 2}


class ProjectSettings(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    # Privacy ceiling over whatever SDKs send (architecture §9, ADR-003): content above it is
    # stored as not_captured; error messages and stack traces follow their own flags.
    capture_ceiling: CaptureCeiling = "metadata_only"
    error_messages: bool = True
    stack_traces: bool = False
    retention_days: int = Field(default=30, ge=1, le=3650)
    max_spans_per_trace: int = Field(default=10_000, ge=1, le=1_000_000)
    max_span_events_per_trace: int = Field(default=100_000, ge=1, le=10_000_000)
    stale_after_s: float = Field(default=900.0, gt=0, le=2_592_000)

    @property
    def stale_after(self) -> timedelta:
        return timedelta(seconds=self.stale_after_s)

    @classmethod
    def from_db(cls, raw: Any) -> ProjectSettings:
        return cls.model_validate(raw if isinstance(raw, dict) else {})

    def to_db(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
