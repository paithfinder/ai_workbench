"""Initial Day 1 data foundation.

Revision ID: 0001_day1_foundation
Revises:
Create Date: 2026-07-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_day1_foundation"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "spaces",
        sa.Column("id", UUID, nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("slug ~ '^[a-z0-9]+(?:-[a-z0-9]+)*$'", name="ck_spaces_slug_format"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_table(
        "repositories",
        sa.Column("id", UUID, nullable=False),
        sa.Column("space_id", UUID, nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(512), nullable=True),
        sa.Column("default_branch", sa.String(255), nullable=True),
        sa.Column("web_url", sa.Text(), nullable=True),
        sa.Column("canonical_root_path", sa.Text(), nullable=True),
        sa.Column("authorization_status", sa.String(32), nullable=False),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "authorization_status IN ('pending', 'authorized', 'revoked')",
            name="ck_repositories_authorization_status",
        ),
        sa.CheckConstraint(
            "authorization_status <> 'authorized' OR authorized_at IS NOT NULL",
            name="ck_repositories_authorized_at",
        ),
        sa.CheckConstraint(
            "authorization_status <> 'revoked' OR revoked_at IS NOT NULL",
            name="ck_repositories_revoked_at",
        ),
        sa.CheckConstraint(
            "(provider = 'local' AND canonical_root_path IS NOT NULL "
            "AND external_id IS NULL AND web_url IS NULL) OR "
            "(provider = 'github' AND external_id IS NOT NULL "
            "AND full_name IS NOT NULL AND web_url IS NOT NULL "
            "AND canonical_root_path IS NULL)",
            name="ck_repositories_provider_fields",
        ),
        sa.CheckConstraint("provider IN ('local', 'github')", name="ck_repositories_provider"),
        sa.ForeignKeyConstraint(["space_id"], ["spaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("space_id", "canonical_root_path", name="uq_repositories_local_root"),
        sa.UniqueConstraint("space_id", "provider", "external_id", name="uq_repositories_external"),
    )
    op.create_index("ix_repositories_space_full_name", "repositories", ["space_id", "full_name"])
    op.create_index(
        "ix_repositories_space_authorization",
        "repositories",
        ["space_id", "authorization_status"],
    )
    op.create_table(
        "sources",
        sa.Column("id", UUID, nullable=False),
        sa.Column("space_id", UUID, nullable=False),
        sa.Column("repository_id", UUID, nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("metadata", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("kind IN ('repository', 'document', 'url', 'notion')", name="ck_sources_kind"),
        sa.CheckConstraint("status IN ('active', 'disabled', 'error')", name="ck_sources_status"),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["space_id"], ["spaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("space_id", "uri", name="uq_sources_space_uri"),
    )
    op.create_index("ix_sources_space_kind_status", "sources", ["space_id", "kind", "status"])
    op.create_index(
        "ix_sources_title_trgm",
        "sources",
        ["title"],
        postgresql_using="gin",
        postgresql_ops={"title": "gin_trgm_ops"},
    )
    op.create_table(
        "source_versions",
        sa.Column("id", UUID, nullable=False),
        sa.Column("source_id", UUID, nullable=False),
        sa.Column("version_identifier", sa.String(512), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("storage_uri", sa.Text(), nullable=True),
        sa.Column("metadata", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_source_versions_sha256"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_id", "version_identifier", name="uq_source_versions_revision"),
    )
    op.create_index(
        "ix_source_versions_source_created",
        "source_versions",
        ["source_id", sa.literal_column("created_at DESC")],
    )
    op.create_table(
        "index_jobs",
        sa.Column("id", UUID, nullable=False),
        sa.Column("space_id", UUID, nullable=False),
        sa.Column("source_version_id", UUID, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("attempt > 0", name="ck_index_jobs_attempt_positive"),
        sa.CheckConstraint("completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at", name="ck_index_jobs_time_order"),
        sa.CheckConstraint("status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')", name="ck_index_jobs_status"),
        sa.ForeignKeyConstraint(["source_version_id"], ["source_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["space_id"], ["spaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_index_jobs_source_version", "index_jobs", ["source_version_id"])
    op.create_index("ix_index_jobs_space_status_created", "index_jobs", ["space_id", "status", "created_at"])
    op.create_table(
        "query_runs",
        sa.Column("id", UUID, nullable=False),
        sa.Column("space_id", UUID, nullable=False),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("query_text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("model_name", sa.String(255), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at", name="ck_query_runs_time_order"),
        sa.CheckConstraint("input_tokens IS NULL OR input_tokens >= 0", name="ck_query_runs_input_tokens"),
        sa.CheckConstraint("output_tokens IS NULL OR output_tokens >= 0", name="ck_query_runs_output_tokens"),
        sa.CheckConstraint("status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')", name="ck_query_runs_status"),
        sa.ForeignKeyConstraint(["space_id"], ["spaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_query_runs_request_id", "query_runs", ["request_id"])
    op.create_index("ix_query_runs_space_created", "query_runs", ["space_id", "created_at"])
    op.create_table(
        "run_events",
        sa.Column("id", UUID, nullable=False),
        sa.Column("query_run_id", UUID, nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("payload", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("sequence >= 0", name="ck_run_events_sequence"),
        sa.ForeignKeyConstraint(["query_run_id"], ["query_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("query_run_id", "sequence", name="uq_run_events_sequence"),
    )
    op.create_index("ix_run_events_run_created", "run_events", ["query_run_id", "created_at"])
    op.create_table(
        "audit_events",
        sa.Column("id", UUID, nullable=False),
        sa.Column("space_id", UUID, nullable=True),
        sa.Column("actor_id", sa.String(255), nullable=True),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("resource_type", sa.String(100), nullable=True),
        sa.Column("resource_id", UUID, nullable=True),
        sa.Column("request_id", sa.String(128), nullable=True),
        sa.Column("correlation_id", sa.String(128), nullable=True),
        sa.Column("payload", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["space_id"], ["spaces.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_events_request_id", "audit_events", ["request_id"])
    op.create_index("ix_audit_events_resource", "audit_events", ["resource_type", "resource_id"])
    op.create_index("ix_audit_events_space_created", "audit_events", ["space_id", "created_at"])


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("run_events")
    op.drop_table("query_runs")
    op.drop_table("index_jobs")
    op.drop_table("source_versions")
    op.drop_index("ix_sources_title_trgm", table_name="sources")
    op.drop_table("sources")
    op.drop_table("repositories")
    op.drop_table("spaces")
    # Extensions may be shared by other applications; downgrade only removes our objects.
