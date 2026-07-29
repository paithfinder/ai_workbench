"""Windows repository path policy unit tests using an injectable adapter."""

from dataclasses import dataclass, field

import pytest

from ai_workbench_api.security.repository_paths import (
    RepositoryPathError,
    RootIdentity,
    WindowsRepositoryPathValidator,
)


@dataclass
class FakeAdapter:
    canonical_path: str = r"D:\WorkBench\Project"
    identity: RootIdentity = field(default_factory=lambda: RootIdentity("volume:file"))
    fixed: bool = True

    def canonicalize_directory(self, raw_path: str) -> str:
        del raw_path
        return self.canonical_path

    def root_identity(self, canonical_path: str) -> RootIdentity:
        del canonical_path
        return self.identity

    def is_fixed_local_drive(self, canonical_path: str) -> bool:
        del canonical_path
        return self.fixed


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("relative\\repo", "invalid_local_path"),
        ("D:relative", "invalid_local_path"),
        (r"\\server\share\repo", "network_path_denied"),
        (r"\\?\D:\repo", "network_path_denied"),
        (r"\\.\D:\repo", "network_path_denied"),
        (r"D:\repo\file.txt:secret", "invalid_local_path"),
        (r"D:\repo\CON", "invalid_local_path"),
        ("D:\\repo\\trailing. ", "invalid_local_path"),
        ("D:\\repo\x00name", "invalid_local_path"),
    ],
)
def test_rejects_unsafe_windows_paths(path: str, code: str) -> None:
    with pytest.raises(RepositoryPathError, match=code) as captured:
        WindowsRepositoryPathValidator(FakeAdapter()).validate(path)
    assert captured.value.code == code
    assert path not in str(captured.value)


def test_canonicalizes_and_builds_case_insensitive_comparison_key() -> None:
    candidate = WindowsRepositoryPathValidator(FakeAdapter()).validate(r"d:/workbench/project")

    assert candidate.canonical_path == r"D:\WorkBench\Project"
    assert candidate.normalized_root_key == r"d:\workbench\project"
    assert candidate.display_name == "Project"
    assert candidate.root_identity == RootIdentity("volume:file")


def test_denies_non_fixed_drive() -> None:
    with pytest.raises(RepositoryPathError, match="network_path_denied"):
        WindowsRepositoryPathValidator(FakeAdapter(fixed=False)).validate(r"Z:\repo")


def test_revalidation_fails_closed_when_root_identity_changes() -> None:
    adapter = FakeAdapter()
    validator = WindowsRepositoryPathValidator(adapter)
    candidate = validator.validate(r"D:\repo")
    adapter.identity = RootIdentity("volume:replacement")

    with pytest.raises(RepositoryPathError, match="authorization_candidate_changed"):
        validator.revalidate(candidate)
