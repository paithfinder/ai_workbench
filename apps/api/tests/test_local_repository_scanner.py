"""Security and determinism tests for the local repository scanner."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_workbench_api.security.repository_paths import identity_from_stat
from ai_workbench_api.sources.local_repository_scanner import (
    LocalRepositoryScanner,
    NativeDirectoryEnumerator,
    ScanError,
    ScanLimits,
    ScanRequest,
)


def request_for(root: Path, *, limits: ScanLimits | None = None) -> ScanRequest:
    return ScanRequest(
        root_path=str(root),
        normalized_root_key=os.path.normcase(os.path.normpath(root)),
        root_identity=identity_from_stat(root.stat()),
        policy_version="local-repository-v1",
        limits=limits or ScanLimits(),
    )


def test_manifest_is_relative_bounded_and_deterministic(tmp_path: Path) -> None:
    (tmp_path / "Z.py").write_text("print('z')\n", encoding="utf-8")
    nested = tmp_path / "src"
    nested.mkdir()
    (nested / "a.py").write_text("print('a')\n", encoding="utf-8")
    scanner = LocalRepositoryScanner()

    first = scanner.scan_sync(request_for(tmp_path))
    second = scanner.scan_sync(request_for(tmp_path))

    expected_paths = ["Z.py"] if os.name == "nt" else ["src/a.py", "Z.py"]
    assert [entry.relative_path for entry in first.entries] == expected_paths
    assert all(not Path(entry.relative_path).is_absolute() for entry in first.entries)
    assert first.manifest_hash == second.manifest_hash
    assert first.stats.eligible_files == len(expected_paths)


def test_hard_denies_take_priority_over_gitignore_negation(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("*\n!.env.example\n!safe.py\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("SECRET=never\n", encoding="utf-8")
    (tmp_path / "safe.py").write_text("safe = True\n", encoding="utf-8")
    (tmp_path / "ignored.py").write_text("ignored = True\n", encoding="utf-8")

    result = LocalRepositoryScanner().scan_sync(request_for(tmp_path))

    assert [entry.relative_path for entry in result.entries] == ["safe.py"]
    assert result.stats.skipped["hard_denied"] == 1
    assert result.stats.skipped["ignored"] >= 1
    assert "SECRET=never" not in repr(result)


def test_binary_private_key_oversize_and_dependency_tree_are_excluded(tmp_path: Path) -> None:
    (tmp_path / "binary.txt").write_bytes(b"hello\x00world")
    (tmp_path / "private.txt").write_text(
        "-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n", encoding="utf-8"
    )
    (tmp_path / "large.py").write_text("x" * 200, encoding="utf-8")
    dependencies = tmp_path / "node_modules"
    dependencies.mkdir()
    (dependencies / "leak.py").write_text("leak = True", encoding="utf-8")

    result = LocalRepositoryScanner().scan_sync(
        request_for(tmp_path, limits=ScanLimits(max_file_bytes=100))
    )

    assert result.entries == ()
    assert result.stats.skipped["binary_or_secret"] == 2
    assert result.stats.skipped["oversize"] == 1
    assert result.stats.skipped["hard_denied"] == 1


def test_invalid_utf8_outside_initial_probe_is_excluded(tmp_path: Path) -> None:
    (tmp_path / "binary.txt").write_bytes(b"a" * 9_000 + b"\xff")

    result = LocalRepositoryScanner().scan_sync(request_for(tmp_path))

    assert result.entries == ()
    assert result.stats.skipped["binary_or_secret"] == 1


def test_late_nul_and_chunk_boundary_private_key_are_excluded(tmp_path: Path) -> None:
    marker = b"-----BEGIN OPENSSH PRIVATE KEY-----"
    boundary_prefix = b"a" * (64 * 1024 - 10)
    (tmp_path / "late-nul.txt").write_bytes(b"a" * 9_000 + b"\x00late")
    (tmp_path / "boundary-key.txt").write_bytes(boundary_prefix + marker + b"\n")

    result = LocalRepositoryScanner().scan_sync(request_for(tmp_path))

    assert result.entries == ()
    assert result.stats.skipped["binary_or_secret"] == 2


def test_root_gitignore_is_bounded_and_identity_checked(tmp_path: Path) -> None:
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("ignored.py\n", encoding="utf-8")
    request = request_for(tmp_path)
    real_open = os.open
    replaced = False

    def replace_before_open(path: os.PathLike[str] | str, flags: int) -> int:
        nonlocal replaced
        if Path(path) == gitignore and not replaced:
            replaced = True
            gitignore.unlink()
            gitignore.write_text("replacement.py\n", encoding="utf-8")
        return real_open(path, flags)

    with patch("ai_workbench_api.sources.local_repository_scanner.os.open", replace_before_open):
        with pytest.raises(ScanError, match="scan_failed_closed"):
            LocalRepositoryScanner().scan_sync(request)

    gitignore.write_text("x" * 101, encoding="utf-8")
    with pytest.raises(ScanError, match="scan_limit_exceeded"):
        LocalRepositoryScanner().scan_sync(
            request_for(tmp_path, limits=ScanLimits(max_file_bytes=100))
        )


def test_entry_budget_bounds_enumeration(tmp_path: Path) -> None:
    for index in range(3):
        (tmp_path / f"special-{index}").mkdir()

    with pytest.raises(ScanError, match="scan_limit_exceeded"):
        LocalRepositoryScanner().scan_sync(
            request_for(tmp_path, limits=ScanLimits(max_entries=2))
        )


def test_root_gitignore_only_does_not_apply_nested_gitignore(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / ".gitignore").write_text("kept.py\n", encoding="utf-8")
    (nested / "kept.py").write_text("kept = True\n", encoding="utf-8")

    result = LocalRepositoryScanner().scan_sync(request_for(tmp_path))

    expected_paths = [] if os.name == "nt" else ["nested/kept.py"]
    assert [entry.relative_path for entry in result.entries] == expected_paths
    if os.name == "nt":
        assert result.stats.skipped["descendant_directory_denied"] == 1


class SwapBeforeOpenEnumerator:
    supports_descendants = True

    def __init__(self) -> None:
        self._native = NativeDirectoryEnumerator()

    def open(
        self, path: Path, expected: os.stat_result
    ) -> AbstractContextManager[Iterator[os.DirEntry[str]]]:
        @contextmanager
        def swapped() -> Iterator[Iterator[os.DirEntry[str]]]:
            replacement = path.with_name(f"{path.name}-replacement")
            path.rename(replacement)
            path.mkdir()
            with self._native.open(path, expected) as entries:
                yield entries

        return swapped()


def test_enumerator_swap_between_check_and_open_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "safe.py").write_text("safe = True\n", encoding="utf-8")

    with pytest.raises(ScanError, match="scan_failed_closed"):
        LocalRepositoryScanner(
            directory_enumerator=SwapBeforeOpenEnumerator()
        ).scan_sync(request_for(tmp_path))


def test_queued_directory_is_revalidated_before_enumeration(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("Windows denies descendant traversal")
    nested = tmp_path / "nested"
    nested.mkdir()
    queued = nested.lstat()
    scanner = LocalRepositoryScanner()
    real_lstat = Path.lstat
    nested_calls = 0

    def changed_lstat(path: Path) -> os.stat_result:
        nonlocal nested_calls
        result = real_lstat(path)
        if path == nested:
            nested_calls += 1
            if nested_calls > 1:
                values = list(result)
                values[1] = queued.st_ino + 1
                return os.stat_result(values)
        return result

    with patch.object(Path, "lstat", changed_lstat):
        with pytest.raises(ScanError, match="scan_failed_closed"):
            scanner.scan_sync(request_for(tmp_path))


def test_empty_manifest_has_stable_hash(tmp_path: Path) -> None:
    first = LocalRepositoryScanner().scan_sync(request_for(tmp_path))
    second = LocalRepositoryScanner().scan_sync(request_for(tmp_path))

    assert first.entries == ()
    assert first.manifest_hash == second.manifest_hash


def test_global_file_and_content_budgets_fail_entire_scan(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("12345", encoding="utf-8")
    (tmp_path / "b.py").write_text("67890", encoding="utf-8")

    with pytest.raises(ScanError, match="scan_limit_exceeded"):
        LocalRepositoryScanner().scan_sync(
            request_for(tmp_path, limits=ScanLimits(max_files=1))
        )
    with pytest.raises(ScanError, match="scan_limit_exceeded"):
        LocalRepositoryScanner().scan_sync(
            request_for(tmp_path, limits=ScanLimits(max_total_bytes=9))
        )


def test_windows_policy_denies_descendant_traversal(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "source.py").write_text("safe = True\n", encoding="utf-8")
    enumerator = NativeDirectoryEnumerator()
    if enumerator.supports_descendants:
        pytest.skip("Windows-only root scan policy")

    result = LocalRepositoryScanner(directory_enumerator=enumerator).scan_sync(
        request_for(tmp_path)
    )

    assert result.entries == ()
    assert result.stats.skipped["descendant_directory_denied"] == 1


def test_symlink_descendant_is_never_followed(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "secret.py").write_text("secret = True", encoding="utf-8")
    link = tmp_path / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink fixture unavailable: {exc.errno}")

    result = LocalRepositoryScanner().scan_sync(request_for(tmp_path))

    assert result.entries == ()
    assert result.stats.skipped["reparse_or_symlink"] == 1


def test_root_identity_mismatch_fails_without_path_disclosure(tmp_path: Path) -> None:
    request = request_for(tmp_path)
    replacement = tmp_path.parent / f"{tmp_path.name}-replacement"
    replacement.mkdir()
    changed = ScanRequest(
        root_path=request.root_path,
        normalized_root_key=request.normalized_root_key,
        root_identity=identity_from_stat(replacement.stat()),
        policy_version=request.policy_version,
    )

    with pytest.raises(ScanError, match="repository_root_changed") as captured:
        LocalRepositoryScanner().scan_sync(changed)
    assert str(tmp_path) not in str(captured.value)


def test_metadata_only_change_keeps_manifest_hash(tmp_path: Path) -> None:
    source = tmp_path / "same.py"
    source.write_text("same = True\n", encoding="utf-8")
    scanner = LocalRepositoryScanner()
    first = scanner.scan_sync(request_for(tmp_path))
    current = source.stat()
    os.utime(source, ns=(current.st_atime_ns, current.st_mtime_ns + 1_000_000))
    second = scanner.scan_sync(request_for(tmp_path))

    assert first.entries[0].modified_ns != second.entries[0].modified_ns
    assert first.manifest_hash == second.manifest_hash
