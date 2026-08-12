"""Add D7 retrieval index runs, chunks, FTS, and pgvector."""

from collections.abc import Sequence

from alembic import op

revision: str = "0008_d7_retrieval_index"
down_revision: str | None = "0007_fix_d6_uuid_contract"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _execute(*statements: str) -> None:
    for statement in statements:
        op.execute(statement)


def upgrade() -> None:
    _execute(
        "CREATE EXTENSION IF NOT EXISTS vector",
        "DROP INDEX uq_jobs_source_version_kind",
        "CREATE UNIQUE INDEX uq_jobs_source_version_kind "
        "ON jobs(source_version_id,kind) "
        "WHERE source_version_id IS NOT NULL "
        "AND kind NOT IN ('source_parse','source_index')",
        """
        CREATE TABLE retrieval_index_runs (
          id uuid PRIMARY KEY,
          job_id uuid NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE RESTRICT,
          space_id uuid NOT NULL REFERENCES knowledge_spaces(id) ON DELETE RESTRICT,
          target_kind varchar(32) NOT NULL,
          target_id uuid NOT NULL,
          scope_node_id uuid REFERENCES knowledge_nodes(id) ON DELETE RESTRICT,
          input_hash varchar(64) NOT NULL,
          source_version_id uuid REFERENCES source_versions(id) ON DELETE CASCADE,
          parse_artifact_id uuid REFERENCES source_parse_artifacts(id) ON DELETE RESTRICT,
          knowledge_revision_id uuid REFERENCES knowledge_revisions(id) ON DELETE CASCADE,
          status varchar(32) NOT NULL,
          index_config_version varchar(100) NOT NULL,
          chunker_version varchar(100) NOT NULL,
          embedding_provider varchar(100) NOT NULL,
          embedding_model varchar(200) NOT NULL,
          embedding_config jsonb NOT NULL DEFAULT '{}'::jsonb,
          embedding_dimensions integer NOT NULL,
          chunk_count integer NOT NULL DEFAULT 0,
          embedded_count integer NOT NULL DEFAULT 0,
          error_code varchar(100),
          error_message varchar(2000),
          started_at timestamptz,
          completed_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT uq_retrieval_index_runs_target_config UNIQUE
            (space_id,target_kind,target_id,input_hash,index_config_version),
          CONSTRAINT ck_retrieval_index_runs_status CHECK
            (status IN ('queued','running','ready','failed')),
          CONSTRAINT ck_retrieval_index_runs_target_kind CHECK
            (target_kind IN ('source_version','knowledge_revision','space_rebuild')),
          CONSTRAINT ck_retrieval_index_runs_hash CHECK (input_hash ~ '^[0-9a-f]{64}$'),
          CONSTRAINT ck_retrieval_index_runs_chunk_count CHECK (chunk_count >= 0),
          CONSTRAINT ck_retrieval_index_runs_embedded_count CHECK (embedded_count >= 0),
          CONSTRAINT ck_retrieval_index_runs_dimensions CHECK (embedding_dimensions > 0)
        )
        """,
        "CREATE INDEX ix_retrieval_index_runs_space_status "
        "ON retrieval_index_runs(space_id,status,created_at)",
        "CREATE UNIQUE INDEX uq_retrieval_index_runs_active_target "
        "ON retrieval_index_runs"
        "(space_id,target_kind,target_id,index_config_version) "
        "WHERE status IN ('queued','running')",
        "CREATE UNIQUE INDEX uq_source_sections_id_artifact_version "
        "ON source_sections(id,parse_artifact_id,source_version_id)",
        """
        CREATE TABLE retrieval_chunks (
          id uuid PRIMARY KEY,
          index_run_id uuid NOT NULL
            REFERENCES retrieval_index_runs(id) ON DELETE CASCADE,
          space_id uuid NOT NULL,
          corpus_kind varchar(32) NOT NULL,
          index_config_version varchar(100) NOT NULL,
          active boolean NOT NULL DEFAULT true,
          knowledge_node_id uuid,
          knowledge_revision_id uuid
            REFERENCES knowledge_revisions(id) ON DELETE CASCADE,
          source_id uuid REFERENCES sources(id) ON DELETE RESTRICT,
          source_version_id uuid,
          parse_artifact_id uuid,
          section_id uuid,
          ordinal integer NOT NULL,
          content_identity varchar(64) NOT NULL,
          content_hash varchar(64) NOT NULL,
          title varchar(1000),
          text text NOT NULL,
          char_count integer NOT NULL,
          token_count integer NOT NULL,
          path ltree NOT NULL,
          heading_path jsonb NOT NULL DEFAULT '[]'::jsonb,
          locator jsonb,
          search_vector tsvector GENERATED ALWAYS AS
            (to_tsvector('simple', text)) STORED,
          embedding_model varchar(200) NOT NULL,
          embedding_config jsonb NOT NULL DEFAULT '{}'::jsonb,
          embedding vector(1024) NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT fk_retrieval_chunks_node_space
            FOREIGN KEY (knowledge_node_id,space_id)
            REFERENCES knowledge_nodes(id,space_id) ON DELETE CASCADE,
          CONSTRAINT fk_retrieval_chunks_source_version_source
            FOREIGN KEY (source_version_id,source_id)
            REFERENCES source_versions(id,source_id) ON DELETE CASCADE,
          CONSTRAINT fk_retrieval_chunks_artifact_version
            FOREIGN KEY (parse_artifact_id,source_version_id)
            REFERENCES source_parse_artifacts(id,source_version_id) ON DELETE CASCADE,
          CONSTRAINT fk_retrieval_chunks_section_artifact_version
            FOREIGN KEY (section_id,parse_artifact_id,source_version_id)
            REFERENCES source_sections(id,parse_artifact_id,source_version_id)
            ON DELETE CASCADE,
          CONSTRAINT uq_retrieval_chunks_run_ordinal UNIQUE(index_run_id,ordinal),
          CONSTRAINT uq_retrieval_chunks_content_config UNIQUE
            (space_id,content_identity,index_config_version,ordinal),
          CONSTRAINT ck_retrieval_chunks_corpus CHECK
            (corpus_kind IN ('source_evidence','confirmed_knowledge')),
          CONSTRAINT ck_retrieval_chunks_identity_shape CHECK (
            (
              corpus_kind='source_evidence'
              AND source_id IS NOT NULL
              AND source_version_id IS NOT NULL
              AND parse_artifact_id IS NOT NULL
              AND section_id IS NOT NULL
              AND knowledge_node_id IS NULL
              AND knowledge_revision_id IS NULL
            ) OR (
              corpus_kind='confirmed_knowledge'
              AND knowledge_node_id IS NOT NULL
              AND knowledge_revision_id IS NOT NULL
              AND source_id IS NULL
              AND source_version_id IS NULL
              AND parse_artifact_id IS NULL
              AND section_id IS NULL
            )
          ),
          CONSTRAINT ck_retrieval_chunks_ordinal CHECK (ordinal >= 0),
          CONSTRAINT ck_retrieval_chunks_char_count CHECK (char_count > 0),
          CONSTRAINT ck_retrieval_chunks_token_count CHECK (token_count > 0),
          CONSTRAINT ck_retrieval_chunks_hash CHECK
            (content_hash ~ '^[0-9a-f]{64}$')
        )
        """,
        "CREATE INDEX ix_retrieval_chunks_space "
        "ON retrieval_chunks(space_id)",
        "CREATE INDEX ix_retrieval_chunks_path "
        "ON retrieval_chunks USING gist(path)",
        "CREATE INDEX ix_retrieval_chunks_source_version "
        "ON retrieval_chunks(source_version_id,ordinal)",
        "CREATE INDEX ix_retrieval_chunks_search "
        "ON retrieval_chunks USING gin(search_vector)",
        "CREATE INDEX ix_retrieval_chunks_embedding_hnsw "
        "ON retrieval_chunks USING hnsw(embedding vector_cosine_ops) "
        "WHERE active",
    )


def downgrade() -> None:
    _execute(
        "DROP TABLE retrieval_chunks",
        "DROP INDEX uq_source_sections_id_artifact_version",
        "DROP TABLE retrieval_index_runs",
        "DROP INDEX uq_jobs_source_version_kind",
        "CREATE UNIQUE INDEX uq_jobs_source_version_kind "
        "ON jobs(source_version_id,kind) "
        "WHERE source_version_id IS NOT NULL AND kind <> 'source_parse'",
    )
