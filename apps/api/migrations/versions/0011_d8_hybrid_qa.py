"""Add D8 auditable hybrid QA turns and citations."""

from collections.abc import Sequence

from alembic import op

revision: str = "0011_d8_hybrid_qa"
down_revision: str | None = "0010_direct_knowledge_import"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _execute(*statements: str) -> None:
    for statement in statements:
        op.execute(statement)


def upgrade() -> None:
    _execute(
        """
        CREATE TABLE qa_turns (
          id uuid PRIMARY KEY,
          space_id uuid NOT NULL REFERENCES knowledge_spaces(id) ON DELETE RESTRICT,
          idempotency_key varchar(255) NOT NULL,
          request_hash varchar(64) NOT NULL,
          question text NOT NULL,
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
          status varchar(32) NOT NULL,
          answer text,
          claims jsonb NOT NULL DEFAULT '[]'::jsonb,
          abstain_code varchar(100),
          error_code varchar(100),
          error_message varchar(2000),
          input_tokens integer NOT NULL DEFAULT 0,
          output_tokens integer NOT NULL DEFAULT 0,
          timings_ms jsonb NOT NULL DEFAULT '{}'::jsonb,
          warnings jsonb NOT NULL DEFAULT '[]'::jsonb,
          provider_request_id varchar(255),
          started_at timestamptz NOT NULL DEFAULT now(),
          completed_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT uq_qa_turns_space_key UNIQUE (space_id,idempotency_key),
          CONSTRAINT uq_qa_turns_id_space UNIQUE (id,space_id),
          CONSTRAINT fk_qa_turns_scope_space FOREIGN KEY (scope_node_id,space_id)
            REFERENCES knowledge_nodes(id,space_id) ON DELETE RESTRICT,
          CONSTRAINT ck_qa_turns_request_hash CHECK (request_hash ~ '^[0-9a-f]{64}$'),
          CONSTRAINT ck_qa_turns_scope_hash CHECK
            (scope_snapshot_hash ~ '^[0-9a-f]{64}$'),
          CONSTRAINT ck_qa_turns_question CHECK (length(btrim(question)) > 0),
          CONSTRAINT ck_qa_turns_status CHECK
            (status IN ('processing','answered','abstained','failed')),
          CONSTRAINT ck_qa_turns_tokens CHECK (input_tokens >= 0 AND output_tokens >= 0),
          CONSTRAINT ck_qa_turns_claims_array CHECK (jsonb_typeof(claims) = 'array'),
          CONSTRAINT ck_qa_turns_claims_shape CHECK (
            (status = 'answered' AND jsonb_array_length(claims) > 0)
            OR (status <> 'answered' AND jsonb_array_length(claims) = 0)
          ),
          CONSTRAINT ck_qa_turns_result_shape CHECK (
            (status = 'processing' AND answer IS NULL AND abstain_code IS NULL
              AND error_code IS NULL AND error_message IS NULL AND completed_at IS NULL)
            OR (status = 'answered' AND answer IS NOT NULL AND length(btrim(answer)) > 0
              AND abstain_code IS NULL AND error_code IS NULL AND completed_at IS NOT NULL)
            OR (status = 'abstained' AND answer IS NULL AND abstain_code IS NOT NULL
              AND error_code IS NULL AND completed_at IS NOT NULL)
            OR (status = 'failed' AND answer IS NULL AND abstain_code IS NULL
              AND error_code IS NOT NULL AND completed_at IS NOT NULL)
          )
        )
        """,
        "CREATE INDEX ix_qa_turns_space_created ON qa_turns(space_id,created_at DESC)",
        "CREATE INDEX ix_qa_turns_space_status ON qa_turns(space_id,status,created_at DESC)",
        """
        CREATE TABLE qa_retrieval_hits (
          id uuid PRIMARY KEY,
          turn_id uuid NOT NULL REFERENCES qa_turns(id) ON DELETE CASCADE,
          chunk_id uuid NOT NULL,
          corpus_kind varchar(32) NOT NULL,
          content_identity varchar(64) NOT NULL,
          content_hash varchar(64) NOT NULL,
          title varchar(1000),
          chunk_text text NOT NULL,
          path text NOT NULL,
          heading_path jsonb NOT NULL DEFAULT '[]'::jsonb,
          locator jsonb,
          knowledge_node_id uuid,
          knowledge_revision_id uuid,
          source_id uuid,
          source_version_id uuid,
          parse_artifact_id uuid,
          section_id uuid,
          keyword_rank integer,
          keyword_score double precision,
          vector_rank integer,
          vector_score double precision,
          rrf_rank integer NOT NULL,
          rrf_score double precision NOT NULL,
          rerank_rank integer,
          rerank_score double precision,
          included_in_context boolean NOT NULL DEFAULT false,
          context_ordinal integer,
          evidence_id varchar(32),
          created_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT uq_qa_retrieval_hits_turn_chunk UNIQUE (turn_id,chunk_id),
          CONSTRAINT uq_qa_retrieval_hits_id_turn UNIQUE (id,turn_id),
          CONSTRAINT uq_qa_retrieval_hits_id_turn_evidence UNIQUE (id,turn_id,evidence_id),
          CONSTRAINT uq_qa_retrieval_hits_turn_evidence UNIQUE (turn_id,evidence_id),
          CONSTRAINT ck_qa_retrieval_hits_corpus CHECK
            (corpus_kind IN ('source_evidence','confirmed_knowledge')),
          CONSTRAINT ck_qa_retrieval_hits_hashes CHECK
            (content_identity ~ '^[0-9a-f]{64}$' AND content_hash ~ '^[0-9a-f]{64}$'),
          CONSTRAINT ck_qa_retrieval_hits_text CHECK (length(chunk_text) > 0),
          CONSTRAINT ck_qa_retrieval_hits_ranks CHECK (
            (keyword_rank IS NULL OR keyword_rank > 0)
            AND (vector_rank IS NULL OR vector_rank > 0)
            AND rrf_rank > 0
            AND (rerank_rank IS NULL OR rerank_rank > 0)
          ),
          CONSTRAINT ck_qa_retrieval_hits_scores CHECK (
            rrf_score NOT IN ('NaN'::double precision, 'Infinity'::double precision,
              '-Infinity'::double precision)
            AND (keyword_score IS NULL OR keyword_score NOT IN
              ('NaN'::double precision, 'Infinity'::double precision,
                '-Infinity'::double precision))
            AND (vector_score IS NULL OR vector_score NOT IN
              ('NaN'::double precision, 'Infinity'::double precision,
                '-Infinity'::double precision))
            AND (rerank_score IS NULL OR rerank_score NOT IN
              ('NaN'::double precision, 'Infinity'::double precision,
                '-Infinity'::double precision))
          ),
          CONSTRAINT ck_qa_retrieval_hits_context CHECK (
            (included_in_context AND context_ordinal IS NOT NULL
              AND context_ordinal >= 0 AND evidence_id IS NOT NULL)
            OR (NOT included_in_context AND context_ordinal IS NULL AND evidence_id IS NULL)
          ),
          CONSTRAINT ck_qa_retrieval_hits_identity_shape CHECK (
            (corpus_kind = 'source_evidence' AND source_id IS NOT NULL
              AND source_version_id IS NOT NULL AND parse_artifact_id IS NOT NULL
              AND section_id IS NOT NULL AND knowledge_node_id IS NULL
              AND knowledge_revision_id IS NULL)
            OR (corpus_kind = 'confirmed_knowledge' AND knowledge_node_id IS NOT NULL
              AND knowledge_revision_id IS NOT NULL AND source_id IS NULL
              AND source_version_id IS NULL AND parse_artifact_id IS NULL
              AND section_id IS NULL)
          )
        )
        """,
        "CREATE INDEX ix_qa_retrieval_hits_turn_rank ON qa_retrieval_hits(turn_id,rrf_rank)",
        """
        CREATE TABLE qa_citations (
          id uuid PRIMARY KEY,
          turn_id uuid NOT NULL REFERENCES qa_turns(id) ON DELETE CASCADE,
          claim_id varchar(100) NOT NULL,
          claim_text text NOT NULL,
          retrieval_hit_id uuid NOT NULL,
          evidence_id varchar(32) NOT NULL,
          corpus_kind varchar(32) NOT NULL,
          content_hash varchar(64) NOT NULL,
          knowledge_node_id uuid,
          knowledge_revision_id uuid,
          source_id uuid,
          source_version_id uuid,
          parse_artifact_id uuid,
          section_id uuid,
          frozen_quote text NOT NULL,
          locator jsonb,
          deep_link text,
          validation_status varchar(32) NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT uq_qa_citations_claim_evidence UNIQUE (turn_id,claim_id,evidence_id),
          CONSTRAINT fk_qa_citations_hit_evidence FOREIGN KEY
            (retrieval_hit_id,turn_id,evidence_id)
            REFERENCES qa_retrieval_hits(id,turn_id,evidence_id) ON DELETE CASCADE,
          CONSTRAINT ck_qa_citations_claim CHECK
            (length(btrim(claim_id)) > 0 AND length(btrim(claim_text)) > 0),
          CONSTRAINT ck_qa_citations_hash CHECK (content_hash ~ '^[0-9a-f]{64}$'),
          CONSTRAINT ck_qa_citations_quote CHECK (length(frozen_quote) > 0),
          CONSTRAINT ck_qa_citations_validation CHECK (validation_status = 'valid'),
          CONSTRAINT ck_qa_citations_identity_shape CHECK (
            (corpus_kind = 'source_evidence' AND source_id IS NOT NULL
              AND source_version_id IS NOT NULL AND parse_artifact_id IS NOT NULL
              AND section_id IS NOT NULL AND knowledge_node_id IS NULL
              AND knowledge_revision_id IS NULL)
            OR (corpus_kind = 'confirmed_knowledge' AND knowledge_node_id IS NOT NULL
              AND knowledge_revision_id IS NOT NULL AND source_id IS NULL
              AND source_version_id IS NULL AND parse_artifact_id IS NULL
              AND section_id IS NULL)
          )
        )
        """,
        "CREATE INDEX ix_qa_citations_turn_claim ON qa_citations(turn_id,claim_id)",
    )


def downgrade() -> None:
    _execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM qa_turns LIMIT 1) THEN
            RAISE EXCEPTION 'cannot downgrade D8 QA schema while audit records exist';
          END IF;
        END
        $$
        """,
        "DROP TABLE qa_citations",
        "DROP TABLE qa_retrieval_hits",
        "DROP TABLE qa_turns",
    )
