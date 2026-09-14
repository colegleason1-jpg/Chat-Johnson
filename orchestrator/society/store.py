"""Society tables (companies on EOS, the academy on the Republic) and thin accessors.

Company state and academy state are separate table groups. ``agents`` carries no company id:
one society feeds every company, and a seat binds an agent to a company only while employed.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .. import vault

SOCIETY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_scope TEXT NOT NULL, key TEXT NOT NULL, name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'studio', vision TEXT NOT NULL DEFAULT '', core_values TEXT NOT NULL DEFAULT '[]',
    core_focus TEXT NOT NULL DEFAULT '', ten_year TEXT NOT NULL DEFAULT '', three_year TEXT NOT NULL DEFAULT '',
    one_year TEXT NOT NULL DEFAULT '', interval_s INTEGER NOT NULL DEFAULT 21600, daily_share REAL NOT NULL DEFAULT 0.35,
    status TEXT NOT NULL DEFAULT 'active', created_at REAL NOT NULL, UNIQUE (project_scope, key)
);
CREATE TABLE IF NOT EXISTS departments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_scope TEXT NOT NULL, company_id INTEGER NOT NULL, key TEXT NOT NULL,
    name TEXT NOT NULL, head_seat_id INTEGER, active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS teams (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_scope TEXT NOT NULL, company_id INTEGER NOT NULL,
    department_id INTEGER, name TEXT NOT NULL, lead_seat_id INTEGER
);
CREATE TABLE IF NOT EXISTS seats (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_scope TEXT NOT NULL, company_id INTEGER, department_id INTEGER,
    team_id INTEGER, key TEXT NOT NULL, title TEXT NOT NULL, roles TEXT NOT NULL DEFAULT '[]', reports_to INTEGER,
    kpis TEXT NOT NULL DEFAULT '{}', importance INTEGER NOT NULL DEFAULT 3, agent_id INTEGER,
    status TEXT NOT NULL DEFAULT 'open', thread_id INTEGER, miss_weeks INTEGER NOT NULL DEFAULT 0,
    miss_week TEXT NOT NULL DEFAULT '', idle_cycles INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS rocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_scope TEXT NOT NULL, company_id INTEGER NOT NULL, quarter TEXT NOT NULL,
    title TEXT NOT NULL, owner_seat_id INTEGER, status TEXT NOT NULL DEFAULT 'on_track', due_at REAL
);
CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_scope TEXT NOT NULL, company_id INTEGER NOT NULL, title TEXT NOT NULL,
    raised_by_seat INTEGER, status TEXT NOT NULL DEFAULT 'open', resolution TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_scope TEXT NOT NULL, company_id INTEGER NOT NULL, seat_id INTEGER,
    text TEXT NOT NULL, due_at REAL, done INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meetings (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_scope TEXT NOT NULL, company_id INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'l10', held_at REAL NOT NULL, agenda TEXT NOT NULL DEFAULT '{}', minutes_artifact_id INTEGER
);
CREATE TABLE IF NOT EXISTS timeline (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_scope TEXT NOT NULL, company_id INTEGER NOT NULL, milestone TEXT NOT NULL,
    due_at REAL, status TEXT NOT NULL DEFAULT 'planned', work_item_id INTEGER
);
CREATE TABLE IF NOT EXISTS catalog (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_scope TEXT NOT NULL, company_id INTEGER NOT NULL, key TEXT NOT NULL,
    title TEXT NOT NULL, field TEXT NOT NULL DEFAULT 'literature', logline TEXT NOT NULL DEFAULT '',
    stage TEXT NOT NULL DEFAULT 'backlog', release_wave INTEGER NOT NULL DEFAULT 2, artifact_id INTEGER
);
CREATE TABLE IF NOT EXISTS work_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_scope TEXT NOT NULL, company_id INTEGER NOT NULL, cycle_id INTEGER,
    catalog_id INTEGER, parent_id INTEGER, title TEXT NOT NULL, brief TEXT NOT NULL DEFAULT '',
    importance INTEGER NOT NULL DEFAULT 3, status TEXT NOT NULL DEFAULT 'backlog', seat_id INTEGER, team_id INTEGER,
    thread_id INTEGER, artifact_id INTEGER, feedback TEXT NOT NULL DEFAULT '', retries INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_work_items_company ON work_items(project_scope, company_id, status, importance DESC, id ASC);
CREATE TABLE IF NOT EXISTS escalations (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_scope TEXT NOT NULL, company_id INTEGER NOT NULL, from_seat INTEGER,
    to_seat INTEGER, direction TEXT NOT NULL DEFAULT 'up', text TEXT NOT NULL, reply TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS scorecard (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_scope TEXT NOT NULL, seat_id INTEGER NOT NULL, agent_id INTEGER,
    week TEXT NOT NULL, kpi TEXT NOT NULL, target REAL NOT NULL, actual REAL NOT NULL, met INTEGER NOT NULL, cycle_id INTEGER
);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_scope TEXT NOT NULL, company_id INTEGER NOT NULL, catalog_id INTEGER,
    source TEXT NOT NULL DEFAULT 'board', text TEXT NOT NULL, themes TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_scope TEXT NOT NULL, name TEXT NOT NULL, persona TEXT NOT NULL DEFAULT '',
    tier TEXT NOT NULL DEFAULT 'producer', employment TEXT NOT NULL DEFAULT 'free', seat_id INTEGER,
    balance INTEGER NOT NULL DEFAULT 0, allowance INTEGER NOT NULL DEFAULT 0, mode TEXT NOT NULL DEFAULT 'idle',
    wake_after REAL NOT NULL DEFAULT 0, interest TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
    thread_id INTEGER, focus TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS personnel_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_scope TEXT NOT NULL, company_id INTEGER, seat_id INTEGER, agent_id INTEGER,
    event TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', approved_by TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS token_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_scope TEXT NOT NULL, agent_id INTEGER NOT NULL, timestamp REAL NOT NULL,
    kind TEXT NOT NULL, amount INTEGER NOT NULL, activity TEXT NOT NULL DEFAULT '', vendor TEXT NOT NULL DEFAULT '',
    cycle_id INTEGER, job_id INTEGER
);
CREATE TABLE IF NOT EXISTS evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_scope TEXT NOT NULL, agent_id INTEGER NOT NULL, grader_agent_id INTEGER,
    kind TEXT NOT NULL DEFAULT 'task', prompt_key TEXT NOT NULL DEFAULT '', score REAL NOT NULL DEFAULT 0,
    rubric TEXT NOT NULL DEFAULT '{}', passed INTEGER NOT NULL DEFAULT 0, cycle_id INTEGER, timestamp REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS dream_bank (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_scope TEXT NOT NULL, agent_id INTEGER NOT NULL, timestamp REAL NOT NULL,
    source TEXT NOT NULL DEFAULT '', query TEXT NOT NULL, findings TEXT NOT NULL, tokens INTEGER NOT NULL DEFAULT 0,
    tags TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS inquiries (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_scope TEXT NOT NULL, agent_id INTEGER NOT NULL, source TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '', cost_tokens INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'done', cycle_id INTEGER,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_scope TEXT NOT NULL, kind TEXT NOT NULL, company_id INTEGER, job_id INTEGER,
    started_at REAL NOT NULL, finished_at REAL, tokens_planned INTEGER NOT NULL DEFAULT 0, tokens_used INTEGER NOT NULL DEFAULT 0,
    calls INTEGER NOT NULL DEFAULT 0, log TEXT NOT NULL DEFAULT '[]', next_run_after REAL, status TEXT NOT NULL DEFAULT 'running'
);
"""

TABLES = (
    "companies", "departments", "teams", "seats", "rocks", "issues", "todos", "meetings", "timeline", "catalog",
    "work_items", "escalations", "scorecard", "feedback", "agents", "token_ledger", "evaluations", "dream_bank",
    "inquiries", "cycles", "personnel_log",
)
WORK_STATUSES = ("backlog", "rated", "assigned", "running", "review", "board", "done", "rejected")
CATALOG_STAGES = ("backlog", "development", "draft", "edit", "preliminary_review", "board_feedback", "final", "published", "marketed")
TIERS = ("producer", "auxiliary", "philosopher")


def _scope(project_scope: str) -> str:
    return (project_scope or "").strip() or "default"


def load_json(text: Any, default: Any) -> Any:
    try:
        value = json.loads(text) if isinstance(text, str) and text else default
    except ValueError:
        return default
    return value if value is not None else default


def insert(table: str, project_scope: str, **columns: Any) -> int:
    if table not in TABLES:
        raise ValueError(f"unknown society table {table!r}")
    columns["project_scope"] = _scope(project_scope)
    names = list(columns)
    values = [json.dumps(v) if isinstance(v, (dict, list)) else v for v in columns.values()]
    with vault._open_database() as connection:
        cursor = connection.execute(
            f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})", tuple(values)
        )
        return int(cursor.lastrowid)


def update(table: str, row_id: int, **columns: Any) -> None:
    if table not in TABLES or not columns:
        return
    names = list(columns)
    values = [json.dumps(v) if isinstance(v, (dict, list)) else v for v in columns.values()]
    with vault._open_database() as connection:
        connection.execute(f"UPDATE {table} SET {', '.join(f'{n} = ?' for n in names)} WHERE id = ?", (*values, int(row_id)))


def row(table: str, row_id: int) -> Optional[Dict[str, Any]]:
    if table not in TABLES:
        return None
    with vault._open_database() as connection:
        found = connection.execute(f"SELECT * FROM {table} WHERE id = ?", (int(row_id),)).fetchone()
    return dict(found) if found else None


def rows(
    table: str, project_scope: str, where: str = "", params: Sequence[Any] = (), order: str = "id ASC", limit: int = 500
) -> List[Dict[str, Any]]:
    if table not in TABLES:
        return []
    clause = f"project_scope = ?{' AND ' + where if where else ''}"
    with vault._open_database() as connection:
        found = connection.execute(
            f"SELECT * FROM {table} WHERE {clause} ORDER BY {order} LIMIT ?", (_scope(project_scope), *params, int(limit))
        ).fetchall()
    return [dict(r) for r in found]


def count(table: str, project_scope: str, where: str = "", params: Sequence[Any] = ()) -> int:
    clause = f"project_scope = ?{' AND ' + where if where else ''}"
    with vault._open_database() as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {clause}", (_scope(project_scope), *params)).fetchone()[0])


# ---- companies and org ----------------------------------------------------------------------

def companies_for(project_scope: str) -> List[Dict[str, Any]]:
    return rows("companies", project_scope, "status != 'archived'")


def company_by_key(project_scope: str, key: str) -> Optional[Dict[str, Any]]:
    found = rows("companies", project_scope, "key = ?", (key,), limit=1)
    return found[0] if found else None


def seats_for(project_scope: str, company_id: Optional[int], status: Optional[str] = None) -> List[Dict[str, Any]]:
    where = "company_id = ?" if company_id is not None else "company_id IS NULL"
    params: List[Any] = [int(company_id)] if company_id is not None else []
    if status:
        where += " AND status = ?"
        params.append(status)
    return rows("seats", project_scope, where, params, order="importance DESC, id ASC")


def seat_by_key(project_scope: str, company_id: int, key: str) -> Optional[Dict[str, Any]]:
    found = rows("seats", project_scope, "company_id = ? AND key = ?", (int(company_id), key), limit=1)
    return found[0] if found else None


def open_seats(project_scope: str, company_id: Optional[int] = None) -> List[Dict[str, Any]]:
    where = "status = 'open' AND agent_id IS NULL"
    params: List[Any] = []
    if company_id is not None:
        where += " AND company_id = ?"
        params.append(int(company_id))
    return rows("seats", project_scope, where, params, order="importance DESC, id ASC")


def departments_for(project_scope: str, company_id: int) -> List[Dict[str, Any]]:
    return rows("departments", project_scope, "company_id = ?", (int(company_id),))


def teams_for(project_scope: str, company_id: int) -> List[Dict[str, Any]]:
    return rows("teams", project_scope, "company_id = ?", (int(company_id),))


def catalog_for(project_scope: str, company_id: int) -> List[Dict[str, Any]]:
    return rows("catalog", project_scope, "company_id = ?", (int(company_id),), order="release_wave ASC, id ASC")


def work_items_for(project_scope: str, company_id: int, statuses: Iterable[str] = (), limit: int = 500) -> List[Dict[str, Any]]:
    statuses = tuple(statuses)
    where = "company_id = ?"
    params: List[Any] = [int(company_id)]
    if statuses:
        where += " AND status IN (" + ",".join("?" for _ in statuses) + ")"
        params.extend(statuses)
    return rows("work_items", project_scope, where, params, order="importance DESC, id ASC", limit=limit)


def add_work_item(project_scope: str, company_id: int, title: str, brief: str, importance: int = 3, **extra: Any) -> int:
    now = time.time()
    return insert(
        "work_items", project_scope, company_id=int(company_id), title=title.strip()[:200], brief=brief.strip()[:4000],
        importance=max(1, min(5, int(importance))), created_at=now, updated_at=now, **extra,
    )


def set_work_status(item_id: int, status: str, **extra: Any) -> None:
    if status not in WORK_STATUSES:
        raise ValueError(f"unknown work status {status!r}")
    update("work_items", item_id, status=status, updated_at=time.time(), **extra)


# ---- agents and the ledger -------------------------------------------------------------------

def agents_for(project_scope: str, tier: Optional[str] = None, employment: Optional[str] = None, limit: int = 1000) -> List[Dict[str, Any]]:
    where, params = [], []
    if tier:
        where.append("tier = ?")
        params.append(tier)
    if employment:
        where.append("employment = ?")
        params.append(employment)
    return rows("agents", project_scope, " AND ".join(where), params, order="id ASC", limit=limit)


def add_agent(project_scope: str, name: str, persona: str, tier: str = "producer", allowance: int = 0, **extra: Any) -> int:
    if tier not in TIERS:
        raise ValueError(f"unknown tier {tier!r}")
    return insert("agents", project_scope, name=name[:120], persona=persona[:4000], tier=tier, allowance=int(allowance), created_at=time.time(), **extra)


def seat_agent(project_scope: str, seat_id: int, agent_id: int) -> None:
    update("seats", seat_id, agent_id=int(agent_id), status="filled", miss_weeks=0, miss_week="", idle_cycles=0)
    update("agents", agent_id, seat_id=int(seat_id), employment="seated", mode="exploit")


def log_personnel(project_scope: str, event: str, company_id: Optional[int] = None, seat_id: Optional[int] = None, agent_id: Optional[int] = None, reason: str = "", approved_by: str = "") -> int:
    return insert("personnel_log", project_scope, company_id=company_id, seat_id=seat_id, agent_id=agent_id, event=event, reason=reason[:500], approved_by=approved_by, created_at=time.time())


def graduate_pool(project_scope: str) -> List[Dict[str, Any]]:
    """Free Philosophers ranked by their latest evaluation score (cooldown honoured), best first."""
    now = time.time()
    pool = [a for a in agents_for(project_scope, tier="philosopher", employment="free") if float(a.get("wake_after") or 0) <= now]

    def rank(agent: Dict[str, Any]) -> float:
        latest = rows("evaluations", project_scope, "agent_id = ?", (int(agent["id"]),), order="id DESC", limit=1)
        return float(latest[0]["score"]) if latest else 0.5

    return sorted(pool, key=lambda a: (-rank(a), int(a["id"])))


def fill_open_seats(project_scope: str, company_id: Optional[int] = None, active_departments_only: bool = True) -> List[Dict[str, Any]]:
    """Graduates take open seats by seat importance; returns the (seat, agent) pairs hired."""
    hired: List[Dict[str, Any]] = []
    pool = graduate_pool(project_scope)
    inactive: set = set()
    if active_departments_only:
        inactive = {int(d["id"]) for d in rows("departments", project_scope, "active = 0")}
    for seat in open_seats(project_scope, company_id):
        if seat.get("department_id") and int(seat["department_id"]) in inactive:
            continue
        if not pool:
            break
        agent = pool.pop(0)
        seat_agent(project_scope, int(seat["id"]), int(agent["id"]))
        log_personnel(project_scope, "hire", company_id=seat.get("company_id"), seat_id=int(seat["id"]), agent_id=int(agent["id"]), reason="graduate filled an open seat", approved_by="ceo")
        hired.append({"seat": seat, "agent": agent})
    return hired


def unseat_agent(project_scope: str, seat_id: int, note: str = "", fired: bool = False) -> None:
    seat = row("seats", seat_id)
    if not seat:
        return
    update("seats", seat_id, agent_id=None, status="open")
    if seat.get("agent_id"):
        update("agents", int(seat["agent_id"]), seat_id=None, employment="fired" if fired else "free", mode="idle", note=note[:500])


def ledger_add(project_scope: str, agent_id: int, kind: str, amount: int, activity: str = "", vendor: str = "", cycle_id: Optional[int] = None, job_id: Optional[int] = None) -> int:
    """Append a ledger entry and move the agent's balance (never below zero); returns the amount actually moved."""
    amount = int(amount)
    agent = row("agents", agent_id)
    if not agent:
        return 0
    if kind in ("spend", "fine"):
        amount = min(amount, int(agent["balance"]))
        delta = -amount
    else:
        delta = amount
    if amount == 0:
        return 0
    insert("token_ledger", project_scope, agent_id=int(agent_id), timestamp=time.time(), kind=kind, amount=amount, activity=activity[:300], vendor=vendor, cycle_id=cycle_id, job_id=job_id)
    update("agents", agent_id, balance=int(agent["balance"]) + delta)
    return amount


def ledger_balance(project_scope: str, agent_id: int) -> int:
    agent = row("agents", agent_id)
    return int(agent["balance"]) if agent else 0


# ---- cycles ----------------------------------------------------------------------------------

def start_cycle(project_scope: str, kind: str, company_id: Optional[int], job_id: Optional[int], tokens_planned: int) -> int:
    return insert("cycles", project_scope, kind=kind, company_id=company_id, job_id=job_id, started_at=time.time(), tokens_planned=int(tokens_planned))


def finish_cycle(cycle_id: int, status: str, tokens_used: int, calls: int, log: Sequence[Mapping[str, Any]], next_run_after: Optional[float] = None) -> None:
    update("cycles", cycle_id, status=status, finished_at=time.time(), tokens_used=int(tokens_used), calls=int(calls), log=list(log), next_run_after=next_run_after)


def cycles_for(project_scope: str, company_id: Optional[int] = None, limit: int = 20) -> List[Dict[str, Any]]:
    where = "company_id = ?" if company_id is not None else ""
    params = [int(company_id)] if company_id is not None else []
    return rows("cycles", project_scope, where, params, order="id DESC", limit=limit)
