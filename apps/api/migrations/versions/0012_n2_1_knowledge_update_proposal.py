"""Add N2.1 knowledge update proposal foundation.

Revision ID: 0012_n2_1_knowledge_update_proposal
Revises: 0011_d8_hybrid_qa
Create Date: 2026-08-14
"""

# SQL statements are kept readable as PostgreSQL blocks.
# ruff: noqa: E501

from collections.abc import Sequence

from alembic import op

revision: str = "0012_n21_proposal"
down_revision: str | None = "0011_d8_hybrid_qa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _execute(*statements: str) -> None:
    for statement in statements:
        op.execute(statement)


def upgrade() -> None:
    _execute(
        "ALTER TABLE sources ADD CONSTRAINT uq_sources_id_space UNIQUE (id,space_id)",
        "ALTER TABLE source_sections ADD CONSTRAINT uq_source_sections_id_space UNIQUE (id,space_id)",
        "ALTER TABLE knowledge_revisions ADD CONSTRAINT uq_knowledge_revisions_id_space UNIQUE (id,space_id)",
        """
        CREATE TABLE research_runs (
          id uuid PRIMARY KEY,
          space_id uuid NOT NULL REFERENCES knowledge_spaces(id) ON DELETE RESTRICT,
          status varchar(32) NOT NULL DEFAULT 'draft',
          origin varchar(100) NOT NULL DEFAULT 'manual',
          model varchar(200),
          prompt_version varchar(100),
          tool_policy_version varchar(100),
          started_at timestamptz,
          completed_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT uq_research_runs_id_space UNIQUE (id, space_id),
          CONSTRAINT ck_research_runs_status CHECK (status IN ('draft','completed','failed'))
        )
        """,
        "CREATE INDEX ix_research_runs_space_status_created ON research_runs(space_id,status,created_at)",
        """
        CREATE TABLE knowledge_update_proposals (
          id uuid PRIMARY KEY,
          space_id uuid NOT NULL REFERENCES knowledge_spaces(id) ON DELETE RESTRICT,
          research_run_id uuid,
          target_node_id uuid,
          target_revision_id uuid,
          target_node_version integer,
          action varchar(32) NOT NULL,
          status varchar(32) NOT NULL DEFAULT 'draft',
          version integer NOT NULL DEFAULT 1,
          suggested_title varchar(500),
          suggested_body text,
          suggested_tags jsonb NOT NULL DEFAULT '[]'::jsonb,
          conditions jsonb NOT NULL DEFAULT '[]'::jsonb,
          exceptions jsonb NOT NULL DEFAULT '[]'::jsonb,
          comparison_summary text,
          confidence double precision,
          uncertainty_reason varchar(2000),
          superseded_by_proposal_id uuid,
          submitted_at timestamptz,
          reviewed_at timestamptz,
          applied_at timestamptz,
          superseded_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT uq_knowledge_update_proposals_id_space UNIQUE (id,space_id),
          CONSTRAINT fk_knowledge_update_proposals_target_node_space FOREIGN KEY (target_node_id,space_id)
            REFERENCES knowledge_nodes(id,space_id) ON DELETE RESTRICT,
          CONSTRAINT fk_knowledge_update_proposals_target_revision_space FOREIGN KEY (target_revision_id,space_id)
            REFERENCES knowledge_revisions(id,space_id) ON DELETE RESTRICT,
          CONSTRAINT fk_knowledge_update_proposals_target_revision_node FOREIGN KEY (target_node_id,target_revision_id)
            REFERENCES knowledge_revisions(node_id,id) ON DELETE RESTRICT,
          CONSTRAINT fk_knowledge_update_proposals_research_run_space FOREIGN KEY (research_run_id,space_id)
            REFERENCES research_runs(id,space_id) ON DELETE RESTRICT,
          CONSTRAINT ck_knowledge_update_proposals_action CHECK
            (action IN ('create','revise','supersede','merge_suggestion','mark_review_recommended')),
          CONSTRAINT ck_knowledge_update_proposals_status CHECK
            (status IN ('draft','pending_review','approved','rejected','applied','superseded')),
          CONSTRAINT ck_knowledge_update_proposals_version_positive CHECK (version > 0),
          CONSTRAINT ck_knowledge_update_proposals_confidence CHECK
            (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
          CONSTRAINT ck_knowledge_update_proposals_title CHECK
            (suggested_title IS NULL OR length(btrim(suggested_title)) > 0)
        )
        """,
        "CREATE INDEX ix_knowledge_update_proposals_space_status_created ON knowledge_update_proposals(space_id,status,created_at)",
        "CREATE INDEX ix_knowledge_update_proposals_space_target_status ON knowledge_update_proposals(space_id,target_node_id,status)",
        """
        CREATE TABLE knowledge_update_proposal_evidence (
          id uuid PRIMARY KEY,
          proposal_id uuid NOT NULL,
          space_id uuid NOT NULL,
          role varchar(32) NOT NULL,
          source_id uuid NOT NULL,
          source_version_id uuid NOT NULL,
          parse_artifact_id uuid NOT NULL,
          section_id uuid NOT NULL,
          knowledge_revision_id uuid,
          frozen_quote text NOT NULL,
          quote_hash varchar(64) NOT NULL,
          content_hash varchar(64) NOT NULL,
          locator jsonb NOT NULL,
          ordinal integer NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT fk_knowledge_update_proposal_evidence_proposal_space FOREIGN KEY (proposal_id,space_id)
            REFERENCES knowledge_update_proposals(id,space_id) ON DELETE CASCADE,
          CONSTRAINT fk_knowledge_update_proposal_evidence_source_space FOREIGN KEY (source_id,space_id)
            REFERENCES sources(id,space_id) ON DELETE RESTRICT,
          CONSTRAINT fk_knowledge_update_proposal_evidence_version_source FOREIGN KEY (source_version_id,source_id)
            REFERENCES source_versions(id,source_id) ON DELETE RESTRICT,
          CONSTRAINT fk_knowledge_update_proposal_evidence_section_space FOREIGN KEY (section_id,space_id)
            REFERENCES source_sections(id,space_id) ON DELETE RESTRICT,
          CONSTRAINT fk_knowledge_update_proposal_evidence_section_artifact_version FOREIGN KEY (section_id,parse_artifact_id,source_version_id)
            REFERENCES source_sections(id,parse_artifact_id,source_version_id) ON DELETE RESTRICT,
          CONSTRAINT fk_knowledge_update_proposal_evidence_revision_space FOREIGN KEY (knowledge_revision_id,space_id)
            REFERENCES knowledge_revisions(id,space_id) ON DELETE RESTRICT,
          CONSTRAINT fk_knowledge_update_proposal_evidence_artifact_version FOREIGN KEY (parse_artifact_id,source_version_id)
            REFERENCES source_parse_artifacts(id,source_version_id) ON DELETE RESTRICT,
          CONSTRAINT ck_knowledge_update_proposal_evidence_role CHECK
            (role IN ('new_support','existing_support','conflict','outdated','contextual')),
          CONSTRAINT ck_knowledge_update_proposal_evidence_ordinal CHECK (ordinal >= 0),
          CONSTRAINT ck_knowledge_update_proposal_evidence_hashes CHECK
            (quote_hash ~ '^[0-9a-f]{64}$' AND content_hash ~ '^[0-9a-f]{64}$'),
          CONSTRAINT ck_knowledge_update_proposal_evidence_quote CHECK (length(frozen_quote) > 0),
          CONSTRAINT uq_knowledge_update_proposal_evidence_ordinal UNIQUE (proposal_id,ordinal),
          CONSTRAINT uq_knowledge_update_proposal_evidence_section_role UNIQUE (proposal_id,section_id,role)
        )
        """,
        """
        CREATE TABLE knowledge_update_proposal_requests (
          id uuid PRIMARY KEY,
          space_id uuid NOT NULL REFERENCES knowledge_spaces(id) ON DELETE RESTRICT,
          proposal_id uuid REFERENCES knowledge_update_proposals(id) ON DELETE RESTRICT,
          idempotency_key varchar(255) NOT NULL,
          operation varchar(32) NOT NULL,
          request_hash varchar(64) NOT NULL,
          expected_version integer,
          created_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT fk_knowledge_update_proposal_requests_proposal_space FOREIGN KEY (proposal_id,space_id)
            REFERENCES knowledge_update_proposals(id,space_id) ON DELETE RESTRICT,
          CONSTRAINT uq_proposal_requests_audit_chain UNIQUE (id,proposal_id,space_id),
          CONSTRAINT uq_knowledge_update_proposal_requests_key UNIQUE (space_id,idempotency_key),
          CONSTRAINT ck_knowledge_update_proposal_requests_operation CHECK
            (operation IN ('create','edit','submit','approve','reject','supersede')),
          CONSTRAINT ck_knowledge_update_proposal_requests_hash CHECK (request_hash ~ '^[0-9a-f]{64}$'),
          CONSTRAINT ck_knowledge_update_proposal_requests_version CHECK
            (expected_version IS NULL OR expected_version > 0)
        )
        """,
        """
        CREATE TABLE knowledge_update_proposal_transitions (
          id uuid PRIMARY KEY,
          proposal_id uuid NOT NULL,
          space_id uuid NOT NULL,
          request_id uuid NOT NULL REFERENCES knowledge_update_proposal_requests(id) ON DELETE RESTRICT,
          operation varchar(32) NOT NULL,
          actor varchar(255) NOT NULL,
          reason varchar(1000),
          before_snapshot jsonb NOT NULL,
          after_snapshot jsonb NOT NULL,
          from_status varchar(32) NOT NULL,
          to_status varchar(32) NOT NULL,
          from_version integer NOT NULL,
          to_version integer NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT fk_knowledge_update_proposal_transitions_proposal_space FOREIGN KEY (proposal_id,space_id)
            REFERENCES knowledge_update_proposals(id,space_id) ON DELETE RESTRICT,
          CONSTRAINT fk_knowledge_update_proposal_transitions_request_chain FOREIGN KEY (request_id,proposal_id,space_id)
            REFERENCES knowledge_update_proposal_requests(id,proposal_id,space_id) ON DELETE RESTRICT,
          CONSTRAINT uq_proposal_transitions_audit_chain UNIQUE (id,proposal_id,space_id),
          CONSTRAINT uq_knowledge_update_proposal_transitions_version UNIQUE (proposal_id,to_version),
          CONSTRAINT uq_knowledge_update_proposal_transitions_request UNIQUE (request_id),
          CONSTRAINT ck_knowledge_update_proposal_transitions_versions CHECK
            (from_version >= 0 AND to_version = from_version + 1)
        )
        """,
        """
        CREATE TABLE knowledge_update_proposal_results (
          id uuid PRIMARY KEY,
          request_id uuid NOT NULL,
          proposal_id uuid NOT NULL,
          transition_id uuid NOT NULL,
          space_id uuid NOT NULL,
          proposal_version integer NOT NULL,
          proposal_status varchar(32) NOT NULL,
          snapshot jsonb NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT fk_knowledge_update_proposal_results_request_chain FOREIGN KEY (request_id,proposal_id,space_id)
            REFERENCES knowledge_update_proposal_requests(id,proposal_id,space_id) ON DELETE CASCADE,
          CONSTRAINT fk_knowledge_update_proposal_results_transition_chain FOREIGN KEY (transition_id,proposal_id,space_id)
            REFERENCES knowledge_update_proposal_transitions(id,proposal_id,space_id) ON DELETE RESTRICT,
          CONSTRAINT uq_knowledge_update_proposal_results_request UNIQUE (request_id),
          CONSTRAINT ck_knowledge_update_proposal_results_version CHECK (proposal_version > 0)
        )
        """,
    )


def downgrade() -> None:
    _execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM knowledge_update_proposals LIMIT 1)
             OR EXISTS (SELECT 1 FROM research_runs LIMIT 1) THEN
            RAISE EXCEPTION 'cannot downgrade N2.1 proposal schema while proposal audit records exist';
          END IF;
        END
        $$
        """,
        "DROP TABLE knowledge_update_proposal_results",
        "DROP TABLE knowledge_update_proposal_transitions",
        "DROP TABLE knowledge_update_proposal_requests",
        "DROP TABLE knowledge_update_proposal_evidence",
        "DROP TABLE knowledge_update_proposals",
        "DROP TABLE research_runs",
        "ALTER TABLE knowledge_revisions DROP CONSTRAINT IF EXISTS uq_knowledge_revisions_id_space",
        "ALTER TABLE source_sections DROP CONSTRAINT IF EXISTS uq_source_sections_id_space",
        "ALTER TABLE sources DROP CONSTRAINT IF EXISTS uq_sources_id_space",
    )
