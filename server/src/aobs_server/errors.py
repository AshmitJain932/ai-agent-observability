"""Errors that map to HTTP problem responses (RFC 9457). Raised by auth, ingest and query;
rendered by the API layer. Messages never contain secrets or another tenant's data."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class ApiError(Exception):
    status: int = 500
    slug: str = "internal-error"
    title: str = "Internal server error"

    def __init__(
        self,
        detail: str | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        extensions: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(detail or self.title)
        self.detail = detail or self.title
        self.headers = dict(headers or {})
        self.extensions = dict(extensions or {})


class BadRequest(ApiError):
    status, slug, title = 400, "bad-request", "Bad request"


class InvalidBatch(ApiError):
    status, slug, title = 400, "invalid-batch", "The batch envelope is invalid"


class UnsupportedSchemaVersion(ApiError):
    status, slug, title = 400, "unsupported-schema-version", "Unsupported schema version"


class Unauthorized(ApiError):
    status, slug, title = 401, "unauthorized", "Missing or invalid API key"

    def __init__(self) -> None:
        # One message for missing, malformed, unknown and revoked keys: nothing to enumerate.
        super().__init__("Missing or invalid API key", headers={"WWW-Authenticate": 'Bearer realm="aobs"'})


class Forbidden(ApiError):
    status, slug, title = 403, "forbidden", "Forbidden"


class NotFound(ApiError):
    status, slug, title = 404, "not-found", "Not found"

    def __init__(self) -> None:
        # Identical for "does not exist" and "belongs to another project" (no existence leaks).
        super().__init__("The requested resource was not found")


class PayloadTooLarge(ApiError):
    status, slug, title = 413, "payload-too-large", "Request body too large"


class UnsupportedMediaType(ApiError):
    status, slug, title = 415, "unsupported-media-type", "Unsupported media type"


class RateLimited(ApiError):
    status, slug, title = 429, "rate-limited", "Too many requests"


class ServiceUnavailable(ApiError):
    status, slug, title = 503, "service-unavailable", "Service temporarily unavailable"
