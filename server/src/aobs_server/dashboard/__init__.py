"""The first web dashboard: a read-only UI over saved trace files (sample recordings shipped with the
repository, or a local directory of JSONL files). See ``app.create_app`` and docs/dashboard.md."""

from .app import DashboardConfig, create_app

__all__ = ["DashboardConfig", "create_app"]
