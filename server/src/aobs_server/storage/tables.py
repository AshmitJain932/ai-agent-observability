"""Table definitions (SQLAlchemy Core). The migration in ``migrations/versions`` creates exactly
these; a test compares the migrated database with this metadata.

Every tenant-owned table leads with ``project_id`` in its primary key (architecture §6).
High-volume tables (spans, span events, content) carry no foreign keys: parents may arrive late
or never, and every write to them happens under the per-trace lock taken on ``traces``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

metadata = MetaData(
    naming_convention={
        "pk": "%(table_name)s_pkey",
        "fk": "%(table_name)s_%(column_0_name)s_fkey",
        "uq": "%(table_name)s_%(column_0_N_name)s_key",
        "ix": "%(table_name)s_%(column_0_N_name)s_idx",
        "ck": "%(table_name)s_%(constraint_name)s_check",
    }
)

NOW = text("now()")
NEW_UUID = text("gen_random_uuid()")


def _ts(name: str, *, nullable: bool = False, default: bool = False) -> Column[Any]:
    return Column(name, DateTime(timezone=True), nullable=nullable, server_default=NOW if default else None)


def _project_fk() -> Column[Any]:
    return Column("project_id", Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)


# --------------------------------------------------------------------------- tenancy and auth

organizations = Table(
    "organizations",
    metadata,
    Column("id", Uuid, primary_key=True, server_default=NEW_UUID),
    Column("name", Text, nullable=False),
    _ts("created_at", default=True),
)

projects = Table(
    "projects",
    metadata,
    Column("id", Uuid, primary_key=True, server_default=NEW_UUID),
    Column("organization_id", Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
    Column("name", Text, nullable=False),
    Column("settings", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    _ts("created_at", default=True),
    UniqueConstraint("organization_id", "name"),
)

api_keys = Table(
    "api_keys",
    metadata,
    Column("id", Uuid, primary_key=True, server_default=NEW_UUID),
    _project_fk(),
    Column("name", Text, nullable=False),
    Column("prefix", Text, nullable=False),  # first characters of the key, for display only
    Column("key_hash", LargeBinary, nullable=False),  # SHA-256 of the full key
    Column("scopes", ARRAY(Text), nullable=False),
    Column("environment", Text, nullable=True),  # if set, the key may only write this environment
    _ts("created_at", default=True),
    _ts("revoked_at", nullable=True),
    UniqueConstraint("key_hash"),
    CheckConstraint("scopes <@ ARRAY['ingest','read','admin']::text[] AND cardinality(scopes) > 0", name="scopes"),
    CheckConstraint("octet_length(key_hash) = 32", name="key_hash_length"),
    Index(None, "project_id"),
)

environments = Table(
    "environments",
    metadata,
    _project_fk(),
    Column("name", Text, nullable=False),
    _ts("first_seen_at", default=True),
    PrimaryKeyConstraint("project_id", "name"),
)

resources = Table(
    "resources",
    metadata,
    _project_fk(),
    Column("resource_hash", Text, nullable=False),  # SHA-256 of the canonical resource JSON
    Column("resource", JSONB, nullable=False),
    _ts("first_seen_at", default=True),
    PrimaryKeyConstraint("project_id", "resource_hash"),
)

# --------------------------------------------------------------------------- traces and spans

traces = Table(
    "traces",
    metadata,
    _project_fk(),
    Column("trace_id", Text, nullable=False),
    # ok | ok_with_errors | error | cancelled | incomplete | open. "open" becomes in_progress or
    # incomplete at read time: in_progress iff now - stale_reference_time < stale_after.
    Column("status", Text, nullable=False),
    _ts("stale_reference_time", nullable=True),
    _ts("started_at", nullable=True),
    _ts("ended_at", nullable=True),
    Column("root_name", Text, nullable=True),
    Column("agent_name", Text, nullable=True),
    Column("session_id", Text, nullable=True),
    Column("span_count", Integer, nullable=False, server_default="0"),
    Column("open_span_count", Integer, nullable=False, server_default="0"),
    Column("error_count", Integer, nullable=False, server_default="0"),
    Column("span_event_count", Integer, nullable=False, server_default="0"),
    Column("kind_counts", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("tokens", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("conflicts", Integer, nullable=False, server_default="0"),
    Column("orphan_count", Integer, nullable=False, server_default="0"),
    Column("truncated", Boolean, nullable=False, server_default=text("false")),
    Column("environments", ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")),
    _ts("first_received_at", default=True),
    _ts("last_received_at", default=True),
    PrimaryKeyConstraint("project_id", "trace_id"),
    Index(None, "project_id", "last_received_at"),
    CheckConstraint("status IN ('ok', 'ok_with_errors', 'error', 'cancelled', 'incomplete', 'open')", name="status"),
)

spans = Table(
    "spans",
    metadata,
    Column("project_id", Uuid, nullable=False),
    Column("trace_id", Text, nullable=False),
    Column("span_id", Text, nullable=False),
    # Hot fields, derived from the merged start + end (end wins).
    Column("parent_span_id", Text, nullable=True),
    Column("kind", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("status", Text, nullable=True),
    _ts("started_at"),
    _ts("ended_at", nullable=True),
    Column("duration_ms", Double, nullable=True),
    Column("session_id", Text, nullable=True),
    Column("agent_name", Text, nullable=True),
    Column("provider", Text, nullable=True),
    Column("request_model", Text, nullable=True),
    Column("response_model", Text, nullable=True),
    Column("tool_name", Text, nullable=True),
    # Winning copies (canonical JSON, after the privacy policy). The end's content block is
    # stored in span_content instead. *_digests holds every distinct digest seen (capped), so the
    # winner (lowest digest) and the conflict count do not depend on arrival order.
    Column("start_event", JSONB, nullable=True),
    Column("start_digests", ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")),
    Column("end_event", JSONB, nullable=True),
    Column("end_digests", ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")),
    Column("resource_hash", Text, nullable=True),
    _ts("first_received_at"),
    _ts("last_received_at"),
    PrimaryKeyConstraint("project_id", "trace_id", "span_id"),
    CheckConstraint("start_event IS NOT NULL OR end_event IS NOT NULL", name="has_event"),
)

span_events = Table(
    "span_events",
    metadata,
    Column("project_id", Uuid, nullable=False),
    Column("event_id", Text, nullable=False),
    Column("trace_id", Text, nullable=False),
    Column("span_id", Text, nullable=False),
    Column("name", Text, nullable=False),
    _ts("time"),
    Column("event", JSONB, nullable=False),
    Column("digests", ARRAY(Text), nullable=False),
    _ts("first_received_at"),
    _ts("last_received_at"),
    PrimaryKeyConstraint("project_id", "event_id"),
    Index(None, "project_id", "trace_id"),
)

span_content = Table(
    "span_content",
    metadata,
    Column("project_id", Uuid, nullable=False),
    Column("trace_id", Text, nullable=False),
    Column("span_id", Text, nullable=False),
    Column("content", JSONB, nullable=False),
    _ts("received_at"),
    PrimaryKeyConstraint("project_id", "trace_id", "span_id"),
)

trace_tombstones = Table(
    "trace_tombstones",
    metadata,
    _project_fk(),
    Column("trace_id", Text, nullable=False),
    _ts("deleted_at", default=True),
    PrimaryKeyConstraint("project_id", "trace_id"),
)

ingest_diagnostics = Table(
    "ingest_diagnostics",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    _project_fk(),
    Column("api_key_id", Uuid, nullable=True),
    _ts("received_at"),
    Column("batch_id", Text, nullable=True),
    Column("counters", JSONB, nullable=False),  # only non-zero counters, e.g. {"tombstoned": 3}
    Index(None, "project_id", "received_at"),
)
