"""Add N2.4 proposal application contract.

Revision ID: 0015_n24_proposal_apply
Revises: 0014_n23_comparison
Create Date: 2026-08-17
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0015_n24_proposal_apply"
down_revision: str | None = "0014_n23_comparison"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE knowledge_update_proposals "
        "ADD COLUMN create_kind varchar(32)"
    )
    op.execute(
        "ALTER TABLE knowledge_update_proposals "
        "ADD CONSTRAINT ck_knowledge_update_proposals_create_kind "
        "CHECK (create_kind IS NULL OR "
        "(action = 'create' AND create_kind IN ('folder','document')))"
    )
    op.execute(
        "ALTER TABLE knowledge_update_proposal_requests "
        "DROP CONSTRAINT ck_knowledge_update_proposal_requests_operation"
    )
    op.execute(
        "ALTER TABLE knowledge_update_proposal_requests "
        "ADD CONSTRAINT ck_knowledge_update_proposal_requests_operation "
        "CHECK (operation IN "
        "('create','edit','submit','approve','apply','reject','supersede'))"
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM knowledge_update_proposal_requests WHERE operation = 'apply'
          ) OR EXISTS (
            SELECT 1 FROM knowledge_update_proposals WHERE create_kind IS NOT NULL
          ) THEN
            RAISE EXCEPTION
              'cannot downgrade N2.4 proposal apply schema while apply records or create_kind exist';
          END IF;
        END
        $$
        """
    )
    op.execute(
        "ALTER TABLE knowledge_update_proposal_requests "
        "DROP CONSTRAINT ck_knowledge_update_proposal_requests_operation"
    )
    op.execute(
        "ALTER TABLE knowledge_update_proposal_requests "
        "ADD CONSTRAINT ck_knowledge_update_proposal_requests_operation "
        "CHECK (operation IN ('create','edit','submit','approve','reject','supersede'))"
    )
    op.execute(
        "ALTER TABLE knowledge_update_proposals "
        "DROP CONSTRAINT ck_knowledge_update_proposals_create_kind"
    )
    op.execute("ALTER TABLE knowledge_update_proposals DROP COLUMN create_kind")
