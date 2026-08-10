import asyncio
from pathlib import Path
from uuid import UUID

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from knowledge_workbench.config import get_settings
from knowledge_workbench.db.session import create_engine


def _assert_rfc_uuid(value: UUID) -> None:
    assert value.version in range(1, 9)
    assert value.variant == "specified in RFC 4122"


async def _assert_knowledge_tree_uuid_contract() -> None:
    engine = create_engine(get_settings())
    try:
        async with engine.connect() as connection:
            nodes = (
                await connection.execute(
                    text(
                        """
                        SELECT id, parent_id, current_revision_id
                        FROM knowledge_nodes
                        ORDER BY path
                        """
                    )
                )
            ).all()
            revisions = (
                await connection.execute(
                    text("SELECT id, node_id FROM knowledge_revisions")
                )
            ).all()
        assert nodes
        for node_id, parent_id, revision_id in nodes:
            _assert_rfc_uuid(node_id)
            if parent_id is not None:
                _assert_rfc_uuid(parent_id)
            if revision_id is not None:
                _assert_rfc_uuid(revision_id)
        for revision_id, node_id in revisions:
            _assert_rfc_uuid(revision_id)
            _assert_rfc_uuid(node_id)
    finally:
        await engine.dispose()


async def _clear_migration_test_records() -> None:
    engine = create_engine(get_settings())
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    DELETE FROM outbox_events
                    WHERE id = '01982ba0-4f20-7000-8000-000000000104'
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    DELETE FROM activity_events
                    WHERE id = '01982ba0-4f20-7000-8000-000000000103'
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    DELETE FROM knowledge_write_requests
                    WHERE id = '01982ba0-4f20-7000-8000-000000000101'
                    """
                )
            )
    finally:
        await engine.dispose()


async def _seed_legacy_uuid_references() -> None:
    engine = create_engine(get_settings())
    try:
        async with engine.begin() as connection:
            root_id, revision_id, child_id, child_path, space_id = (
                await connection.execute(
                    text(
                        """
                        SELECT root.id,
                               root.current_revision_id,
                               child.id,
                               child.path::text,
                               root.space_id
                        FROM knowledge_nodes root
                        JOIN knowledge_nodes child ON child.parent_id = root.id
                        WHERE root.kind = 'root'
                        ORDER BY child.path
                        LIMIT 1
                        """
                    )
                )
            ).one()
            legacy_root_id, legacy_revision_id = (
                await connection.execute(
                    text(
                        """
                        SELECT md5(
                                   'knowledge-root:' || CAST(:space_id AS text)
                               )::uuid,
                               md5(
                                   'knowledge-root-revision:' ||
                                   CAST(:space_id AS text)
                               )::uuid
                        """
                    ),
                    {"space_id": str(space_id)},
                )
            ).one()
            assert root_id != legacy_root_id
            assert revision_id != legacy_revision_id
            legacy_root_label = "n" + str(legacy_root_id).replace("-", "")
            current_root_label = "n" + str(root_id).replace("-", "")
            legacy_child_path = child_path.replace(current_root_label, legacy_root_label)

            await connection.execute(
                text(
                    """
                    ALTER TABLE knowledge_nodes
                        ALTER CONSTRAINT fk_knowledge_nodes_parent_space
                        DEFERRABLE INITIALLY DEFERRED
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    ALTER TABLE knowledge_revisions
                        ALTER CONSTRAINT fk_knowledge_revisions_node_space
                        DEFERRABLE INITIALLY DEFERRED
                    """
                )
            )
            await connection.execute(text("SET CONSTRAINTS ALL DEFERRED"))
            await connection.execute(
                text(
                    """
                    ALTER TABLE knowledge_nodes
                        DISABLE TRIGGER knowledge_tree_shape_guard
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    UPDATE knowledge_nodes child
                    SET parent_id = :legacy_root_id,
                        path = CAST(:legacy_child_path AS ltree)
                    WHERE child.id = :child_id
                    """
                ),
                {
                    "legacy_root_id": legacy_root_id,
                    "legacy_child_path": legacy_child_path,
                    "child_id": child_id,
                },
            )
            await connection.execute(
                text(
                    """
                    UPDATE knowledge_revisions revision
                    SET node_id = :legacy_root_id,
                        id = :legacy_revision_id
                    WHERE revision.id = :revision_id
                    """
                ),
                {
                    "legacy_root_id": legacy_root_id,
                    "legacy_revision_id": legacy_revision_id,
                    "revision_id": revision_id,
                },
            )
            await connection.execute(
                text(
                    """
                    UPDATE knowledge_nodes root
                    SET id = :legacy_root_id,
                        current_revision_id = :legacy_revision_id,
                        path = CAST(:legacy_root_label AS ltree)
                    WHERE root.id = :root_id
                    """
                ),
                {
                    "legacy_root_id": legacy_root_id,
                    "legacy_revision_id": legacy_revision_id,
                    "legacy_root_label": legacy_root_label,
                    "root_id": root_id,
                },
            )
            await connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            await connection.execute(
                text("ALTER TABLE knowledge_nodes ENABLE TRIGGER knowledge_tree_shape_guard")
            )
            await connection.execute(
                text(
                    """
                    ALTER TABLE knowledge_nodes
                        ALTER CONSTRAINT fk_knowledge_nodes_parent_space
                        NOT DEFERRABLE
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    ALTER TABLE knowledge_revisions
                        ALTER CONSTRAINT fk_knowledge_revisions_node_space
                        NOT DEFERRABLE
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO knowledge_write_requests (
                        id, space_id, idempotency_key, operation, request_hash
                    ) VALUES (
                        '01982ba0-4f20-7000-8000-000000000101',
                        :space_id,
                        'migration-uuid-contract',
                        'create',
                        :request_hash
                    )
                    """
                ),
                {"space_id": space_id, "request_hash": "a" * 64},
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO knowledge_write_results (
                        id, request_id, node_id, node_version, snapshot
                    ) VALUES (
                        '01982ba0-4f20-7000-8000-000000000102',
                        '01982ba0-4f20-7000-8000-000000000101',
                        :child_id,
                        1,
                        CAST(:snapshot AS jsonb)
                    )
                    """
                ),
                {
                    "child_id": child_id,
                    "snapshot": (
                        '{"node":{"id":"'
                        + str(child_id)
                        + '","parent_id":"'
                        + str(legacy_root_id)
                        + '","path":"'
                        + legacy_child_path
                        + '","current_revision_id":null},'
                        + '"revision":null}'
                    ),
                },
            )
            payload = (
                '{"knowledge_node_id":"'
                + str(child_id)
                + '","parent_id":"'
                + str(legacy_root_id)
                + '","revision_id":"'
                + str(legacy_revision_id)
                + '"}'
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO activity_events (
                        id, space_id, event_type, entity_type, entity_id,
                        actor_type, payload
                    ) VALUES (
                        '01982ba0-4f20-7000-8000-000000000103',
                        :space_id,
                        'knowledge_node.created',
                        'knowledge_node',
                        :child_id,
                        'user',
                        CAST(:payload AS jsonb)
                    )
                    """
                ),
                {"space_id": space_id, "child_id": child_id, "payload": payload},
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO outbox_events (
                        id, space_id, aggregate_type, aggregate_id, event_type,
                        deduplication_key, payload
                    ) VALUES (
                        '01982ba0-4f20-7000-8000-000000000104',
                        :space_id,
                        'knowledge_node',
                        :child_id,
                        'knowledge_node.created',
                        'migration-uuid-contract',
                        CAST(:payload AS jsonb)
                    )
                    """
                ),
                {"space_id": space_id, "child_id": child_id, "payload": payload},
            )
    finally:
        await engine.dispose()


async def _assert_legacy_uuid_references_repaired() -> None:
    engine = create_engine(get_settings())
    try:
        async with engine.connect() as connection:
            root_id, revision_id, root_path = (
                await connection.execute(
                    text(
                        """
                        SELECT id, current_revision_id, path::text
                        FROM knowledge_nodes
                        WHERE kind = 'root'
                        """
                    )
                )
            ).one()
            snapshot = (
                await connection.execute(
                    text(
                        """
                        SELECT snapshot
                        FROM knowledge_write_results
                        WHERE id = '01982ba0-4f20-7000-8000-000000000102'
                        """
                    )
                )
            ).scalar_one()
            activity_payload = (
                await connection.execute(
                    text(
                        """
                        SELECT payload
                        FROM activity_events
                        WHERE id = '01982ba0-4f20-7000-8000-000000000103'
                        """
                    )
                )
            ).scalar_one()
            outbox_payload = (
                await connection.execute(
                    text(
                        """
                        SELECT payload
                        FROM outbox_events
                        WHERE id = '01982ba0-4f20-7000-8000-000000000104'
                        """
                    )
                )
            ).scalar_one()

        assert snapshot["node"]["parent_id"] == str(root_id)
        assert snapshot["node"]["path"].startswith(root_path)
        assert activity_payload["parent_id"] == str(root_id)
        assert activity_payload["revision_id"] == str(revision_id)
        assert outbox_payload["parent_id"] == str(root_id)
        assert outbox_payload["revision_id"] == str(revision_id)
    finally:
        await engine.dispose()


def test_migration_replay() -> None:
    api_root = Path(__file__).resolve().parents[2]
    config = Config(str(api_root / "alembic.ini"))
    command.upgrade(config, "head")
    asyncio.run(_assert_knowledge_tree_uuid_contract())
    command.downgrade(config, "base")
    command.upgrade(config, "0006_d6_knowledge_tree")
    asyncio.run(_seed_legacy_uuid_references())
    command.upgrade(config, "head")
    asyncio.run(_assert_knowledge_tree_uuid_contract())
    asyncio.run(_assert_legacy_uuid_references_repaired())
    asyncio.run(_clear_migration_test_records())
