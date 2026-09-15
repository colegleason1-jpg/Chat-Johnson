"""Process-wide quota buckets, one per credential, shared by every ledger that uses that credential.

The UI's script threads and the job runner's worker threads draw on the same per-vendor buckets,
so a mission in the background and a chat send in the foreground are metered together. A bucket
is keyed by ``vendor:sha256(key)[:16]``: adding a second vendor's key never resets the first
vendor's counters, and two visitors with different keys never share a bucket. Daily counts are
persisted in the vault so the app and the worker containers count one day together and a restart
keeps the count.
"""
from __future__ import annotations

import hashlib
import threading
import time
from typing import Dict, Optional, Tuple

from .config import PROVIDERS, daily_cap, resolve_secret
from .quota import Bucket, DailyStore, QuotaLedger

_BUCKETS: Dict[str, Bucket] = {}
_LOCKS: Dict[str, threading.Lock] = {}
_REGISTRY_LOCK = threading.Lock()
EVICT_AFTER_SECONDS = 48 * 3600.0
_STORE: Optional[DailyStore] = None
_LAST_EVICTION = 0.0


class VaultDailyStore:
    """Daily counters in the vault's ``quota_usage`` table (lazy import: the vault imports nothing from here)."""

    def load(self, key: str, day: str) -> Tuple[int, int]:
        from . import vault

        return vault.quota_usage_load(key, day)

    def add(self, key: str, day: str, tokens: int, requests: int) -> None:
        from . import vault

        vault.quota_usage_add(key, day, tokens, requests)


def daily_store() -> DailyStore:
    global _STORE
    if _STORE is None:
        _STORE = VaultDailyStore()
    return _STORE


def _key_material(vendor: str) -> str:
    """The credential hash for a vendor in this context, from the first keyed env name ("none" without a key)."""
    from .router import BYOK_ENV_KEYS  # local import: router imports config, so keep this module light

    for env in BYOK_ENV_KEYS.get(vendor, ()):
        value = resolve_secret(env)
        if value:
            return hashlib.sha256(value.encode()).hexdigest()[:16]
    if vendor == "local" and resolve_secret("CHAT_JOHNSON_LOCAL_KEY"):
        return "local"
    return "none"


def bucket_key(vendor: str) -> str:
    return f"{vendor}:{_key_material(vendor)}"


def credential_fingerprint() -> str:
    """Non-reversible id of the keys in effect for this context (overlay + environment)."""
    from .router import BYOK_ENV_KEYS

    material = "|".join(f"{vendor}:{_key_material(vendor)}" for vendor in BYOK_ENV_KEYS if _key_material(vendor) != "none")
    return hashlib.sha256(material.encode()).hexdigest()[:16] if material else "no-keys"


def _vendor_limits() -> Dict[str, Tuple[int, int]]:
    from .discovery import vendor_for

    # One credential = one bucket: start from the legacy ceilings; Cortex tightens them later.
    limits: Dict[str, Tuple[int, int]] = {}
    for name, cfg in PROVIDERS.items():
        vendor = vendor_for(name)
        rpm, tpm = limits.get(vendor, (cfg.rpm_limit, cfg.tpm_limit))
        limits[vendor] = (min(rpm, cfg.rpm_limit), min(tpm, cfg.tpm_limit))
    return limits


def _shared_bucket(vendor: str) -> Bucket:
    """The process-wide bucket for this vendor's credential, created with its persisted daily count."""
    key = bucket_key(vendor)
    with _REGISTRY_LOCK:
        bucket = _BUCKETS.get(key)
        if bucket is None:
            bucket = Bucket(key=key, store=daily_store())
            bucket._sync(time.time())
            _BUCKETS[key] = bucket
        bucket.last_used = time.time()
        return bucket


def _evict_stale() -> None:
    global _LAST_EVICTION
    now = time.time()
    if now - _LAST_EVICTION < 600.0:
        return
    _LAST_EVICTION = now
    with _REGISTRY_LOCK:
        for key in [k for k, b in _BUCKETS.items() if now - b.last_used > EVICT_AFTER_SECONDS]:
            _BUCKETS.pop(key, None)


def get_quota_ledger() -> QuotaLedger:
    """A ledger over the shared buckets of the keys bound in this context; daily caps for every vendor."""
    from .router import BYOK_ENV_KEYS

    _evict_stale()
    limits = _vendor_limits()
    vendors = set(limits) | set(BYOK_ENV_KEYS) | {"local"}
    daily = {vendor: daily_cap(vendor) for vendor in vendors}
    buckets = {vendor: _shared_bucket(vendor) for vendor in vendors}
    return QuotaLedger(limits, daily, buckets=buckets, bucket_factory=_shared_bucket)


def get_request_lock() -> threading.Lock:
    """Serialises the quota check, the request, and the ledger record for one credential set."""
    fingerprint = credential_fingerprint()
    with _REGISTRY_LOCK:
        lock = _LOCKS.get(fingerprint)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[fingerprint] = lock
        return lock


CHAT_LOCK_TIMEOUT_SECONDS = 20.0   # a chat send waits this long for a background job to release the provider, then proceeds
JOB_YIELD_SECONDS = 30.0           # a background job steps aside for a waiting chat send for at most this long
_CHAT_WAITING = threading.Event()  # set while the operator's chat is waiting for the request lock


def acquire_for_chat(lock: threading.Lock, timeout: float = CHAT_LOCK_TIMEOUT_SECONDS) -> bool:
    """The operator's send takes the lock with a bounded wait, flagging background jobs to step aside meanwhile.

    False means the wait ran out: the send proceeds without the lock (the ledger still meters every attempt and
    the router handles a 429), which is better than a chat that looks frozen behind an academy cycle.
    """
    _CHAT_WAITING.set()
    try:
        return bool(lock.acquire(timeout=max(0.0, float(timeout))))
    finally:
        _CHAT_WAITING.clear()


def chat_waiting() -> bool:
    return _CHAT_WAITING.is_set()


def yield_to_chat(max_wait: float = JOB_YIELD_SECONDS, step: float = 0.2) -> float:
    """Background jobs call this before taking the lock: wait while a chat send is waiting; returns the seconds yielded."""
    waited = 0.0
    while _CHAT_WAITING.is_set() and waited < float(max_wait):
        time.sleep(step)
        waited += step
    return round(waited, 2)


class job_lock:
    """``with job_lock(ctx.request_lock):`` yields to a waiting chat, then holds the lock for one provider call."""

    def __init__(self, lock: threading.Lock) -> None:
        self.lock = lock

    def __enter__(self) -> None:
        yield_to_chat()
        self.lock.acquire()

    def __exit__(self, *exc: object) -> None:
        self.lock.release()


def reset_for_tests() -> None:
    with _REGISTRY_LOCK:
        _BUCKETS.clear()
        _LOCKS.clear()
    _CHAT_WAITING.clear()
