"""Encrypted hand-off of a job's session secrets between the app and a worker container.

The app that enqueues a job and the worker that runs it are different processes on the VM. The
secrets a job needs (a GitHub token, the paid slot key) are session-only by rule, so they travel
as one Fernet blob keyed by ``CHAT_JOHNSON_JOB_KEY``, which both containers hold in ``.env``. The
row holds ciphertext only for the job's lifetime: the worker deletes it on claim. Without the key
(or without ``cryptography``) nothing is written and the in-process hand-off applies.
"""
from __future__ import annotations

import json
import os
from typing import Dict, Mapping, Optional

KEY_ENV = "CHAT_JOHNSON_JOB_KEY"


def _fernet():  # -> Optional[Fernet]
    key = os.environ.get(KEY_ENV, "").strip()
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        return None
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError):
        return None


def available() -> bool:
    """True when a valid key and the cipher are both present in this process."""
    return _fernet() is not None


def generate_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode("ascii")


def encrypt(secrets: Mapping[str, str]) -> Optional[bytes]:
    cipher = _fernet()
    if cipher is None:
        return None
    payload = json.dumps({str(k): str(v) for k, v in secrets.items() if v}).encode("utf-8")
    return cipher.encrypt(payload)


def decrypt(blob: Optional[bytes]) -> Dict[str, str]:
    cipher = _fernet()
    if cipher is None or not blob:
        return {}
    try:
        data = json.loads(cipher.decrypt(bytes(blob)).decode("utf-8"))
    except Exception:  # a blob from another key or a corrupt row yields nothing, never a crash
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(data, dict)}
