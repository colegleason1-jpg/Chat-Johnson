"""Vendor model discovery and transient-error policy shared by every provider path.

Vendors retire model ids without warning and throttle popular ones with 503
"high demand" responses. This module keeps one process-wide cache of live
model ids per vendor, a preference order for choosing replacements, and the
small set of rules the routers use to decide whether to retry, switch model,
or fail over. It depends only on ``requests`` so both the Cortex router and
the legacy provider client can use it without import cycles.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional, Tuple

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

# vendor -> live model id discovered after a retirement or availability error
DISCOVERED: Dict[str, str] = {}

# Preference order per vendor. Newest general-purpose Flash / gpt-oss first,
# lighter siblings after them so a "high demand" 503 has somewhere to go.
VENDOR_PREFERENCES: Dict[str, Tuple[str, ...]] = {
    "gemini": (
        "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.1-flash", "gemini-3-flash",
        "gemini-3.6-flash-lite", "gemini-3.5-flash-lite", "gemini-2.5-flash", "gemini-2.5-flash-lite",
        "gemini-3.1-pro", "gemini-2.5-pro",
    ),
    "groq": ("openai/gpt-oss-120b", "qwen/qwen3.6-27b", "openai/gpt-oss-20b", "llama-3.3-70b-versatile"),
    "huggingface": ("Qwen/Qwen2.5-Coder-32B-Instruct", "Qwen/Qwen3-Coder-30B-A3B-Instruct"),
}

# Provider / endpoint names that share a vendor model list.
_VENDOR_ALIASES = {"google_ai_studio": "gemini", "gemini": "gemini", "groq": "groq", "huggingface": "huggingface"}

_RETIRED_MARKERS = (
    "not found", "decommissioned", "deprecated", "does not exist", "not supported",
    "no longer", "not available", "unsupported model", "invalid model",
)
TRANSIENT_STATUSES = (408, 425, 429, 500, 502, 503, 504)
MAX_TRANSIENT_ATTEMPTS = 3
MAX_BACKOFF_SECONDS = 8.0


def vendor_for(name: str) -> str:
    return _VENDOR_ALIASES.get(name, name)


def discovered(name: str) -> Optional[str]:
    return DISCOVERED.get(vendor_for(name))


def looks_like_retired_model(status_code: int, body: str) -> bool:
    lowered = (body or "").lower()
    return status_code in (400, 404, 410) and any(marker in lowered for marker in _RETIRED_MARKERS)


def is_transient(status_code: int) -> bool:
    return status_code in TRANSIENT_STATUSES


def retry_delay(attempt: int, retry_after: Optional[str] = None) -> float:
    """Seconds to wait before retry number ``attempt`` (1-based), Retry-After aware."""
    if retry_after:
        try:
            return max(0.0, min(float(retry_after), MAX_BACKOFF_SECONDS))
        except ValueError:
            pass
    return float(min(2 ** (attempt - 1), MAX_BACKOFF_SECONDS))


def sleep(seconds: float) -> None:  # separated so tests can stub it
    if seconds > 0:
        time.sleep(seconds)


def list_models(kind: str, base_url: str, key: str, timeout: int = 20) -> List[str]:
    """Generation-capable model ids from the vendor's list endpoint (needs a key)."""
    if requests is None or not key:
        return []
    try:
        if kind == "gemini":
            response = requests.get(
                f"{base_url}/models", headers={"x-goog-api-key": key}, params={"pageSize": 200}, timeout=timeout
            )
            if response.status_code != 200:
                return []
            names: List[str] = []
            for row in response.json().get("models", []):
                if "generateContent" in row.get("supportedGenerationMethods", []):
                    names.append(str(row.get("name", "")).split("/", 1)[-1])
            return [name for name in names if name]
        response = requests.get(f"{base_url}/models", headers={"Authorization": f"Bearer {key}"}, timeout=timeout)
        if response.status_code != 200:
            return []
        return [str(row.get("id", "")) for row in response.json().get("data", []) if row.get("id")]
    except (requests.RequestException, ValueError, AttributeError):
        return []


def rank_models(vendor: str, available: List[str], exclude: Tuple[str, ...] = ()) -> List[str]:
    """Available ids in preference order, then any remaining flash-like ids for Gemini."""
    excluded = set(exclude)
    ranked = [name for name in VENDOR_PREFERENCES.get(vendor, ()) if name in available and name not in excluded]
    if vendor == "gemini":
        extra = sorted(
            name for name in available
            if "flash" in name and name not in ranked and name not in excluded
            and not any(tag in name for tag in ("image", "tts", "live", "audio", "embedding"))
        )
        ranked.extend(reversed(extra))  # newest-looking names sort last alphabetically
    if not ranked:
        ranked = [name for name in available if name not in excluded]
    return ranked


def discover(name: str, kind: str, base_url: str, key: str, timeout: int = 20, exclude: Tuple[str, ...] = ()) -> Optional[str]:
    """Pick a live model for this vendor, cache it, and return it (or None)."""
    vendor = vendor_for(name)
    ranked = rank_models(vendor, list_models(kind, base_url, key, timeout=timeout), exclude=exclude)
    if not ranked:
        return None
    DISCOVERED[vendor] = ranked[0]
    return ranked[0]


def alternates(name: str, kind: str, base_url: str, key: str, current: str, timeout: int = 20, limit: int = 2) -> List[str]:
    """Up to ``limit`` sibling models to try when ``current`` is overloaded."""
    vendor = vendor_for(name)
    ranked = rank_models(vendor, list_models(kind, base_url, key, timeout=timeout), exclude=(current,))
    return ranked[:limit]
