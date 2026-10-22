"""The first web dashboard: a read-only UI over saved trace files (sample recordings installed with the
package, or a local directory of JSONL files) and stored traces. See ``app.create_app`` and docs/dashboard.md."""

from .app import DashboardConfig, create_app

__all__ = ["DashboardConfig", "create_app"]
