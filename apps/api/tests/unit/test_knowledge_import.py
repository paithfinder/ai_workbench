from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from knowledge_workbench.application.knowledge_import import (
    KnowledgeImportCreate,
    prepare_knowledge_import,
)
from knowledge_workbench.config import Settings
from knowledge_workbench.core.errors import AppError


def request(*documents: tuple[str, str]) -> KnowledgeImportCreate:
    return KnowledgeImportCreate(
        parent_id=None,
        expected_parent_version=1,
        root_name="人工知识库",
        documents=[
            {"relative_path": relative_path, "body": body}
            for relative_path, body in documents
        ],
    )


def test_prepare_import_derives_folders_and_stable_hash() -> None:
    settings = Settings(app_env="test")
    first = prepare_knowledge_import(
        request(("编程/Python/异步.md", "正文"), ("产品/需求.txt", "需求")),
        settings=settings,
    )
    second = prepare_knowledge_import(
        request(("产品/需求.txt", "需求"), ("编程/Python/异步.md", "正文")),
        settings=settings,
    )

    assert first.root_name == "人工知识库"
    assert first.folders == (("产品",), ("编程",), ("编程", "Python"))
    assert first.folder_count == 4
    assert first.document_count == 2
    assert first.entry_count == 6
    assert first.request_hash == second.request_hash
    assert [item.relative_path for item in first.documents] == [
        "产品/需求.txt",
        "编程/Python/异步.md",
    ]


def test_prepare_import_hash_covers_target_and_body() -> None:
    settings = Settings(app_env="test")
    base = request(("README.md", "正文"))
    changed_body = request(("README.md", "修改"))
    changed_parent = base.model_copy(update={"parent_id": uuid4()})

    hashes = {
        prepare_knowledge_import(item, settings=settings).request_hash
        for item in (base, changed_body, changed_parent)
    }
    assert len(hashes) == 3


@pytest.mark.parametrize(
    "path",
    [
        "../secret.md",
        "/absolute.md",
        "C:/notes.md",
        "folder\\note.md",
        "folder//note.md",
        "folder/note.pdf",
        "folder/\x00note.md",
    ],
)
def test_prepare_import_rejects_unsafe_or_unsupported_paths(path: str) -> None:
    with pytest.raises(AppError) as caught:
        prepare_knowledge_import(request((path, "正文")), settings=Settings(app_env="test"))

    assert caught.value.code == "invalid_knowledge_import"
    assert caught.value.status_code == 422


def test_prepare_import_rejects_unsafe_root_names() -> None:
    for root_name in ("folder/name", "folder\\name"):
        invalid = request(("note.md", "正文")).model_copy(update={"root_name": root_name})
        with pytest.raises(AppError, match="path separators"):
            prepare_knowledge_import(invalid, settings=Settings(app_env="test"))


def test_prepare_import_rejects_casefold_duplicates_and_limits() -> None:
    with pytest.raises(AppError, match="duplicate"):
        prepare_knowledge_import(
            request(("Notes.md", "one"), ("notes.md", "two")),
            settings=Settings(app_env="test"),
        )

    with pytest.raises(AppError) as caught:
        prepare_knowledge_import(
            request(("note.md", "知识")),
            settings=Settings(app_env="test", knowledge_import_max_total_body_bytes=5),
        )
    assert caught.value.status_code == 413


def test_schema_forbids_unknown_fields_and_runtime_limits_document() -> None:
    with pytest.raises(ValidationError):
        KnowledgeImportCreate.model_validate(
            {
                "expected_parent_version": 1,
                "root_name": "Notes",
                "documents": [{"relative_path": "note.md", "body": "", "extra": True}],
            }
        )
    with pytest.raises(AppError, match="20000 characters"):
        prepare_knowledge_import(
            request(("note.md", "x" * 20_001)), settings=Settings(app_env="test")
        )
