"""Add N2.2 external search and selected web source persistence.

Revision ID: 0013_n22_search
Revises: 0012_n21_proposal
Create Date: 2026-08-15
"""

# SQL statements are kept readable as PostgreSQL blocks.
# ruff: noqa: E501

from collections.abc import Sequence

from alembic import op

revision: str = "0013_n22_search"
down_revision: str | None = "0012_n21_proposal"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _execute(*statements: str) -> None:
    for statement in statements:
        op.execute(statement)


def upgrade() -> None:
    _execute(
        "ALTER TABLE jobs ADD CONSTRAINT uq_jobs_id_space UNIQUE (id,space_id)",
        "ALTER TABLE research_runs ADD COLUMN query text",
        "ALTER TABLE research_runs ADD COLUMN provider varchar(100)",
        "ALTER TABLE research_runs ADD COLUMN provider_model varchar(200)",
        "ALTER TABLE research_runs ADD COLUMN provider_request_id varchar(255)",
        "ALTER TABLE research_runs ADD COLUMN search_filters jsonb NOT NULL DEFAULT '{}'::jsonb",
        "ALTER TABLE research_runs ADD COLUMN failure_code varchar(100)",
        "ALTER TABLE research_runs ADD COLUMN failure_message varchar(1000)",
        "ALTER TABLE research_runs ADD CONSTRAINT ck_research_runs_query CHECK (query IS NULL OR length(btrim(query)) > 0)",
        """
        CREATE TABLE research_search_requests (
          id uuid PRIMARY KEY,
          space_id uuid NOT NULL,
          research_run_id uuid NOT NULL,
          idempotency_key varchar(255) NOT NULL,
          request_hash varchar(64) NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT uq_research_search_requests_key UNIQUE (space_id,idempotency_key),
          CONSTRAINT uq_research_search_requests_chain UNIQUE (id,research_run_id,space_id),
          CONSTRAINT fk_research_search_requests_run_space FOREIGN KEY (research_run_id,space_id)
            REFERENCES research_runs(id,space_id) ON DELETE RESTRICT,
          CONSTRAINT ck_research_search_requests_hash CHECK (request_hash ~ '^[0-9a-f]{64}$')
        )
        """,
        """
        CREATE TABLE research_search_results (
          id uuid PRIMARY KEY,
          space_id uuid NOT NULL,
          research_run_id uuid NOT NULL,
          ordinal integer NOT NULL,
          title varchar(500) NOT NULL,
          url varchar(2000) NOT NULL,
          snippet text,
          provider_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
          created_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT fk_research_search_results_run_space FOREIGN KEY (research_run_id,space_id)
            REFERENCES research_runs(id,space_id) ON DELETE CASCADE,
          CONSTRAINT uq_research_search_results_ordinal UNIQUE (research_run_id,ordinal),
          CONSTRAINT ck_research_search_results_ordinal CHECK (ordinal >= 0),
          CONSTRAINT ck_research_search_results_url CHECK (length(btrim(url)) > 0),
          CONSTRAINT ck_research_search_results_title CHECK (length(btrim(title)) > 0)
        )
        """,
        "CREATE INDEX ix_research_search_results_run_ordinal ON research_search_results(research_run_id,ordinal)",
        """
        CREATE TABLE research_source_selection_batches (
          id uuid PRIMARY KEY,
          space_id uuid NOT NULL,
          research_run_id uuid NOT NULL,
          idempotency_key varchar(255) NOT NULL,
          request_hash varchar(64) NOT NULL,
          status varchar(32) NOT NULL DEFAULT 'pending',
          created_at timestamptz NOT NULL DEFAULT now(),
          completed_at timestamptz,
          CONSTRAINT uq_research_selection_batches_key UNIQUE (research_run_id,idempotency_key),
          CONSTRAINT uq_research_selection_batches_chain UNIQUE (id,research_run_id,space_id),
          CONSTRAINT fk_research_selection_batches_run_space FOREIGN KEY (research_run_id,space_id)
            REFERENCES research_runs(id,space_id) ON DELETE RESTRICT,
          CONSTRAINT ck_research_selection_batches_hash CHECK (request_hash ~ '^[0-9a-f]{64}$'),
          CONSTRAINT ck_research_selection_batches_status CHECK
            (status IN ('pending','completed','completed_with_failures'))
        )
        """,
        "CREATE INDEX ix_research_selection_batches_run_created ON research_source_selection_batches(research_run_id,created_at)",
        """
        CREATE TABLE research_source_selections (
          id uuid PRIMARY KEY,
          space_id uuid NOT NULL,
          research_run_id uuid NOT NULL,
          batch_id uuid NOT NULL,
          ordinal integer NOT NULL,
          requested_url varchar(2000) NOT NULL,
          title varchar(500) NOT NULL,
          status varchar(32) NOT NULL DEFAULT 'pending',
          source_id uuid,
          source_version_id uuid,
          job_id uuid,
          error_code varchar(100),
          error_message varchar(1000),
          created_at timestamptz NOT NULL DEFAULT now(),
          completed_at timestamptz,
          CONSTRAINT fk_research_source_selections_batch_chain FOREIGN KEY
            (batch_id,research_run_id,space_id)
            REFERENCES research_source_selection_batches(id,research_run_id,space_id) ON DELETE CASCADE,
          CONSTRAINT fk_research_source_selections_run_space FOREIGN KEY (research_run_id,space_id)
            REFERENCES research_runs(id,space_id) ON DELETE RESTRICT,
          CONSTRAINT fk_research_source_selections_source_space FOREIGN KEY (source_id,space_id)
            REFERENCES sources(id,space_id) ON DELETE RESTRICT,
          CONSTRAINT fk_research_source_selections_version_source FOREIGN KEY (source_version_id,source_id)
            REFERENCES source_versions(id,source_id) ON DELETE RESTRICT,
          CONSTRAINT fk_research_source_selections_job_space FOREIGN KEY (job_id,space_id)
            REFERENCES jobs(id,space_id) ON DELETE RESTRICT,
          CONSTRAINT ck_research_source_selections_ordinal CHECK (ordinal >= 0),
          CONSTRAINT ck_research_source_selections_status CHECK
            (status IN ('pending','succeeded','failed')),
          CONSTRAINT ck_research_source_selections_url CHECK (length(btrim(requested_url)) > 0),
          CONSTRAINT uq_research_source_selections_ordinal UNIQUE (batch_id,ordinal),
          CONSTRAINT uq_research_source_selections_url UNIQUE (batch_id,requested_url)
        )
        """,
        "CREATE INDEX ix_research_source_selections_batch_ordinal ON research_source_selections(batch_id,ordinal)",
    )


def downgrade() -> None:
    _execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM research_search_requests LIMIT 1)
             OR EXISTS (SELECT 1 FROM research_search_results LIMIT 1)
             OR EXISTS (SELECT 1 FROM research_source_selection_batches LIMIT 1)
             OR EXISTS (SELECT 1 FROM research_source_selections LIMIT 1)
             OR EXISTS (SELECT 1 FROM research_runs WHERE origin = 'external_search' LIMIT 1) THEN
            RAISE EXCEPTION 'cannot downgrade N2.2 search schema while research audit records exist';
          END IF;
        END
        $$
        """,
        "DROP TABLE research_source_selections",
        "DROP TABLE research_source_selection_batches",
        "DROP TABLE research_search_results",
        "DROP TABLE research_search_requests",
        "ALTER TABLE research_runs DROP CONSTRAINT IF EXISTS ck_research_runs_query",
        "ALTER TABLE research_runs DROP COLUMN IF EXISTS failure_message",
        "ALTER TABLE research_runs DROP COLUMN IF EXISTS failure_code",
        "ALTER TABLE research_runs DROP COLUMN IF EXISTS search_filters",
        "ALTER TABLE research_runs DROP COLUMN IF EXISTS provider_request_id",
        "ALTER TABLE research_runs DROP COLUMN IF EXISTS provider_model",
        "ALTER TABLE research_runs DROP COLUMN IF EXISTS provider",
        "ALTER TABLE research_runs DROP COLUMN IF EXISTS query",
        "ALTER TABLE jobs DROP CONSTRAINT IF EXISTS uq_jobs_id_space",
    )
