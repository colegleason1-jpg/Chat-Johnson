"""The quiet period after an operator's send: background cycles wait so the chat keeps its free-tier window.

A company cycle is up to thirty-one calls under the same request lock and the same per-minute windows as
the chat. It used to start the moment its interval passed, so the operator's next send waited behind it or
raced it for the window. Now every chat send stamps the scope, cycles due inside the quiet period re-queue
themselves for its end, and the tick schedules its next look for then. Only timestamps are stored.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, Mapping, Optional

SETTING = "last_chat_send"
QUIET_AFTER_CHAT_SECONDS = 300.0
QUIET_AFTER_PAGE_SECONDS = 600.0


def note_chat_send(project_scope: str, page: bool = False, now: Optional[float] = None) -> None:
    """Stamp the scope: the operator just sent a message (a page request keeps the quiet period longer)."""
    from . import vault

    stamp = float(now if now is not None else time.time())
    vault.setting_set(project_scope, SETTING, json.dumps({"at": stamp, "page": bool(page)}))


def quiet_seconds(project_scope: str, now: Optional[float] = None) -> float:
    """Seconds a background cycle should still wait for this scope; 0 when the chat has been quiet long enough."""
    from . import vault

    try:
        stored = json.loads(vault.setting_get(project_scope, SETTING, "") or "{}")
    except ValueError:
        return 0.0
    if not isinstance(stored, dict):
        return 0.0
    try:
        at = float(stored.get("at") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    window = QUIET_AFTER_PAGE_SECONDS if stored.get("page") else QUIET_AFTER_CHAT_SECONDS
    return max(0.0, at + window - float(now if now is not None else time.time()))


def deferral(
    project_scope: str, kind: str, payload: Mapping[str, Any], secrets: Mapping[str, str], job_id: int,
    enqueue: Callable[..., int], progress: Optional[Callable[..., None]] = None, now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """When the scope is inside its quiet period, re-queue this cycle for its end and return the result to report;
    None means run now. The re-queued job keeps the payload, so a deferral changes when, never what."""
    remaining = quiet_seconds(project_scope, now)
    if remaining <= 0:
        return None
    stamp = float(now if now is not None else time.time())
    next_job = enqueue(project_scope, kind, {k: v for k, v in payload.items() if k != "chained_from"} | {"deferred_from": int(job_id)}, secrets, run_after=stamp + remaining)
    if progress is not None:
        progress(step=0, total=1, text=f"Waiting {int(remaining) + 1} s: the operator just sent a chat message, and the chat keeps its free-tier window")
    return {"status": "deferred", "quiet_s": int(remaining) + 1, "next_job": int(next_job), "calls": 0, "tokens": 0}
