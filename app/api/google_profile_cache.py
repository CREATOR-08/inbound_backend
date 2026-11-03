import hashlib
import os
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from time import monotonic

DEFAULT_GOOGLE_PROFILE_CACHE_TTL_SECONDS = 30.0
MAX_GOOGLE_PROFILE_CACHE_TTL_SECONDS = 300.0
MAX_GOOGLE_PROFILE_CACHE_ENTRIES = 2048


def _configured_cache_ttl() -> float:
    value = os.getenv(
        "GOOGLE_PROFILE_VERIFICATION_CACHE_TTL_SECONDS",
        str(DEFAULT_GOOGLE_PROFILE_CACHE_TTL_SECONDS),
    )
    try:
        ttl_seconds = float(value)
    except ValueError as error:
        raise RuntimeError(
            "GOOGLE_PROFILE_VERIFICATION_CACHE_TTL_SECONDS must be between 0 and 300."
        ) from error
    if not 0 <= ttl_seconds <= MAX_GOOGLE_PROFILE_CACHE_TTL_SECONDS:
        raise RuntimeError(
            "GOOGLE_PROFILE_VERIFICATION_CACHE_TTL_SECONDS must be between 0 and 300."
        )
    return ttl_seconds


@dataclass(frozen=True)
class _VerifiedGoogleIdentity:
    email: str
    expires_at: float


class GoogleProfileVerificationCache:
    def __init__(
        self,
        ttl_seconds: float,
        max_entries: int = MAX_GOOGLE_PROFILE_CACHE_ENTRIES,
    ) -> None:
        if not 0 <= ttl_seconds <= MAX_GOOGLE_PROFILE_CACHE_TTL_SECONDS:
            raise ValueError("Cache TTL must be between 0 and 300 seconds.")
        if max_entries < 1:
            raise ValueError("Cache capacity must be positive.")
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._entries: OrderedDict[bytes, _VerifiedGoogleIdentity] = OrderedDict()
        self._lock = RLock()

    @staticmethod
    def _key(access_token: str) -> bytes:
        return hashlib.sha256(access_token.encode("utf-8")).digest()

    @staticmethod
    def _expiry_timestamp(token_expiry: datetime) -> float:
        if token_expiry.tzinfo is None:
            token_expiry = token_expiry.replace(tzinfo=timezone.utc)
        return token_expiry.timestamp()

    def get(
        self,
        access_token: str,
        expected_email: str,
        token_expiry: datetime | None = None,
    ) -> str | None:
        key = self._key(access_token)
        now = monotonic()
        with self._lock:
            identity = self._entries.get(key)
            if identity is None:
                return None
            if identity.expires_at <= now:
                del self._entries[key]
                return None
            if (
                token_expiry is not None
                and self._expiry_timestamp(token_expiry) <= datetime.now(timezone.utc).timestamp()
            ):
                del self._entries[key]
                return None
            if identity.email.casefold() != expected_email.casefold():
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return identity.email

    def set(
        self,
        access_token: str,
        verified_email: str,
        token_expiry: datetime | None = None,
    ) -> None:
        if self._ttl_seconds == 0:
            return
        now = monotonic()
        expires_at = now + self._ttl_seconds
        if token_expiry is not None:
            token_ttl = self._expiry_timestamp(token_expiry) - datetime.now(
                timezone.utc
            ).timestamp()
            if token_ttl <= 0:
                return
            expires_at = min(expires_at, now + token_ttl)

        key = self._key(access_token)
        with self._lock:
            self._entries[key] = _VerifiedGoogleIdentity(
                email=verified_email,
                expires_at=expires_at,
            )
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def invalidate(self, access_token: str) -> None:
        with self._lock:
            self._entries.pop(self._key(access_token), None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


google_profile_verification_cache = GoogleProfileVerificationCache(
    _configured_cache_ttl()
)


def invalidate_google_profile_verification(access_token: str | None) -> None:
    if access_token:
        google_profile_verification_cache.invalidate(access_token)


def clear_google_profile_verification_cache() -> None:
    google_profile_verification_cache.clear()
