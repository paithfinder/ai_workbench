"""Add N2.3 proposal comparison audit persistence.

Revision ID: 0014_n23_comparison
Revises: 0013_n22_search
Create Date: 2026-08-16
"""

# SQL statements are kept readable as PostgreSQL blocks.
# ruff: noqa: E501

from collections.abc import Sequence

from alembic import op

revision: str = "0014_n23_comparison"
down_revision: str | None = "0013_n22_search"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _execute(*statements: str) -> None:
    for statement in statements:
        op.execute(statement)


def upgrade() -> None:
    _execute(
        """
        CREATE TABLE proposal_comparison_runs (
          id uuid PRIMARY KEY,
          space_id uuid NOT NULL REFERENCES knowledge_spaces(id) ON DELETE RESTRICT,
          research_run_id uuid NOT NULL,
          selection_batch_id uuid NOT NULL,
          idempotency_key varchar(255) NOT NULL,
          request_hash varchar(64) NOT NULL,
          scope_node_id uuid NOT NULL,
          include_descendants boolean NOT NULL,
          scope_snapshot jsonb NOT NULL,
          scope_snapshot_hash varchar(64) NOT NULL,
          index_config_version varchar(100),
          retrieval_config jsonb NOT NULL,
          reranker_config jsonb NOT NULL,
          context_config jsonb NOT NULL,
          ai_provider varchar(100) NOT NULL,
          ai_model varchar(200) NOT NULL,
          prompt_version varchar(100) NOT NULL,
          schema_version varchar(100) NOT NULL,
          status varchar(32) NOT NULL DEFAULT 'processing',
          comparison_kind varchar(32),
          proposal_id uuid,
          failure_code varchar(100),
          failure_message varchar(2000),
          input_tokens integer NOT NULL DEFAULT 0,
          output_tokens integer NOT NULL DEFAULT 0,
          timings_ms jsonb NOT NULL DEFAULT '{}'::jsonb,
          warnings jsonb NOT NULL DEFAULT '[]'::jsonb,
          provider_request_id varchar(255),
          started_at timestamptz NOT NULL DEFAULT now(),
          completed_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT uq_proposal_comparison_runs_space_key UNIQUE (space_id,idempotency_key),
          CONSTRAINT uq_proposal_comparison_runs_id_space UNIQUE (id,space_id),
          CONSTRAINT uq_proposal_comparison_runs_proposal UNIQUE (proposal_id),
          CONSTRAINT fk_proposal_comparison_runs_research_run_space FOREIGN KEY (research_run_id,space_id)
            REFERENCES research_runs(id,space_id) ON DELETE RESTRICT,
          CONSTRAINT fk_proposal_comparison_runs_selection_batch_chain FOREIGN KEY
            (selection_batch_id,research_run_id,space_id)
            REFERENCES research_source_selection_batches(id,research_run_id,space_id) ON DELETE RESTRICT,
          CONSTRAINT fk_proposal_comparison_runs_scope_space FOREIGN KEY (scope_node_id,space_id)
            REFERENCES knowledge_nodes(id,space_id) ON DELETE RESTRICT,
          CONSTRAINT fk_proposal_comparison_runs_proposal_space FOREIGN KEY (proposal_id,space_id)
            REFERENCES knowledge_update_proposals(id,space_id) ON DELETE RESTRICT,
          CONSTRAINT ck_proposal_comparison_runs_request_hash CHECK (request_hash ~ '^[0-9a-f]{64}$'),
          CONSTRAINT ck_proposal_comparison_runs_scope_hash CHECK (scope_snapshot_hash ~ '^[0-9a-f]{64}$'),
          CONSTRAINT ck_proposal_comparison_runs_status CHECK
            (status IN ('processing','completed','no_evidence','failed')),
          CONSTRAINT ck_proposal_comparison_runs_kind CHECK
            (comparison_kind IS NULL OR comparison_kind IN
              ('support','supplement','conflict','outdated','duplicate','no_evidence')),
          CONSTRAINT ck_proposal_comparison_runs_tokens CHECK
            (input_tokens >= 0 AND output_tokens >= 0),
          CONSTRAINT ck_proposal_comparison_runs_json_shapes CHECK
            (jsonb_typeof(scope_snapshot) = 'object'
             AND jsonb_typeof(retrieval_config) = 'object'
             AND jsonb_typeof(reranker_config) = 'object'
             AND jsonb_typeof(context_config) = 'object'
             AND jsonb_typeof(timings_ms) = 'object'
             AND jsonb_typeof(warnings) = 'array'),
          CONSTRAINT ck_proposal_comparison_runs_result_shape CHECK
            ((status = 'processing' AND comparison_kind IS NULL AND proposal_id IS NULL
              AND failure_code IS NULL AND failure_message IS NULL AND completed_at IS NULL)
             OR (status = 'completed' AND comparison_kind IS NOT NULL
                 AND comparison_kind <> 'no_evidence' AND proposal_id IS NOT NULL
                 AND failure_code IS NULL AND completed_at IS NOT NULL)
             OR (status = 'no_evidence' AND comparison_kind = 'no_evidence'
                 AND proposal_id IS NULL AND failure_code IS NULL AND completed_at IS NOT NULL)
             OR (status = 'failed' AND comparison_kind IS NULL AND proposal_id IS NULL
                 AND failure_code IS NOT NULL AND completed_at IS NOT NULL))
        )
        """,
        "CREATE INDEX ix_proposal_comparison_runs_space_created ON proposal_comparison_runs(space_id,created_at)",
        "CREATE INDEX ix_proposal_comparison_runs_space_status_created ON proposal_comparison_runs(space_id,status,created_at)",
        "CREATE INDEX ix_proposal_comparison_runs_research_batch ON proposal_comparison_runs(research_run_id,selection_batch_id)",
        """
        CREATE TABLE proposal_comparison_candidates (
          id uuid PRIMARY KEY,
          comparison_run_id uuid NOT NULL,
          space_id uuid NOT NULL,
          candidate_id varchar(64) NOT NULL,
          ordinal integer NOT NULL,
          candidate_kind varchar(32) NOT NULL,
          selection_id uuid,
          source_id uuid NOT NULL,
          source_version_id uuid NOT NULL,
          parse_artifact_id uuid NOT NULL,
          section_id uuid NOT NULL,
          knowledge_node_id uuid,
          knowledge_revision_id uuid,
          knowledge_evidence_id uuid,
          title varchar(1000),
          frozen_quote text NOT NULL,
          quote_hash varchar(64) NOT NULL,
          content_hash varchar(64) NOT NULL,
          locator jsonb NOT NULL,
          keyword_rank integer,
          keyword_score double precision,
          vector_rank integer,
          vector_score double precision,
          rrf_rank integer,
          rrf_score double precision,
          rerank_rank integer,
          rerank_score double precision,
          included_in_context boolean NOT NULL DEFAULT false,
          context_ordinal integer,
          created_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT fk_proposal_comparison_candidates_run_space FOREIGN KEY (comparison_run_id,space_id)
            REFERENCES proposal_comparison_runs(id,space_id) ON DELETE CASCADE,
          CONSTRAINT fk_proposal_comparison_candidates_selection FOREIGN KEY (selection_id)
            REFERENCES research_source_selections(id) ON DELETE RESTRICT,
          CONSTRAINT fk_proposal_comparison_candidates_source_space FOREIGN KEY (source_id,space_id)
            REFERENCES sources(id,space_id) ON DELETE RESTRICT,
          CONSTRAINT fk_proposal_comparison_candidates_version_source FOREIGN KEY (source_version_id,source_id)
            REFERENCES source_versions(id,source_id) ON DELETE RESTRICT,
          CONSTRAINT fk_proposal_comparison_candidates_artifact_version FOREIGN KEY (parse_artifact_id,source_version_id)
            REFERENCES source_parse_artifacts(id,source_version_id) ON DELETE RESTRICT,
          CONSTRAINT fk_proposal_comparison_candidates_section_artifact_version FOREIGN KEY
            (section_id,parse_artifact_id,source_version_id)
            REFERENCES source_sections(id,parse_artifact_id,source_version_id) ON DELETE RESTRICT,
          CONSTRAINT fk_proposal_comparison_candidates_node_space FOREIGN KEY (knowledge_node_id,space_id)
            REFERENCES knowledge_nodes(id,space_id) ON DELETE RESTRICT,
          CONSTRAINT fk_proposal_comparison_candidates_revision_space FOREIGN KEY (knowledge_revision_id,space_id)
            REFERENCES knowledge_revisions(id,space_id) ON DELETE RESTRICT,
          CONSTRAINT fk_proposal_comparison_candidates_evidence FOREIGN KEY (knowledge_evidence_id)
            REFERENCES knowledge_evidence(id) ON DELETE RESTRICT,
          CONSTRAINT uq_proposal_comparison_candidates_ordinal UNIQUE (comparison_run_id,ordinal),
          CONSTRAINT uq_proposal_comparison_candidates_id UNIQUE (comparison_run_id,candidate_id),
          CONSTRAINT ck_proposal_comparison_candidates_ordinal CHECK (ordinal >= 0),
          CONSTRAINT ck_proposal_comparison_candidates_kind CHECK
            (candidate_kind IN ('new_source','existing_evidence')),
          CONSTRAINT ck_proposal_comparison_candidates_hashes CHECK
            (content_hash ~ '^[0-9a-f]{64}$' AND quote_hash ~ '^[0-9a-f]{64}$'),
          CONSTRAINT ck_proposal_comparison_candidates_quote CHECK (length(btrim(frozen_quote)) > 0),
          CONSTRAINT ck_proposal_comparison_candidates_ranks CHECK
            ((keyword_rank IS NULL OR keyword_rank > 0)
             AND (vector_rank IS NULL OR vector_rank > 0)
             AND (rrf_rank IS NULL OR rrf_rank > 0)
             AND (rerank_rank IS NULL OR rerank_rank > 0)),
          CONSTRAINT ck_proposal_comparison_candidates_scores CHECK
            ((rrf_score IS NULL OR rrf_score NOT IN
                ('NaN'::double precision, 'Infinity'::double precision, '-Infinity'::double precision))
             AND (keyword_score IS NULL OR keyword_score NOT IN
                ('NaN'::double precision, 'Infinity'::double precision, '-Infinity'::double precision))
             AND (vector_score IS NULL OR vector_score NOT IN
                ('NaN'::double precision, 'Infinity'::double precision, '-Infinity'::double precision))
             AND (rerank_score IS NULL OR rerank_score NOT IN
                ('NaN'::double precision, 'Infinity'::double precision, '-Infinity'::double precision))),
          CONSTRAINT ck_proposal_comparison_candidates_context CHECK
            ((included_in_context AND context_ordinal IS NOT NULL AND context_ordinal >= 0)
             OR (NOT included_in_context AND context_ordinal IS NULL)),
          CONSTRAINT ck_proposal_comparison_candidates_identity_shape CHECK
            ((candidate_kind = 'new_source' AND selection_id IS NOT NULL
              AND source_id IS NOT NULL AND source_version_id IS NOT NULL
              AND parse_artifact_id IS NOT NULL AND section_id IS NOT NULL
              AND knowledge_node_id IS NULL AND knowledge_revision_id IS NULL
              AND knowledge_evidence_id IS NULL)
             OR (candidate_kind = 'existing_evidence' AND selection_id IS NULL
                 AND source_id IS NOT NULL AND source_version_id IS NOT NULL
                 AND parse_artifact_id IS NOT NULL AND section_id IS NOT NULL
                 AND knowledge_node_id IS NOT NULL AND knowledge_revision_id IS NOT NULL
                 AND knowledge_evidence_id IS NOT NULL))
        )
        """,
        "CREATE INDEX ix_proposal_comparison_candidates_run_ordinal ON proposal_comparison_candidates(comparison_run_id,ordinal)",
    )


def downgrade() -> None:
    _execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM proposal_comparison_runs LIMIT 1)
             OR EXISTS (SELECT 1 FROM proposal_comparison_candidates LIMIT 1) THEN
            RAISE EXCEPTION 'cannot downgrade N2.3 comparison schema while comparison audit records exist';
          END IF;
        END
        $$
        """,
        "DROP TABLE proposal_comparison_candidates",
        "DROP TABLE proposal_comparison_runs",
    )
