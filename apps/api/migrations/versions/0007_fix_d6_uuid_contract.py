"""Repair deterministic D6 UUIDs so strict clients accept them.

Revision ID: 0007_fix_d6_uuid_contract
Revises: 0006_d6_knowledge_tree
Create Date: 2026-08-07
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007_fix_d6_uuid_contract"
down_revision: str | None = "0006_d6_knowledge_tree"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _deterministic_uuid_sql(value_sql: str) -> str:
    digest = f"md5({value_sql})"
    return (
        f"(substr({digest}, 1, 12) || '5' || substr({digest}, 14, 3) || "
        f"'8' || substr({digest}, 18, 15))::uuid"
    )


OLD_ROOT_UUID_SQL = "md5('knowledge-root:' || space.id::text)::uuid"
NEW_ROOT_UUID_SQL = _deterministic_uuid_sql("'knowledge-root:' || space.id::text")
OLD_ROOT_REVISION_UUID_SQL = (
    "md5('knowledge-root-revision:' || space.id::text)::uuid"
)
NEW_ROOT_REVISION_UUID_SQL = _deterministic_uuid_sql(
    "'knowledge-root-revision:' || space.id::text"
)
OLD_SOURCE_UUID_SQL = (
    "md5('knowledge-source:' || node.parent_id::text || ':' || "
    "node.source_version_id::text)::uuid"
)
NEW_SOURCE_UUID_SQL = _deterministic_uuid_sql(
    "'knowledge-source:' || node.parent_id::text || ':' || node.source_version_id::text"
)


def _execute_statements(*statements: str) -> None:
    # asyncpg prepared statements accept one SQL command at a time.
    for statement in statements:
        op.execute(statement)


def upgrade() -> None:
    # Build a complete old->new identity map before changing any FK target. Runtime-created
    # UUIDv4 nodes are excluded; only the deterministic rows introduced by D6 are repaired.
    _execute_statements(
        """
        CREATE TEMP TABLE d6_node_uuid_map (
            old_id uuid PRIMARY KEY,
            new_id uuid NOT NULL UNIQUE
        ) ON COMMIT DROP
        """,
        f"""
        INSERT INTO d6_node_uuid_map (old_id, new_id)
        SELECT {OLD_ROOT_UUID_SQL}, {NEW_ROOT_UUID_SQL}
        FROM knowledge_spaces space
        JOIN knowledge_nodes node ON node.space_id = space.id
        WHERE node.kind = 'root' AND node.id = {OLD_ROOT_UUID_SQL}
        """,
        f"""
        INSERT INTO d6_node_uuid_map (old_id, new_id)
        SELECT node.id, {NEW_SOURCE_UUID_SQL}
        FROM knowledge_nodes node
        WHERE node.kind = 'source'
          AND node.source_version_id IS NOT NULL
          AND node.id = {OLD_SOURCE_UUID_SQL}
        """,
        """
        CREATE TEMP TABLE d6_revision_uuid_map (
            old_id uuid PRIMARY KEY,
            new_id uuid NOT NULL UNIQUE
        ) ON COMMIT DROP
        """,
        f"""
        INSERT INTO d6_revision_uuid_map (old_id, new_id)
        SELECT {OLD_ROOT_REVISION_UUID_SQL}, {NEW_ROOT_REVISION_UUID_SQL}
        FROM knowledge_spaces space
        JOIN knowledge_nodes root ON root.space_id = space.id AND root.kind = 'root'
        JOIN knowledge_revisions revision ON revision.node_id = root.id
        WHERE revision.id = {OLD_ROOT_REVISION_UUID_SQL}
        """,
        """
        ALTER TABLE knowledge_nodes
            ALTER CONSTRAINT fk_knowledge_nodes_parent_space DEFERRABLE INITIALLY DEFERRED
        """,
        """
        ALTER TABLE knowledge_revisions
            ALTER CONSTRAINT fk_knowledge_revisions_node_space DEFERRABLE INITIALLY DEFERRED
        """,
        """
        ALTER TABLE extraction_candidates
            ALTER CONSTRAINT fk_extraction_candidates_destination DEFERRABLE INITIALLY DEFERRED
        """,
        """
        ALTER TABLE candidate_review_results
            ALTER CONSTRAINT candidate_review_results_knowledge_node_id_fkey
            DEFERRABLE INITIALLY DEFERRED
        """,
        """
        ALTER TABLE candidate_review_results
            ALTER CONSTRAINT candidate_review_results_knowledge_revision_id_fkey
            DEFERRABLE INITIALLY DEFERRED
        """,
        """
        ALTER TABLE knowledge_write_results
            ALTER CONSTRAINT knowledge_write_results_node_id_fkey
            DEFERRABLE INITIALLY DEFERRED
        """,
        """
        ALTER TABLE review_cards
            ALTER CONSTRAINT fk_review_cards_node_space DEFERRABLE INITIALLY DEFERRED
        """,
        """
        ALTER TABLE review_cards
            ALTER CONSTRAINT review_cards_knowledge_revision_id_fkey
            DEFERRABLE INITIALLY DEFERRED
        """,
        """
        ALTER TABLE knowledge_evidence
            ALTER CONSTRAINT knowledge_evidence_revision_id_fkey
            DEFERRABLE INITIALLY DEFERRED
        """,
        "SET CONSTRAINTS ALL DEFERRED",
        "ALTER TABLE knowledge_nodes DISABLE TRIGGER knowledge_tree_shape_guard",
        """
        UPDATE knowledge_nodes node
        SET parent_id = mapping.new_id
        FROM d6_node_uuid_map mapping
        WHERE node.parent_id = mapping.old_id
        """,
        """
        UPDATE knowledge_revisions revision
        SET node_id = mapping.new_id
        FROM d6_node_uuid_map mapping
        WHERE revision.node_id = mapping.old_id
        """,
        """
        UPDATE extraction_candidates candidate
        SET suggested_destination_id = mapping.new_id
        FROM d6_node_uuid_map mapping
        WHERE candidate.suggested_destination_id = mapping.old_id
        """,
        """
        UPDATE candidate_review_results result
        SET knowledge_node_id = mapping.new_id
        FROM d6_node_uuid_map mapping
        WHERE result.knowledge_node_id = mapping.old_id
        """,
        """
        UPDATE knowledge_write_results result
        SET node_id = mapping.new_id
        FROM d6_node_uuid_map mapping
        WHERE result.node_id = mapping.old_id
        """,
        """
        UPDATE review_cards card
        SET knowledge_node_id = mapping.new_id
        FROM d6_node_uuid_map mapping
        WHERE card.knowledge_node_id = mapping.old_id
        """,
        """
        UPDATE knowledge_write_results result
        SET snapshot = jsonb_set(
            result.snapshot,
            '{node,id}',
            to_jsonb(mapping.new_id::text),
            false
        )
        FROM d6_node_uuid_map mapping
        WHERE result.snapshot #>> '{node,id}' = mapping.old_id::text
        """,
        """
        UPDATE knowledge_write_results result
        SET snapshot = jsonb_set(
            result.snapshot,
            '{node,parent_id}',
            to_jsonb(mapping.new_id::text),
            false
        )
        FROM d6_node_uuid_map mapping
        WHERE result.snapshot #>> '{node,parent_id}' = mapping.old_id::text
        """,
        """
        UPDATE knowledge_write_results result
        SET snapshot = jsonb_set(
            result.snapshot,
            '{node,path}',
            to_jsonb(replace(
                result.snapshot #>> '{node,path}',
                'n' || replace(mapping.old_id::text, '-', ''),
                'n' || replace(mapping.new_id::text, '-', '')
            )),
            false
        )
        FROM d6_node_uuid_map mapping
        WHERE result.snapshot #>> '{node,path}' LIKE
              '%' || 'n' || replace(mapping.old_id::text, '-', '') || '%'
        """,
        """
        UPDATE knowledge_write_results result
        SET snapshot = jsonb_set(
            result.snapshot,
            '{revision,node_id}',
            to_jsonb(mapping.new_id::text),
            false
        )
        FROM d6_node_uuid_map mapping
        WHERE result.snapshot #>> '{revision,node_id}' = mapping.old_id::text
        """,
        """
        UPDATE activity_events event
        SET entity_id = mapping.new_id
        FROM d6_node_uuid_map mapping
        WHERE event.entity_id = mapping.old_id
        """,
        """
        UPDATE activity_events event
        SET payload = jsonb_set(
            event.payload,
            '{knowledge_node_id}',
            to_jsonb(mapping.new_id::text),
            false
        )
        FROM d6_node_uuid_map mapping
        WHERE event.payload ->> 'knowledge_node_id' = mapping.old_id::text
        """,
        """
        UPDATE activity_events event
        SET payload = jsonb_set(
            event.payload,
            '{parent_id}',
            to_jsonb(mapping.new_id::text),
            false
        )
        FROM d6_node_uuid_map mapping
        WHERE event.payload ->> 'parent_id' = mapping.old_id::text
        """,
        """
        UPDATE outbox_events event
        SET aggregate_id = mapping.new_id
        FROM d6_node_uuid_map mapping
        WHERE event.aggregate_id = mapping.old_id
        """,
        """
        UPDATE outbox_events event
        SET payload = jsonb_set(
            event.payload,
            '{knowledge_node_id}',
            to_jsonb(mapping.new_id::text),
            false
        )
        FROM d6_node_uuid_map mapping
        WHERE event.payload ->> 'knowledge_node_id' = mapping.old_id::text
        """,
        """
        UPDATE outbox_events event
        SET payload = jsonb_set(
            event.payload,
            '{parent_id}',
            to_jsonb(mapping.new_id::text),
            false
        )
        FROM d6_node_uuid_map mapping
        WHERE event.payload ->> 'parent_id' = mapping.old_id::text
        """,
        """
        UPDATE knowledge_nodes node
        SET id = mapping.new_id
        FROM d6_node_uuid_map mapping
        WHERE node.id = mapping.old_id
        """,
        """
        UPDATE knowledge_nodes node
        SET current_revision_id = mapping.new_id
        FROM d6_revision_uuid_map mapping
        WHERE node.current_revision_id = mapping.old_id
        """,
        """
        UPDATE knowledge_evidence evidence
        SET revision_id = mapping.new_id
        FROM d6_revision_uuid_map mapping
        WHERE evidence.revision_id = mapping.old_id
        """,
        """
        UPDATE review_cards card
        SET knowledge_revision_id = mapping.new_id
        FROM d6_revision_uuid_map mapping
        WHERE card.knowledge_revision_id = mapping.old_id
        """,
        """
        UPDATE candidate_review_results result
        SET knowledge_revision_id = mapping.new_id
        FROM d6_revision_uuid_map mapping
        WHERE result.knowledge_revision_id = mapping.old_id
        """,
        """
        UPDATE knowledge_write_results result
        SET snapshot = jsonb_set(
            result.snapshot,
            '{node,current_revision_id}',
            to_jsonb(mapping.new_id::text),
            false
        )
        FROM d6_revision_uuid_map mapping
        WHERE result.snapshot #>> '{node,current_revision_id}' = mapping.old_id::text
        """,
        """
        UPDATE knowledge_write_results result
        SET snapshot = jsonb_set(
            result.snapshot,
            '{revision,id}',
            to_jsonb(mapping.new_id::text),
            false
        )
        FROM d6_revision_uuid_map mapping
        WHERE result.snapshot #>> '{revision,id}' = mapping.old_id::text
        """,
        """
        UPDATE activity_events event
        SET payload = jsonb_set(
            event.payload,
            '{revision_id}',
            to_jsonb(mapping.new_id::text),
            false
        )
        FROM d6_revision_uuid_map mapping
        WHERE event.payload ->> 'revision_id' = mapping.old_id::text
        """,
        """
        UPDATE activity_events event
        SET payload = jsonb_set(
            event.payload,
            '{knowledge_revision_id}',
            to_jsonb(mapping.new_id::text),
            false
        )
        FROM d6_revision_uuid_map mapping
        WHERE event.payload ->> 'knowledge_revision_id' = mapping.old_id::text
        """,
        """
        UPDATE outbox_events event
        SET payload = jsonb_set(
            event.payload,
            '{revision_id}',
            to_jsonb(mapping.new_id::text),
            false
        )
        FROM d6_revision_uuid_map mapping
        WHERE event.payload ->> 'revision_id' = mapping.old_id::text
        """,
        """
        UPDATE outbox_events event
        SET payload = jsonb_set(
            event.payload,
            '{knowledge_revision_id}',
            to_jsonb(mapping.new_id::text),
            false
        )
        FROM d6_revision_uuid_map mapping
        WHERE event.payload ->> 'knowledge_revision_id' = mapping.old_id::text
        """,
        """
        UPDATE knowledge_revisions revision
        SET id = mapping.new_id
        FROM d6_revision_uuid_map mapping
        WHERE revision.id = mapping.old_id
        """,
        """
        WITH RECURSIVE tree AS (
            SELECT node.id, ('n' || replace(node.id::text, '-', ''))::ltree AS path
            FROM knowledge_nodes node
            WHERE node.kind = 'root'
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
        """,
        "SET CONSTRAINTS ALL IMMEDIATE",
        "ALTER TABLE knowledge_nodes ENABLE TRIGGER knowledge_tree_shape_guard",
        """
        ALTER TABLE knowledge_nodes
            ALTER CONSTRAINT fk_knowledge_nodes_parent_space NOT DEFERRABLE
        """,
        """
        ALTER TABLE knowledge_revisions
            ALTER CONSTRAINT fk_knowledge_revisions_node_space NOT DEFERRABLE
        """,
        """
        ALTER TABLE extraction_candidates
            ALTER CONSTRAINT fk_extraction_candidates_destination NOT DEFERRABLE
        """,
        """
        ALTER TABLE candidate_review_results
            ALTER CONSTRAINT candidate_review_results_knowledge_node_id_fkey NOT DEFERRABLE
        """,
        """
        ALTER TABLE candidate_review_results
            ALTER CONSTRAINT candidate_review_results_knowledge_revision_id_fkey NOT DEFERRABLE
        """,
        """
        ALTER TABLE knowledge_write_results
            ALTER CONSTRAINT knowledge_write_results_node_id_fkey NOT DEFERRABLE
        """,
        """
        ALTER TABLE review_cards
            ALTER CONSTRAINT fk_review_cards_node_space NOT DEFERRABLE
        """,
        """
        ALTER TABLE review_cards
            ALTER CONSTRAINT review_cards_knowledge_revision_id_fkey NOT DEFERRABLE
        """,
        """
        ALTER TABLE knowledge_evidence
            ALTER CONSTRAINT knowledge_evidence_revision_id_fkey NOT DEFERRABLE
        """,
    )


def downgrade() -> None:
    # UUID identity repair is intentionally irreversible. Reintroducing non-RFC UUIDs would
    # restore a known API contract violation and could invalidate links written after upgrade.
    pass
