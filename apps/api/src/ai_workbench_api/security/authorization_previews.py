"""Bounded, one-use authorization preview storage."""

from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ai_workbench_api.security.repository_paths import RepositoryPathCandidate


class AuthorizationPreviewError(ValueError):
    """A preview lifecycle failure represented by a stable safe code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class AuthorizationPreview:
    """A newly issued opaque authorization preview."""

    token: str = field(repr=False)
    expires_at: datetime
    candidate: RepositoryPathCandidate = field(repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class _StoredPreview:
    candidate: RepositoryPathCandidate
    expires_monotonic: float
    expires_at: datetime


class AuthorizationPreviewStore:
    """Thread-safe process-local preview store with atomic consumption."""

    def __init__(
        self,
        *,
        ttl: timedelta = timedelta(minutes=10),
        capacity: int = 128,
        monotonic: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] | None = None,
    ) -> None:
        if ttl <= timedelta(0):
            raise ValueError("ttl must be positive")
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._ttl_seconds = ttl.total_seconds()
        self._capacity = capacity
        self._monotonic = monotonic
        self._utcnow = utcnow or (lambda: datetime.now(UTC))
        self._entries: OrderedDict[str, _StoredPreview] = OrderedDict()
        self._lock = threading.Lock()

    def create(self, candidate: RepositoryPathCandidate) -> AuthorizationPreview:
        """Store a candidate and return a 256-bit opaque, expiring token."""

        now = self._monotonic()
        expires_at = self._utcnow() + timedelta(seconds=self._ttl_seconds)
        with self._lock:
            self._purge_expired(now)
            while len(self._entries) >= self._capacity:
                self._entries.popitem(last=False)
            token = secrets.token_urlsafe(32)
            while token in self._entries:  # pragma: no cover - cryptographic collision defense
                token = secrets.token_urlsafe(32)
            self._entries[token] = _StoredPreview(
                candidate=candidate,
                expires_monotonic=now + self._ttl_seconds,
                expires_at=expires_at,
            )
        return AuthorizationPreview(token=token, expires_at=expires_at, candidate=candidate)

    def consume(self, token: str) -> RepositoryPathCandidate:
        """Atomically consume a live candidate; every token is usable at most once."""

        if not isinstance(token, str) or not token:
            raise AuthorizationPreviewError("authorization_preview_expired")
        now = self._monotonic()
        with self._lock:
            entry = self._entries.pop(token, None)
            self._purge_expired(now)
        if entry is None or entry.expires_monotonic <= now:
            raise AuthorizationPreviewError("authorization_preview_expired")
        return entry.candidate

    def __len__(self) -> int:
        with self._lock:
            self._purge_expired(self._monotonic())
            return len(self._entries)

    def _purge_expired(self, now: float) -> None:
        expired = [
            token
            for token, entry in self._entries.items()
            if entry.expires_monotonic <= now
        ]
        for token in expired:
            del self._entries[token]
