"""A local demo collector: one project holding the sample recordings (``aobs-server demo``).

``seed`` is what ``compose.yaml`` runs once the collector is up (docs/docker.md):

1. find the demo organization and project by name, or create them (metadata-only project defaults);
2. if the project already holds every sample trace, stop: seeding is idempotent;
3. otherwise create a short-lived ingest key, send every batch of the sample recordings to the
   collector's ``POST /v1/ingest`` (the same path an SDK uses), and revoke the key afterwards,
   whether or not sending worked.

The key is never printed, logged or written anywhere; only its hash is stored, and it is revoked
before ``seed`` returns. Re-sending after a partial failure is safe because ingestion is idempotent:
events already stored are counted as duplicates.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine, func, select

from .auth.service import create_key
from .project_settings import ProjectSettings
from .storage import tenancy
from .storage.tables import organizations, projects, traces

DEMO_ORGANIZATION = "aobs-demo"
DEMO_PROJECT = "demo"
SEED_KEY_NAME = "demo-seed"
SAMPLE_DIRS = ("traces", "compare", "groups")
MAX_RESPONSE_BYTES = 1 << 20


class SeedError(Exception):
    """The sample traces could not be sent to the collector."""


@dataclass
class SeedResult:
    organization_id: UUID
    project_id: UUID
    seeded: bool  # False: the project already held every sample trace, nothing was sent
    batches_sent: int = 0
    counts: Counter[str] = field(default_factory=Counter)
    stored_traces: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "organization_id": self.organization_id,
            "project_id": self.project_id,
            "seeded": self.seeded,
            "batches_sent": self.batches_sent,
            "counts": dict(self.counts),
            "stored_traces": self.stored_traces,
        }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect could forward the Authorization header elsewhere: treat it as an error."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def find_project(conn: Connection, organization: str, project: str) -> tuple[UUID, UUID] | None:
    """(organization id, project id) of the oldest project named ``project`` in an organization
    named ``organization``, or None."""
    row = conn.execute(
        select(organizations.c.id, projects.c.id.label("project_id"))
        .join(projects, projects.c.organization_id == organizations.c.id)
        .where(organizations.c.name == organization, projects.c.name == project)
        .order_by(projects.c.created_at, projects.c.id)
        .limit(1)
    ).first()
    return None if row is None else (row.id, row.project_id)


def _ensure_project(conn: Connection, organization: str, project: str) -> tuple[UUID, UUID]:
    found = find_project(conn, organization, project)
    if found is not None:
        return found
    org_id = conn.execute(
        select(organizations.c.id)
        .where(organizations.c.name == organization)
        .order_by(organizations.c.created_at, organizations.c.id)
        .limit(1)
    ).scalar_one_or_none()
    if org_id is None:
        org_id = tenancy.create_organization(conn, organization)
    return org_id, tenancy.create_project(conn, org_id, project, ProjectSettings())


def stored_trace_count(conn: Connection, project_id: UUID) -> int:
    query = select(func.count()).select_from(traces).where(traces.c.project_id == project_id)
    return int(conn.execute(query).scalar_one())


def _missing_traces(conn: Connection, project_id: UUID, trace_ids: set[str]) -> set[str]:
    query = select(traces.c.trace_id).where(traces.c.project_id == project_id, traces.c.trace_id.in_(trace_ids))
    return trace_ids - set(conn.execute(query).scalars())


def sample_batches(samples_dir: Path) -> tuple[list[tuple[str, bytes]], set[str]]:
    """Every batch (one JSONL line) of the sample recordings, labelled ``dir/file.jsonl:line``, and
    the trace ids they hold."""
    batches: list[tuple[str, bytes]] = []
    trace_ids: set[str] = set()
    for name in SAMPLE_DIRS:
        for path in sorted((samples_dir / name).glob("*.jsonl")):
            for number, line in enumerate(path.read_bytes().splitlines(), start=1):
                if not line.strip():
                    continue
                label = f"{name}/{path.name}:{number}"
                try:
                    events = json.loads(line)["events"]
                    trace_ids.update(str(event["trace_id"]) for event in events)
                except (ValueError, KeyError, TypeError):
                    raise SeedError(f"{label} is not a trace batch") from None
                batches.append((label, line))
    return batches, trace_ids


def ingest_url(collector: str) -> str:
    if urllib.parse.urlsplit(collector).scheme not in ("http", "https"):
        raise SeedError(f"the collector must be an http:// or https:// URL, got {collector!r}")
    return collector.rstrip("/") + "/v1/ingest"


def _post(opener: urllib.request.OpenerDirector, url: str, key: str, body: bytes, timeout_s: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    with opener.open(request, timeout=timeout_s) as response:
        data = json.loads(response.read(MAX_RESPONSE_BYTES))
    return data if isinstance(data, dict) else {}


def send_batches(collector: str, key: str, batches: list[tuple[str, bytes]], timeout_s: float = 30.0) -> Counter[str]:
    """POST each batch to ``collector``'s ingest API; the summed per-event outcome counts.
    Any non-200 answer or connection failure is a SeedError naming the batch (never the key)."""
    url = ingest_url(collector)
    opener = urllib.request.build_opener(_NoRedirect)
    totals: Counter[str] = Counter()
    for label, body in batches:
        try:
            result = _post(opener, url, key, body, timeout_s)
        except urllib.error.HTTPError as exc:
            exc.close()
            raise SeedError(f"the collector at {url} answered {exc.code} to {label}") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            reason = getattr(exc, "reason", exc)
            raise SeedError(f"cannot send {label} to {url}: {reason}") from None
        counts = result.get("counts")
        if isinstance(counts, dict):
            totals.update({k: v for k, v in counts.items() if isinstance(v, int)})
    return totals


def seed(
    engine: Engine,
    collector: str,
    samples_dir: Path,
    organization: str = DEMO_ORGANIZATION,
    project: str = DEMO_PROJECT,
) -> SeedResult:
    """Create the demo project if needed and, unless it already holds every sample trace, send it the
    samples."""
    ingest_url(collector)
    batches, trace_ids = sample_batches(samples_dir)
    if not batches:
        raise SeedError(f"no sample recordings found in {samples_dir}")
    with engine.begin() as conn:
        org_id, project_id = _ensure_project(conn, organization, project)
        if not _missing_traces(conn, project_id, trace_ids):
            return SeedResult(org_id, project_id, seeded=False, stored_traces=stored_trace_count(conn, project_id))
    with engine.begin() as conn:
        key_id, key = create_key(conn, project_id, SEED_KEY_NAME, ("ingest",), None)
    try:
        counts = send_batches(collector, key.secret, batches)
    finally:
        with engine.begin() as conn:
            tenancy.revoke_api_key(conn, key_id, datetime.now(UTC))
    with engine.connect() as conn:
        stored = stored_trace_count(conn, project_id)
    return SeedResult(org_id, project_id, seeded=True, batches_sent=len(batches), counts=counts, stored_traces=stored)
