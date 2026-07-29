"""Bounded, deterministic, read-only local repository scanner."""

from __future__ import annotations

import codecs
import ctypes
import hashlib
import json
import os
import stat as stat_module
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Final, Protocol

import anyio
import pathspec

from ai_workbench_api.security.repository_paths import RootIdentity, identity_from_stat

_REPARSE_POINT: Final = 0x400
_READ_CHUNK_SIZE: Final = 64 * 1024
_PRIVATE_KEY_MARKERS: Final = (
    b"-----BEGIN PRIVATE KEY-----",
    b"-----BEGIN RSA PRIVATE KEY-----",
    b"-----BEGIN EC PRIVATE KEY-----",
    b"-----BEGIN DSA PRIVATE KEY-----",
    b"-----BEGIN OPENSSH PRIVATE KEY-----",
    b"-----BEGIN ENCRYPTED PRIVATE KEY-----",
)
_MAX_PRIVATE_KEY_MARKER_LENGTH: Final = max(map(len, _PRIVATE_KEY_MARKERS))
_HARD_DIRECTORY_NAMES: Final = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".cache",
        ".mypy_cache",
        ".next",
        ".npm",
        ".pnpm-store",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".turbo",
        ".venv",
        "__pycache__",
        "build",
        "coverage",
        "dist",
        "node_modules",
        "out",
        "site-packages",
        "target",
        "venv",
    }
)
_HARD_FILE_NAMES: Final = frozenset(
    {
        ".netrc",
        ".npmrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "secrets.json",
    }
)
_HARD_BINARY_EXTENSIONS: Final = frozenset(
    {
        ".7z", ".a", ".avi", ".bin", ".bmp", ".bz2", ".class", ".db", ".deb",
        ".dll", ".dmg", ".dylib", ".eot", ".exe", ".flac", ".gif", ".gz", ".ico",
        ".iso", ".jar", ".jpeg", ".jpg", ".jks", ".kdbx", ".key", ".lib", ".lockb",
        ".m4a", ".mkv", ".mov", ".mp3", ".mp4", ".msi", ".o", ".obj", ".otf", ".p12",
        ".pdf", ".pem", ".pfx", ".png", ".ppk", ".pyc", ".pyd", ".rar", ".rpm", ".so",
        ".sqlite", ".sqlite3", ".tar", ".tgz", ".tiff", ".ttf", ".wav", ".webm",
        ".webp", ".woff", ".woff2", ".xz", ".zip", ".zst",
    }
)
_TEXT_EXTENSIONS: Final = frozenset(
    {
        ".c", ".cc", ".cfg", ".clj", ".cmake", ".conf", ".cpp", ".cs", ".css",
        ".csv", ".cxx", ".dockerfile", ".editorconfig", ".fish", ".go", ".graphql",
        ".h", ".hpp", ".html", ".ini", ".java", ".js", ".json", ".jsx", ".kt",
        ".kts", ".less", ".lua", ".md", ".mdx", ".mjs", ".php", ".properties",
        ".proto", ".ps1", ".py", ".pyi", ".rb", ".rs", ".rst", ".scss", ".sh",
        ".sql", ".svelte", ".swift", ".toml", ".ts", ".tsx", ".txt", ".vue", ".xml",
        ".yaml", ".yml", ".zsh",
    }
)
_TEXT_FILE_NAMES: Final = frozenset(
    {
        "dockerfile", "gemfile", "justfile", "license", "makefile", "procfile", "readme",
    }
)


class DirectoryEnumerator(Protocol):
    """Open a directory for enumeration while binding it to a checked identity."""

    @property
    def supports_descendants(self) -> bool:
        """Whether descendant directory enumeration is safely supported."""

    def open(
        self, path: Path, expected: os.stat_result
    ) -> AbstractContextManager[Iterator[os.DirEntry[str]]]:
        """Return a bounded-lifetime iterator tied to the expected directory."""


class NativeDirectoryEnumerator:
    """Handle-tied POSIX enumeration and root-only guarded Windows enumeration."""

    @property
    def supports_descendants(self) -> bool:
        # Python exposes fd-relative scandir on POSIX. On Windows os.scandir cannot
        # enumerate via a directory handle, so descendants are denied for Day 2.
        return os.name != "nt"

    @contextmanager
    def open(
        self, path: Path, expected: os.stat_result
    ) -> Iterator[Iterator[os.DirEntry[str]]]:
        if os.name == "nt":
            with _open_windows_directory_guard(path, expected):
                with os.scandir(path) as entries:
                    yield entries
            return

        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise ScanError("scan_failed_closed") from exc
        try:
            opened = os.fstat(descriptor)
            if (
                not stat_module.S_ISDIR(opened.st_mode)
                or not _same_directory_identity(expected, opened)
            ):
                raise ScanError("scan_failed_closed")
            with os.scandir(descriptor) as entries:
                yield entries
        except OSError as exc:
            raise ScanError("scan_failed_closed") from exc
        finally:
            os.close(descriptor)


class ScanError(RuntimeError):
    """A scanner failure represented only by a stable non-sensitive code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ScanLimits:
    """Hard resource limits for one scan."""

    max_directories: int = 5_000
    max_files: int = 20_000
    max_depth: int = 40
    max_file_bytes: int = 2 * 1024 * 1024
    max_total_bytes: int = 100 * 1024 * 1024
    timeout_seconds: float = 30.0
    max_entries: int = 100_000

    def __post_init__(self) -> None:
        values = (
            self.max_directories,
            self.max_files,
            self.max_entries,
            self.max_depth,
            self.max_file_bytes,
            self.max_total_bytes,
        )
        if any(value < 1 for value in values) or self.timeout_seconds <= 0:
            raise ValueError("scan limits must be positive")


@dataclass(frozen=True, slots=True)
class ScanRequest:
    """Validated authorization snapshot passed from the repository service."""

    root_path: str
    normalized_root_key: str
    root_identity: RootIdentity
    policy_version: str
    limits: ScanLimits = field(default_factory=ScanLimits)
    ignore_patterns: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """Path-safe immutable metadata for an eligible source file."""

    relative_path: str
    content_hash: str
    size_bytes: int
    modified_ns: int
    file_identity: str


@dataclass(frozen=True, slots=True)
class ScanStats:
    """Aggregate scan statistics safe to return or audit."""

    directories_visited: int
    files_seen: int
    eligible_files: int
    eligible_bytes: int
    skipped: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Deterministic repository manifest and aggregate statistics."""

    entries: tuple[ManifestEntry, ...]
    manifest_hash: str
    stats: ScanStats


class LocalRepositoryScanner:
    """Scan an authorized root without subprocesses, Git, writes, or link traversal."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        directory_enumerator: DirectoryEnumerator | None = None,
    ) -> None:
        self._monotonic = monotonic
        self._directory_enumerator = directory_enumerator or NativeDirectoryEnumerator()

    async def scan(self, request: ScanRequest) -> ScanResult:
        """Run the blocking scanner in a worker thread."""

        return await anyio.to_thread.run_sync(self.scan_sync, request)

    def scan_sync(self, request: ScanRequest) -> ScanResult:
        """Run a bounded scan and fail closed if its authorization snapshot changes."""

        started = self._monotonic()
        root = Path(request.root_path)
        try:
            root_resolved = root.resolve(strict=True)
            root_stat = root_resolved.lstat()
        except OSError as exc:
            raise ScanError("repository_root_changed") from exc
        if (
            identity_from_stat(root_stat) != request.root_identity
            or not stat_module.S_ISDIR(root_stat.st_mode)
            or _is_link_or_reparse(root_resolved, root_stat)
        ):
            raise ScanError("repository_root_changed")

        ignore = self._load_ignore(root_resolved, request, started)
        entries: list[ManifestEntry] = []
        skipped: Counter[str] = Counter()
        directories_visited = 0
        files_seen = 0
        eligible_bytes = 0
        entries_seen = 0
        stack: list[tuple[Path, int, os.stat_result]] = [(root_resolved, 0, root_stat)]

        while stack:
            self._check_deadline(started, request.limits)
            directory, depth, queued_stat = stack.pop()
            self._verify_directory_before_enumeration(
                root_resolved, directory, queued_stat, request.root_identity
            )
            directories_visited += 1
            if directories_visited > request.limits.max_directories:
                raise ScanError("scan_limit_exceeded")

            pending_directories: list[tuple[Path, int, os.stat_result]] = []
            try:
                with self._directory_enumerator.open(directory, queued_stat) as children:
                    for child in children:
                        entries_seen += 1
                        if entries_seen > request.limits.max_entries:
                            raise ScanError("scan_limit_exceeded")
                        self._check_deadline(started, request.limits)
                        child_path = Path(child.path)
                        relative_path = self._relative_path(root_resolved, child_path)
                        try:
                            # DirEntry.stat() may report zero file IDs on Windows; Path.lstat()
                            # uses the full stat implementation needed for identity checks.
                            child_stat = child_path.lstat()
                        except OSError:
                            skipped["unavailable"] += 1
                            continue
                        if _is_link_or_reparse(child_path, child_stat):
                            skipped["reparse_or_symlink"] += 1
                            continue

                        if stat_module.S_ISDIR(child_stat.st_mode):
                            if _hard_denied_directory(child.name):
                                skipped["hard_denied"] += 1
                            elif ignore.match_file(f"{relative_path}/"):
                                skipped["ignored"] += 1
                            elif depth >= request.limits.max_depth:
                                raise ScanError("scan_limit_exceeded")
                            elif not self._directory_enumerator.supports_descendants:
                                skipped["descendant_directory_denied"] += 1
                            else:
                                pending_directories.append(
                                    (child_path, depth + 1, child_stat)
                                )
                            continue
                        if not stat_module.S_ISREG(child_stat.st_mode):
                            skipped["special_file"] += 1
                            continue

                        files_seen += 1
                        if files_seen > request.limits.max_files:
                            raise ScanError("scan_limit_exceeded")
                        # Security order: hard policy always precedes ignore rules.
                        if _hard_denied_file(child.name):
                            skipped["hard_denied"] += 1
                        elif ignore.match_file(relative_path):
                            skipped["ignored"] += 1
                        elif not _is_allowed_text_name(child.name):
                            skipped["unsupported_type"] += 1
                        elif child_stat.st_size > request.limits.max_file_bytes:
                            skipped["oversize"] += 1
                        else:
                            entry = self._hash_stable_file(
                                root_resolved,
                                child_path,
                                relative_path,
                                child_stat,
                                request,
                                started,
                            )
                            if entry is None:
                                skipped["binary_or_secret"] += 1
                            else:
                                eligible_bytes += entry.size_bytes
                                if eligible_bytes > request.limits.max_total_bytes:
                                    raise ScanError("scan_limit_exceeded")
                                entries.append(entry)
            except ScanError:
                raise
            except OSError as exc:
                raise ScanError("scan_failed_closed") from exc

            # Traversal order need not be stable: manifest entries are sorted below.
            stack.extend(pending_directories)

        ending_root_stat = self._safe_root_stat(root_resolved)
        if identity_from_stat(ending_root_stat) != request.root_identity:
            raise ScanError("repository_root_changed")

        entries.sort(key=lambda entry: (entry.relative_path.casefold(), entry.relative_path))
        immutable_entries = tuple(entries)
        manifest_hash = _manifest_hash(request.policy_version, immutable_entries)
        return ScanResult(
            entries=immutable_entries,
            manifest_hash=manifest_hash,
            stats=ScanStats(
                directories_visited=directories_visited,
                files_seen=files_seen,
                eligible_files=len(immutable_entries),
                eligible_bytes=eligible_bytes,
                skipped=MappingProxyType(dict(sorted(skipped.items()))),
            ),
        )

    def _load_ignore(
        self, root: Path, request: ScanRequest, started: float
    ) -> pathspec.PathSpec:
        """Read only the root .gitignore through one no-follow, bounded handle."""

        patterns = list(request.ignore_patterns)
        gitignore = root / ".gitignore"
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        try:
            before = gitignore.lstat()
        except FileNotFoundError:
            descriptor = None
        except OSError as exc:
            raise ScanError("scan_failed_closed") from exc
        else:
            if (
                not stat_module.S_ISREG(before.st_mode)
                or _is_link_or_reparse(gitignore, before)
            ):
                raise ScanError("scan_failed_closed")
            try:
                descriptor = _open_no_follow(gitignore, flags)
            except OSError as exc:
                raise ScanError("scan_failed_closed") from exc
        if descriptor is not None:
            raw_parts: list[bytes] = []
            total = 0
            try:
                opened = os.fstat(descriptor)
                if (
                    not stat_module.S_ISREG(opened.st_mode)
                    or _is_link_or_reparse(gitignore, opened)
                    or not _same_file_snapshot(before, opened)
                ):
                    raise ScanError("scan_failed_closed")
                if opened.st_size > request.limits.max_file_bytes:
                    raise ScanError("scan_limit_exceeded")
                while True:
                    self._check_deadline(started, request.limits)
                    chunk = os.read(descriptor, _READ_CHUNK_SIZE)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > request.limits.max_file_bytes:
                        raise ScanError("scan_limit_exceeded")
                    raw_parts.append(chunk)
                after = os.fstat(descriptor)
                if not _same_file_snapshot(opened, after) or total != opened.st_size:
                    raise ScanError("scan_failed_closed")
            except OSError as exc:
                raise ScanError("scan_failed_closed") from exc
            finally:
                os.close(descriptor)
            raw = b"".join(raw_parts)
            if b"\x00" in raw:
                raise ScanError("scan_failed_closed")
            try:
                patterns.extend(raw.decode("utf-8").splitlines())
            except UnicodeDecodeError as exc:
                raise ScanError("scan_failed_closed") from exc
        try:
            return pathspec.PathSpec.from_lines("gitwildmatch", patterns)
        except (TypeError, ValueError) as exc:
            raise ScanError("scan_failed_closed") from exc

    def _hash_stable_file(
        self,
        root: Path,
        path: Path,
        relative_path: str,
        before: os.stat_result,
        request: ScanRequest,
        started: float,
    ) -> ManifestEntry | None:
        self._verify_root(root, request.root_identity)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        try:
            descriptor = _open_no_follow(path, flags)
        except OSError:
            return None
        digest = hashlib.sha256()
        decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        total = 0
        marker_overlap = b""
        contains_secret_marker = False
        contains_nul = False
        try:
            opened = os.fstat(descriptor)
            if not _same_file_snapshot(before, opened):
                raise ScanError("scan_failed_closed")
            while True:
                self._check_deadline(started, request.limits)
                chunk = os.read(descriptor, _READ_CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > request.limits.max_file_bytes:
                    raise ScanError("scan_failed_closed")
                contains_nul = contains_nul or b"\x00" in chunk
                marker_window = marker_overlap + chunk
                contains_secret_marker = contains_secret_marker or any(
                    marker in marker_window for marker in _PRIVATE_KEY_MARKERS
                )
                marker_overlap = marker_window[-(_MAX_PRIVATE_KEY_MARKER_LENGTH - 1) :]
                try:
                    decoder.decode(chunk, final=False)
                except UnicodeDecodeError:
                    return None
                digest.update(chunk)
            try:
                decoder.decode(b"", final=True)
            except UnicodeDecodeError:
                return None
            after = os.fstat(descriptor)
        except OSError as exc:
            raise ScanError("scan_failed_closed") from exc
        finally:
            os.close(descriptor)
        if not _same_file_snapshot(before, after) or total != before.st_size:
            raise ScanError("scan_failed_closed")
        try:
            path_after = path.lstat()
        except OSError as exc:
            raise ScanError("scan_failed_closed") from exc
        if _is_link_or_reparse(path, path_after) or not _same_file_snapshot(before, path_after):
            raise ScanError("scan_failed_closed")
        self._verify_root(root, request.root_identity)
        if contains_nul or contains_secret_marker:
            return None
        return ManifestEntry(
            relative_path=relative_path,
            content_hash=digest.hexdigest(),
            size_bytes=total,
            modified_ns=before.st_mtime_ns,
            file_identity=_stat_identity(before),
        )

    def _verify_directory_before_enumeration(
        self,
        root: Path,
        directory: Path,
        queued_stat: os.stat_result,
        expected_root: RootIdentity,
    ) -> None:
        """Revalidate a queued descendant immediately before directory enumeration."""

        self._relative_path(root, directory)
        self._verify_root(root, expected_root)
        try:
            current = directory.lstat()
        except OSError as exc:
            raise ScanError("scan_failed_closed") from exc
        if (
            not stat_module.S_ISDIR(current.st_mode)
            or _is_link_or_reparse(directory, current)
            or not _same_directory_identity(queued_stat, current)
        ):
            raise ScanError("scan_failed_closed")

    @staticmethod
    def _relative_path(root: Path, child: Path) -> str:
        try:
            if os.path.commonpath((os.fspath(root), os.fspath(child))) != os.fspath(root):
                raise ScanError("scan_failed_closed")
            relative = child.relative_to(root)
        except (OSError, ValueError) as exc:
            raise ScanError("scan_failed_closed") from exc
        if relative.is_absolute() or ".." in relative.parts:
            raise ScanError("scan_failed_closed")
        return relative.as_posix()

    @staticmethod
    def _safe_root_stat(root: Path) -> os.stat_result:
        try:
            result = root.lstat()
        except OSError as exc:
            raise ScanError("repository_root_changed") from exc
        if not stat_module.S_ISDIR(result.st_mode) or _is_link_or_reparse(root, result):
            raise ScanError("repository_root_changed")
        return result

    def _verify_root(self, root: Path, expected: RootIdentity) -> None:
        if identity_from_stat(self._safe_root_stat(root)) != expected:
            raise ScanError("repository_root_changed")

    def _check_deadline(self, started: float, limits: ScanLimits) -> None:
        if self._monotonic() - started > limits.timeout_seconds:
            raise ScanError("scan_limit_exceeded")


@contextmanager
def _open_windows_directory_guard(
    path: Path, expected: os.stat_result
) -> Iterator[None]:
    """Hold a no-reparse Windows directory handle without delete sharing."""

    if os.name != "nt":
        yield
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        str(path),
        0x80,  # FILE_READ_ATTRIBUTES
        0x1 | 0x2,  # share read/write, deliberately deny delete/rename
        None,
        3,  # OPEN_EXISTING
        0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle in {None, invalid_handle}:
        raise ScanError("scan_failed_closed")
    try:
        current = path.lstat()
        if (
            not stat_module.S_ISDIR(current.st_mode)
            or _is_link_or_reparse(path, current)
            or not _same_directory_identity(expected, current)
        ):
            raise ScanError("scan_failed_closed")
        yield
        after = path.lstat()
        if (
            _is_link_or_reparse(path, after)
            or not _same_directory_identity(expected, after)
        ):
            raise ScanError("scan_failed_closed")
    except OSError as exc:
        raise ScanError("scan_failed_closed") from exc
    finally:
        kernel32.CloseHandle(handle)


def _open_no_follow(path: Path, flags: int) -> int:
    """Open a file without following links where the platform supports it."""

    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if no_follow:
        return os.open(path, flags | no_follow)
    # Windows has no os.O_NOFOLLOW; callers lstat immediately before opening and
    # compare that snapshot with fstat before consuming any bytes.
    return os.open(path, flags)


def _is_link_or_reparse(path: Path, result: os.stat_result) -> bool:
    del path
    return stat_module.S_ISLNK(result.st_mode) or bool(
        (getattr(result, "st_file_attributes", 0) or 0) & _REPARSE_POINT
    )


def _hard_denied_directory(name: str) -> bool:
    return name.casefold() in _HARD_DIRECTORY_NAMES


def _hard_denied_file(name: str) -> bool:
    lowered = name.casefold()
    suffix = Path(lowered).suffix
    return (
        lowered == ".env"
        or lowered.startswith(".env.")
        or lowered in _HARD_FILE_NAMES
        or suffix in _HARD_BINARY_EXTENSIONS
        or "credential" in lowered
        or "secret" in lowered
    )


def _is_allowed_text_name(name: str) -> bool:
    lowered = name.casefold()
    return Path(lowered).suffix in _TEXT_EXTENSIONS or lowered in _TEXT_FILE_NAMES


def _stat_identity(result: os.stat_result) -> str:
    return f"{result.st_dev:x}:{result.st_ino:x}"


def _same_directory_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and stat_module.S_IFMT(left.st_mode) == stat_module.S_IFMT(right.st_mode)
    )


def _same_file_snapshot(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
        and stat_module.S_IFMT(left.st_mode) == stat_module.S_IFMT(right.st_mode)
    )


def _manifest_hash(policy_version: str, entries: tuple[ManifestEntry, ...]) -> str:
    payload = {
        "policy_version": policy_version,
        "entries": [
            {
                "relative_path": entry.relative_path,
                "size_bytes": entry.size_bytes,
                "content_hash": entry.content_hash,
            }
            for entry in entries
        ],
    }
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
