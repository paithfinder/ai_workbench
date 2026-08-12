"""Add atomic direct knowledge folder imports."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_direct_knowledge_import"
down_revision: str | None = "0009_d7_cjk_fts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "knowledge_import_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("root_node_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entry_count", sa.Integer(), nullable=False),
        sa.Column("folder_count", sa.Integer(), nullable=False),
        sa.Column("document_count", sa.Integer(), nullable=False),
        sa.Column("total_body_utf8_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "request_hash ~ '^[0-9a-f]{64}$'",
            name="ck_knowledge_import_requests_hash",
        ),
        sa.CheckConstraint("entry_count > 0", name="ck_knowledge_import_requests_entry_count"),
        sa.CheckConstraint("folder_count > 0", name="ck_knowledge_import_requests_folder_count"),
        sa.CheckConstraint(
            "document_count > 0", name="ck_knowledge_import_requests_document_count"
        ),
        sa.CheckConstraint(
            "entry_count = folder_count + document_count",
            name="ck_knowledge_import_requests_counts",
        ),
        sa.CheckConstraint(
            "total_body_utf8_bytes >= 0",
            name="ck_knowledge_import_requests_body_bytes",
        ),
        sa.ForeignKeyConstraint(
            ["space_id"], ["knowledge_spaces.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["root_node_id"], ["knowledge_nodes.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "space_id", "idempotency_key", name="uq_knowledge_import_requests_key"
        ),
        sa.UniqueConstraint(
            "root_node_id", name="uq_knowledge_import_requests_root_node"
        ),
    )
    op.create_table(
        "knowledge_import_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("relative_path", sa.String(4000), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("node_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("body_utf8_bytes", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("ordinal >= 0", name="ck_knowledge_import_items_ordinal"),
        sa.CheckConstraint(
            "kind IN ('folder','document')", name="ck_knowledge_import_items_kind"
        ),
        sa.CheckConstraint(
            "body_utf8_bytes >= 0", name="ck_knowledge_import_items_body_bytes"
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_knowledge_import_items_hash",
        ),
        sa.ForeignKeyConstraint(
            ["request_id"], ["knowledge_import_requests.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["node_id"], ["knowledge_nodes.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["revision_id"], ["knowledge_revisions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "request_id", "ordinal", name="uq_knowledge_import_items_ordinal"
        ),
        sa.UniqueConstraint(
            "request_id", "relative_path", name="uq_knowledge_import_items_path"
        ),
        sa.UniqueConstraint(
            "request_id", "node_id", name="uq_knowledge_import_items_node"
        ),
    )


def downgrade() -> None:
    op.drop_table("knowledge_import_items")
    op.drop_table("knowledge_import_requests")
