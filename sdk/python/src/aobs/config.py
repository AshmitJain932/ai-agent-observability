"""SDK configuration: explicit arguments > environment variables > defaults.

Configuration problems raise :class:`ConfigError` only when ``strict=True``. Otherwise the SDK
falls back to a safe default, logs a warning and counts ``config_warnings`` — instrumentation must
never stop an application from starting.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from . import _sanitize as S
from ._diagnostics import Diagnostics, logger
from .fingerprint import MIN_KEY_BYTES, Fingerprinter
from .privacy import CAPTURE_MODES, DEFAULT_REDACTORS, CaptureMode, CapturePolicy, Redactor


class ConfigError(ValueError):
    pass


# Tuning knobs and their allowed ranges. max_batch_events is capped by the schema (≤ 1,000 events).
_TUNING_LIMITS: dict[str, tuple[Any, Any]] = {
    "max_queue_size": (100, 1_000_000),
    "max_batch_events": (1, 1000),
    "max_batch_bytes": (16_384, 1_048_576),
    "schedule_delay_s": (0.01, 60.0),
    "start_delay_s": (0.0, 60.0),
    "export_timeout_s": (0.1, 120.0),
    "max_export_attempts": (1, 10),
    "retry_backoff_initial_s": (0.001, 60.0),
    "retry_backoff_max_s": (0.001, 300.0),
    "shutdown_timeout_s": (0.0, 120.0),
}

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


@dataclass(frozen=True, repr=False)
class Config:
    service_name: str
    environment: str
    service_version: str | None
    endpoint: str | None
    api_key: str | None
    enabled: bool
    policy: CapturePolicy
    fingerprint_inputs: bool
    fingerprinter: Fingerprinter
    max_queue_size: int = 10_000
    max_batch_events: int = 500
    max_batch_bytes: int = 900_000
    schedule_delay_s: float = 1.0
    start_delay_s: float = 1.0
    export_timeout_s: float = 10.0
    max_export_attempts: int = 4
    retry_backoff_initial_s: float = 0.5
    retry_backoff_max_s: float = 30.0
    shutdown_timeout_s: float = 5.0
    headers: Mapping[str, str] = field(default_factory=dict)

    def __repr__(self) -> str:  # never print credentials or the fingerprint key
        return (
            f"Config(service_name={self.service_name!r}, environment={self.environment!r}, "
            f"endpoint={self.endpoint!r}, api_key={'<set>' if self.api_key else None}, "
            f"capture={self.policy.mode!r}, enabled={self.enabled}, fingerprinter={self.fingerprinter!r})"
        )


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else None


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    if value.lower() in _TRUE:
        return True
    if value.lower() in _FALSE:
        return False
    return default


def _problem(message: str, strict: bool, diag: Diagnostics) -> None:
    if strict:
        raise ConfigError(message)
    diag.inc("config_warnings")
    logger.warning("aobs configuration: %s", message)


def build_config(
    diag: Diagnostics,
    *,
    service_name: str | None = None,
    environment: str | None = None,
    service_version: str | None = None,
    endpoint: str | None = None,
    api_key: str | None = None,
    enabled: bool | None = None,
    capture: str | None = None,
    capture_error_messages: bool | None = None,
    capture_stack_traces: bool | None = None,
    max_content_bytes: int = 32768,
    redactors: Sequence[Redactor] | None = None,
    use_default_redactors: bool = True,
    fingerprint_inputs: bool = True,
    fingerprint_key: bytes | None = None,
    fingerprint_key_id: str | None = None,
    strict: bool = False,
    **tuning: Any,
) -> Config:
    unknown = set(tuning) - set(_TUNING_LIMITS) - {"headers"}
    if unknown:
        raise TypeError(f"unknown aobs.init() arguments: {sorted(unknown)}")
    for key, (low, high) in _TUNING_LIMITS.items():
        if key in tuning:
            value = tuning[key]
            clamped = type(low)(min(max(value, low), high))
            if clamped != value:
                _problem(f"{key}={value!r} outside [{low}, {high}]; using {clamped!r}", strict, diag)
            tuning[key] = clamped

    name = service_name or _env("AOBS_SERVICE_NAME") or os.path.basename(sys.argv[0] or "") or "unknown_service"
    env_raw = environment or _env("AOBS_ENVIRONMENT") or "development"
    env = S.environment(env_raw)
    if env != env_raw:
        _problem(f"environment {env_raw!r} normalized to {env!r}", strict, diag)

    mode_raw = capture or _env("AOBS_CAPTURE") or "metadata_only"
    if mode_raw not in CAPTURE_MODES:
        _problem(f"unknown capture mode {mode_raw!r}; using 'metadata_only'", strict, diag)
        mode_raw = "metadata_only"
    mode: CaptureMode = mode_raw  # type: ignore[assignment]

    chosen: list[Redactor] = list(DEFAULT_REDACTORS) if use_default_redactors else []
    chosen.extend(redactors or ())
    policy = CapturePolicy(
        mode=mode,
        error_messages=capture_error_messages
        if capture_error_messages is not None
        else _env_bool("AOBS_CAPTURE_ERROR_MESSAGES", True),
        stack_traces=capture_stack_traces
        if capture_stack_traces is not None
        else _env_bool("AOBS_CAPTURE_STACK_TRACES", False),
        max_content_bytes=max(256, min(int(max_content_bytes), 65_536)),
        redactors=tuple(chosen),
    )

    endpoint_value = endpoint or _env("AOBS_ENDPOINT")
    key_value = api_key or _env("AOBS_API_KEY")
    if endpoint_value:
        parts = urlsplit(endpoint_value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            _problem(f"endpoint {endpoint_value!r} is not an http(s) URL; export disabled", strict, diag)
            endpoint_value = None
        elif parts.scheme == "http" and key_value and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
            _problem("the API key will be sent over plaintext HTTP to a non-local host; use https", strict, diag)

    return Config(
        service_name=S.label(name, S.MAX_SHORT_LABEL, "unknown_service"),
        environment=env,
        service_version=S.label(service_version or _env("AOBS_SERVICE_VERSION") or "", S.MAX_SHORT_LABEL, "") or None,
        endpoint=endpoint_value,
        api_key=key_value,
        enabled=enabled if enabled is not None else _env_bool("AOBS_ENABLED", True),
        policy=policy,
        fingerprint_inputs=fingerprint_inputs,
        fingerprinter=_fingerprinter(fingerprint_key, fingerprint_key_id, strict, diag),
        **tuning,
    )


def _fingerprinter(key: bytes | None, key_id: str | None, strict: bool, diag: Diagnostics) -> Fingerprinter:
    if key is None:
        env_key = _env("AOBS_FINGERPRINT_KEY")
        if env_key is not None:
            try:
                key = bytes.fromhex(env_key)
            except ValueError:
                _problem("AOBS_FINGERPRINT_KEY is not hex; using a random per-process key", strict, diag)
                return Fingerprinter.random()
    key_id = key_id or _env("AOBS_FINGERPRINT_KEY_ID")
    if key is None:
        if key_id is not None:
            _problem("fingerprint key id given without a key; using a random per-process key", strict, diag)
        return Fingerprinter.random()
    if key_id is None:
        _problem("fingerprint key given without a key id; using a random per-process key", strict, diag)
        return Fingerprinter.random()
    if len(key) < MIN_KEY_BYTES:
        _problem(f"fingerprint key shorter than {MIN_KEY_BYTES} bytes; using a random per-process key", strict, diag)
        return Fingerprinter.random()
    if not S.valid_key_id(key_id):
        _problem("fingerprint key id must match ^[a-z0-9_-]{1,32}$; using a random per-process key", strict, diag)
        return Fingerprinter.random()
    return Fingerprinter(key, key_id)
