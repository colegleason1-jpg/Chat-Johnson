"""GitHub OAuth helpers that survive the redirect.

The browser leaves the app for github.com and comes back to a fresh Streamlit
session, so CSRF state cannot live in session memory. The state is a signed
token (HMAC over nonce and timestamp with the OAuth client secret) verified
statelessly on return, with a short expiry.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from typing import Tuple

STATE_TTL_SECONDS = 600


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def mint_state(secret: str, now: float | None = None) -> str:
    """Return ``<nonce>.<timestamp>.<signature>`` for the authorize URL."""
    nonce = base64.urlsafe_b64encode(secrets.token_bytes(18)).decode("ascii").rstrip("=")
    stamp = str(int(now if now is not None else time.time()))
    return f"{nonce}.{stamp}.{_sign(secret, f'{nonce}.{stamp}')}"


def verify_state(secret: str, state: str, now: float | None = None) -> Tuple[bool, str]:
    """Check signature and age of a returned state; never trusts session memory."""
    parts = (state or "").split(".")
    if len(parts) != 3 or not secret:
        return False, "malformed state"
    nonce, stamp, signature = parts
    if not hmac.compare_digest(_sign(secret, f"{nonce}.{stamp}"), signature):
        return False, "state signature mismatch"
    try:
        issued = int(stamp)
    except ValueError:
        return False, "malformed timestamp"
    age = (now if now is not None else time.time()) - issued
    if age < -60 or age > STATE_TTL_SECONDS:
        return False, "state expired"
    return True, "ok"
