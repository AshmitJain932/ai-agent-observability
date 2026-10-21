"""The collector HTTP API (``POST /v1/ingest``)."""

from .app import create_app

__all__ = ["create_app"]
