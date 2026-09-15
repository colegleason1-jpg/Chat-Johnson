"""The academy cycle (Plato's Republic): Producers work, Auxiliaries grade and guard, Philosophers graduate.

Every promotion pairs a model grade with the deterministic check, promotions are capped per cycle,
and graduates fill open company seats by seat importance. Bounded by calls and the academy's
treasury share like every cycle.
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple

from .. import vault
from ..errors import plain_error
from ..jobs import JobCancelled, JobContext, enqueue, register_handler
from ..router import _estimate_tokens, cortex_wait_seconds, generate_mode, local_endpoint, local_first_generate, strip_reasoning_tags
from . import economy, evaluate, store
from .cycles import CycleBudget, CycleBudgetExceeded, MAX_WAIT_SECONDS, WORKSPACE
from .personas import build_academy_messages
from .templates import ALLOWANCES

KIND_ACADEMY = "academy_cycle"
DEFAULT_MAX_CALLS = 24
DEFAULT_MAX_TOKENS = 40_000
DEFAULT_CALL_TOKENS = 700
PRODUCERS_PER_CYCLE = 4   # the floor; the budget raises it up to PRODUCERS_MAX so the society actually works
PRODUCERS_MAX = 8
PROMOTIONS_PER_CYCLE = 3
PASSES_TO_AUXILIARY = 3
AGREEMENTS_TO_EXAM = 3
EXAM_PASS_SCORE = 70
EXAM_COOLDOWN_SECONDS = 3 * 86_400.0
ALLOWANCE_SHARE = 0.2  # of the cycle's token budget may be paid out as allowances
TEACHING_PREFIX = "TEACHING"

PRODUCER_TASKS: Tuple[Tuple[str, str], ...] = (
    ("Summarise a brief", "Summarise this in 120 words for a busy editor: 'The studio releases six literature works in wave one after a preliminary review by the board; feedback from each work shapes marketing and the company vision; the remaining eleven projects are outlined meanwhile.' Keep every number."),
    ("Parse a list into a table", "Turn these into a markdown table with columns title, field, wave: 'IP 01 literature wave 1; IP 07 entertainment wave 2; IP 12 literature wave 2; IP 03 literature wave 1'. Then add one line counting rows per wave."),
    ("Write a product description", "Write a 120-word plain description of a 'Supply Chain Optimizer' that routes stock between warehouses to cut shortages, from these facts: cuts stockouts, runs nightly, exports a CSV plan, needs no new hardware. No claims beyond the facts."),
    ("Outline a chapter", "From this premise, 'a lighthouse keeper discovers the tides are answering her', write a five-beat outline for chapter one with one sentence per beat and a closing question for the next chapter."),
    ("Rewrite plainly", "Rewrite in plain language at a ninth-grade level, keeping the meaning: 'Pursuant to the aforementioned review cadence, deliverables shall be remanded to their originating seat upon editorial non-acceptance.'"),
    ("Extract entities", "List every person, place, and organisation in: 'Mara left Port Ellis for the Harrow Institute, where Dr. Okafor kept the archive that the Council had sealed.' Group them under three headings."),
    ("Draft an FAQ answer", "Answer in under 60 words for a product FAQ: 'Does the Chat Bot Book System store my books?' Facts: books stay on the user's device; only passages the user selects are sent to the model; nothing is kept after the session."),
    ("Make a checklist", "Write a seven-item checklist for releasing a finished literature work: from final edit to the published artifact and the marketing hand-off. One line per item, in order."),
)
GRADE_PROMPT = (
    "You are an Auxiliary guardian grading a Producer's work. Grade strictly against the brief.\nBRIEF: {brief}\nWORK:\n{work}\n"
    "Reply with lines only: 'SCORE: <0-100>', then 'PASS' or 'FAIL', up to 3 lines of notes, and 'AUDIT: ok' or 'AUDIT: <problem>' when the work is empty, off-brief, padded, contains placeholders, or makes unsafe claims."
)
PHILOSOPHER_EXAM = (
    "Philosopher evaluation. Facts: a studio has six works in editorial review, two writers idle, a board review due in 10 days, "
    "and a marketing department that opens only after wave one is published. Write three sections headed exactly CONTEXT, PLAN, "
    "SYNTHESIS: CONTEXT restates only the facts given and names what is unknown; PLAN gives ordered steps for the next 10 days with "
    "what each needs and produces; SYNTHESIS combines them into one recommendation in under 120 words. At least 250 words in total."
)
EXAM_GRADE_PROMPT = (
    "Grade this evaluation answer per rubric. Reply lines only: 'CONTEXT: <0-100>' (used only the facts, named unknowns), "
    "'PLANNING: <0-100>' (ordered, concrete steps with inputs and outputs), 'SYNTHESIS: <0-100>' (one coherent recommendation), then 'PASS' or 'FAIL'.\nANSWER:\n{answer}"
)
_SCORE_RE = re.compile(r"SCORE\s*:\s*(\d{1,3})", re.I)
_PASS_LINE_RE = re.compile(r"^\s*(?:verdict\s*:\s*)?(PASS|FAIL)\b", re.I)


def verdict_passes(verdict: str) -> Optional[bool]:
    """PASS/FAIL from the grader's lines ('PASS', 'Verdict: PASS', 'PASS.'); None when no line says either."""
    for line in verdict.splitlines():
        match = _PASS_LINE_RE.match(line)
        if match:
            return match.group(1).upper() == "PASS"
    return None


def producer_count(max_tokens: int, call_tokens: int, floor: int = PRODUCERS_PER_CYCLE, ceiling: int = PRODUCERS_MAX) -> int:
    """How many producers work this cycle: the budget decides between the floor and the ceiling (a task plus its grade ≈ 4 calls' tokens)."""
    return max(floor, min(ceiling, int(max_tokens) // max(1, int(call_tokens) * 4)))
_RUBRIC_RE = re.compile(r"(CONTEXT|PLANNING|SYNTHESIS)\s*:\s*(\d{1,3})", re.I)
_AUDIT_RE = re.compile(r"AUDIT\s*:\s*(.+)", re.I)


def ensure_agent_thread(scope: str, agent: Dict[str, Any]) -> int:
    if agent.get("thread_id") and vault.thread_by_id(int(agent["thread_id"]), scope):
        return int(agent["thread_id"])
    thread_id = vault.create_thread(scope, title=f"Academy · {agent['name']}", workspace=WORKSPACE)
    store.update("agents", int(agent["id"]), thread_id=thread_id)
    agent["thread_id"] = thread_id
    return thread_id


def call_free(ctx: JobContext, budget: CycleBudget, cycle_id: int, agent: Dict[str, Any], prompt: str, task_type: str, max_tokens: int, mode: str = "normal", role_note: str = "", prefer_local: bool = False) -> Tuple[str, Any]:
    """One metered call by an academy agent in its own thread (the seat-less twin of cycles.call).

    ``prefer_local`` sends cheap labour (Producer tasks, leisure notes) to a registered local model first.
    """
    scope = ctx.project_scope
    thread_id = ensure_agent_thread(scope, agent)
    from .leisure import dream_excerpt_for  # local: leisure imports this module

    excerpt = dream_excerpt_for(scope, agent.get("id"), prompt)
    messages = build_academy_messages(scope, agent, prompt, thread_id, max_tokens, role_note=(role_note + ("\nFROM YOUR OWN RESEARCH (leisure notes):\n" + excerpt if excerpt else "")).strip())
    if not budget.allows(_estimate_tokens(messages) + int(max_tokens)):
        raise CycleBudgetExceeded(f"{budget.calls} calls / {budget.tokens} tokens used")
    ctx.check_cancel()
    wait = cortex_wait_seconds(ctx.ledger, messages, max_tokens)
    if 0 < wait <= MAX_WAIT_SECONDS:
        ctx.progress(text=f"Waiting {int(wait) + 1}s for a free-tier window ({agent['name']})…")
        ctx.sleep(wait + 0.5)
    started = time.perf_counter()
    with ctx.request_lock:
        if prefer_local and local_endpoint() is not None:
            answer, decision = local_first_generate(mode, task_type, messages, ctx.ledger, max_tokens=max_tokens, temperature=0.3)
        else:
            answer, decision = generate_mode(mode, task_type, messages, ctx.ledger, max_tokens=max_tokens, temperature=0.3)
    answer = strip_reasoning_tags(answer)
    vault.append_message(scope, "user", prompt, mode=mode, thread_id=thread_id, workspace=WORKSPACE, task_type=task_type)
    vault.append_message(scope, "assistant", answer, provider=f"{decision.provider}/{decision.model}", mode=mode, thread_id=thread_id, workspace=WORKSPACE, task_type=task_type)
    try:
        vault.record_route(scope, "academy", task_type, f"{decision.provider}/{decision.model}", mode, int((time.perf_counter() - started) * 1000), decision.finish, decision.reason)
    except Exception:
        pass
    budget.step(economy.charge(scope, agent.get("id"), messages, answer, decision, cycle_id=cycle_id, job_id=ctx.job_id))
    return answer, decision


def _task_count(scope: str, agent_id: int) -> int:
    return store.count("evaluations", scope, "agent_id = ? AND kind = 'task'", (int(agent_id),))


def _passes(scope: str, agent_id: int) -> int:
    return store.count("evaluations", scope, "agent_id = ? AND kind = 'task' AND passed = 1", (int(agent_id),))


def _last_exam(scope: str, agent_id: int) -> float:
    latest = store.rows("evaluations", scope, "agent_id = ? AND kind = 'graduation'", (int(agent_id),), order="id DESC", limit=1)
    return float(latest[0]["timestamp"]) if latest else 0.0


def _agreements(scope: str, grader_id: int) -> int:
    return sum(1 for r in store.rows("evaluations", scope, "grader_agent_id = ? AND kind = 'task'", (int(grader_id),), limit=500) if store.load_json(r.get("rubric"), {}).get("agree"))


def _promote(scope: str, agent: Dict[str, Any], tier: str, cycle_id: int, score: float, reason: str) -> None:
    store.update("agents", int(agent["id"]), tier=tier, allowance=ALLOWANCES[tier])
    store.insert("evaluations", scope, agent_id=int(agent["id"]), kind="promotion" if tier == "auxiliary" else "graduation", prompt_key=tier, score=score, rubric={"reason": reason}, passed=1, cycle_id=cycle_id, timestamp=time.time())
    store.log_personnel(scope, "promote" if tier == "auxiliary" else "graduate", agent_id=int(agent["id"]), reason=reason, approved_by="academy")


def run_now(scope: str, secrets: Dict[str, str], mode: str = "normal", call_tokens: int = DEFAULT_CALL_TOKENS, chain: bool = False, interval_s: float = 3 * 3600, max_calls: int = DEFAULT_MAX_CALLS, max_tokens: int = DEFAULT_MAX_TOKENS, run_after: float = 0.0) -> int:
    payload = {"mode": mode, "call_tokens": int(call_tokens), "chain": bool(chain), "interval_s": float(interval_s), "max_calls": int(max_calls), "max_tokens": int(max_tokens)}
    return enqueue(scope, KIND_ACADEMY, payload, secrets, run_after=run_after)


def academy_cycle(ctx: JobContext) -> Dict[str, Any]:
    scope = ctx.project_scope
    payload = ctx.payload
    mode = str(payload.get("mode") or "normal")
    call_tokens = int(payload.get("call_tokens") or DEFAULT_CALL_TOKENS)
    treasury = economy.Treasury(ctx.ledger, economy.shares_for(scope))
    budget = CycleBudget(int(payload.get("max_calls") or DEFAULT_MAX_CALLS), treasury.cycle_budget("academy", int(payload.get("max_tokens") or DEFAULT_MAX_TOKENS)))
    cycle_id = store.start_cycle(scope, "academy", None, ctx.job_id, budget.max_tokens)
    if budget.max_tokens < call_tokens * 2:
        note = [{"step": "budget", "stopped": f"treasury exhausted for today: {budget.max_tokens} tokens available for the academy"}]
        store.finish_cycle(cycle_id, "budget", 0, 0, note)
        ctx.progress(step=6, total=6, text="No treasury left for the academy today; nothing was called")
        return {"cycle_id": cycle_id, "status": "budget", "calls": 0, "tokens": 0, "promoted": 0, "graduated": 0, "hired": 0, "log": note, "next_job": None}
    log: List[Dict[str, Any]] = []
    status = "done"
    stage = "allowance"
    promoted = graduated = 0
    hired: List[Dict[str, Any]] = []
    try:
        # Allowances come out of this cycle's budget (free agents only), so nothing is minted from nothing.
        granted = economy.allowance_run(scope, cycle_id, max_total=int(budget.max_tokens * ALLOWANCE_SHARE))
        budget.tokens += granted
        log.append({"step": "allowance", "tokens": granted})
        ctx.progress(step=1, total=6, text=f"Allowances paid: {granted} tokens")

        # Producers work: the ones with the fewest tasks so far go first; the budget decides how many.
        stage = "producers"
        producers = sorted(store.agents_for(scope, tier="producer", employment="free"), key=lambda a: (_task_count(scope, int(a["id"])), int(a["id"])))
        pending: List[Tuple[Dict[str, Any], Tuple[str, str], str]] = []
        count = int(payload.get("producers_per_cycle") or producer_count(budget.max_tokens, call_tokens))
        for agent in producers[:count]:
            title, brief = PRODUCER_TASKS[(int(agent["id"]) + cycle_id) % len(PRODUCER_TASKS)]
            teaching = str(agent.get("note") or "")
            role_note = f"A guardian's note on your last task: {teaching[len(TEACHING_PREFIX) + 1:].strip()}" if teaching.startswith(TEACHING_PREFIX) else ""
            text, _ = call_free(ctx, budget, cycle_id, agent, f"TASK: {title}\n{brief}", "quick_text", call_tokens, mode=mode, role_note=role_note, prefer_local=True)
            pending.append((agent, (title, brief), text))
        log.append({"step": "producers", "tasks": len(pending)})
        ctx.progress(step=2, total=6, text=f"{len(pending)} producer task(s) done")

        # Auxiliaries grade and audit; the deterministic check sits beside every grade.
        stage = "grading"
        graders = store.agents_for(scope, tier="auxiliary", employment="free")
        for index, (agent, (title, brief), text) in enumerate(pending):
            check = evaluate.deterministic_check(brief, text, min_words=40)
            grader = graders[index % len(graders)] if graders else None
            model_pass: Optional[bool] = None
            score = float(check["score"])
            audit = "no grader"
            if grader:
                try:
                    verdict, _ = call_free(ctx, budget, cycle_id, grader, GRADE_PROMPT.format(brief=brief, work=text[:4000]), "reasoning", min(call_tokens, 400), mode=mode, role_note="You are grading as a guardian this cycle.")
                except CycleBudgetExceeded:
                    verdict = ""
                if verdict:
                    match = _SCORE_RE.search(verdict)
                    score = min(100, int(match.group(1))) / 100 if match else score
                    model_pass = verdict_passes(verdict)
                    audit_match = _AUDIT_RE.search(verdict)
                    audit = audit_match.group(1).strip()[:200] if audit_match else "ok"
            passed = bool(check["passed"] and (model_pass if model_pass is not None else True))
            if grader and verdict and not passed:
                # Auxiliaries teach as well as guard: the grader's notes reach the producer's next task.
                notes = " ".join(line.strip() for line in verdict.splitlines()[2:5] if line.strip() and not line.upper().startswith("AUDIT"))[:300]
                if notes:
                    store.update("agents", int(agent["id"]), note=f"{TEACHING_PREFIX} ({grader['name']}): {notes}")
            elif passed and str(agent.get("note") or "").startswith(TEACHING_PREFIX):
                store.update("agents", int(agent["id"]), note="")
            store.insert("evaluations", scope, agent_id=int(agent["id"]), grader_agent_id=int(grader["id"]) if grader else None, kind="task", prompt_key=title,
                         score=round(score, 2), rubric={"check": check, "model_pass": model_pass, "audit": audit, "agree": (model_pass == check["passed"]) if model_pass is not None else None},
                         passed=int(passed), cycle_id=cycle_id, timestamp=time.time())
        log.append({"step": "grading", "graded": len(pending), "graders": len(graders)})
        ctx.progress(step=3, total=6, text=f"{len(pending)} task(s) graded")

        # Promotions: Producer → Auxiliary on passes; Auxiliary → Philosopher through the exam.
        stage = "promotions"
        for agent in store.agents_for(scope, tier="producer", employment="free"):
            if promoted >= PROMOTIONS_PER_CYCLE:
                break
            if _passes(scope, int(agent["id"])) >= PASSES_TO_AUXILIARY:
                _promote(scope, agent, "auxiliary", cycle_id, 1.0, f"{PASSES_TO_AUXILIARY} graded passes")
                promoted += 1
        # Candidates rotate: a failed exam sits out the cooldown, and the longest-waiting candidate goes first.
        now = time.time()
        candidates = [a for a in store.agents_for(scope, tier="auxiliary", employment="free") if _agreements(scope, int(a["id"])) >= AGREEMENTS_TO_EXAM]
        candidates = [a for a in candidates if now - _last_exam(scope, int(a["id"])) >= EXAM_COOLDOWN_SECONDS]
        candidates.sort(key=lambda a: (_last_exam(scope, int(a["id"])), int(a["id"])))
        exam_log: Dict[str, Any] = {}
        if candidates:
            candidate = candidates[0]
            answer, _ = call_free(ctx, budget, cycle_id, candidate, PHILOSOPHER_EXAM, "reasoning", max(call_tokens, 900), mode=mode, role_note="This is your Philosopher evaluation.")
            check = evaluate.deterministic_check(PHILOSOPHER_EXAM, answer, min_words=250)
            scores: Dict[str, int] = {}
            examiners = store.agents_for(scope, tier="philosopher", employment="free") or store.agents_for(scope, tier="philosopher")
            model_pass = None
            if examiners:
                try:
                    verdict, _ = call_free(ctx, budget, cycle_id, examiners[0], EXAM_GRADE_PROMPT.format(answer=answer[:6000]), "reasoning", min(call_tokens, 300), mode=mode, role_note="You are examining a graduation candidate.")
                    scores = {k.lower(): min(100, int(v)) for k, v in _RUBRIC_RE.findall(verdict)}
                    model_pass = verdict_passes(verdict)
                    if model_pass is None:
                        model_pass = "PASS" in verdict.upper() and "FAIL" not in verdict.upper().splitlines()[-1]
                except CycleBudgetExceeded:
                    verdict = ""
            rubric_ok = bool(scores) and all(scores.get(k, 0) >= EXAM_PASS_SCORE for k in ("context", "planning", "synthesis"))
            passed = bool(check["passed"] and (rubric_ok if scores else check["words"] >= 250) and (model_pass if model_pass is not None else True))
            average = (sum(scores.values()) / (100 * len(scores))) if scores else float(check["score"])
            store.insert("evaluations", scope, agent_id=int(candidate["id"]), grader_agent_id=int(examiners[0]["id"]) if examiners else None, kind="graduation", prompt_key="philosopher_exam",
                         score=round(average, 2), rubric={"check": check, "scores": scores, "model_pass": model_pass}, passed=int(passed), cycle_id=cycle_id, timestamp=time.time())
            if passed:
                _promote(scope, candidate, "philosopher", cycle_id, average, "passed the Philosopher evaluation")
                graduated += 1
            exam_log = {"candidate": candidate["name"], "passed": passed, "scores": scores}
        log.append({"step": "promotions", "promoted": promoted, "graduated": graduated, "exam": exam_log})
        ctx.progress(step=4, total=6, text=f"{promoted} promoted, {graduated} graduated")

        # Graduates fill open seats in every company.
        stage = "graduation"
        hired = store.fill_open_seats(scope)
        log.append({"step": "hiring", "hired": [{"seat": h["seat"]["title"], "agent": h["agent"]["name"]} for h in hired]})
        ctx.progress(step=5, total=6, text=f"{len(hired)} seat(s) filled")
    except CycleBudgetExceeded as exc:
        status = "budget"
        log.append({"step": stage, "stopped": f"budget exhausted at {exc}"})
    except JobCancelled:
        store.finish_cycle(cycle_id, "cancelled", budget.tokens, budget.calls, log)
        raise
    except Exception as exc:  # the cycle row records the failure; the tick backs off instead of retrying blindly
        log.append({"step": stage, "error": plain_error(exc)[:300]})
        store.finish_cycle(cycle_id, "failed", budget.tokens, budget.calls, log)
        raise
    next_run_after: Optional[float] = None
    next_job: Optional[int] = None
    if payload.get("chain"):
        next_run_after = time.time() + max(60.0, float(payload.get("interval_s") or 3 * 3600))
        next_job = enqueue(scope, KIND_ACADEMY, dict(payload, chained_from=ctx.job_id), ctx.secrets, run_after=next_run_after)
    store.finish_cycle(cycle_id, status, budget.tokens, budget.calls, log, next_run_after)
    ctx.progress(step=6, total=6, text="Academy cycle finished")
    return {"cycle_id": cycle_id, "status": status, "calls": budget.calls, "tokens": budget.tokens, "promoted": promoted, "graduated": graduated, "hired": len(hired), "log": log, "next_job": next_job}


register_handler(KIND_ACADEMY, academy_cycle)
