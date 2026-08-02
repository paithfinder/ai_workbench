"""Add D3 immutable source parsing artifacts and stable sections.

Revision ID: 0003_d3_source_parsing
Revises: 0002_d2_source_ingestion
Create Date: 2026-08-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_d3_source_parsing"
down_revision: str | None = "0002_d2_source_ingestion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "source_versions",
        sa.Column(
            "acquisition_type",
            sa.String(32),
            server_default=sa.text("'upload'"),
            nullable=False,
        ),
    )
    op.add_column("source_versions", sa.Column("source_uri", sa.String(2000)))
    op.add_column(
        "source_versions",
        sa.Column(
            "acquisition_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "source_versions",
        sa.Column("current_parse_artifact_id", postgresql.UUID(as_uuid=True)),
    )
    for column in (
        "expected_content_sha256",
        "original_filename",
        "media_type",
        "size_bytes",
        "upload_storage_key",
        "upload_idempotency_key",
        "upload_request_hash",
        "upload_expires_at",
    ):
        op.alter_column("source_versions", column, nullable=True)
    op.create_unique_constraint(
        "uq_source_versions_id_source", "source_versions", ["id", "source_id"]
    )
    op.create_check_constraint(
        "ck_source_versions_acquisition_type",
        "source_versions",
        "acquisition_type IN ('upload','pasted_text','web_fetch')",
    )
    op.create_check_constraint(
        "ck_source_versions_upload_identity",
        "source_versions",
        "acquisition_type <> 'upload' OR (expected_content_sha256 IS NOT NULL "
        "AND original_filename IS NOT NULL AND media_type IS NOT NULL "
        "AND size_bytes IS NOT NULL AND upload_storage_key IS NOT NULL "
        "AND upload_idempotency_key IS NOT NULL AND upload_request_hash IS NOT NULL "
        "AND upload_expires_at IS NOT NULL)",
    )

    op.add_column(
        "jobs",
        sa.Column(
            "attempt_budget_start",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_jobs_attempt_budget_start",
        "jobs",
        "attempt_budget_start >= 0 AND attempt_budget_start <= attempt_count",
    )
    op.create_check_constraint(
        "ck_job_attempts_lease_timeline",
        "job_attempts",
        "heartbeat_at >= started_at AND lease_expires_at >= heartbeat_at",
    )
    op.create_check_constraint(
        "ck_job_attempts_finished_state",
        "job_attempts",
        "(status = 'running' AND finished_at IS NULL) OR "
        "(status <> 'running' AND finished_at IS NOT NULL)",
    )

    op.alter_column("source_create_requests", "source_id", nullable=True)
    op.add_column(
        "source_create_requests",
        sa.Column("lease_token", postgresql.UUID(as_uuid=True)),
    )
    op.add_column(
        "source_create_requests",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
    )
    op.create_check_constraint(
        "ck_source_create_requests_lease_state",
        "source_create_requests",
        "((source_id IS NOT NULL) AND lease_token IS NULL AND lease_expires_at IS NULL) "
        "OR ((source_id IS NULL) AND ((lease_token IS NULL AND lease_expires_at IS NULL) "
        "OR (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)))",
    )

    op.create_table(
        "source_parse_artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("parser_name", sa.String(100), nullable=False),
        sa.Column("parser_version", sa.String(100), nullable=False),
        sa.Column(
            "parser_config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("native_storage_key", sa.String(1000)),
        sa.Column("native_sha256", sa.String(64)),
        sa.Column("markdown_storage_key", sa.String(1000)),
        sa.Column("markdown_sha256", sa.String(64)),
        sa.Column("canonical_storage_key", sa.String(1000)),
        sa.Column("canonical_content_sha256", sa.String(64)),
        sa.Column("page_count", sa.Integer()),
        sa.Column(
            "warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("error_code", sa.String(100)),
        sa.Column("error_message", sa.String(2000)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("revision > 0", name="ck_source_parse_artifacts_revision_positive"),
        sa.CheckConstraint(
            "status IN ('queued','parsing','ready','failed')",
            name="ck_source_parse_artifacts_status",
        ),
        sa.CheckConstraint(
            "canonical_content_sha256 IS NULL OR canonical_content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_source_parse_artifacts_canonical_sha256",
        ),
        sa.CheckConstraint(
            "page_count IS NULL OR page_count >= 0", name="ck_source_parse_artifacts_page_count"
        ),
        sa.ForeignKeyConstraint(
            ["source_version_id"], ["source_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "source_version_id", name="uq_source_parse_artifacts_id_version"
        ),
        sa.UniqueConstraint(
            "source_version_id", "revision", name="uq_source_parse_artifacts_revision"
        ),
    )
    op.create_index(
        "uq_source_parse_artifacts_active",
        "source_parse_artifacts",
        ["source_version_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued','parsing')"),
    )
    op.create_foreign_key(
        "fk_source_versions_current_parse_artifact",
        "source_versions",
        "source_parse_artifacts",
        ["current_parse_artifact_id", "id"],
        ["id", "source_version_id"],
        deferrable=True,
        initially="DEFERRED",
    )

    op.create_table(
        "source_sections",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parse_artifact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("block_id", sa.String(128), nullable=False),
        sa.Column("parent_block_id", sa.String(128)),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("block_type", sa.String(64), nullable=False),
        sa.Column("title", sa.String(1000)),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "heading_path",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("page_number", sa.Integer()),
        sa.Column("paragraph_index", sa.Integer()),
        sa.Column("bbox", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("locator", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("quote_hash", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column(
            "provenance",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("ordinal >= 0", name="ck_source_sections_ordinal_nonnegative"),
        sa.CheckConstraint(
            "page_number IS NULL OR page_number > 0", name="ck_source_sections_page_positive"
        ),
        sa.CheckConstraint(
            "paragraph_index IS NULL OR paragraph_index >= 0",
            name="ck_source_sections_paragraph_nonnegative",
        ),
        sa.CheckConstraint(
            "quote_hash ~ '^[0-9a-f]{64}$'", name="ck_source_sections_quote_hash"
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_source_sections_content_hash"
        ),
        sa.ForeignKeyConstraint(
            ["parse_artifact_id", "source_version_id"],
            ["source_parse_artifacts.id", "source_parse_artifacts.source_version_id"],
            name="fk_source_sections_artifact_version",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["space_id"], ["knowledge_spaces.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("parse_artifact_id", "ordinal", name="uq_source_sections_ordinal"),
        sa.UniqueConstraint("parse_artifact_id", "block_id", name="uq_source_sections_block"),
    )
    op.create_index(
        "ix_source_sections_version_ordinal",
        "source_sections",
        ["source_version_id", "ordinal", "id"],
    )

    op.create_table(
        "source_parse_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parse_artifact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("kind IN ('initial','reparse')", name="ck_source_parse_requests_kind"),
        sa.CheckConstraint(
            "request_hash ~ '^[0-9a-f]{64}$'", name="ck_source_parse_requests_hash"
        ),
        sa.ForeignKeyConstraint(
            ["parse_artifact_id", "source_version_id"],
            ["source_parse_artifacts.id", "source_parse_artifacts.source_version_id"],
            name="fk_source_parse_requests_artifact_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_version_id", "idempotency_key", name="uq_source_parse_requests_key"
        ),
        sa.UniqueConstraint("parse_artifact_id", name="uq_source_parse_requests_artifact"),
        sa.UniqueConstraint("job_id", name="uq_source_parse_requests_job"),
    )
    op.drop_index("uq_jobs_source_version_kind", table_name="jobs")
    op.create_index(
        "uq_jobs_source_version_kind",
        "jobs",
        ["source_version_id", "kind"],
        unique=True,
        postgresql_where=sa.text(
            "source_version_id IS NOT NULL AND kind <> 'source_parse'"
        ),
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM source_versions WHERE acquisition_type <> 'upload'
            ) THEN
                RAISE EXCEPTION
                    'Cannot downgrade D3 while non-upload source versions exist; '
                    'export/remove them first';
            END IF;
        END $$
        """
    )
    op.drop_index("uq_jobs_source_version_kind", table_name="jobs")
    op.create_index(
        "uq_jobs_source_version_kind",
        "jobs",
        ["source_version_id", "kind"],
        unique=True,
        postgresql_where=sa.text("source_version_id IS NOT NULL"),
    )
    op.drop_table("source_parse_requests")
    op.drop_index("ix_source_sections_version_ordinal", table_name="source_sections")
    op.drop_table("source_sections")
    op.drop_constraint(
        "fk_source_versions_current_parse_artifact", "source_versions", type_="foreignkey"
    )
    op.drop_index("uq_source_parse_artifacts_active", table_name="source_parse_artifacts")
    op.drop_table("source_parse_artifacts")
    op.drop_constraint(
        "ck_source_create_requests_lease_state",
        "source_create_requests",
        type_="check",
    )
    op.drop_column("source_create_requests", "lease_expires_at")
    op.drop_column("source_create_requests", "lease_token")
    op.alter_column("source_create_requests", "source_id", nullable=False)
    op.drop_constraint(
        "ck_job_attempts_finished_state", "job_attempts", type_="check"
    )
    op.drop_constraint(
        "ck_job_attempts_lease_timeline", "job_attempts", type_="check"
    )
    op.drop_constraint("ck_jobs_attempt_budget_start", "jobs", type_="check")
    op.drop_column("jobs", "attempt_budget_start")
    op.drop_constraint("ck_source_versions_upload_identity", "source_versions", type_="check")
    op.drop_constraint("ck_source_versions_acquisition_type", "source_versions", type_="check")
    for column in (
        "upload_expires_at",
        "upload_request_hash",
        "upload_idempotency_key",
        "upload_storage_key",
        "size_bytes",
        "media_type",
        "original_filename",
        "expected_content_sha256",
    ):
        op.alter_column("source_versions", column, nullable=False)
    op.drop_constraint("uq_source_versions_id_source", "source_versions", type_="unique")
    op.drop_column("source_versions", "current_parse_artifact_id")
    op.drop_column("source_versions", "acquisition_metadata")
    op.drop_column("source_versions", "source_uri")
    op.drop_column("source_versions", "acquisition_type")
