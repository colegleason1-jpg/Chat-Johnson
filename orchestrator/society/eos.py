"""Traction/EOS mechanics: the weekly scorecard, the Level 10 agenda and minutes, rocks, to-dos, and timeline drift.

KPIs are measured over a rolling seven-day window, so the first cycle of a week never sees zero
deliverables; the scorecard keeps one row per seat, week, and KPI (a cycle replaces its own week's
row). Minutes update rocks and to-dos; catalog stages complete timeline milestones deterministically.
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Sequence

from ..keyword_search import keywords
from . import store

DAY = 86_400.0
WINDOW_DAYS = 7.0
_MINUTE_LINE = re.compile(r"^\s*(HEADLINE|ISSUE|TODO|QUESTION|ROCK|DONE)\s*:\s*(.+?)\s*$", re.I)
ROCK_STATUSES = ("on_track", "off_track", "done")


def iso_week(timestamp: float = 0.0) -> str:
    year, week, _ = time.gmtime(timestamp or time.time()).isocalendar() if hasattr(time.gmtime(), "isocalendar") else (0, 0, 0)
    if not year:
        import datetime as _dt

        year, week, _ = _dt.datetime.utcfromtimestamp(timestamp or time.time()).isocalendar()
    return f"{year}-W{week:02d}"


def _window_start(timestamp: float = 0.0) -> float:
    return (timestamp or time.time()) - WINDOW_DAYS * DAY


def scorecard_review(scope: str, company_id: int, cycle_id: int) -> List[Dict[str, Any]]:
    """Compute each filled seat's KPI actuals over the last seven days; one row per (seat, week, kpi); returns the misses."""
    week = iso_week()
    since = _window_start()
    misses: List[Dict[str, Any]] = []
    done_by_seat: Dict[int, int] = {}
    for item in store.work_items_for(scope, company_id, ("board", "done"), limit=2000):
        if float(item["updated_at"]) >= since and item.get("seat_id"):
            done_by_seat[int(item["seat_id"])] = done_by_seat.get(int(item["seat_id"]), 0) + 1
    cycles_in_window = sum(1 for c in store.cycles_for(scope, company_id, limit=200) if float(c["started_at"]) >= since)
    for seat in store.seats_for(scope, company_id, status="filled"):
        kpis = store.load_json(seat.get("kpis"), {})
        agent_id = seat.get("agent_id")
        graded = store.rows("evaluations", scope, "agent_id = ? AND kind = 'task' AND timestamp >= ?", (int(agent_id or 0), since), limit=200)
        reviews_given = store.count("evaluations", scope, "grader_agent_id = ? AND timestamp >= ?", (int(agent_id or 0), since))
        for kpi, target in kpis.items():
            if kpi == "deliverables":
                actual = float(done_by_seat.get(int(seat["id"]), 0))
            elif kpi == "review_pass_rate":
                actual = (sum(1 for g in graded if g["passed"]) / len(graded)) if graded else 1.0
            elif kpi == "reviews":
                actual = float(reviews_given)
            elif kpi == "reports":
                actual = float(cycles_in_window)  # this cycle's row is already in the table
            else:
                actual = 0.0
            met = actual >= float(target)
            store.delete("scorecard", scope, "seat_id = ? AND week = ? AND kpi = ?", (int(seat["id"]), week, kpi))
            store.insert("scorecard", scope, seat_id=int(seat["id"]), agent_id=agent_id, week=week, kpi=kpi, target=float(target), actual=actual, met=int(met), cycle_id=cycle_id)
            if not met:
                misses.append({"seat": seat["key"], "kpi": kpi, "target": target, "actual": actual})
    return misses


def timeline_drift(scope: str, company_id: int, horizon_days: float = 7.0) -> List[Dict[str, Any]]:
    now = time.time()
    return [
        m for m in store.rows("timeline", scope, "company_id = ? AND status != 'done'", (int(company_id),))
        if m.get("due_at") and float(m["due_at"]) <= now + horizon_days * DAY
    ]


def l10_agenda(scope: str, company: Dict[str, Any], misses: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    company_id = int(company["id"])
    return {
        "scorecard_misses": list(misses)[:8],
        "rocks": [{"title": r["title"], "status": r["status"]} for r in store.rows("rocks", scope, "company_id = ?", (company_id,), limit=8)],
        "open_issues": [i["title"] for i in store.rows("issues", scope, "company_id = ? AND status = 'open'", (company_id,), limit=8)],
        "open_todos": [f"{t['text'][:80]}" for t in store.rows("todos", scope, "company_id = ? AND done = 0", (company_id,), limit=8)],
        "escalations": [e["text"][:160] for e in store.rows("escalations", scope, "company_id = ? AND status = 'open'", (company_id,), limit=5)],
        "board_feedback": [f["text"][:200] for f in store.rows("feedback", scope, "company_id = ?", (company_id,), order="id DESC", limit=3)],
        "timeline_drift": [{"milestone": m["milestone"], "due_in_days": round((float(m["due_at"]) - time.time()) / DAY, 1)} for m in timeline_drift(scope, company_id)],
        "waiting_for_board": store.count("work_items", scope, "company_id = ? AND status = 'board'", (company_id,)),
        "in_review": store.count("work_items", scope, "company_id = ? AND status = 'review'", (company_id,)),
        "backlog": store.count("work_items", scope, "company_id = ? AND status = 'backlog'", (company_id,)),
    }


def agenda_text(agenda: Dict[str, Any]) -> str:
    lines = ["Segue and scorecard:"]
    lines += [f"- miss: {m['seat']} {m['kpi']} {m['actual']} < {m['target']}" for m in agenda["scorecard_misses"]] or ["- all KPIs met"]
    lines.append("Rocks: " + (", ".join(f"{r['title']} [{r['status']}]" for r in agenda["rocks"]) or "none"))
    lines.append("Open issues: " + ("; ".join(agenda["open_issues"]) or "none"))
    lines.append("Open to-dos: " + ("; ".join(agenda.get("open_todos", [])) or "none"))
    lines.append("Escalations: " + ("; ".join(agenda["escalations"]) or "none"))
    lines.append("Board feedback: " + ("; ".join(agenda["board_feedback"]) or "none yet"))
    lines.append("Timeline: " + ("; ".join(f"{m['milestone']} due in {m['due_in_days']} d" for m in agenda["timeline_drift"]) or "no milestone within a week"))
    lines.append(f"Work: {agenda['backlog']} in backlog, {agenda['in_review']} in review, {agenda['waiting_for_board']} waiting for the board.")
    return "\n".join(lines)


def parse_minutes(text: str) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {"headline": [], "issue": [], "todo": [], "question": [], "rock": [], "done": []}
    for line in text.splitlines():
        match = _MINUTE_LINE.match(line)
        if match:
            out[match.group(1).lower()].append(match.group(2).strip()[:300])
    return out


def _overlap(a: str, b: str) -> int:
    return len(set(keywords(a)) & set(keywords(b)))


def apply_minutes(scope: str, company_id: int, minutes: Dict[str, List[str]], seats_by_key: Dict[str, Dict[str, Any]]) -> Dict[str, int]:
    """Rocks change status and to-dos close from the minutes' ROCK and DONE lines (keyword matched, never invented)."""
    changed = {"rocks": 0, "todos": 0}
    rocks = store.rows("rocks", scope, "company_id = ?", (int(company_id),), limit=20)
    for line in minutes.get("rock", []):
        title, _, status = line.partition("|")
        status = status.strip().lower().replace(" ", "_")
        if status not in ROCK_STATUSES or not rocks:
            continue
        best = max(rocks, key=lambda r: _overlap(r["title"], title))
        if _overlap(best["title"], title) >= 2 and best["status"] != status:
            store.update("rocks", int(best["id"]), status=status)
            changed["rocks"] += 1
    open_todos = store.rows("todos", scope, "company_id = ? AND done = 0", (int(company_id),), limit=50)
    for line in minutes.get("done", []):
        seat_key, _, text = line.partition(":")
        seat = seats_by_key.get(seat_key.strip())
        candidates = [t for t in open_todos if not seat or t.get("seat_id") == seat["id"]] or open_todos
        if not candidates:
            continue
        best = max(candidates, key=lambda t: _overlap(t["text"], text or line))
        if _overlap(best["text"], text or line) >= 1:
            store.update("todos", int(best["id"]), done=1)
            open_todos = [t for t in open_todos if t["id"] != best["id"]]
            changed["todos"] += 1
    return changed


_MILESTONE_RULES = (
    # (milestone word, the catalog stages that mean "this milestone is behind us" for every wave-1 work)
    ("outline", ("draft", "edit", "preliminary_review", "board_feedback", "final", "published", "marketed")),
    ("outlines", ("draft", "edit", "preliminary_review", "board_feedback", "final", "published", "marketed")),
    ("draft", ("edit", "preliminary_review", "board_feedback", "final", "published", "marketed")),
    ("drafts", ("edit", "preliminary_review", "board_feedback", "final", "published", "marketed")),
    ("editorial", ("preliminary_review", "board_feedback", "final", "published", "marketed")),
    ("review", ("board_feedback", "final", "published", "marketed")),
    ("publish", ("published", "marketed")),
)
_GENERIC_WORDS = {"done", "set", "drafted", "previewed", "complete", "wave", "board"}


def advance_timeline(scope: str, company_id: int) -> int:
    """Mark timeline milestones done from the catalog's stages (studio) or finished work items per product (software); returns how many."""
    changed = 0
    catalog = store.rows("catalog", scope, "company_id = ?", (int(company_id),), limit=200)
    wave_one = [c for c in catalog if int(c.get("release_wave") or 0) == 1] or catalog
    done_titles = [str(i["title"]).lower() for i in store.work_items_for(scope, company_id, ("done", "board"), limit=1000)]
    for milestone in store.rows("timeline", scope, "company_id = ? AND status != 'done'", (int(company_id),), limit=50):
        words = set(keywords(str(milestone["milestone"])))
        finished = False
        for word, stages in _MILESTONE_RULES:
            if word in words and wave_one and all(c["stage"] in stages for c in wave_one):
                finished = True
                break
        if not finished and not any(word in words for word, _ in _MILESTONE_RULES):
            # Software milestones ("Positioning done"): every product has a finished item that names the milestone's word.
            key_words = [w for w in words if w not in _GENERIC_WORDS]
            finished = bool(key_words) and bool(catalog) and all(
                any(any(w in title for w in key_words) and str(c["title"]).lower() in title for title in done_titles) for c in catalog
            )
        if finished:
            store.update("timeline", int(milestone["id"]), status="done")
            changed += 1
    return changed
