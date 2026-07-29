"""Fail-closed validation for local Windows repository roots."""

from __future__ import annotations

import ctypes
import ntpath
import os
import re
import stat as stat_module
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Protocol

POLICY_VERSION = "local-repository-v1"

_DRIVE_FIXED = 3
_INVALID_COMPONENT_CHARS = frozenset('<>"|?*')
_RESERVED_DEVICE_NAME = re.compile(
    r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", re.IGNORECASE
)
_DEVICE_PREFIXES = ("\\\\?\\", "\\\\.\\", "\\??\\", "//?/", "//./")


class RepositoryPathError(ValueError):
    """A path validation failure containing only a stable, non-sensitive code."""

    def __init__(self, code: str = "invalid_local_path") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class RootIdentity:
    """Opaque filesystem identity for an authorized repository root."""

    value: str


@dataclass(frozen=True, slots=True)
class RepositoryPathCandidate:
    """Validated repository root data suitable for a short-lived preview."""

    canonical_path: str
    normalized_root_key: str
    root_identity: RootIdentity
    display_name: str
    policy_version: str = POLICY_VERSION


class WindowsPathAdapter(Protocol):
    """Injectable platform operations used by Windows path validation."""

    def canonicalize_directory(self, raw_path: str) -> str:
        """Resolve an existing readable directory to its canonical path."""

    def root_identity(self, canonical_path: str) -> RootIdentity:
        """Return the stable identity of an existing directory."""

    def is_fixed_local_drive(self, canonical_path: str) -> bool:
        """Return whether the path resides on a local fixed drive."""


class RepositoryPathValidator(Protocol):
    """Backend integration contract for repository path authorization."""

    def validate(self, raw_path: str) -> RepositoryPathCandidate:
        """Validate and canonicalize a submitted repository root."""

    def revalidate(self, candidate: RepositoryPathCandidate) -> RepositoryPathCandidate:
        """Revalidate a preview candidate immediately before authorization."""


def identity_from_stat(stat_result: os.stat_result) -> RootIdentity:
    """Build a non-path filesystem identity from an ``os.stat`` result."""

    return RootIdentity(f"{stat_result.st_dev:x}:{stat_result.st_ino:x}")


class NativeWindowsPathAdapter:
    """Real Windows filesystem and fixed-volume operations."""

    def canonicalize_directory(self, raw_path: str) -> str:
        if os.name != "nt":
            raise RepositoryPathError()
        try:
            resolved = Path(raw_path).resolve(strict=True)
            if not resolved.is_dir():
                raise RepositoryPathError()
            # Opening the directory catches ACL-denied roots without reading descendants.
            with os.scandir(resolved):
                pass
            return str(resolved)
        except RepositoryPathError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise RepositoryPathError() from exc

    def root_identity(self, canonical_path: str) -> RootIdentity:
        try:
            result = os.stat(canonical_path, follow_symlinks=True)
        except OSError as exc:
            raise RepositoryPathError("authorization_candidate_changed") from exc
        if not stat_module.S_ISDIR(result.st_mode):
            raise RepositoryPathError("authorization_candidate_changed")
        return identity_from_stat(result)

    def is_fixed_local_drive(self, canonical_path: str) -> bool:
        if os.name != "nt":
            return False
        volume_path = ctypes.create_unicode_buffer(261)
        kernel32 = ctypes.windll.kernel32
        if not kernel32.GetVolumePathNameW(canonical_path, volume_path, len(volume_path)):
            return False
        return bool(kernel32.GetDriveTypeW(volume_path.value) == _DRIVE_FIXED)


class WindowsRepositoryPathValidator:
    """Validate Windows repository roots without accepting network/device paths."""

    def __init__(self, adapter: WindowsPathAdapter | None = None) -> None:
        self._adapter = adapter or NativeWindowsPathAdapter()

    def validate(self, raw_path: str) -> RepositoryPathCandidate:
        _validate_windows_syntax(raw_path)
        try:
            canonical_path = self._adapter.canonicalize_directory(raw_path)
        except RepositoryPathError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise RepositoryPathError() from exc

        _validate_windows_syntax(canonical_path)
        if not self._adapter.is_fixed_local_drive(canonical_path):
            raise RepositoryPathError("network_path_denied")

        identity = self._adapter.root_identity(canonical_path)
        windows_path = PureWindowsPath(canonical_path)
        canonical = str(windows_path)
        return RepositoryPathCandidate(
            canonical_path=canonical,
            normalized_root_key=ntpath.normcase(ntpath.normpath(canonical)),
            root_identity=identity,
            display_name=windows_path.name or windows_path.drive,
        )

    def revalidate(self, candidate: RepositoryPathCandidate) -> RepositoryPathCandidate:
        current = self.validate(candidate.canonical_path)
        if (
            current.normalized_root_key != candidate.normalized_root_key
            or current.root_identity != candidate.root_identity
            or current.policy_version != candidate.policy_version
        ):
            raise RepositoryPathError("authorization_candidate_changed")
        return current


def _validate_windows_syntax(raw_path: str) -> None:
    if not isinstance(raw_path, str) or not raw_path or not raw_path.strip() or "\x00" in raw_path:
        raise RepositoryPathError()
    if raw_path != raw_path.strip():
        raise RepositoryPathError()

    slash_normalized = raw_path.replace("/", "\\")
    lowered = slash_normalized.casefold()
    if slash_normalized.startswith("\\\\"):
        raise RepositoryPathError("network_path_denied")
    if any(lowered.startswith(prefix.casefold()) for prefix in _DEVICE_PREFIXES):
        raise RepositoryPathError()

    windows_path = PureWindowsPath(slash_normalized)
    if not windows_path.is_absolute() or not windows_path.drive or not windows_path.root:
        raise RepositoryPathError()
    if len(windows_path.drive) != 2 or windows_path.drive[1] != ":":
        raise RepositoryPathError("network_path_denied")

    for index, component in enumerate(windows_path.parts):
        if index == 0 and component.endswith("\\"):
            continue
        if component in {"", ".", ".."} or component.endswith((".", " ")):
            raise RepositoryPathError()
        if ":" in component or any(
            character in component for character in _INVALID_COMPONENT_CHARS
        ):
            raise RepositoryPathError()
        if _RESERVED_DEVICE_NAME.fullmatch(component):
            raise RepositoryPathError()
