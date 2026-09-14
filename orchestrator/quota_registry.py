"""Process-wide quota ledgers, one per distinct credential set.

Shared by the UI's script threads and the job runner's worker threads, so a mission
running in the background and a chat send in the foreground draw on the same per-vendor
buckets. Entries are keyed by a non-reversible fingerprint of the keys in effect in the
calling context (session overlay first, then the environment); two visitors with
different keys never share a bucket.
"""
from __future__ import annotations

import hashlib
import threading
from typing import Dict, Tuple

from .config import PROVIDERS, daily_cap, resolve_secret
from .quota import QuotaLedger

_REGISTRY: Dict[str, Tuple[QuotaLedger, threading.Lock]] = {}
_REGISTRY_LOCK = threading.Lock()


def credential_fingerprint() -> str:
    """Non-reversible id of the keys in effect for this context (overlay + environment)."""
    from .router import BYOK_ENV_KEYS  # local import: router imports config, so keep this module light

    material = "|".join(
        f"{env}:{hashlib.sha256(resolve_secret(env).encode()).hexdigest()[:16]}"
        for names in BYOK_ENV_KEYS.values() for env in names if resolve_secret(env)
    )
    return hashlib.sha256(material.encode()).hexdigest()[:16] if material else "no-keys"


def _entry() -> Tuple[QuotaLedger, threading.Lock]:
    fingerprint = credential_fingerprint()
    with _REGISTRY_LOCK:
        entry = _REGISTRY.get(fingerprint)
        if entry is None:
            from .discovery import vendor_for

            # One credential = one bucket: start from the legacy ceilings; Cortex tightens them later.
            limits: Dict[str, Tuple[int, int]] = {}
            for name, cfg in PROVIDERS.items():
                vendor = vendor_for(name)
                rpm, tpm = limits.get(vendor, (cfg.rpm_limit, cfg.tpm_limit))
                limits[vendor] = (min(rpm, cfg.rpm_limit), min(tpm, cfg.tpm_limit))
            entry = (QuotaLedger(limits, {vendor: daily_cap(vendor) for vendor in limits}), threading.Lock())
            _REGISTRY[fingerprint] = entry
    return entry


def get_quota_ledger() -> QuotaLedger:
    """The ledger for the keys bound in this context."""
    return _entry()[0]


def get_request_lock() -> threading.Lock:
    """Serialises the quota check, the request, and the ledger record for one credential set."""
    return _entry()[1]
