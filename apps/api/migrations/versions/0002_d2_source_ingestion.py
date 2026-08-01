"""Add D2 source ingestion, jobs, attempts, and outbox.

Revision ID: 0002_d2_source_ingestion
Revises: 0001_d1_foundation
Create Date: 2026-07-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_d2_source_ingestion"
down_revision: str | None = "0001_d1_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("source_versions", sa.Column("expected_content_sha256", sa.String(64)))
    op.add_column("source_versions", sa.Column("upload_storage_key", sa.String(1000)))
    op.add_column("source_versions", sa.Column("object_etag", sa.String(255)))
    op.add_column("source_versions", sa.Column("upload_idempotency_key", sa.String(255)))
    op.add_column("source_versions", sa.Column("upload_request_hash", sa.String(64)))
    op.add_column("source_versions", sa.Column("completion_idempotency_key", sa.String(255)))
    op.add_column("source_versions", sa.Column("upload_expires_at", sa.DateTime(timezone=True)))
    op.add_column("source_versions", sa.Column("completed_at", sa.DateTime(timezone=True)))
    op.add_column(
        "source_versions",
        sa.Column(
            "parse_status",
            sa.String(32),
            server_default=sa.text("'not_started'"),
            nullable=False,
        ),
    )

    # SourceVersion rows created by D1 cannot be promoted safely because D1 did not
    # persist the immutable object's ETag. Refuse a lossy upgrade with an actionable error.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM source_versions) THEN
                RAISE EXCEPTION
                    'D2 migration cannot safely upgrade legacy source_versions because D1 '
                    'did not persist object ETags; export/remove those rows and retry';
            END IF;
        END $$
        """
    )

    # Retain the normalization/preflight for databases restored from experimental D1
    # builds where the explicit empty-table guard is intentionally removed by an operator.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM source_versions first_version
                JOIN source_versions duplicate
                  ON duplicate.source_id = first_version.source_id
                 AND duplicate.id > first_version.id
                 AND lower(duplicate.content_sha256) = lower(first_version.content_sha256)
            ) THEN
                RAISE EXCEPTION
                    'Legacy source_versions contain SHA-256 values that collide after lowercasing';
            END IF;
        END $$
        """
    )
    # D1 accepted any 64-character value. Normalize valid uppercase digests and stop with
    # an actionable database error before adding D2's stronger content-identity constraints.
    op.execute("UPDATE source_versions SET content_sha256 = lower(content_sha256)")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM source_versions
                WHERE content_sha256 !~ '^[0-9a-f]{64}$'
            ) THEN
                RAISE EXCEPTION
                    'D2 migration requires every legacy content_sha256 to be a SHA-256 hex digest';
            END IF;
        END $$
        """
    )

    # Preserve any D1 rows while giving new reservations an explicit upload identity.
    op.execute(
        """
        UPDATE source_versions
        SET expected_content_sha256 = content_sha256,
            upload_storage_key = COALESCE(storage_key, 'legacy/' || id::text),
            upload_idempotency_key = 'legacy:' || id::text,
            upload_request_hash = content_sha256,
            upload_expires_at = created_at
        """
    )
    op.alter_column("source_versions", "content_sha256", nullable=True)
    op.alter_column("source_versions", "expected_content_sha256", nullable=False)
    op.alter_column("source_versions", "upload_storage_key", nullable=False)
    op.alter_column("source_versions", "upload_idempotency_key", nullable=False)
    op.alter_column("source_versions", "upload_request_hash", nullable=False)
    op.alter_column("source_versions", "upload_expires_at", nullable=False)
    op.drop_constraint("uq_source_versions_content", "source_versions", type_="unique")
    op.create_unique_constraint(
        "uq_source_versions_upload_idempotency",
        "source_versions",
        ["source_id", "upload_idempotency_key"],
    )
    op.create_index(
        "uq_source_versions_content",
        "source_versions",
        ["source_id", "content_sha256"],
        unique=True,
        postgresql_where=sa.text("content_sha256 IS NOT NULL"),
    )
    op.create_check_constraint(
        "ck_source_versions_parse_status",
        "source_versions",
        "parse_status IN ('not_started','queued','parsing','ready','failed')",
    )
    op.create_check_constraint(
        "ck_source_versions_content_sha256",
        "source_versions",
        "content_sha256 IS NULL OR content_sha256 ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "ck_source_versions_expected_sha256",
        "source_versions",
        "expected_content_sha256 ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "ck_source_versions_completed_identity",
        "source_versions",
        "completed_at IS NULL OR (content_sha256 IS NOT NULL AND storage_key IS NOT NULL "
        "AND object_etag IS NOT NULL)",
    )

    op.add_column(
        "jobs",
        sa.Column(
            "retryable", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM jobs
                WHERE source_version_id IS NOT NULL
                GROUP BY source_version_id, kind
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'D2 migration requires duplicate legacy jobs per source version/kind '
                    'to be resolved before upgrade';
            END IF;
        END $$
        """
    )
    op.create_index(
        "uq_jobs_source_version_kind",
        "jobs",
        ["source_version_id", "kind"],
        unique=True,
        postgresql_where=sa.text("source_version_id IS NOT NULL"),
    )

    op.create_table(
        "job_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("celery_task_id", sa.String(255)),
        sa.Column("worker_name", sa.String(255)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(100)),
        sa.Column("error_message", sa.String(2000)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("attempt_number > 0", name="ck_job_attempts_number_positive"),
        sa.CheckConstraint(
            "status IN ('running','succeeded','failed')", name="ck_job_attempts_status"
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "attempt_number", name="uq_job_attempts_number"),
    )
    op.create_index("ix_job_attempts_job_status", "job_attempts", ["job_id", "status"])
    op.create_index(
        "ix_job_attempts_status_lease", "job_attempts", ["status", "lease_expires_at"]
    )

    op.create_table(
        "source_create_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["space_id"], ["knowledge_spaces.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("space_id", "idempotency_key", name="uq_source_create_requests_key"),
    )

    op.create_table(
        "job_retry_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("target_attempt_number", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "target_attempt_number > 0", name="ck_job_retry_target_attempt_positive"
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "idempotency_key", name="uq_job_retry_requests_key"),
        sa.UniqueConstraint(
            "job_id", "target_attempt_number", name="uq_job_retry_requests_attempt"
        ),
    )

    op.create_table(
        "outbox_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("aggregate_type", sa.String(100), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("deduplication_key", sa.String(255), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.String(2000)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_outbox_attempt_count_nonnegative"
        ),
        sa.ForeignKeyConstraint(["space_id"], ["knowledge_spaces.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("deduplication_key"),
    )
    op.create_index(
        "ix_outbox_events_pending",
        "outbox_events",
        ["available_at", "created_at"],
        postgresql_where=sa.text("published_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_events_pending", table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_table("job_retry_requests")
    op.drop_table("source_create_requests")
    op.drop_index("ix_job_attempts_status_lease", table_name="job_attempts")
    op.drop_index("ix_job_attempts_job_status", table_name="job_attempts")
    op.drop_table("job_attempts")

    op.drop_index("uq_jobs_source_version_kind", table_name="jobs")
    op.drop_column("jobs", "retryable")

    op.drop_constraint(
        "ck_source_versions_completed_identity", "source_versions", type_="check"
    )
    op.drop_constraint(
        "ck_source_versions_expected_sha256", "source_versions", type_="check"
    )
    op.drop_constraint(
        "ck_source_versions_content_sha256", "source_versions", type_="check"
    )
    op.drop_constraint("ck_source_versions_parse_status", "source_versions", type_="check")
    op.drop_index("uq_source_versions_content", table_name="source_versions")
    op.drop_constraint(
        "uq_source_versions_upload_idempotency", "source_versions", type_="unique"
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM source_versions WHERE content_sha256 IS NULL) THEN
                RAISE EXCEPTION
                    'Cannot downgrade D2 while pending upload reservations exist; '
                    'complete or explicitly remove those reservations before retrying';
            END IF;
        END $$
        """
    )
    op.alter_column("source_versions", "content_sha256", nullable=False)
    op.create_unique_constraint(
        "uq_source_versions_content",
        "source_versions",
        ["source_id", "content_sha256"],
    )
    op.drop_column("source_versions", "parse_status")
    op.drop_column("source_versions", "completed_at")
    op.drop_column("source_versions", "upload_expires_at")
    op.drop_column("source_versions", "completion_idempotency_key")
    op.drop_column("source_versions", "upload_request_hash")
    op.drop_column("source_versions", "upload_idempotency_key")
    op.drop_column("source_versions", "object_etag")
    op.drop_column("source_versions", "upload_storage_key")
    op.drop_column("source_versions", "expected_content_sha256")
