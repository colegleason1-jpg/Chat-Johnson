"""The society tick: one chained job that wakes due agents for leisure and schedules the cycles.

While the process is alive (and, on the VM worker, around the clock) the tick runs every few
minutes: it queues a company cycle for every company whose interval has passed, an academy cycle on
its own interval, and runs the leisure batch inline. Companies and the academy no longer chain
themselves when the tick is running; the tick is the scheduler.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from .. import vault
from ..jobs import ACTIVE_STATUSES, JobCancelled, JobContext, enqueue, register_handler
from . import academy, cycles, economy, leisure, store

KIND_TICK = "society_tick"
DEFAULT_INTERVAL_S = 30 * 60
DEFAULT_ACADEMY_INTERVAL_S = 3 * 3600
LEISURE_MAX_CALLS = 8
LEISURE_MAX_TOKENS = 12_000


def _company_job_active(scope: str, company_id: int) -> bool:
    for row in vault.list_jobs(scope, ACTIVE_STATUSES, limit=100, kind=cycles.KIND_COMPANY):
        if vault.job_view(row)["payload"].get("company_id") == company_id:
            return True
    return False


def _academy_job_active(scope: str) -> bool:
    return bool(vault.list_jobs(scope, ACTIVE_STATUSES, limit=1, kind=academy.KIND_ACADEMY))


def _last_finished(scope: str, kind: str, company_id: Optional[int]) -> float:
    where = "kind = ? AND finished_at IS NOT NULL" + (" AND company_id = ?" if company_id is not None else "")
    params: List[Any] = [kind] + ([int(company_id)] if company_id is not None else [])
    rows = store.rows("cycles", scope, where, params, order="id DESC", limit=1)
    return float(rows[0]["finished_at"]) if rows else 0.0


def society_tick(ctx: JobContext) -> Dict[str, Any]:
    scope = ctx.project_scope
    payload = ctx.payload
    mode = str(payload.get("mode") or "normal")
    interval = float(payload.get("interval_s") or DEFAULT_INTERVAL_S)
    academy_interval = float(payload.get("academy_interval_s") or DEFAULT_ACADEMY_INTERVAL_S)
    now = time.time()
    queued: List[str] = []
    # Companies whose interval has passed and that have no cycle queued or running.
    for company in store.companies_for(scope):
        cid = int(company["id"])
        if now - _last_finished(scope, "company", cid) >= float(company["interval_s"]) and not _company_job_active(scope, cid):
            cycles.run_now(scope, cid, ctx.secrets, mode=mode, call_tokens=int(payload.get("call_tokens") or cycles.DEFAULT_CALL_TOKENS), chain=False)
            queued.append(company["name"])
    if store.agents_for(scope) and now - _last_finished(scope, "academy", None) >= academy_interval and not _academy_job_active(scope):
        academy.run_now(scope, ctx.secrets, mode=mode, call_tokens=min(int(payload.get("call_tokens") or 900), 900), chain=False)
        queued.append("academy")
    # Leisure runs inline: a few agents explore per tick.
    treasury = economy.Treasury(ctx.ledger)
    budget = cycles.CycleBudget(LEISURE_MAX_CALLS, max(800, treasury.cycle_budget("leisure", LEISURE_MAX_TOKENS)))
    cycle_id = store.start_cycle(scope, "leisure", None, ctx.job_id, budget.max_tokens)
    explored: List[Dict[str, Any]] = []
    status = "done"
    try:
        explored = leisure.run_leisure(ctx, budget, cycle_id, academy.call_free, cap=int(payload.get("leisure_cap") or leisure.MAX_PER_TICK), custom_sources=payload.get("custom_sources") or [], mode=mode)
    except JobCancelled:
        store.finish_cycle(cycle_id, "cancelled", budget.tokens, budget.calls, [])
        raise
    store.finish_cycle(cycle_id, status, budget.tokens, budget.calls, [{"step": "leisure", "explored": explored, "queued": queued}])
    next_run_after = now + max(60.0, interval)
    next_job = enqueue(scope, KIND_TICK, {k: v for k, v in payload.items() if k != "chained_from"} | {"chained_from": ctx.job_id}, ctx.secrets, run_after=next_run_after)
    store.insert("cycles", scope, kind="tick", company_id=None, job_id=ctx.job_id, started_at=now, finished_at=time.time(), tokens_planned=0, tokens_used=budget.tokens, calls=budget.calls, log=[{"queued": queued, "explored": len(explored)}], next_run_after=next_run_after, status="done")
    ctx.progress(step=1, total=1, text=f"Tick: queued {', '.join(queued) or 'nothing'}; {len(explored)} agent(s) explored")
    return {"queued": queued, "explored": explored, "next_job": next_job, "next_run_after": next_run_after, "calls": budget.calls, "tokens": budget.tokens}


def start_tick(scope: str, secrets: Dict[str, str], mode: str = "normal", interval_s: float = DEFAULT_INTERVAL_S, academy_interval_s: float = DEFAULT_ACADEMY_INTERVAL_S, leisure_cap: int = leisure.MAX_PER_TICK, custom_sources: Optional[List[Dict[str, Any]]] = None, call_tokens: int = cycles.DEFAULT_CALL_TOKENS) -> int:
    payload = {"mode": mode, "interval_s": float(interval_s), "academy_interval_s": float(academy_interval_s), "leisure_cap": int(leisure_cap), "custom_sources": list(custom_sources or []), "call_tokens": int(call_tokens)}
    return enqueue(scope, KIND_TICK, payload, secrets)


def tick_state(scope: str) -> Dict[str, Any]:
    rows = [vault.job_view(r) for r in vault.list_jobs(scope, ACTIVE_STATUSES, limit=10, kind=KIND_TICK)]
    return {"running": bool(rows), "jobs": rows, "next_run_after": max((r["run_after"] for r in rows), default=0.0)}


def stop_tick(scope: str) -> int:
    stopped = 0
    for row in vault.list_jobs(scope, ACTIVE_STATUSES, limit=50, kind=KIND_TICK):
        vault.request_cancel(int(row["id"]))
        stopped += 1
    return stopped


register_handler(KIND_TICK, society_tick)
