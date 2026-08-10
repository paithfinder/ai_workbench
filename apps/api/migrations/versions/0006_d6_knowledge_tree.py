"""Add D6 persistent knowledge tree and frozen citations.

Revision ID: 0006_d6_knowledge_tree
Revises: 0005_d5_candidate_review
Create Date: 2026-08-05
"""

import hashlib
import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.types import UserDefinedType

revision: str = "0006_d6_knowledge_tree"
down_revision: str | None = "0005_d5_candidate_review"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _deterministic_uuid_sql(value_sql: str) -> str:
    """Build a deterministic RFC 9562 UUIDv5 from an SQL text expression."""
    digest = f"md5({value_sql})"
    return (
        f"(substr({digest}, 1, 12) || '5' || substr({digest}, 14, 3) || "
        f"'8' || substr({digest}, 18, 15))::uuid"
    )


ROOT_UUID_SQL = _deterministic_uuid_sql("'knowledge-root:' || id::text")
ROOT_REVISION_UUID_SQL = _deterministic_uuid_sql(
    "'knowledge-root-revision:' || space.id::text"
)
SOURCE_UUID_SQL = _deterministic_uuid_sql(
    "'knowledge-source:' || source.parent_id::text || ':' || source.source_version_id::text"
)


class Ltree(UserDefinedType[str]):
    cache_ok = True

    def get_col_spec(self, **_kw: object) -> str:
        return "LTREE"


def _revision_content_hash(
    *,
    title: str,
    body: str,
    tags: list[str],
    conditions: list[str],
    exceptions: list[str],
) -> str:
    canonical = json.dumps(
        {
            "body": body,
            "conditions": conditions,
            "exceptions": exceptions,
            "tags": tags,
            "title": title,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _backfill_revision_hashes() -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            """
            SELECT id, title, body, tags, conditions, exceptions
            FROM knowledge_revisions
            """
        )
    ).mappings()
    updates: list[dict[str, Any]] = []
    for row in rows:
        updates.append(
            {
                "revision_id": row["id"],
                "content_hash": _revision_content_hash(
                    title=row["title"],
                    body=row["body"],
                    tags=list(row["tags"]),
                    conditions=list(row["conditions"]),
                    exceptions=list(row["exceptions"]),
                ),
            }
        )
    if updates:
        connection.execute(
            sa.text(
                """
                UPDATE knowledge_revisions
                SET content_hash = :content_hash
                WHERE id = :revision_id
                """
            ),
            updates,
        )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS ltree")
    # Fire the DEFERRABLE INITIALLY DEFERRED FK left over from D5
    # (fk_knowledge_nodes_current_revision) before ALTERing knowledge_nodes,
    # otherwise PostgreSQL rejects the ALTER with "pending trigger events".
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")
    op.drop_constraint("ck_knowledge_nodes_parent_kind", "knowledge_nodes", type_="check")
    op.drop_constraint("ck_knowledge_nodes_kind", "knowledge_nodes", type_="check")
    op.add_column("knowledge_nodes", sa.Column("path", Ltree()))
    op.add_column(
        "knowledge_nodes",
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )
    op.add_column(
        "knowledge_nodes",
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "knowledge_nodes",
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sources.id", ondelete="RESTRICT"),
        ),
    )
    op.add_column(
        "knowledge_nodes",
        sa.Column("source_version_id", postgresql.UUID(as_uuid=True)),
    )
    op.create_foreign_key(
        "fk_knowledge_nodes_source_version_source",
        "knowledge_nodes",
        "source_versions",
        ["source_version_id", "source_id"],
        ["id", "source_id"],
        ondelete="RESTRICT",
    )
    op.add_column("knowledge_nodes", sa.Column("deleted_at", sa.DateTime(timezone=True)))

    op.add_column("knowledge_revisions", sa.Column("content_hash", sa.String(64)))
    op.add_column("knowledge_revisions", sa.Column("edit_reason", sa.String(1000)))
    _backfill_revision_hashes()
    op.alter_column("knowledge_revisions", "content_hash", nullable=False)
    op.create_check_constraint(
        "ck_knowledge_revisions_content_hash",
        "knowledge_revisions",
        "content_hash ~ '^[0-9a-f]{64}$'",
    )

    op.add_column("knowledge_evidence", sa.Column("frozen_quote", sa.Text()))
    op.execute(
        """
        UPDATE knowledge_evidence evidence
        SET frozen_quote = section.text
        FROM source_sections section
        WHERE section.id = evidence.section_id
          AND section.source_version_id = evidence.source_version_id
          AND section.parse_artifact_id = evidence.parse_artifact_id
          AND section.quote_hash = evidence.quote_hash
          AND section.content_hash = evidence.content_hash
        """
    )
    # A NULL here means a D5 citation identity was already inconsistent. Failing the
    # migration is safer than manufacturing a quote or redirecting it to current data.
    op.alter_column("knowledge_evidence", "frozen_quote", nullable=False)

    # One immutable root per space. Labels use IDs, never editable titles.
    op.execute(
        f"""
        INSERT INTO knowledge_nodes (id, space_id, kind, path, version, sort_order)
        SELECT {ROOT_UUID_SQL},
               id,
               'root',
               ('n' || replace(({ROOT_UUID_SQL})::text, '-', ''))::ltree,
               1,
               0
        FROM knowledge_spaces
        """
    )
    root_revisions = [
        {
            "revision_id": str(row.id),
            "node_id": str(row.node_id),
            "space_id": str(row.space_id),
            "title": row.title,
            "content_hash": _revision_content_hash(
                title=row.title,
                body="",
                tags=[],
                conditions=[],
                exceptions=[],
            ),
        }
        for row in op.get_bind()
        .execute(
            sa.text(
                f"""
                SELECT {ROOT_REVISION_UUID_SQL} AS id,
                       root.id AS node_id,
                       space.id AS space_id,
                       space.name AS title
                FROM knowledge_spaces space
                JOIN knowledge_nodes root
                  ON root.space_id = space.id AND root.kind = 'root'
                """
            )
        )
        .mappings()
    ]
    if root_revisions:
        op.get_bind().execute(
            sa.text(
                """
                INSERT INTO knowledge_revisions (
                    id, node_id, space_id, revision_number, title, body, tags,
                    conditions, exceptions, actor, content_hash, edit_reason
                )
                VALUES (
                    :revision_id, :node_id, :space_id, 1, :title, '',
                    '[]'::jsonb, '[]'::jsonb, '[]'::jsonb, 'system',
                    :content_hash, 'D6 root backfill'
                )
                """
            ),
            root_revisions,
        )
    op.execute(
        """
        UPDATE knowledge_nodes root
        SET current_revision_id = revision.id
        FROM knowledge_revisions revision
        WHERE revision.node_id = root.id AND root.kind = 'root'
        """
    )
    op.execute(
        """
        UPDATE knowledge_nodes child
        SET parent_id = root.id
        FROM knowledge_nodes root
        WHERE root.space_id = child.space_id
          AND root.kind = 'root'
          AND child.kind <> 'root'
          AND child.parent_id IS NULL
        """
    )
    op.execute(
        """
        WITH RECURSIVE tree AS (
            SELECT id, path FROM knowledge_nodes WHERE kind = 'root'
            UNION ALL
            SELECT child.id,
                   tree.path || ('n' || replace(child.id::text, '-', ''))::ltree
            FROM knowledge_nodes child
            JOIN tree ON child.parent_id = tree.id
        )
        UPDATE knowledge_nodes node
        SET path = tree.path
        FROM tree
        WHERE node.id = tree.id
        """
    )

    # A source identity belongs to the exact immutable version cited by points in a
    # document. It is a read-only tree leaf and does not own a KnowledgeRevision.
    op.execute(
        f"""
        WITH source_candidates AS (
            SELECT DISTINCT ON (point.parent_id, evidence.source_version_id)
                   point.space_id,
                   point.parent_id,
                   document.path AS document_path,
                   evidence.source_version_id,
                   version.source_id,
                   evidence.created_at
            FROM knowledge_nodes point
            JOIN knowledge_revisions revision ON revision.node_id = point.id
            JOIN knowledge_evidence evidence ON evidence.revision_id = revision.id
            JOIN source_versions version ON version.id = evidence.source_version_id
            JOIN knowledge_nodes document ON document.id = point.parent_id
            WHERE point.kind = 'point'
            ORDER BY point.parent_id, evidence.source_version_id, evidence.created_at
        ),
        ranked_sources AS (
            SELECT candidate.*,
                   dense_rank() OVER (
                       PARTITION BY candidate.parent_id
                       ORDER BY candidate.source_version_id
                   ) AS source_order
            FROM source_candidates candidate
        )
        INSERT INTO knowledge_nodes (
            id, space_id, parent_id, kind, path, version, sort_order, source_id,
            source_version_id
        )
        SELECT {SOURCE_UUID_SQL},
               source.space_id,
               source.parent_id,
               'source',
               source.document_path || (
                   'n' || replace(
                       ({SOURCE_UUID_SQL})::text,
                       '-',
                       ''
                   )
               )::ltree,
               1,
               COALESCE((
                   SELECT max(sibling.sort_order)
                   FROM knowledge_nodes sibling
                   WHERE sibling.parent_id = source.parent_id
               ), -1) + source.source_order,
               source.source_id,
               source.source_version_id
        FROM ranked_sources source
        ON CONFLICT (id) DO NOTHING
        """
    )

    # The INSERTs above re-armed deferred FK triggers; flush them again before
    # the final ALTERs on knowledge_nodes (alter_column + check constraints).
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")
    op.alter_column("knowledge_nodes", "path", nullable=False)
    op.create_check_constraint(
        "ck_knowledge_nodes_version_positive", "knowledge_nodes", "version > 0"
    )
    op.create_check_constraint(
        "ck_knowledge_nodes_sort_nonnegative", "knowledge_nodes", "sort_order >= 0"
    )
    op.create_check_constraint(
        "ck_knowledge_nodes_kind",
        "knowledge_nodes",
        "kind IN ('root','folder','document','point','source')",
    )
    op.create_check_constraint(
        "ck_knowledge_nodes_shape",
        "knowledge_nodes",
        "(kind = 'root' AND parent_id IS NULL AND source_id IS NULL "
        "AND source_version_id IS NULL AND origin_candidate_id IS NULL) OR "
        "(kind = 'source' AND parent_id IS NOT NULL AND source_id IS NOT NULL "
        "AND source_version_id IS NOT NULL AND current_revision_id IS NULL "
        "AND origin_candidate_id IS NULL) OR "
        "(kind IN ('folder','document') AND parent_id IS NOT NULL "
        "AND source_id IS NULL AND source_version_id IS NULL "
        "AND origin_candidate_id IS NULL) OR "
        "(kind = 'point' AND parent_id IS NOT NULL AND source_id IS NULL "
        "AND source_version_id IS NULL)",
    )
    op.create_index(
        "ix_knowledge_nodes_path", "knowledge_nodes", ["path"], postgresql_using="gist"
    )
    op.create_index(
        "ix_knowledge_nodes_space_parent_order",
        "knowledge_nodes",
        ["space_id", "parent_id", "sort_order", "id"],
    )
    op.create_index(
        "uq_knowledge_nodes_space_path",
        "knowledge_nodes",
        ["space_id", "path"],
        unique=True,
    )
    op.create_index(
        "uq_knowledge_nodes_space_root",
        "knowledge_nodes",
        ["space_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'root' AND deleted_at IS NULL"),
    )
    op.create_index(
        "uq_knowledge_nodes_document_source_version",
        "knowledge_nodes",
        ["space_id", "parent_id", "source_version_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'source' AND deleted_at IS NULL"),
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_knowledge_tree_shape()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            parent_kind text;
            parent_path ltree;
            parent_space uuid;
        BEGIN
            IF NEW.kind = 'root' THEN
                IF NEW.parent_id IS NOT NULL OR nlevel(NEW.path) <> 1
                   OR NEW.path::text <> 'n' || replace(NEW.id::text, '-', '') THEN
                    RAISE EXCEPTION 'invalid knowledge root shape';
                END IF;
                RETURN NEW;
            END IF;

            SELECT kind, path, space_id
            INTO parent_kind, parent_path, parent_space
            FROM knowledge_nodes
            WHERE id = NEW.parent_id;

            IF parent_space IS NULL OR parent_space <> NEW.space_id THEN
                RAISE EXCEPTION 'knowledge parent must belong to the same space';
            END IF;
            IF NOT (
                (parent_kind = 'root' AND NEW.kind IN ('folder', 'document')) OR
                (parent_kind = 'folder' AND NEW.kind IN ('folder', 'document')) OR
                (parent_kind = 'document' AND NEW.kind IN ('point', 'source'))
            ) THEN
                RAISE EXCEPTION 'invalid knowledge parent-child kinds';
            END IF;
            IF NEW.path <> parent_path || subpath(NEW.path, nlevel(NEW.path) - 1, 1)
               OR subpath(NEW.path, nlevel(NEW.path) - 1, 1)::text <>
                  'n' || replace(NEW.id::text, '-', '') THEN
                RAISE EXCEPTION 'knowledge path must extend its parent path';
            END IF;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER knowledge_tree_shape_guard
        AFTER INSERT OR UPDATE OF parent_id, kind, path, space_id
        ON knowledge_nodes
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION enforce_knowledge_tree_shape()
        """
    )

    op.create_table(
        "knowledge_write_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "operation IN ('create','edit','move','delete')",
            name="ck_knowledge_write_requests_operation",
        ),
        sa.CheckConstraint(
            "request_hash ~ '^[0-9a-f]{64}$'",
            name="ck_knowledge_write_requests_hash",
        ),
        sa.ForeignKeyConstraint(
            ["space_id"], ["knowledge_spaces.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "space_id", "idempotency_key", name="uq_knowledge_write_requests_key"
        ),
    )
    op.create_table(
        "knowledge_write_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("node_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("node_version", sa.Integer(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "node_version > 0", name="ck_knowledge_write_results_version"
        ),
        sa.ForeignKeyConstraint(
            ["request_id"], ["knowledge_write_requests.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["node_id"], ["knowledge_nodes.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id", name="uq_knowledge_write_results_request"),
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM knowledge_write_requests) OR EXISTS (
                SELECT 1 FROM knowledge_nodes
                WHERE kind = 'folder' OR deleted_at IS NOT NULL OR version > 1
            ) OR EXISTS (
                SELECT 1 FROM knowledge_revisions WHERE revision_number > 1
            ) THEN
                RAISE EXCEPTION USING MESSAGE =
                    'Cannot downgrade D6 while tree business writes exist';
            END IF;
        END $$
        """
    )
    op.drop_table("knowledge_write_results")
    op.drop_table("knowledge_write_requests")
    op.execute("DROP TRIGGER knowledge_tree_shape_guard ON knowledge_nodes")
    op.execute("DROP FUNCTION enforce_knowledge_tree_shape()")
    op.drop_index("uq_knowledge_nodes_document_source_version", table_name="knowledge_nodes")
    op.drop_index("uq_knowledge_nodes_space_root", table_name="knowledge_nodes")
    op.drop_index("uq_knowledge_nodes_space_path", table_name="knowledge_nodes")
    op.drop_index("ix_knowledge_nodes_space_parent_order", table_name="knowledge_nodes")
    op.drop_index("ix_knowledge_nodes_path", table_name="knowledge_nodes")
    op.drop_constraint("ck_knowledge_nodes_shape", "knowledge_nodes", type_="check")
    op.drop_constraint("ck_knowledge_nodes_kind", "knowledge_nodes", type_="check")
    op.drop_constraint(
        "ck_knowledge_nodes_sort_nonnegative", "knowledge_nodes", type_="check"
    )
    op.drop_constraint(
        "ck_knowledge_nodes_version_positive", "knowledge_nodes", type_="check"
    )
    op.execute("DELETE FROM knowledge_nodes WHERE kind = 'source'")
    op.execute(
        """
        UPDATE knowledge_nodes document
        SET parent_id = NULL
        FROM knowledge_nodes root
        WHERE document.parent_id = root.id
          AND document.kind = 'document'
          AND root.kind = 'root'
        """
    )
    op.execute(
        """
        UPDATE knowledge_nodes root
        SET current_revision_id = NULL
        WHERE root.kind = 'root'
        """
    )
    op.execute(
        """
        DELETE FROM knowledge_revisions revision
        USING knowledge_nodes root
        WHERE revision.node_id = root.id AND root.kind = 'root'
        """
    )
    op.execute("DELETE FROM knowledge_nodes WHERE kind = 'root'")
    op.drop_column("knowledge_evidence", "frozen_quote")
    op.drop_constraint(
        "ck_knowledge_revisions_content_hash", "knowledge_revisions", type_="check"
    )
    op.drop_column("knowledge_revisions", "edit_reason")
    op.drop_column("knowledge_revisions", "content_hash")
    op.drop_column("knowledge_nodes", "deleted_at")
    op.drop_constraint(
        "fk_knowledge_nodes_source_version_source",
        "knowledge_nodes",
        type_="foreignkey",
    )
    op.drop_column("knowledge_nodes", "source_version_id")
    op.drop_column("knowledge_nodes", "source_id")
    op.drop_column("knowledge_nodes", "sort_order")
    op.drop_column("knowledge_nodes", "version")
    op.drop_column("knowledge_nodes", "path")
    op.create_check_constraint(
        "ck_knowledge_nodes_kind", "knowledge_nodes", "kind IN ('document','point')"
    )
    op.create_check_constraint(
        "ck_knowledge_nodes_parent_kind",
        "knowledge_nodes",
        "(kind = 'document' AND parent_id IS NULL AND origin_candidate_id IS NULL) OR "
        "(kind = 'point' AND parent_id IS NOT NULL AND origin_candidate_id IS NOT NULL)",
    )
