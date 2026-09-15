"""Plain-language explanations of provider failures for the operator.

Vendor error bodies are technical and sometimes contain secrets; the UI shows this sentence
first and keeps the raw text, redacted, in a "Technical detail" expander and in the session's
provider events. Errors that no rule matches are passed through redacted, because mission and
connector failures carry their own plain wording.
"""
from __future__ import annotations

import re
from typing import Optional

from .vault import redact_secrets

_STATUS_RE = re.compile(r"HTTP (\d{3})")
_WAIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*s(?:ec|econds)?\b", re.I)
_VENDOR_RE = re.compile(r"^(google_ai_studio|groq|huggingface|gemini|nvidia|openrouter|cerebras|mistral|paid slot)\b", re.I)
# generate_mode wraps a Cortex failure with the legacy fallback's failure; the Cortex part is the cause.
_CORTEX_PART_RE = re.compile(r"strict Cortex routing failed \((.*)\); legacy provider fallback failed", re.S)
_CEILING_RE = re.compile(r"needs ~(\d+) tokens but the ceiling is (\d+) TPM")


def _status_of(exc: BaseException) -> Optional[int]:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    match = _STATUS_RE.search(str(exc))
    return int(match.group(1)) if match else None


def plain_error(exc: BaseException) -> str:
    """One sentence saying what went wrong and what to do; never the raw vendor body."""
    return _explain(str(exc), _status_of(exc))


def _explain(text: str, status: Optional[int]) -> str:
    inner = _CORTEX_PART_RE.search(text)
    if inner:
        # The legacy pool reads environment keys only, so its "no provider has a key" is not the
        # operator's problem when session keys are set; explain what stopped the Cortex route.
        part = inner.group(1)
        match = _STATUS_RE.search(part)
        return _explain(part, int(match.group(1)) if match else None)
    lower = text.lower()
    vendor_match = _VENDOR_RE.search(text)
    vendor = vendor_match.group(1) if vendor_match else "the provider"
    if "no byok key configured" in lower or "no provider keys" in lower or "no legacy provider available" in lower:
        return "No API key is set for this request: paste a free-tier key in the sidebar (API keys) and try again."
    if "daily cap" in lower:
        wait = re.search(r"resets in about (\d+) h", text)
        when = f" in about {wait.group(1)} h" if wait else " at midnight UTC"
        return f"The daily token cap is reached for every keyed vendor; it resets{when}. Add another vendor's key or raise CHAT_JOHNSON_DAILY_<VENDOR> on your own host."
    ceilings = _CEILING_RE.findall(text)
    if ceilings and "requests used" not in lower and "would exceed" not in lower:
        needed = max(int(need) for need, _ in ceilings)
        widest = max(int(ceiling) for _, ceiling in ceilings)
        return (
            f"This request is too large for every keyed vendor's per-minute window (~{needed} tokens with the output budget; "
            f"the widest keyed window is {widest} TPM): lower the output token budget in the sidebar, or start a fresh chat."
        )
    if "no headroom" in lower or "headroom" in lower and "found no" in lower or "requests used in the last minute" in lower:
        wait = _WAIT_RE.search(text)
        when = f" about {int(float(wait.group(1)))} s" if wait else " a minute"
        return f"Every keyed provider is inside its free-tier window; wait{when} and send again (the app paces missions automatically)."
    if status == 401:
        return f"{vendor} rejected the key (HTTP 401): re-paste it in the sidebar, or create a new key on the vendor's site."
    if status == 403:
        return f"{vendor} refused access (HTTP 403): the key lacks permission for this model or region, or the account needs activation."
    if status == 404 or "retired" in lower or "not found" in lower and "model" in lower:
        return f"{vendor} no longer serves that model (HTTP 404): rediscovery picks a live one; set a model override in the sidebar to choose."
    if status == 429 or "rate limit" in lower or "quota" in lower:
        wait = _WAIT_RE.search(text)
        when = f" ~{int(float(wait.group(1)))} s" if wait else " a minute"
        return f"{vendor} rate-limited the request (HTTP 429): the free-tier window is full; wait{when} or add another vendor's key."
    if status is not None and status >= 500:
        return f"{vendor} had a server error (HTTP {status}): it usually clears within a minute; the router retries transient errors."
    if "timed out" in lower or "timeout" in lower:
        return f"{vendor} did not answer in time: the model may be overloaded; try again or lower the output budget."
    if "connection" in lower or "network" in lower or "name or service" in lower or "ssl" in lower:
        return f"Network problem reaching {vendor}: check the connection and try again."
    if "empty stream" in lower or "filtered" in lower or "blocked" in lower:
        return f"{vendor} returned no text: the answer was blocked, filtered, or cut off; rephrase or lower the output budget."
    if "all cortex endpoints failed" in lower:
        return "Every keyed endpoint failed this request: see the routing log for each reason; keys, windows, or models are the usual causes."
    return redact_secrets(text)[:300]
