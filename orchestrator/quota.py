"""Quota Ledger: tracks RPM, TPM and daily-token buckets per provider.

Free tiers throttle on requests-per-minute AND tokens-per-minute, so both
are tracked with sliding 60s windows plus a rolling daily token budget.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Tuple


WINDOW_SECONDS = 60.0
DAY_SECONDS = 86_400.0


@dataclass
class Bucket:
    requests: Deque = field(default_factory=deque)
    tokens: Deque = field(default_factory=deque)  # (timestamp, tokens) pairs
    daily_tokens: int = 0
    day_started_at: float = field(default_factory=time.time)

    def _roll_day(self, now: float) -> None:
        if now - self.day_started_at >= DAY_SECONDS:
            self.daily_tokens = 0
            self.day_started_at = now


class QuotaLedger:
    """Thread-safe per-provider rate accounting."""

    def __init__(self, limits: Dict[str, Tuple[int, int]]):
        # limits: provider -> (rpm_limit, tpm_limit)
        self._limits = dict(limits)
        self._buckets: Dict[str, Bucket] = {
            name: Bucket() for name in limits
        }
        self._lock = threading.Lock()

    def register(self, provider: str, rpm_limit: int, tpm_limit: int) -> None:
        with self._lock:
            self._limits[provider] = (rpm_limit, tpm_limit)
            self._buckets.setdefault(provider, Bucket())

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
            self._buckets.setdefault(provider, Bucket())

    def known(self, provider: str) -> bool:
        with self._lock:
            return provider in self._limits

    def record_attempt(self, provider: str) -> None:
        """Count one HTTP request against the RPM window without charging tokens.

        Every real POST counts toward a vendor's request ceiling, including
        retries, rediscovery, and probes; tokens are charged separately on success.
        """
        now = time.time()
        with self._lock:
            bucket = self._buckets.setdefault(provider, Bucket())
            self._limits.setdefault(provider, (10**9, 10**9))
            self._prune(bucket, now)
            bucket.requests.append(now)

    def _prune(self, bucket: Bucket, now: float) -> None:
        while bucket.requests and now - bucket.requests[0] > WINDOW_SECONDS:
            bucket.requests.popleft()
        while bucket.tokens and now - bucket.tokens[0][0] > WINDOW_SECONDS:
            bucket.tokens.popleft()
        bucket._roll_day(now)

    def usage(self, provider: str) -> Dict[str, float]:
        """Current window usage snapshot."""
        now = time.time()
        with self._lock:
            bucket = self._buckets[provider]
            self._prune(bucket, now)
            rpm, tpm = self._limits[provider]
            tokens_60s = sum(t for _, t in bucket.tokens)
            return {
                "rpm_used": len(bucket.requests),
                "rpm_limit": rpm,
                "tpm_used": tokens_60s,
                "tpm_limit": tpm,
                "daily_tokens": bucket.daily_tokens,
                "headroom": min(
                    1 - len(bucket.requests) / max(rpm, 1),
                    1 - tokens_60s / max(tpm, 1),
                ),
            }

    def has_headroom(self, provider: str, est_tokens: int) -> bool:
        u = self.usage(provider)
        return (
            u["rpm_used"] < u["rpm_limit"]
            and u["tpm_used"] + est_tokens <= u["tpm_limit"]
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
            return max(0.0, need_req_wait, need_tok_wait)

    def record(self, provider: str, tokens: int, count_request: bool = True) -> None:
        """Charge `tokens` for one completed request (and one request unless already counted)."""
        now = time.time()
        with self._lock:
            bucket = self._buckets.setdefault(provider, Bucket())
            self._limits.setdefault(provider, (10**9, 10**9))
            self._prune(bucket, now)
            if count_request:
                bucket.requests.append(now)
            bucket.tokens.append((now, max(tokens, 1)))
            bucket.daily_tokens += max(tokens, 1)
