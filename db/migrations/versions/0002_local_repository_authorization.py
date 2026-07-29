"""Add secure local repository authorization and immutable scan manifests.

Revision ID: 0002_local_repository_authorization
Revises: 0001_day1_foundation
Create Date: 2026-07-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_local_repository_authorization"
down_revision: str | None = "0001_day1_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.drop_constraint("uq_repositories_local_root", "repositories", type_="unique")
    op.drop_constraint("ck_repositories_authorized_at", "repositories", type_="check")
    op.add_column("repositories", sa.Column("normalized_root_key", sa.Text(), nullable=True))
    op.add_column("repositories", sa.Column("root_identity", sa.String(128), nullable=True))
    op.add_column(
        "repositories",
        sa.Column("authorization_epoch", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("repositories", sa.Column("policy_version", sa.String(64), nullable=True))
    # Day 1 local authorizations lack a trusted filesystem identity. Revoke them rather than
    # manufacturing one; the user can safely reauthorize through the preview flow.
    op.execute(
        "UPDATE repositories SET authorization_status = 'revoked', revoked_at = now(), "
        "authorization_epoch = 1 WHERE provider = 'local' "
        "AND authorization_status = 'authorized'"
    )
    op.create_check_constraint(
        "ck_repositories_authorization_epoch",
        "repositories",
        "authorization_epoch >= 0",
    )
    op.create_check_constraint(
        "ck_repositories_authorized_fields",
        "repositories",
        "authorization_status <> 'authorized' OR "
        "(authorized_at IS NOT NULL AND revoked_at IS NULL AND normalized_root_key IS NOT NULL "
        "AND root_identity IS NOT NULL AND policy_version IS NOT NULL AND authorization_epoch > 0)",
    )
    op.create_index(
        "uq_repositories_local_root_key",
        "repositories",
        ["space_id", "normalized_root_key"],
        unique=True,
        postgresql_where=sa.text("provider = 'local'"),
    )

    op.create_check_constraint(
        "ck_sources_repository_kind", "sources", "repository_id IS NULL OR kind = 'repository'"
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM sources WHERE repository_id IS NOT NULL
                GROUP BY repository_id HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION USING
                    MESSAGE = '0002 migration blocked: multiple sources reference one repository',
                    HINT = 'Merge duplicate repository sources and repoint their versions before retrying.';
            END IF;
        END $$;
        """
    )
    op.create_index(
        "uq_sources_repository",
        "sources",
        ["repository_id"],
        unique=True,
        postgresql_where=sa.text("repository_id IS NOT NULL"),
    )

    op.add_column(
        "source_versions",
        sa.Column("authorization_epoch", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "source_versions",
        sa.Column("policy_version", sa.String(64), server_default="legacy-v1", nullable=False),
    )
    op.create_check_constraint(
        "ck_source_versions_authorization_epoch",
        "source_versions",
        "authorization_epoch >= 0",
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM source_versions
                GROUP BY source_id, content_hash HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION USING
                    MESSAGE = '0002 migration blocked: duplicate legacy source-version content',
                    HINT = 'Deduplicate versions by source_id/content_hash and repoint index jobs before retrying.';
            END IF;
        END $$;
        """
    )
    op.create_unique_constraint(
        "uq_source_versions_manifest_epoch_policy",
        "source_versions",
        ["source_id", "content_hash", "authorization_epoch", "policy_version"],
    )

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM index_jobs
                GROUP BY source_version_id, attempt HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION USING
                    MESSAGE = '0002 migration blocked: duplicate index-job attempts',
                    HINT = 'Renumber duplicate attempts per source_version_id before retrying.';
            END IF;
            IF EXISTS (
                SELECT 1 FROM index_jobs WHERE status IN ('pending', 'running')
                GROUP BY source_version_id HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION USING
                    MESSAGE = '0002 migration blocked: multiple active index jobs for one version',
                    HINT = 'Cancel all but one pending/running job per source version before retrying.';
            END IF;
        END $$;
        """
    )
    op.create_unique_constraint(
        "uq_index_jobs_version_attempt", "index_jobs", ["source_version_id", "attempt"]
    )
    op.create_index(
        "uq_index_jobs_active_version",
        "index_jobs",
        ["source_version_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )

    op.create_table(
        "scan_manifest_entries",
        sa.Column("id", UUID, nullable=False),
        sa.Column("source_version_id", UUID, nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("modified_ns", sa.BigInteger(), nullable=False),
        sa.Column("file_identity", sa.String(128), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "relative_path <> '' AND left(relative_path, 1) <> '/' "
            "AND relative_path !~ '(^|/)\\.\\.(/|$)' "
            "AND position('\\\\' in relative_path) = 0",
            name="ck_scan_manifest_entries_relative_path",
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_scan_manifest_entries_sha256"
        ),
        sa.CheckConstraint("size_bytes >= 0", name="ck_scan_manifest_entries_size"),
        sa.CheckConstraint("modified_ns >= 0", name="ck_scan_manifest_entries_modified_ns"),
        sa.ForeignKeyConstraint(
            ["source_version_id"], ["source_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_version_id", "relative_path", name="uq_scan_manifest_entries_path"
        ),
    )
    op.create_index(
        "ix_scan_manifest_entries_version", "scan_manifest_entries", ["source_version_id"]
    )


def downgrade() -> None:
    op.drop_table("scan_manifest_entries")
    op.drop_index("uq_index_jobs_active_version", table_name="index_jobs")
    op.drop_constraint("uq_index_jobs_version_attempt", "index_jobs", type_="unique")
    op.drop_constraint(
        "uq_source_versions_manifest_epoch_policy", "source_versions", type_="unique"
    )
    op.drop_constraint(
        "ck_source_versions_authorization_epoch", "source_versions", type_="check"
    )
    op.drop_column("source_versions", "policy_version")
    op.drop_column("source_versions", "authorization_epoch")
    op.drop_index("uq_sources_repository", table_name="sources")
    op.drop_constraint("ck_sources_repository_kind", "sources", type_="check")
    op.drop_index("uq_repositories_local_root_key", table_name="repositories")
    op.drop_constraint("ck_repositories_authorized_fields", "repositories", type_="check")
    op.drop_constraint("ck_repositories_authorization_epoch", "repositories", type_="check")
    op.drop_column("repositories", "policy_version")
    op.drop_column("repositories", "authorization_epoch")
    op.drop_column("repositories", "root_identity")
    op.drop_column("repositories", "normalized_root_key")
    op.create_check_constraint(
        "ck_repositories_authorized_at",
        "repositories",
        "authorization_status <> 'authorized' OR authorized_at IS NOT NULL",
    )
    op.create_unique_constraint(
        "uq_repositories_local_root", "repositories", ["space_id", "canonical_root_path"]
    )
