"""Task Finder missions as background jobs: the port of the former in-script executor.

The handler only writes to the vault (messages, route log, artifact, thread mission); the UI
renders from the vault while the job runs. Everything that used to read Streamlit session
state arrives in the payload (mode, budget, paid-slot model) or in the job's secrets.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from . import vault
from .errors import plain_error
from .jobs import JobCancelled, JobContext, register_handler
from .connectors_nodes import run_connector
from .missions import assemble_deliverable, deliverable_slug, normalise_plan, parse_length_target, task_plan, text_measure
from .prompting import build_prompt_messages
from .router import PaidReasoningSlot, RouteDecision, cortex_wait_seconds, generate_mode, strip_reasoning_tags
from .spatial import SceneError, parse_scene_block, scene_json, scene_markdown, solve_layout
from .webqa import browser_available, browser_check, check_markdown, check_url, first_url
from .quota_registry import job_lock

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


def run_executor(executor: str, step: Dict[str, Any], goal: str, outputs: List[Tuple[str, str]], scope: str, thread_id: int, extras: Dict[str, Any]) -> Tuple[str, RouteDecision]:
    """Deterministic steps: the layout solver and the web QA check. No model call, no quota."""
    if executor == "solver":
        spec = None
        for _, text in reversed(outputs):
            spec = parse_scene_block(text)
            if spec is not None:
                break
        if spec is None:
            raise SceneError("no ```scene block was produced by the earlier steps; ask for the scene spec again")
        placed = solve_layout(spec)
        body = scene_json(placed)
        artifact_id, _ = vault.save_artifact(scope, f"scene-{thread_id}.json", f"missions/scene-{thread_id}.json", body, "json")
        extras["scene_artifact"] = int(artifact_id)
        extras["scene_report"] = placed.report
        answer = scene_markdown(placed) + "\n\n```scene\n" + body + "\n```"
        return answer, RouteDecision("local-executor", "solver", str(step.get("type") or "quick_text"), "deterministic layout solver")
    if executor == "webqa":
        url = first_url(str(step.get("description", ""))) or first_url(goal) or first_url("\n".join(t for _, t in outputs))
        if not url:
            raise ValueError("no http(s) URL found in the mission; name the URL to check")
        result = check_url(url)
        browser = browser_check(url, [{"expect_text": "Chat Johnson"}] if "health=1" not in url else []) if browser_available() else {"available": False, "error": "no browser here"}
        extras["webqa"] = {"http": result, "browser": browser}
        return check_markdown(result, browser), RouteDecision("local-executor", "webqa", str(step.get("type") or "quick_text"), "web QA check")
    raise ValueError(f"unknown executor {executor!r}")


class MissionStopped(Exception):
    """Raised inside the node loop when a failed node's policy is ``stop``."""


def _inputs_context(node: Dict[str, Any], outputs: List[Tuple[str, str]], by_id: Dict[int, Tuple[str, str]]) -> str:
    """Earlier outputs a node asked for by step number, prepended verbatim as context (the chat carries the rest)."""
    parts = []
    for ref in node.get("inputs") or []:
        found = by_id.get(int(ref))
        if found is not None:
            parts.append(f"### Output of step {ref} · {found[0]}\n{found[1]}")
    return "\n\n".join(parts)


def _run_model_node(ctx: JobContext, node: Dict[str, Any], context: str, thread_id: int, mode: str, budget: int, paid_slot: Optional[PaidReasoningSlot], position: int) -> Tuple[str, RouteDecision]:
    # Built right before the call so this step sees every result before it.
    step_budget = int(node.get("config", {}).get("max_tokens") or budget)
    messages = build_prompt_messages(ctx.project_scope, str(node.get("description", "")), context, workspace=WORKSPACE, thread_id=thread_id, max_tokens=step_budget)
    wait = cortex_wait_seconds(ctx.ledger, messages, step_budget)
    if 0 < wait <= MISSION_MAX_WAIT_SECONDS:
        ctx.progress(text=f"Waiting {int(wait) + 1}s for a free-tier window before step {position}…")
        ctx.sleep(wait + 0.5)
    # The lock covers selection, request, and ledger record, so a chat send cannot overspend the same key.
    with job_lock(ctx.request_lock):  # steps aside for a waiting chat send first
        answer, decision = generate_mode(mode, str(node.get("type") or "chat"), messages, ctx.ledger, max_tokens=step_budget, temperature=0.2, paid_slot=paid_slot)
    return strip_reasoning_tags(answer), decision


def _run_sub_mission(ctx: JobContext, node: Dict[str, Any], goal: str, position: int, prefix: str, state: Dict[str, Any]) -> Tuple[str, RouteDecision]:
    """A nested mission in the same chat: its nodes run with ``[Sub n.m]`` titles and its deliverable is this node's output."""
    config = node.get("config") or {}
    statement = str(config.get("statement") or "").strip() or goal
    count = max(1, min(int(config.get("sections") or config.get("count") or 3), 8))
    if prefix:
        raise ValueError("sub-missions nest one level only")
    sub_plan = normalise_plan(task_plan(statement, count, max_tokens=state["budget"]))
    sub_outputs: List[Tuple[str, str]] = []
    _run_nodes(ctx, sub_plan, statement, sub_outputs, state, prefix=f"Sub {position}.")
    if not sub_outputs:
        raise ValueError("every node of the sub-mission failed")
    sections = [answer for title, answer in sub_outputs if "draft section" in title.lower()] or [answer for _, answer in sub_outputs]
    deliverable = assemble_deliverable(statement, sections)
    return deliverable, RouteDecision("local-executor", "sub_mission", str(node.get("type") or "plan"), f"{len(sub_outputs)} of {len(sub_plan)} sub-node(s) succeeded")


def _run_nodes(ctx: JobContext, plan: List[Dict[str, Any]], goal: str, outputs: List[Tuple[str, str]], state: Dict[str, Any], prefix: str = "") -> None:
    """Run nodes strictly in order. ``state`` carries the counters, extras, and settings shared with sub-missions."""
    scope = ctx.project_scope
    thread_id = int(ctx.thread_id or 0)
    mode, budget = state["mode"], state["budget"]
    extras: Dict[str, Any] = state["extras"]
    by_id: Dict[int, Tuple[str, str]] = {}
    total = len(plan)
    for position, node in enumerate(plan, start=1):
        ctx.check_cancel()
        started = time.perf_counter()
        step_type = str(node.get("type") or "chat")
        label = f"{prefix}{position}" if prefix else str(position)
        title = str(node.get("title") or f"Step {position}")
        shown_title = f"[{prefix}{position}] {title}" if prefix else title
        executor = str(node.get("executor") or "model")
        policy = str(node.get("on_failure") or "stop")
        attempts = 2 if policy == "retry_once" else 1
        answer = decision = None
        error: Optional[Exception] = None
        for attempt in range(1, attempts + 1):
            try:
                context = _inputs_context(node, outputs, by_id)
                if executor == "model":
                    answer, decision = _run_model_node(ctx, node, context, thread_id, mode, budget, state["paid_slot"], position)
                elif executor == "connector":
                    config = dict(node.get("config") or {})
                    name = str(config.get("connector") or "")
                    answer = run_connector(ctx, name, config, outputs, extras)
                    decision = RouteDecision("local-executor", name or "connector", step_type, "connector node")
                elif executor == "sub_mission":
                    answer, decision = _run_sub_mission(ctx, node, goal, position, prefix, state)
                else:
                    answer, decision = run_executor(executor, node, goal, outputs, scope, thread_id, extras)
                error = None
                break
            except JobCancelled:
                raise
            except Exception as exc:  # the policy decides; the error is recorded either way
                error = exc
                if attempt < attempts:
                    ctx.progress(text=f"Step {label} failed once ({plain_error(exc)[:80]}); retrying…")
        counted = not prefix  # sub-mission nodes report through their parent node, not the mission counters
        if error is not None or answer is None or decision is None:
            state["failed"] += int(counted)
            reason = plain_error(error) if error is not None else "no output"
            state["failures"].append((shown_title, reason))
            _record(scope, step_type, "failed", mode, started, str(error)[:160])
            if prefix:
                ctx.progress(text=f"Step {label} failed: {reason[:100]}")
            else:
                ctx.progress(step=position, total=total, succeeded=state["succeeded"], failed=state["failed"], text=f"{state['succeeded']} succeeded · {state['failed']} failed · {position}/{total} done")
            if policy == "skip":
                continue
            remaining = total - position
            if remaining:
                state["failures"].append((f"{prefix}steps {position + 1}–{total}", f"not run: step {label} failed and its policy is {policy}"))
            raise MissionStopped(shown_title)
        output_target = str(node.get("output") or "chat")
        artifact_note = ""
        if output_target in ("artifact", "both"):
            path = f"missions/node-{thread_id}-{label.replace('.', '-')}.md"
            artifact_id, version = vault.save_artifact(scope, path.rsplit("/", 1)[-1], path, answer, "markdown")
            extras.setdefault("node_artifacts", []).append({"step": label, "title": title, "artifact_id": int(artifact_id), "version": int(version)})
            artifact_note = f"Locked as artifact v{version} (id {artifact_id}, {path})."
        chat_text = answer if output_target != "artifact" else f"{artifact_note}\n\n{answer[:400]}" + ("…" if len(answer) > 400 else "")
        vault.append_message(scope, "user", f"[{shown_title}] {node.get('description', '')}", mode=mode, thread_id=thread_id, workspace=WORKSPACE, task_type=step_type)
        vault.append_message(
            scope, "assistant", chat_text, provider=f"{decision.provider}/{decision.model}",
            mode=mode, thread_id=thread_id, workspace=WORKSPACE, task_type=step_type,
        )
        _record(scope, step_type, f"{decision.provider}/{decision.model}", mode, started, decision.reason, decision.finish)
        outputs.append((title, answer))
        by_id[int(node.get("id") or position)] = (title, answer)
        state["succeeded"] += int(counted)
        state["truncated"] += int(counted and decision.finish == "length")
        ctx.progress(last_route=f"{decision.provider}/{decision.model}")
        if not prefix:
            ctx.progress(step=position, total=total, succeeded=state["succeeded"], failed=state["failed"], text=f"{state['succeeded']} succeeded · {state['failed']} failed · {position}/{total} done")


def run_mission_job(ctx: JobContext) -> Dict[str, Any]:
    """Run the nodes strictly in order; each one sees the results before it. Failures are recorded, never stored as answers."""
    payload = ctx.payload
    scope = ctx.project_scope
    thread_id = int(ctx.thread_id or 0)
    goal = str(payload.get("goal", ""))
    plan = normalise_plan(list(payload.get("plan") or []))
    mode = str(payload.get("mode") or "normal")
    budget = int(payload.get("max_tokens") or 2048)
    total = len(plan)
    outputs: List[Tuple[str, str]] = []
    state: Dict[str, Any] = {
        "mode": mode, "budget": budget, "paid_slot": paid_slot_for(ctx), "extras": {},
        "succeeded": 0, "failed": 0, "truncated": 0, "failures": [],
    }
    ctx.progress(step=0, total=total, succeeded=0, failed=0, text="Starting workstreams…")
    stopped = ""
    try:
        _run_nodes(ctx, plan, goal, outputs, state)
    except MissionStopped as exc:
        stopped = str(exc)
    succeeded, failed = state["succeeded"], state["failed"]
    if succeeded:
        # Pinned only once there is something to continue from; an all-failed launch leaves the next message free to be a new mission.
        vault.set_thread_mission(thread_id, goal, scope)
    summary: Dict[str, Any] = {
        "thread_id": thread_id, "goal": goal, "steps": total, "succeeded": succeeded, "failed": failed,
        "truncated": state["truncated"], "failures": state["failures"], "stopped_at": stopped, **state["extras"],
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
