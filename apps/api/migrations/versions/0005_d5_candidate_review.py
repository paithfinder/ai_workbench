"""Add D5 candidate review and accepted knowledge records.

Revision ID: 0005_d5_candidate_review
Revises: 0004_d4_ai_extraction
Create Date: 2026-08-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_d5_candidate_review"
down_revision: str | None = "0004_d4_ai_extraction"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_SPACE_ID = "01982ba0-4f20-7000-8000-000000000001"
DEFAULT_DESTINATION_ID = "01982ba0-4f20-7000-8000-000000000002"
DEFAULT_DESTINATION_REVISION_ID = "01982ba0-4f20-7000-8000-000000000003"


def upgrade() -> None:
    op.drop_constraint(
        "ck_extraction_candidates_verification_reason",
        "extraction_candidates",
        type_="check",
    )
    op.drop_constraint(
        "ck_extraction_candidates_status", "extraction_candidates", type_="check"
    )
    op.add_column(
        "extraction_candidates",
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )
    op.add_column(
        "extraction_candidates", sa.Column("rejection_reason", sa.String(1000))
    )
    op.add_column(
        "extraction_candidates", sa.Column("reviewed_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "extraction_candidates",
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_check_constraint(
        "ck_extraction_candidates_version_positive",
        "extraction_candidates",
        "version > 0",
    )
    op.create_check_constraint(
        "ck_extraction_candidates_status",
        "extraction_candidates",
        "status IN ('pending_review','needs_verification','accepted','rejected')",
    )
    op.create_check_constraint(
        "ck_extraction_candidates_verification_reason",
        "extraction_candidates",
        "status <> 'needs_verification' OR "
        "(verification_reason IS NOT NULL AND btrim(verification_reason) <> '')",
    )
    op.create_check_constraint(
        "ck_extraction_candidates_rejection_reason",
        "extraction_candidates",
        "status = 'rejected' OR rejection_reason IS NULL",
    )

    op.create_table(
        "knowledge_nodes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True)),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("origin_candidate_id", postgresql.UUID(as_uuid=True)),
        sa.Column("current_revision_id", postgresql.UUID(as_uuid=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("kind IN ('document','point')", name="ck_knowledge_nodes_kind"),
        sa.CheckConstraint(
            "(kind = 'document' AND parent_id IS NULL AND origin_candidate_id IS NULL) OR "
            "(kind = 'point' AND parent_id IS NOT NULL AND origin_candidate_id IS NOT NULL)",
            name="ck_knowledge_nodes_parent_kind",
        ),
        sa.ForeignKeyConstraint(
            ["origin_candidate_id"],
            ["extraction_candidates.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_id", "space_id"],
            ["knowledge_nodes.id", "knowledge_nodes.space_id"],
            name="fk_knowledge_nodes_parent_space",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["space_id"], ["knowledge_spaces.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "space_id", name="uq_knowledge_nodes_id_space"),
        sa.UniqueConstraint("origin_candidate_id", name="uq_knowledge_nodes_origin_candidate"),
    )
    op.create_index(
        "ix_knowledge_nodes_space_parent_kind",
        "knowledge_nodes",
        ["space_id", "parent_id", "kind"],
    )

    op.create_table(
        "knowledge_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("node_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "tags",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
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
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("revision_number > 0", name="ck_knowledge_revisions_number_positive"),
        sa.ForeignKeyConstraint(
            ["node_id", "space_id"],
            ["knowledge_nodes.id", "knowledge_nodes.space_id"],
            name="fk_knowledge_revisions_node_space",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("node_id", "id", name="uq_knowledge_revisions_node_id"),
        sa.UniqueConstraint(
            "node_id", "revision_number", name="uq_knowledge_revisions_node_number"
        ),
    )
    op.create_foreign_key(
        "fk_knowledge_nodes_current_revision",
        "knowledge_nodes",
        "knowledge_revisions",
        ["id", "current_revision_id"],
        ["node_id", "id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )

    op.create_table(
        "knowledge_evidence",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parse_artifact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("section_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quote_hash", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("locator", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "quote_hash ~ '^[0-9a-f]{64}$'", name="ck_knowledge_evidence_quote_hash"
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_knowledge_evidence_content_hash"
        ),
        sa.ForeignKeyConstraint(
            ["parse_artifact_id", "source_version_id"],
            ["source_parse_artifacts.id", "source_parse_artifacts.source_version_id"],
            name="fk_knowledge_evidence_artifact_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["revision_id"], ["knowledge_revisions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["section_id"], ["source_sections.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["space_id"], ["knowledge_spaces.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("revision_id", "section_id", name="uq_knowledge_evidence_section"),
    )

    op.create_table(
        "review_cards",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("knowledge_node_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("knowledge_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("status IN ('active','retired')", name="ck_review_cards_status"),
        sa.ForeignKeyConstraint(
            ["knowledge_node_id", "space_id"],
            ["knowledge_nodes.id", "knowledge_nodes.space_id"],
            name="fk_review_cards_node_space",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_revision_id"], ["knowledge_revisions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("knowledge_node_id", name="uq_review_cards_node"),
    )

    op.create_table(
        "candidate_reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("reason", sa.String(1000)),
        sa.Column("before_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("after_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("from_status", sa.String(32), nullable=False),
        sa.Column("to_status", sa.String(32), nullable=False),
        sa.Column("from_version", sa.Integer(), nullable=False),
        sa.Column("to_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "action IN ('edit','accept','mark_needs_verification','reject')",
            name="ck_candidate_reviews_action",
        ),
        sa.CheckConstraint(
            "from_version > 0 AND to_version = from_version + 1",
            name="ck_candidate_reviews_versions",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["extraction_candidates.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["space_id"], ["knowledge_spaces.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("candidate_id", "to_version", name="uq_candidate_reviews_version"),
    )
    op.create_index(
        "ix_candidate_reviews_space_created", "candidate_reviews", ["space_id", "created_at"]
    )

    op.create_table(
        "candidate_review_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("expected_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "operation IN ('edit','accept','mark_needs_verification','reject')",
            name="ck_candidate_review_requests_operation",
        ),
        sa.CheckConstraint(
            "request_hash ~ '^[0-9a-f]{64}$'", name="ck_candidate_review_requests_hash"
        ),
        sa.CheckConstraint(
            "expected_version > 0", name="ck_candidate_review_requests_version_positive"
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["extraction_candidates.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["space_id"], ["knowledge_spaces.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "candidate_id", "idempotency_key", name="uq_candidate_review_requests_key"
        ),
    )

    op.create_table(
        "candidate_review_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_version", sa.Integer(), nullable=False),
        sa.Column("candidate_status", sa.String(32), nullable=False),
        sa.Column("knowledge_node_id", postgresql.UUID(as_uuid=True)),
        sa.Column("knowledge_revision_id", postgresql.UUID(as_uuid=True)),
        sa.Column("review_card_id", postgresql.UUID(as_uuid=True)),
        sa.Column(
            "evidence_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "candidate_version > 0", name="ck_candidate_review_results_version_positive"
        ),
        sa.CheckConstraint(
            "candidate_status IN ('pending_review','needs_verification','accepted','rejected')",
            name="ck_candidate_review_results_status",
        ),
        sa.CheckConstraint(
            "(candidate_status = 'accepted' AND knowledge_node_id IS NOT NULL "
            "AND knowledge_revision_id IS NOT NULL AND review_card_id IS NOT NULL) OR "
            "(candidate_status <> 'accepted' AND knowledge_node_id IS NULL "
            "AND knowledge_revision_id IS NULL AND review_card_id IS NULL)",
            name="ck_candidate_review_results_acceptance",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_review_id"], ["candidate_reviews.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_node_id"], ["knowledge_nodes.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_revision_id"], ["knowledge_revisions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["candidate_review_requests.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["review_card_id"], ["review_cards.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id", name="uq_candidate_review_results_request"),
    )

    op.create_foreign_key(
        "fk_extraction_candidates_destination",
        "extraction_candidates",
        "knowledge_nodes",
        ["suggested_destination_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.execute(
        f"""
        INSERT INTO knowledge_nodes (id, space_id, kind)
        VALUES (
            '{DEFAULT_DESTINATION_ID}'::uuid,
            '{DEFAULT_SPACE_ID}'::uuid,
            'document'
        )
        ON CONFLICT (id) DO NOTHING
        """
    )
    op.execute(
        f"""
        INSERT INTO knowledge_revisions (
            id, node_id, space_id, revision_number, title, body, actor
        )
        VALUES (
            '{DEFAULT_DESTINATION_REVISION_ID}'::uuid,
            '{DEFAULT_DESTINATION_ID}'::uuid,
            '{DEFAULT_SPACE_ID}'::uuid,
            1,
            '默认知识文档',
            '',
            'system'
        )
        ON CONFLICT (id) DO NOTHING
        """
    )
    op.execute(
        f"""
        UPDATE knowledge_nodes
        SET current_revision_id = '{DEFAULT_DESTINATION_REVISION_ID}'::uuid
        WHERE id = '{DEFAULT_DESTINATION_ID}'::uuid
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM extraction_candidates
                WHERE status IN ('accepted', 'rejected')
            ) THEN
                RAISE EXCEPTION
                    'Cannot downgrade D5 while terminal candidate reviews exist; '
                    'export/remove them first';
            END IF;
        END $$
        """
    )
    op.drop_constraint(
        "fk_extraction_candidates_destination", "extraction_candidates", type_="foreignkey"
    )
    op.drop_table("candidate_review_results")
    op.drop_table("candidate_review_requests")
    op.drop_index("ix_candidate_reviews_space_created", table_name="candidate_reviews")
    op.drop_table("candidate_reviews")
    op.drop_table("review_cards")
    op.drop_table("knowledge_evidence")
    op.drop_constraint(
        "fk_knowledge_nodes_current_revision", "knowledge_nodes", type_="foreignkey"
    )
    op.drop_table("knowledge_revisions")
    op.drop_index("ix_knowledge_nodes_space_parent_kind", table_name="knowledge_nodes")
    op.drop_table("knowledge_nodes")
    op.drop_constraint(
        "ck_extraction_candidates_rejection_reason",
        "extraction_candidates",
        type_="check",
    )
    op.drop_constraint(
        "ck_extraction_candidates_verification_reason",
        "extraction_candidates",
        type_="check",
    )
    op.drop_constraint(
        "ck_extraction_candidates_status", "extraction_candidates", type_="check"
    )
    op.drop_constraint(
        "ck_extraction_candidates_version_positive",
        "extraction_candidates",
        type_="check",
    )
    op.drop_column("extraction_candidates", "updated_at")
    op.drop_column("extraction_candidates", "reviewed_at")
    op.drop_column("extraction_candidates", "rejection_reason")
    op.drop_column("extraction_candidates", "version")
    op.create_check_constraint(
        "ck_extraction_candidates_status",
        "extraction_candidates",
        "status IN ('pending_review','needs_verification')",
    )
    op.create_check_constraint(
        "ck_extraction_candidates_verification_reason",
        "extraction_candidates",
        "(status = 'needs_verification' AND verification_reason IS NOT NULL) "
        "OR status = 'pending_review'",
    )
