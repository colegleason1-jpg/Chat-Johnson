"""Task Finder missions as background jobs: the port of the former in-script executor.

The handler only writes to the vault (messages, route log, artifact, thread mission); the UI
renders from the vault while the job runs. Everything that used to read Streamlit session
state arrives in the payload (mode, budget, paid-slot model) or in the job's secrets.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from . import vault
from .jobs import JobCancelled, JobContext, register_handler
from .missions import assemble_deliverable, deliverable_slug, parse_length_target, text_measure
from .prompting import build_prompt_messages
from .router import PaidReasoningSlot, cortex_wait_seconds, generate_mode, strip_reasoning_tags

KIND = "mission"
WORKSPACE = "task_finder"
MISSION_MAX_WAIT_SECONDS = 65.0  # one free-tier window; longer waits surface as a failed step instead


def paid_slot_for(ctx: JobContext) -> Optional[PaidReasoningSlot]:
    """The Heavy Mode critique slot, rebuilt from the payload and the job's secrets (never the environment)."""
    if ctx.payload.get("mode") != "heavy":
        return None
    model = str(ctx.payload.get("paid_model") or "").strip() or PaidReasoningSlot().model
    return PaidReasoningSlot(api_key=ctx.secrets.get("paid_slot_key", ""), model=model, enabled=bool(ctx.payload.get("paid_enabled")))


def _record(scope: str, task_type: str, route: str, mode: str, started: float, reason: str, finish: str = "") -> None:
    try:
        vault.record_route(scope, WORKSPACE, task_type, route, mode, int((time.perf_counter() - started) * 1000), finish, reason)
    except Exception:  # telemetry must never fail a step
        pass


def run_mission_job(ctx: JobContext) -> Dict[str, Any]:
    """Run the workstreams strictly in order; each one sees the results before it. Failures are recorded, never stored as answers."""
    payload = ctx.payload
    scope = ctx.project_scope
    thread_id = int(ctx.thread_id or 0)
    goal = str(payload.get("goal", ""))
    plan: List[Dict[str, Any]] = list(payload.get("plan") or [])
    mode = str(payload.get("mode") or "normal")
    budget = int(payload.get("max_tokens") or 2048)
    paid_slot = paid_slot_for(ctx)
    total = len(plan)
    succeeded = failed = truncated = 0
    failures: List[Tuple[str, str]] = []
    outputs: List[Tuple[str, str]] = []
    ctx.progress(step=0, total=total, succeeded=0, failed=0, text="Starting workstreams…")
    for finished, step in enumerate(plan, start=1):
        ctx.check_cancel()
        started = time.perf_counter()
        step_type = str(step.get("type") or "quick_text")
        title = str(step.get("title") or f"Step {finished}")
        try:
            # Built right before the call so this step sees every result before it.
            messages = build_prompt_messages(scope, str(step.get("description", "")), workspace=WORKSPACE, thread_id=thread_id, max_tokens=budget)
            wait = cortex_wait_seconds(ctx.ledger, messages, budget)
            if 0 < wait <= MISSION_MAX_WAIT_SECONDS:
                ctx.progress(text=f"Waiting {int(wait) + 1}s for a free-tier window before step {finished}…")
                ctx.sleep(wait + 0.5)
            # The lock covers selection, request, and ledger record, so a chat send cannot overspend the same key.
            with ctx.request_lock:
                answer, decision = generate_mode(mode, step_type, messages, ctx.ledger, max_tokens=budget, temperature=0.2, paid_slot=paid_slot)
            answer = strip_reasoning_tags(answer)
            vault.append_message(scope, "user", f"[{title}] {step.get('description', '')}", mode=mode, thread_id=thread_id, workspace=WORKSPACE, task_type=step_type)
            vault.append_message(
                scope, "assistant", answer, provider=f"{decision.provider}/{decision.model}",
                mode=mode, thread_id=thread_id, workspace=WORKSPACE, task_type=step_type,
            )
            _record(scope, step_type, f"{decision.provider}/{decision.model}", mode, started, decision.reason, decision.finish)
            outputs.append((title, answer))
            succeeded += 1
            truncated += int(decision.finish == "length")
            ctx.progress(last_route=f"{decision.provider}/{decision.model}")
        except JobCancelled:
            raise
        except Exception as exc:
            failed += 1
            failures.append((title, str(exc)[:600]))
            _record(scope, step_type, "failed", mode, started, str(exc)[:160])
        ctx.progress(step=finished, total=total, succeeded=succeeded, failed=failed, text=f"{succeeded} succeeded · {failed} failed · {finished}/{total} done")
    if succeeded:
        # Pinned only once there is something to continue from; an all-failed launch leaves the next message free to be a new mission.
        vault.set_thread_mission(thread_id, goal)
    summary: Dict[str, Any] = {
        "thread_id": thread_id, "goal": goal, "steps": total, "succeeded": succeeded, "failed": failed,
        "truncated": truncated, "failures": failures,
    }
    sections = [answer for title, answer in outputs if title.lower().startswith("draft section")]
    if plan and plan[0].get("kind") == "writing" and sections:
        # The deliverable is assembled deterministically and locked; the chat keeps the per-section record.
        deliverable = assemble_deliverable(goal, sections)
        slug = deliverable_slug(goal)
        artifact_id, version = vault.save_artifact(
            scope, f"mission-{thread_id}-{slug}.md", f"missions/mission-{thread_id}-{slug}.md", deliverable, "markdown"
        )
        target = parse_length_target(goal)
        summary.update({
            "deliverable_artifact": int(artifact_id), "deliverable_version": int(version), "sections": len(sections),
            "measure": text_measure(deliverable), "target_words": target["words"] if target else None,
            "target_label": f"{target['amount']} {target['unit']}(s)" if target else "",
        })
    return summary


register_handler(KIND, run_mission_job)
