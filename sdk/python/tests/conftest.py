from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest

import aobs
from aobs.export import InMemoryExporter

FAST = {"schedule_delay_s": 0.02, "retry_backoff_initial_s": 0.01, "retry_backoff_max_s": 0.05}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in (
        "AOBS_ENDPOINT",
        "AOBS_API_KEY",
        "AOBS_ENABLED",
        "AOBS_CAPTURE",
        "AOBS_ENVIRONMENT",
        "AOBS_SERVICE_NAME",
        "AOBS_FINGERPRINT_KEY",
        "AOBS_FINGERPRINT_KEY_ID",
        "AOBS_CAPTURE_ERROR_MESSAGES",
        "AOBS_CAPTURE_STACK_TRACES",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    aobs.shutdown(timeout_s=2)


@pytest.fixture
def make_sdk() -> Callable[..., InMemoryExporter]:
    """Initialize the SDK with an in-memory exporter; returns the exporter."""

    def make(**options: Any) -> InMemoryExporter:
        exporter = options.pop("exporter", None) or InMemoryExporter()
        defaults = {"service_name": "sdk-tests", "environment": "test", **FAST}
        aobs.init(exporter=exporter, **{**defaults, **options})
        return exporter

    return make


@pytest.fixture
def sdk(make_sdk: Callable[..., InMemoryExporter]) -> InMemoryExporter:
    return make_sdk()
