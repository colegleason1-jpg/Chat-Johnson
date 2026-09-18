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
from .missions import PREVIEW_REPAIR_ROUNDS, assemble_deliverable, deliverable_slug, normalise_plan, parse_length_target, task_plan, text_measure
from .preview import EXTERNAL_RESOURCE_RE, extract_preview_fence, page_completeness
from .prompting import build_prompt_messages
from .router import PAGE_OUTPUT_TOKENS, PaidReasoningSlot, ProviderError, RouteDecision, cortex_wait_seconds, generate_mode, headroom_wait_seconds, strip_reasoning_tags
from .sandbox_preview import strip_hazards
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


def newest_page(outputs: List[Tuple[str, str]], state: Dict[str, Any]) -> str:
    """The page the mission is working on: the last whole page an earlier step produced, else the one it was launched with."""
    for _, text in reversed(outputs):
        found, closed = extract_preview_fence(text)
        if found and closed:
            return found
    return str(state.get("page") or "")


def page_check(source: str) -> Tuple[List[str], str]:
    """The static verdict on a page (no browser here): completeness reasons, plus a plain report the operator can read."""
    reasons = list(page_completeness(source))
    _, hazards = strip_hazards(source)
    external = sorted({match.group(0)[:80] for match in EXTERNAL_RESOURCE_RE.finditer(source)})
    lines = [f"Page check (static, no browser): {len(source):,} characters, {source.lower().count('<script')} script block(s)."]
    lines.append("Structure: " + ("complete." if not reasons else "INCOMPLETE: " + "; ".join(reasons) + "."))
    if external:
        reasons.append(f"{len(external)} external resource(s) the canvas would block")
        lines.append("External resources the canvas never loads (inline them): " + "; ".join(external))
    if hazards:
        lines.append("Removed before running: " + "; ".join(hazards))
    lines.append("Verdict: " + ("the page can be put on the canvas." if not reasons else "the page needs repair before it can work: " + "; ".join(reasons) + "."))
    lines.append("To see it run and be fixed automatically, send it to Chat Bot with the canvas set to Run the page.")
    return reasons, "\n".join(lines)


def run_executor(executor: str, step: Dict[str, Any], goal: str, outputs: List[Tuple[str, str]], scope: str, thread_id: int, extras: Dict[str, Any]) -> Tuple[str, RouteDecision]:
    """Deterministic steps: the layout solver, the web QA check and the static page check. No model call, no quota."""
    if executor == "preview.validate":
        source = newest_page(outputs, extras.get("state") or {})
        if not source.strip():
            raise ValueError("no page to check: the mission was launched without a page on the canvas and no earlier step produced one")
        reasons, report = page_check(source)
        extras["page_check"] = {"complete": not reasons, "reasons": reasons}
        return report, RouteDecision("local-executor", "preview.validate", str(step.get("type") or "quick_text"), "static page check")
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


def _run_model_node(ctx: JobContext, node: Dict[str, Any], context: str, thread_id: int, mode: str, budget: int, paid_slot: Optional[PaidReasoningSlot], position: int, page: str = "", description: str = "") -> Tuple[str, RouteDecision]:
    # Built right before the call so this step sees every result before it. A mission that carries a page sends it
    # whole on every model step (the deliverable is the page) and asks only endpoints that can write one.
    step_budget = int(node.get("config", {}).get("max_tokens") or budget)
    if page:
        step_budget = max(step_budget, PAGE_OUTPUT_TOKENS)
    messages = build_prompt_messages(
        ctx.project_scope, description or str(node.get("description", "")), context, workspace=WORKSPACE, thread_id=thread_id, max_tokens=step_budget,
        **({"current_page": page, "interface": True, "app_state": f"APP STATE: this is a mission step; the page on the canvas ({len(page)} characters) is sent below whole."} if page else {}),
    )
    wait = cortex_wait_seconds(ctx.ledger, messages, step_budget)
    if 0 < wait <= MISSION_MAX_WAIT_SECONDS:
        ctx.progress(text=f"Waiting {int(wait) + 1}s for a free-tier window before step {position}…")
        ctx.sleep(wait + 0.5)
    for attempt in (1, 2):
        # The lock covers selection, request, and ledger record, so a chat send cannot overspend the same key.
        try:
            with job_lock(ctx.request_lock):  # steps aside for a waiting chat send first
                answer, decision = generate_mode(
                    mode, str(node.get("type") or "chat"), messages, ctx.ledger, max_tokens=step_budget, temperature=0.2, paid_slot=paid_slot,
                    **({"interface": True} if page else {}),
                )
            return strip_reasoning_tags(answer), decision
        except ProviderError as exc:
            # A full free-tier window is a pause, not a failed step: wait once for it and send again.
            retry_wait = headroom_wait_seconds(exc, ctx.ledger, messages, step_budget, MISSION_MAX_WAIT_SECONDS) if attempt == 1 else 0.0
            if not retry_wait:
                raise
            ctx.progress(text=f"Step {position}: the free-tier window is full; waiting {int(retry_wait) + 1}s and sending again…")
            ctx.sleep(retry_wait + 0.5)
    raise ProviderError("unreachable")  # pragma: no cover


def _run_preview_repair(ctx: JobContext, node: Dict[str, Any], context: str, outputs: List[Tuple[str, str]], state: Dict[str, Any], position: int) -> Tuple[str, RouteDecision]:
    """Bounded regenerate loop: ask for the whole page, check it statically, ask again with the findings; stop when it passes."""
    source = newest_page(outputs, state)
    if not source.strip():
        raise ValueError("no page to repair: the mission was launched without a page on the canvas and no earlier step produced one")
    rounds = max(1, min(int((node.get("config") or {}).get("rounds") or PREVIEW_REPAIR_ROUNDS), 4))
    reasons, report = page_check(source)
    request = str(node.get("description", ""))
    last_report = report
    for round_no in range(1, rounds + 1):
        ctx.progress(text=f"Step {position}: repair round {round_no} of {rounds}…")
        brief = (
            f"{request}\n\nThe static check of the current page says:\n{last_report}\n\n"
            "Return the whole corrected page as one complete ```html fence (inline CSS and JavaScript, no external URLs)."
        )
        answer, decision = _run_model_node(ctx, node, context, int(ctx.thread_id or 0), state["mode"], state["budget"], state["paid_slot"], position, page=source, description=brief)
        candidate, closed = extract_preview_fence(answer)
        if not candidate:
            last_report = "The answer carried no ```html page fence; return the page itself."
            continue
        reasons = list(page_completeness(candidate, closed, decision.finish))
        checked, last_report = page_check(candidate)
        reasons = reasons + [r for r in checked if r not in reasons]
        source = candidate
        if not reasons:
            state["page"] = candidate
            decision.reason = f"repaired in {round_no} round(s); static check passed; {decision.reason}"
            return answer, decision
        decision.reason = f"round {round_no}: {'; '.join(reasons)}; {decision.reason}"
    state["page"] = source
    raise ValueError(f"the page still fails the static check after {rounds} round(s): {'; '.join(reasons) if reasons else last_report[:160]}")


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
    extras["state"] = state  # the deterministic executors read the launch page through it
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
                    answer, decision = _run_model_node(ctx, node, context, thread_id, mode, budget, state["paid_slot"], position, page=str(state.get("page") or ""))
                elif executor == "preview.repair":
                    answer, decision = _run_preview_repair(ctx, node, context, outputs, state, position)
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
            # Said in the chat, where the operator looks; a failed step used to leave nothing behind but a job row.
            vault.append_message(scope, "system", f"Step {label} ({title}) failed: {reason}", mode=mode, thread_id=thread_id, workspace=WORKSPACE, task_type=step_type, finish="failed")
            if prefix:
                ctx.progress(text=f"Step {label} failed: {reason[:100]}")
            else:
                ctx.progress(step=position, total=total, succeeded=state["succeeded"], failed=state["failed"], text=f"{state['succeeded']} succeeded · {state['failed']} failed · {position}/{total} done")
            if policy == "skip":
                continue
            remaining = total - position
            if remaining:
                state["failures"].append((f"{prefix}steps {position + 1}–{total}", f"not run: step {label} failed and its policy is {policy}"))
                vault.append_message(scope, "system", f"The mission stopped at step {label}; steps {position + 1}–{total} did not run (failure policy: {policy}).", mode=mode, thread_id=thread_id, workspace=WORKSPACE, task_type=step_type, finish="failed")
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
        produced, closed = extract_preview_fence(answer)
        if produced and closed and not page_completeness(produced, closed, decision.finish):
            state["page"] = produced  # the newest whole page is what the next step and the canvas see
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
        "page": str(payload.get("page") or ""),  # the canvas page the mission was launched with, when it refers to it
    }
    launched_page = state["page"]
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
    extras = {key: value for key, value in state["extras"].items() if key != "state"}
    summary: Dict[str, Any] = {
        "thread_id": thread_id, "goal": goal, "steps": total, "succeeded": succeeded, "failed": failed,
        "truncated": state["truncated"], "failures": state["failures"], "stopped_at": stopped, **extras,
    }
    final_page = str(state.get("page") or "")
    if final_page.strip() and final_page != launched_page:
        # The page travels back: locked as an artifact and offered to the canvas by the launch summary.
        artifact_id, version = vault.save_artifact(scope, f"page-{thread_id}.html", f"missions/page-{thread_id}.html", final_page, "html")
        summary.update({"page_artifact": int(artifact_id), "page_version": int(version), "page_complete": not page_completeness(final_page)})
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
