"""Quota Ledger: tracks RPM, TPM and daily-token buckets per provider.

Free tiers throttle on requests-per-minute AND tokens-per-minute, so both are tracked with
sliding 60 s windows plus a daily token budget that resets at UTC midnight. A ``Bucket`` may be
shared between ledgers (the registry shares one bucket per credential) and may persist its daily
counter through a ``DailyStore`` so the app and the worker containers count the same day together.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, Optional, Protocol, Tuple


WINDOW_SECONDS = 60.0
DAY_SECONDS = 86_400.0
SYNC_SECONDS = 10.0  # how stale a persisted daily count may be before it is re-read


def utc_day(now: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))


def seconds_to_utc_midnight(now: Optional[float] = None) -> float:
    current = now if now is not None else time.time()
    return max(0.0, DAY_SECONDS - (current % DAY_SECONDS))


class DailyStore(Protocol):
    """Persisted per-day token counts, keyed by the bucket's credential key."""

    def load(self, key: str, day: str) -> Tuple[int, int]: ...  # (tokens, requests)

    def add(self, key: str, day: str, tokens: int, requests: int) -> None: ...


@dataclass
class Bucket:
    requests: Deque = field(default_factory=deque)
    tokens: Deque = field(default_factory=deque)  # (timestamp, tokens) pairs
    daily_tokens: int = 0
    day: str = field(default_factory=utc_day)
    key: str = ""                 # "vendor:credential-hash" for persistence and sharing
    store: Optional[DailyStore] = None
    synced_at: float = 0.0
    last_used: float = field(default_factory=time.time)

    def _roll_day(self, now: float) -> None:
        today = utc_day(now)
        if today != self.day:
            self.daily_tokens = 0
            self.day = today
            self.synced_at = 0.0

    def _sync(self, now: float) -> None:
        """Re-read the persisted count so another process's usage is visible within SYNC_SECONDS."""
        if self.store is None or now - self.synced_at < SYNC_SECONDS:
            return
        try:
            tokens, _ = self.store.load(self.key, self.day)
            self.daily_tokens = max(int(tokens), self.daily_tokens if self.synced_at == 0.0 else 0)
        except Exception:  # persistence must never block a request
            pass
        self.synced_at = now

    def _persist(self, tokens: int, requests: int) -> None:
        if self.store is None:
            return
        try:
            self.store.add(self.key, self.day, tokens, requests)
        except Exception:
            pass


class QuotaLedger:
    """Thread-safe per-provider rate accounting."""

    def __init__(
        self,
        limits: Dict[str, Tuple[int, int]],
        daily_limits: Optional[Dict[str, int]] = None,
        buckets: Optional[Dict[str, Bucket]] = None,
        bucket_factory: Optional[Callable[[str], Bucket]] = None,
    ):
        # limits: provider -> (rpm_limit, tpm_limit); daily_limits: provider -> tokens per UTC day (0 = uncapped)
        self._limits = dict(limits)
        self._daily: Dict[str, int] = {name: int(value) for name, value in (daily_limits or {}).items()}
        self._factory = bucket_factory or (lambda provider: Bucket())
        self._buckets: Dict[str, Bucket] = dict(buckets or {})
        for name in limits:
            self._buckets.setdefault(name, self._factory(name))
        self._lock = threading.Lock()

    def _bucket(self, provider: str) -> Bucket:
        bucket = self._buckets.get(provider)
        if bucket is None:
            bucket = self._factory(provider)
            self._buckets[provider] = bucket
        return bucket

    def register(self, provider: str, rpm_limit: int, tpm_limit: int, daily_limit: Optional[int] = None) -> None:
        with self._lock:
            self._limits[provider] = (rpm_limit, tpm_limit)
            if daily_limit is not None:
                self._daily[provider] = int(daily_limit)
            self._bucket(provider)

    def set_daily_limit(self, provider: str, tokens: int) -> None:
        """Cap tokens per UTC day for a provider (0 removes the cap); the hard stop behind the treasury."""
        with self._lock:
            self._daily[provider] = max(0, int(tokens))
            self._limits.setdefault(provider, (10**9, 10**9))
            self._bucket(provider)

    def daily_limit(self, provider: str) -> int:
        with self._lock:
            return int(self._daily.get(provider, 0))

    def tighten(self, provider: str, rpm_limit: int, tpm_limit: int) -> None:
        """Register, or lower existing limits to the stricter of the two policies.

        One credential must have exactly one bucket, so when two routing tables
        describe the same vendor the tighter ceiling wins.
        """
        with self._lock:
            current = self._limits.get(provider)
            if current is None:
                self._limits[provider] = (rpm_limit, tpm_limit)
            else:
                self._limits[provider] = (min(current[0], rpm_limit), min(current[1], tpm_limit))
            self._bucket(provider)

    def known(self, provider: str) -> bool:
        with self._lock:
            return provider in self._limits

    def providers(self) -> Tuple[str, ...]:
        with self._lock:
            return tuple(self._limits)

    def record_attempt(self, provider: str) -> None:
        """Count one HTTP request against the RPM window without charging tokens.

        Every real POST counts toward a vendor's request ceiling, including
        retries, rediscovery, and probes; tokens are charged separately on success.
        """
        now = time.time()
        with self._lock:
            bucket = self._bucket(provider)
            self._limits.setdefault(provider, (10**9, 10**9))
            self._prune(bucket, now)
            bucket.requests.append(now)
            bucket.last_used = now
            bucket._persist(0, 1)

    def _prune(self, bucket: Bucket, now: float) -> None:
        while bucket.requests and now - bucket.requests[0] > WINDOW_SECONDS:
            bucket.requests.popleft()
        while bucket.tokens and now - bucket.tokens[0][0] > WINDOW_SECONDS:
            bucket.tokens.popleft()
        bucket._roll_day(now)
        bucket._sync(now)

    def usage(self, provider: str) -> Dict[str, float]:
        """Current window usage snapshot (KeyError for a provider this ledger never registered)."""
        now = time.time()
        with self._lock:
            bucket = self._buckets[provider]
            self._prune(bucket, now)
            rpm, tpm = self._limits[provider]
            daily = int(self._daily.get(provider, 0))
            tokens_60s = sum(t for _, t in bucket.tokens)
            headroom = min(1 - len(bucket.requests) / max(rpm, 1), 1 - tokens_60s / max(tpm, 1))
            if daily > 0:
                headroom = min(headroom, 1 - bucket.daily_tokens / daily)
            return {
                "rpm_used": len(bucket.requests),
                "rpm_limit": rpm,
                "tpm_used": tokens_60s,
                "tpm_limit": tpm,
                "daily_tokens": bucket.daily_tokens,
                "daily_limit": daily,
                "day_resets_in": seconds_to_utc_midnight(now),
                "headroom": headroom,
            }

    def has_headroom(self, provider: str, est_tokens: int) -> bool:
        u = self.usage(provider)
        return (
            u["rpm_used"] < u["rpm_limit"]
            and u["tpm_used"] + est_tokens <= u["tpm_limit"]
            and (u["daily_limit"] <= 0 or u["daily_tokens"] + est_tokens <= u["daily_limit"])
        )

    def wait_seconds(self, provider: str, est_tokens: int) -> float:
        """How long until a request of est_tokens fits (0 if it fits now)."""
        now = time.time()
        with self._lock:
            bucket = self._buckets[provider]
            self._prune(bucket, now)
            rpm, tpm = self._limits[provider]
            need_req_wait = (
                WINDOW_SECONDS - (now - bucket.requests[0])
                if len(bucket.requests) >= rpm and bucket.requests
                else 0.0
            )
            tokens_60s = sum(t for _, t in bucket.tokens)
            need_tok_wait = (
                WINDOW_SECONDS - (now - bucket.tokens[0][0])
                if tokens_60s + est_tokens > tpm and bucket.tokens
                else 0.0
            )
            daily = int(self._daily.get(provider, 0))
            need_day_wait = seconds_to_utc_midnight(now) if daily > 0 and bucket.daily_tokens + est_tokens > daily else 0.0
            return max(0.0, need_req_wait, need_tok_wait, need_day_wait)

    def record(self, provider: str, tokens: int, count_request: bool = True) -> None:
        """Charge `tokens` for one completed request (and one request unless already counted)."""
        now = time.time()
        with self._lock:
            bucket = self._bucket(provider)
            self._limits.setdefault(provider, (10**9, 10**9))
            self._prune(bucket, now)
            charged = max(tokens, 1)
            if count_request:
                bucket.requests.append(now)
            bucket.tokens.append((now, charged))
            bucket.daily_tokens += charged
            bucket.last_used = now
            bucket._persist(charged, 1 if count_request else 0)
