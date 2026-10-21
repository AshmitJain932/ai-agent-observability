"""Initial schema: tenancy, API keys, traces, spans, span events, content, tombstones, diagnostics.

Revision ID: 0001
Revises: (none)
Create Date: 2026-10-12

Mirrors aobs_server.storage.tables (a test compares the migrated database with that metadata).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "organizations",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("organizations_pkey")),
    )
    op.create_table(
        "span_content",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column("span_id", sa.Text(), nullable=False),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("project_id", "trace_id", "span_id", name=op.f("span_content_pkey")),
    )
    op.create_table(
        "span_events",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column("span_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("digests", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("first_received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_received_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("project_id", "event_id", name=op.f("span_events_pkey")),
    )
    op.create_index(
        op.f("span_events_project_id_trace_id_idx"), "span_events", ["project_id", "trace_id"], unique=False
    )
    op.create_table(
        "spans",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column("span_id", sa.Text(), nullable=False),
        sa.Column("parent_span_id", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Double(), nullable=True),
        sa.Column("session_id", sa.Text(), nullable=True),
        sa.Column("agent_name", sa.Text(), nullable=True),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("request_model", sa.Text(), nullable=True),
        sa.Column("response_model", sa.Text(), nullable=True),
        sa.Column("tool_name", sa.Text(), nullable=True),
        sa.Column("start_event", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("start_digests", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]"), nullable=False),
        sa.Column("end_event", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("end_digests", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]"), nullable=False),
        sa.Column("resource_hash", sa.Text(), nullable=True),
        sa.Column("first_received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_received_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("start_event IS NOT NULL OR end_event IS NOT NULL", name=op.f("spans_has_event_check")),
        sa.PrimaryKeyConstraint("project_id", "trace_id", "span_id", name=op.f("spans_pkey")),
    )
    op.create_table(
        "projects",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "settings", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], name=op.f("projects_organization_id_fkey"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("projects_pkey")),
        sa.UniqueConstraint("organization_id", "name", name=op.f("projects_organization_id_name_key")),
    )
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("prefix", sa.Text(), nullable=False),
        sa.Column("key_hash", sa.LargeBinary(), nullable=False),
        sa.Column("scopes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("environment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "scopes <@ ARRAY['ingest','read','admin']::text[] AND cardinality(scopes) > 0",
            name=op.f("api_keys_scopes_check"),
        ),
        sa.CheckConstraint("octet_length(key_hash) = 32", name=op.f("api_keys_key_hash_length_check")),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("api_keys_project_id_fkey"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("api_keys_pkey")),
        sa.UniqueConstraint("key_hash", name=op.f("api_keys_key_hash_key")),
    )
    op.create_index(op.f("api_keys_project_id_idx"), "api_keys", ["project_id"], unique=False)
    op.create_table(
        "environments",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("environments_project_id_fkey"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("project_id", "name", name=op.f("environments_pkey")),
    )
    op.create_table(
        "ingest_diagnostics",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("api_key_id", sa.Uuid(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("batch_id", sa.Text(), nullable=True),
        sa.Column("counters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("ingest_diagnostics_project_id_fkey"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("ingest_diagnostics_pkey")),
    )
    op.create_index(
        op.f("ingest_diagnostics_project_id_received_at_idx"),
        "ingest_diagnostics",
        ["project_id", "received_at"],
        unique=False,
    )
    op.create_table(
        "resources",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("resource_hash", sa.Text(), nullable=False),
        sa.Column("resource", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("resources_project_id_fkey"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("project_id", "resource_hash", name=op.f("resources_pkey")),
    )
    op.create_table(
        "trace_tombstones",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("trace_tombstones_project_id_fkey"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("project_id", "trace_id", name=op.f("trace_tombstones_pkey")),
    )
    op.create_table(
        "traces",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("stale_reference_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("root_name", sa.Text(), nullable=True),
        sa.Column("agent_name", sa.Text(), nullable=True),
        sa.Column("session_id", sa.Text(), nullable=True),
        sa.Column("span_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("open_span_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("span_event_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "kind_counts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "tokens", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("conflicts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("orphan_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("truncated", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("environments", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]"), nullable=False),
        sa.Column("first_received_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_received_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('ok', 'ok_with_errors', 'error', 'cancelled', 'incomplete', 'open')",
            name=op.f("traces_status_check"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("traces_project_id_fkey"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("project_id", "trace_id", name=op.f("traces_pkey")),
    )
    op.create_index(
        op.f("traces_project_id_last_received_at_idx"), "traces", ["project_id", "last_received_at"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("traces_project_id_last_received_at_idx"), table_name="traces")
    op.drop_table("traces")
    op.drop_table("trace_tombstones")
    op.drop_table("resources")
    op.drop_index(op.f("ingest_diagnostics_project_id_received_at_idx"), table_name="ingest_diagnostics")
    op.drop_table("ingest_diagnostics")
    op.drop_table("environments")
    op.drop_index(op.f("api_keys_project_id_idx"), table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_table("projects")
    op.drop_table("spans")
    op.drop_index(op.f("span_events_project_id_trace_id_idx"), table_name="span_events")
    op.drop_table("span_events")
    op.drop_table("span_content")
    op.drop_table("organizations")
