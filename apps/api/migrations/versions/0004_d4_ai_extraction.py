"""Add D4 AI extraction jobs, candidates, and evidence links.

Revision ID: 0004_d4_ai_extraction
Revises: 0003_d3_source_parsing
Create Date: 2026-08-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_d4_ai_extraction"
down_revision: str | None = "0003_d3_source_parsing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "extraction_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parse_artifact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(100)),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("prompt_version", sa.String(100), nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("latency_ms", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("provider_request_id", sa.String(255)),
        sa.Column("error_code", sa.String(100)),
        sa.Column("error_message", sa.String(2000)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('queued','running','ready','failed')",
            name="ck_extraction_jobs_status",
        ),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND latency_ms >= 0",
            name="ck_extraction_jobs_metrics_nonnegative",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["space_id"], ["knowledge_spaces.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["source_version_id"], ["source_versions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["parse_artifact_id"], ["source_parse_artifacts.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", name="uq_extraction_jobs_job"),
        sa.UniqueConstraint(
            "source_version_id", "prompt_version", name="uq_extraction_jobs_version_prompt"
        ),
    )
    op.create_index(
        "ix_extraction_jobs_space_status_created",
        "extraction_jobs",
        ["space_id", "status", "created_at"],
    )

    op.create_table(
        "extraction_candidates",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("extraction_job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "tags",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("suggested_destination_id", postgresql.UUID(as_uuid=True)),
        sa.Column("atomicity", sa.String(32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("verification_reason", sa.String(1000)),
        sa.Column(
            "conditions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "exceptions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("ordinal >= 0", name="ck_extraction_candidates_ordinal_nonnegative"),
        sa.CheckConstraint(
            "status IN ('pending_review','needs_verification')",
            name="ck_extraction_candidates_status",
        ),
        sa.CheckConstraint(
            "atomicity IN ('atomic','needs_split')",
            name="ck_extraction_candidates_atomicity",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_extraction_candidates_confidence",
        ),
        sa.CheckConstraint(
            "(status = 'needs_verification' AND verification_reason IS NOT NULL) "
            "OR status = 'pending_review'",
            name="ck_extraction_candidates_verification_reason",
        ),
        sa.ForeignKeyConstraint(
            ["extraction_job_id"], ["extraction_jobs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["space_id"], ["knowledge_spaces.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["source_version_id"], ["source_versions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "extraction_job_id", "ordinal", name="uq_extraction_candidates_ordinal"
        ),
    )
    op.create_index(
        "ix_extraction_candidates_space_status_created",
        "extraction_candidates",
        ["space_id", "status", "created_at"],
    )

    op.create_table(
        "candidate_evidence",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("section_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quote_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["extraction_candidates.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_version_id"], ["source_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["section_id"], ["source_sections.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("candidate_id", "section_id", name="uq_candidate_evidence_section"),
    )


def downgrade() -> None:
    op.drop_table("candidate_evidence")
    op.drop_index(
        "ix_extraction_candidates_space_status_created", table_name="extraction_candidates"
    )
    op.drop_table("extraction_candidates")
    op.drop_index("ix_extraction_jobs_space_status_created", table_name="extraction_jobs")
    op.drop_table("extraction_jobs")
