"""Cycle handlers on the job runner: the company cycle (EOS) and the shared per-call helper.

Every model call in the society goes through ``call``: persona messages → free-tier wait → request
lock → ``generate_mode`` → the seat's thread → route log → token ledger → cycle budget.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .. import vault
from ..jobs import JobCancelled, JobContext, enqueue, register_handler
from ..missions import assemble_deliverable, deliverable_slug, task_plan
from ..router import _estimate_tokens, cortex_wait_seconds, generate_mode, strip_reasoning_tags
from . import economy, eos, evaluate, store
from .personas import build_agent_messages
from .templates import match_seat

KIND_COMPANY = "company_cycle"
DEFAULT_MAX_CALLS = 31
DEFAULT_MAX_TOKENS = 80_000
DEFAULT_CALL_TOKENS = 1_200
MAX_WAIT_SECONDS = 65.0
WORKSPACE = "society"
MAX_ITEM_RETRIES = 2

L10_PROMPT = (
    "Run this week's Level 10 meeting from the agenda below. Reply with lines only, no prose: up to 3 lines "
    "'HEADLINE: <one sentence>', up to 3 lines 'ISSUE: <title> | RESOLUTION: <what we do>', up to 5 lines "
    "'TODO: <seat_key>: <task>', and at most one 'QUESTION: <a decision only the board can make>'.\nAGENDA:\n{agenda}\nSEAT KEYS: {seats}"
)
RATE_PROMPT = "Rate each backlog item 1–5 for importance to the rocks and the timeline (5 = do first). Reply one line per item, exactly '#<id>: <1-5>'.\nROCKS: {rocks}\nITEMS:\n{items}"
DELEGATE_PROMPT = (
    "Assign each item to the best seat by roles and current load, or to 'analytics_lead' when it must be broken down first. "
    "Reply one line per item, exactly '#<id> -> <seat_key>'.\nSEATS (key: title · roles · load):\n{seats}\nITEMS:\n{items}"
)
BREAKDOWN_PROMPT = "Break this item into 2–4 concrete sub-items a single seat can finish in one sitting. Reply lines only, exactly '- <title> :: <one-paragraph brief>'.\nITEM: {title}\nBRIEF: {brief}"
REVIEW_PROMPT = "Review the deliverable against its brief. First line exactly PASS or FAIL, then up to 5 lines of specific notes.\nBRIEF: {brief}\nDELIVERABLE:\n{body}"
REPORT_PROMPT = "Write this cycle's report for the board in under 200 words: what moved, what waits for the board, issues, and the next cycle. Facts:\n{facts}"

_RATE_RE = re.compile(r"#\s*(\d+)\s*[:\-–]\s*([1-5])")
_ASSIGN_RE = re.compile(r"#\s*(\d+)\s*(?:->|→|:)\s*([a-z0-9_]+)", re.I)
_SUB_RE = re.compile(r"^\s*[-*]\s*(.+?)\s*::\s*(.+?)\s*$", re.M)


class CycleBudgetExceeded(Exception):
    pass


@dataclass
class CycleBudget:
    max_calls: int
    max_tokens: int
    calls: int = 0
    tokens: int = 0
    log: List[Dict[str, Any]] = field(default_factory=list)

    def allows(self, estimate: int) -> bool:
        return self.calls < self.max_calls and self.tokens + int(estimate) <= self.max_tokens

    def step(self, tokens: int) -> None:
        self.calls += 1
        self.tokens += int(tokens)


def ensure_seat_thread(scope: str, seat: Dict[str, Any], company: Dict[str, Any]) -> int:
    if seat.get("thread_id") and vault.thread_by_id(int(seat["thread_id"])):
        return int(seat["thread_id"])
    thread_id = vault.create_thread(scope, title=f"{company['name']} · {seat['title']}", workspace=WORKSPACE)
    store.update("seats", int(seat["id"]), thread_id=thread_id)
    seat["thread_id"] = thread_id
    return thread_id


def call(
    ctx: JobContext, budget: CycleBudget, cycle_id: int, agent: Dict[str, Any], seat: Dict[str, Any], company: Dict[str, Any],
    prompt: str, task_type: str, seats_by_id: Dict[int, Dict[str, Any]], max_tokens: int, mode: str = "normal",
) -> Tuple[str, Any]:
    """One metered model call in a seat's own thread; raises CycleBudgetExceeded before overspending."""
    thread_id = ensure_seat_thread(ctx.project_scope, seat, company)
    messages = build_agent_messages(ctx.project_scope, agent, seat, company, prompt, thread_id, max_tokens, seats_by_id=seats_by_id)
    estimate = _estimate_tokens(messages) + int(max_tokens)
    if not budget.allows(estimate):
        raise CycleBudgetExceeded(f"{budget.calls} calls / {budget.tokens} tokens used")
    ctx.check_cancel()
    wait = cortex_wait_seconds(ctx.ledger, messages, max_tokens)
    if 0 < wait <= MAX_WAIT_SECONDS:
        ctx.progress(text=f"Waiting {int(wait) + 1}s for a free-tier window ({seat['title']})…")
        ctx.sleep(wait + 0.5)
    started = time.perf_counter()
    with ctx.request_lock:
        answer, decision = generate_mode(mode, task_type, messages, ctx.ledger, max_tokens=max_tokens, temperature=0.3)
    answer = strip_reasoning_tags(answer)
    scope = ctx.project_scope
    vault.append_message(scope, "user", prompt, mode=mode, thread_id=thread_id, workspace=WORKSPACE, task_type=task_type)
    vault.append_message(scope, "assistant", answer, provider=f"{decision.provider}/{decision.model}", mode=mode, thread_id=thread_id, workspace=WORKSPACE, task_type=task_type)
    try:
        vault.record_route(scope, "company", task_type, f"{decision.provider}/{decision.model}", mode, int((time.perf_counter() - started) * 1000), decision.finish, decision.reason)
    except Exception:
        pass
    tokens = economy.charge(scope, agent.get("id"), messages, answer, decision, cycle_id=cycle_id, job_id=ctx.job_id)
    budget.step(tokens)
    return answer, decision


def schedule_next(ctx: JobContext, payload: Dict[str, Any], interval_s: float) -> Tuple[int, float]:
    run_after = time.time() + max(60.0, float(interval_s))
    job_id = enqueue(ctx.project_scope, KIND_COMPANY, dict(payload, chained_from=ctx.job_id), ctx.secrets, thread_id=ctx.thread_id, run_after=run_after)
    return job_id, run_after


def run_now(
    scope: str, company_id: int, secrets: Dict[str, str], mode: str = "normal", call_tokens: int = DEFAULT_CALL_TOKENS,
    chain: bool = False, interval_s: Optional[float] = None, board_thread_id: Optional[int] = None,
    max_calls: int = DEFAULT_MAX_CALLS, max_tokens: int = DEFAULT_MAX_TOKENS, run_after: float = 0.0,
) -> int:
    company = store.row("companies", company_id)
    if not company:
        raise KeyError(f"no company {company_id}")
    payload = {
        "company_id": int(company_id), "mode": mode, "call_tokens": int(call_tokens), "chain": bool(chain),
        "interval_s": float(interval_s if interval_s is not None else company["interval_s"]), "board_thread_id": board_thread_id,
        "max_calls": int(max_calls), "max_tokens": int(max_tokens),
    }
    return enqueue(scope, KIND_COMPANY, payload, secrets, thread_id=board_thread_id, run_after=run_after)


def _lines_for_items(items: Sequence[Dict[str, Any]]) -> str:
    return "\n".join(f"#{i['id']} · {i['title']} — {i['brief'][:160]}" for i in items)


def company_cycle(ctx: JobContext) -> Dict[str, Any]:
    """L10 → rate → delegate → analytics → execute → editor review → report, all bounded by the cycle budget."""
    scope = ctx.project_scope
    payload = ctx.payload
    company = store.row("companies", int(payload["company_id"]))
    if not company:
        raise RuntimeError(f"company {payload.get('company_id')} not found")
    company_id = int(company["id"])
    mode = str(payload.get("mode") or "normal")
    call_tokens = int(payload.get("call_tokens") or DEFAULT_CALL_TOKENS)
    treasury = economy.Treasury(ctx.ledger)
    hard_cap = int(payload.get("max_tokens") or DEFAULT_MAX_TOKENS)
    share_key = "company" if company["key"] == "avs_studio" else "company_2"
    budget = CycleBudget(int(payload.get("max_calls") or DEFAULT_MAX_CALLS), max(call_tokens * 2, treasury.cycle_budget(share_key, hard_cap)))
    cycle_id = store.start_cycle(scope, "company", company_id, ctx.job_id, budget.max_tokens)
    seats = store.seats_for(scope, company_id)
    seats_by_id = {int(s["id"]): s for s in seats}
    seats_by_key = {s["key"]: s for s in seats}
    agents = {int(a["id"]): a for a in store.agents_for(scope)}
    active_depts = {int(d["id"]) for d in store.departments_for(scope, company_id) if d["active"]}
    load: Dict[int, int] = {}
    for item in store.work_items_for(scope, company_id, ("assigned", "running"), limit=500):
        if item.get("seat_id"):
            load[int(item["seat_id"])] = load.get(int(item["seat_id"]), 0) + 1

    def actor(key: str) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        seat = seats_by_key.get(key)
        agent = agents.get(int(seat["agent_id"])) if seat and seat.get("agent_id") else None
        return seat, agent

    def ask(key: str, prompt: str, task_type: str = "reasoning", tokens: Optional[int] = None) -> str:
        seat, agent = actor(key)
        if not seat or not agent:
            return ""
        text, _ = call(ctx, budget, cycle_id, agent, seat, company, prompt, task_type, seats_by_id, tokens or call_tokens, mode=mode)
        return text

    log: List[Dict[str, Any]] = []
    status = "done"
    to_board = 0
    stage = "scorecard"
    total_steps = 8
    try:
        # 1 · scorecard (deterministic)
        misses = eos.scorecard_review(scope, company_id, cycle_id)
        log.append({"step": "scorecard", "misses": len(misses)})
        ctx.progress(step=1, total=total_steps, text=f"Scorecard: {len(misses)} miss(es)")

        # 2 · Level 10 meeting (EA to the CEO)
        stage = "l10"
        agenda = eos.l10_agenda(scope, company, misses)
        minutes_text = ask("ea_ceo", L10_PROMPT.format(agenda=eos.agenda_text(agenda), seats=", ".join(seats_by_key)))
        minutes = eos.parse_minutes(minutes_text)
        for issue in minutes["issue"]:
            title, _, resolution = issue.partition("|")
            store.insert("issues", scope, company_id=company_id, title=title.strip()[:200], status="resolved" if resolution.strip() else "open", resolution=resolution.replace("RESOLUTION:", "").strip()[:500], created_at=time.time())
        for todo in minutes["todo"]:
            seat_key, _, text = todo.partition(":")
            seat = seats_by_key.get(seat_key.strip())
            store.insert("todos", scope, company_id=company_id, seat_id=seat["id"] if seat else None, text=(text or todo).strip()[:300], due_at=time.time() + 7 * eos.DAY, created_at=time.time())
        minutes_artifact = None
        if minutes_text.strip():
            body = f"# L10 minutes · {company['name']} · {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}\n\n## Agenda\n{eos.agenda_text(agenda)}\n\n## Minutes\n{minutes_text.strip()}\n"
            minutes_artifact, _ = vault.save_artifact(scope, f"l10-{company['key']}-{int(time.time())}.md", f"company/{company['key']}/l10/{time.strftime('%Y%m%d-%H%M', time.gmtime())}.md", body, "markdown")
        store.insert("meetings", scope, company_id=company_id, kind="l10", held_at=time.time(), agenda=agenda, minutes_artifact_id=minutes_artifact)
        log.append({"step": "l10", "headlines": minutes["headline"][:3], "issues": len(minutes["issue"]), "todos": len(minutes["todo"]), "question": minutes["question"][:1]})
        ctx.progress(step=2, total=total_steps, text="L10 held")

        # 3 · CEO rates the backlog
        stage = "rate"
        backlog = store.work_items_for(scope, company_id, ("backlog",), limit=10)
        if backlog:
            rocks = ", ".join(r["title"] for r in store.rows("rocks", scope, "company_id = ?", (company_id,), limit=6))
            text = ask("ceo", RATE_PROMPT.format(rocks=rocks or "none", items=_lines_for_items(backlog)))
            ratings = {int(i): int(r) for i, r in _RATE_RE.findall(text)}
            for item in backlog:
                store.set_work_status(int(item["id"]), "rated", importance=ratings.get(int(item["id"]), int(item["importance"])))
        log.append({"step": "rate", "items": len(backlog)})
        ctx.progress(step=3, total=total_steps, text=f"{len(backlog)} item(s) rated")

        # 4 · EA delegates by role and load
        stage = "delegate"
        rated = store.work_items_for(scope, company_id, ("rated",), limit=12)
        workers = [s for s in seats if s.get("agent_id") and s["key"] not in ("ea_board", "ceo", "ea_ceo") and int(s.get("department_id") or 0) in active_depts]
        if rated and workers:
            menu = "\n".join(f"{s['key']}: {s['title']} · {'; '.join(store.load_json(s.get('roles'), []))} · load {load.get(int(s['id']), 0)}" for s in workers)
            text = ask("ea_ceo", DELEGATE_PROMPT.format(seats=menu, items=_lines_for_items(rated)))
            chosen = {int(i): key for i, key in _ASSIGN_RE.findall(text)}
            for item in rated:
                seat = seats_by_key.get(chosen.get(int(item["id"]), ""))
                if not seat or not seat.get("agent_id"):
                    seat = match_seat(item["title"], item["brief"], workers)
                if seat:
                    store.set_work_status(int(item["id"]), "assigned", seat_id=int(seat["id"]), team_id=seat.get("team_id"), cycle_id=cycle_id)
                    load[int(seat["id"])] = load.get(int(seat["id"]), 0) + 1
        log.append({"step": "delegate", "items": len(rated)})
        ctx.progress(step=4, total=total_steps, text=f"{len(rated)} item(s) delegated")

        # 5 · analytics breaks large items down (≤ 2 calls)
        stage = "analytics"
        analytics, analytics_agent = actor("analytics_lead")
        broken = 0
        if analytics and analytics_agent:
            for item in [i for i in store.work_items_for(scope, company_id, ("assigned",), limit=200) if i.get("seat_id") == analytics["id"]][:2]:
                text = ask("analytics_lead", BREAKDOWN_PROMPT.format(title=item["title"], brief=item["brief"][:1500]))
                subs = _SUB_RE.findall(text)[:4]
                for title, brief in subs:
                    seat = match_seat(title, brief, workers) if workers else None
                    child = store.add_work_item(scope, company_id, title, brief, importance=int(item["importance"]), parent_id=int(item["id"]), catalog_id=item.get("catalog_id"), cycle_id=cycle_id)
                    if seat:
                        store.set_work_status(child, "assigned", seat_id=int(seat["id"]), team_id=seat.get("team_id"))
                if subs:
                    store.set_work_status(int(item["id"]), "done", feedback=f"broken down into {len(subs)} sub-item(s)")
                    broken += 1
        log.append({"step": "analytics", "broken_down": broken})
        ctx.progress(step=5, total=total_steps, text=f"{broken} item(s) broken down")

        # 6 · seats execute one item each as a bounded sub-mission
        stage = "execute"
        executed = 0
        assigned = [i for i in store.work_items_for(scope, company_id, ("assigned",), limit=200) if i.get("seat_id") and i.get("seat_id") != (analytics or {}).get("id")]
        seen_seats: set = set()
        for item in assigned:
            seat = seats_by_id.get(int(item["seat_id"]))
            agent = agents.get(int(seat["agent_id"])) if seat and seat.get("agent_id") else None
            if not seat or not agent or int(seat["id"]) in seen_seats:
                continue
            seen_seats.add(int(seat["id"]))
            store.set_work_status(int(item["id"]), "running", cycle_id=cycle_id)
            plan = task_plan(item["brief"] or item["title"], 1, max_tokens=call_tokens)[:3]
            outputs: List[Tuple[str, str]] = []
            try:
                for step in plan:
                    feedback = f"\nEDITOR NOTES FROM THE LAST ROUND:\n{item['feedback']}" if item.get("feedback") else ""
                    prompt = f"WORK ITEM: {item['title']} (importance {item['importance']})\nBRIEF: {item['brief']}{feedback}\n\nTASK: {step['description']}"
                    text, _ = call(ctx, budget, cycle_id, agent, seat, company, prompt, str(step["type"]), seats_by_id, call_tokens, mode=mode)
                    outputs.append((str(step["title"]), text))
            except CycleBudgetExceeded:
                store.set_work_status(int(item["id"]), "assigned")
                raise
            sections = [t for title, t in outputs if title.lower().startswith("draft section")]
            body = assemble_deliverable(item["title"], sections) if sections else (outputs[-1][1] if outputs else "")
            slug = deliverable_slug(item["title"])
            artifact_id, _ = vault.save_artifact(scope, f"{company['key']}-{slug}.md", f"company/{company['key']}/{slug}.md", body, "markdown")
            store.set_work_status(int(item["id"]), "review", artifact_id=int(artifact_id), thread_id=seat.get("thread_id"))
            if item.get("catalog_id"):
                cat = store.row("catalog", int(item["catalog_id"]))
                if cat and cat["stage"] in ("backlog", "development"):
                    store.update("catalog", int(cat["id"]), stage="draft")
            executed += 1
            ctx.progress(step=6, total=total_steps, text=f"{executed} deliverable(s) produced")
        log.append({"step": "execute", "deliverables": executed})

        # 7 · editor review (the reviewer seat: managing editor or QA reviewer)
        stage = "review"
        reviewer_key = "managing_editor" if "managing_editor" in seats_by_key else "qa_reviewer"
        reviewer, reviewer_agent = actor(reviewer_key)
        passed_count = failed_count = 0
        if reviewer and reviewer_agent:
            for item in store.work_items_for(scope, company_id, ("review",), limit=6):
                artifact = vault.export_artifact(int(item["artifact_id"]))[1] if item.get("artifact_id") else ""
                check = evaluate.deterministic_check(item["brief"], artifact)
                verdict = ask(reviewer_key, REVIEW_PROMPT.format(brief=item["brief"][:1200], body=artifact[:6000]), tokens=min(call_tokens, 500))
                model_pass = verdict.strip().upper().startswith("PASS")
                passed = bool(model_pass and check["passed"])
                worker_seat = seats_by_id.get(int(item["seat_id"])) if item.get("seat_id") else None
                store.insert("evaluations", scope, agent_id=int(worker_seat["agent_id"]) if worker_seat and worker_seat.get("agent_id") else 0,
                             grader_agent_id=int(reviewer_agent["id"]), kind="task", prompt_key="editor_review",
                             score=float(check["score"]), rubric={"check": check, "model_pass": model_pass}, passed=int(passed), cycle_id=cycle_id, timestamp=time.time())
                notes = "\n".join(verdict.strip().splitlines()[1:6]).strip()
                if passed:
                    store.set_work_status(int(item["id"]), "board", feedback=notes)
                    passed_count += 1
                    if item.get("catalog_id"):
                        cat = store.row("catalog", int(item["catalog_id"]))
                        if cat and cat["stage"] in ("draft", "edit"):
                            store.update("catalog", int(cat["id"]), stage="preliminary_review")
                else:
                    failed_count += 1
                    if int(item.get("retries") or 0) + 1 >= MAX_ITEM_RETRIES:
                        store.set_work_status(int(item["id"]), "rejected", feedback=notes or "did not pass review", retries=int(item.get("retries") or 0) + 1)
                    else:
                        store.set_work_status(int(item["id"]), "assigned", feedback=notes or f"failed the deterministic check: {check}", retries=int(item.get("retries") or 0) + 1)
        log.append({"step": "review", "passed": passed_count, "failed": failed_count})
        ctx.progress(step=7, total=total_steps, text=f"Review: {passed_count} passed, {failed_count} returned")
        to_board = store.count("work_items", scope, "company_id = ? AND status = 'board'", (company_id,))

        # 8 · report to the board
        stage = "report"
        facts = "\n".join([
            f"- scorecard misses: {len(misses)}", f"- headlines: {' / '.join(minutes['headline'][:3]) or 'none'}",
            f"- rated {len(backlog)}, delegated {len(rated)}, broken down {broken}, produced {executed}, review passed {passed_count}, returned {failed_count}",
            f"- waiting for the board: {to_board}", f"- question for the board: {minutes['question'][0] if minutes['question'] else 'none'}",
            f"- budget: {budget.calls} calls, {budget.tokens} tokens of {budget.max_tokens}",
        ])
        report = ask("ea_board", REPORT_PROMPT.format(facts=facts), task_type="quick_text", tokens=min(call_tokens, 600))
        if not report.strip():
            report = "Cycle report (deterministic):\n" + facts
        board_thread = payload.get("board_thread_id") or int(vault.active_thread(scope, "company")["id"])
        vault.append_message(scope, "assistant", report.strip(), provider="ea_board", mode=mode, thread_id=int(board_thread), workspace="company", task_type="report")
        log.append({"step": "report", "chars": len(report)})
        ctx.progress(step=8, total=total_steps, text="Report sent to the board")
    except CycleBudgetExceeded as exc:
        status = "budget"
        log.append({"step": stage, "stopped": f"budget exhausted at {exc}"})
    except JobCancelled:
        store.finish_cycle(cycle_id, "cancelled", budget.tokens, budget.calls, log)
        raise
    next_run_after: Optional[float] = None
    next_job: Optional[int] = None
    if payload.get("chain"):
        next_job, next_run_after = schedule_next(ctx, {k: v for k, v in payload.items() if k != "chained_from"}, float(payload.get("interval_s") or company["interval_s"]))
    store.finish_cycle(cycle_id, status, budget.tokens, budget.calls, log, next_run_after)
    return {"cycle_id": cycle_id, "status": status, "calls": budget.calls, "tokens": budget.tokens, "to_board": to_board, "log": log, "next_job": next_job, "next_run_after": next_run_after}


register_handler(KIND_COMPANY, company_cycle)
