"""A minimal environment for child processes: nothing the operator's keys live in.

MCP servers and a repository's pytest run are code the operator did not write. On the VM the
worker's environment holds every provider key, so a child must start from a short allowlist and
receive only what its declaration names.
"""
from __future__ import annotations

import os
from typing import Dict, Mapping, Optional, Sequence

PASSTHROUGH: Sequence[str] = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP", "PYTHONPATH", "SYSTEMROOT", "TZ")


def minimal_env(extra: Optional[Mapping[str, str]] = None, passthrough: Sequence[str] = PASSTHROUGH) -> Dict[str, str]:
    """Only the allowlisted names from the current environment plus ``extra``; ``extra`` wins on a clash."""
    env: Dict[str, str] = {name: os.environ[name] for name in passthrough if os.environ.get(name)}
    env.setdefault("PATH", os.defpath)
    env.setdefault("HOME", os.path.expanduser("~"))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for name, value in (extra or {}).items():
        if value is not None:
            env[str(name)] = str(value)
    return env


def self_hosted() -> bool:
    """True on the operator's own VM (``CHAT_JOHNSON_SELF_HOSTED=1``); shared hosts keep code execution off."""
    return os.environ.get("CHAT_JOHNSON_SELF_HOSTED", "").strip() == "1"
