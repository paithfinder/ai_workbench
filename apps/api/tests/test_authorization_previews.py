"""Authorization preview token lifecycle tests."""

from datetime import UTC, datetime, timedelta

import pytest

from ai_workbench_api.security.authorization_previews import (
    AuthorizationPreviewError,
    AuthorizationPreviewStore,
)
from ai_workbench_api.security.repository_paths import RepositoryPathCandidate, RootIdentity


def candidate(name: str = "repo") -> RepositoryPathCandidate:
    return RepositoryPathCandidate(
        canonical_path=rf"D:\{name}",
        normalized_root_key=rf"d:\{name}",
        root_identity=RootIdentity(f"id:{name}"),
        display_name=name,
    )


def test_token_is_256_bits_opaque_and_one_use() -> None:
    store = AuthorizationPreviewStore()
    preview = store.create(candidate())

    assert len(preview.token) >= 43
    assert repr(preview).find(preview.token) == -1
    assert store.consume(preview.token) == candidate()
    with pytest.raises(AuthorizationPreviewError, match="authorization_preview_expired"):
        store.consume(preview.token)


def test_expiry_is_enforced() -> None:
    now = [100.0]
    wall = datetime(2026, 1, 1, tzinfo=UTC)
    store = AuthorizationPreviewStore(
        ttl=timedelta(seconds=10), monotonic=lambda: now[0], utcnow=lambda: wall
    )
    preview = store.create(candidate())
    now[0] = 110.0

    assert preview.expires_at == wall + timedelta(seconds=10)
    with pytest.raises(AuthorizationPreviewError, match="authorization_preview_expired"):
        store.consume(preview.token)


def test_capacity_evicts_oldest_live_candidate() -> None:
    store = AuthorizationPreviewStore(capacity=2)
    first = store.create(candidate("first"))
    second = store.create(candidate("second"))
    third = store.create(candidate("third"))

    assert len(store) == 2
    with pytest.raises(AuthorizationPreviewError):
        store.consume(first.token)
    assert store.consume(second.token).display_name == "second"
    assert store.consume(third.token).display_name == "third"
