"""Local SQLite source of truth for Chat Johnson.

Holds the rolling 200-message history, versioned locked artifacts, secret
redaction, and deterministic structural texturization.  This module has no
Streamlit dependency so it can be exercised directly by tests.

API keys never enter this database: every persisted string passes through
:func:`redact_secrets` first.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


DEFAULT_DB_PATH = "chat_johnson_vault.db"
MESSAGE_WINDOW = 200
# "company" is the board inbox, "society" holds every seat's working thread, "academy" the training society.
WORKSPACES = ("task_finder", "repository", "chat_bot", "normal_chat", "company", "society", "academy")
DEFAULT_WORKSPACE = "normal_chat"


def _workspace(value: Optional[str]) -> str:
    return value if value in WORKSPACES else DEFAULT_WORKSPACE


def database_path() -> Path:
    """Resolve the vault path at call time (CHAT_JOHNSON_DB_PATH overrides)."""
    return Path(os.environ.get("CHAT_JOHNSON_DB_PATH", DEFAULT_DB_PATH))


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS message_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content TEXT NOT NULL,
    timestamp REAL NOT NULL,
    token_count INTEGER NOT NULL CHECK (token_count >= 0),
    project_scope TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'normal'
);

CREATE INDEX IF NOT EXISTS idx_message_scope_time
    ON message_history(project_scope, timestamp DESC, id DESC);

CREATE TABLE IF NOT EXISTS artifact_store (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    file_path TEXT NOT NULL DEFAULT '',
    code_body TEXT NOT NULL,
    structural_summary TEXT NOT NULL,
    project_scope TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at REAL NOT NULL,
    source_message_id INTEGER,
    FOREIGN KEY (source_message_id) REFERENCES message_history(id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_artifact_version
    ON artifact_store(project_scope, name, file_path, version);

CREATE INDEX IF NOT EXISTS idx_artifact_scope_time
    ON artifact_store(project_scope, created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS message_archive (
    id INTEGER PRIMARY KEY,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    timestamp REAL NOT NULL,
    token_count INTEGER NOT NULL,
    project_scope TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'normal',
    archived_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_archive_scope_time
    ON message_archive(project_scope, timestamp ASC, id ASC);

CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_scope TEXT NOT NULL,
    covers_from_id INTEGER NOT NULL,
    covers_to_id INTEGER NOT NULL,
    message_count INTEGER NOT NULL,
    content TEXT NOT NULL,
    method TEXT NOT NULL DEFAULT 'extractive',
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_summaries_scope_time
    ON summaries(project_scope, created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS threads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_scope TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'migrated', 'archived')),
    parent_thread_id INTEGER,
    digest_artifact_id INTEGER,
    generation INTEGER NOT NULL DEFAULT 1,
    workspace TEXT NOT NULL DEFAULT 'normal_chat',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_threads_scope_updated
    ON threads(project_scope, workspace, updated_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS route_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    project_scope TEXT NOT NULL,
    workspace TEXT NOT NULL DEFAULT '',
    task_type TEXT NOT NULL DEFAULT '',
    route TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'normal',
    ms INTEGER NOT NULL DEFAULT 0,
    finish TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    runner_up TEXT NOT NULL DEFAULT '',
    chaos_gain REAL NOT NULL DEFAULT 0,
    chaos_profile TEXT NOT NULL DEFAULT '',
    jitter REAL NOT NULL DEFAULT 0,
    outcome TEXT NOT NULL DEFAULT '',
    message_id INTEGER,
    fragility REAL,
    explored INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS quality_priors (
    project_scope TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    task_type TEXT NOT NULL,
    alpha REAL NOT NULL DEFAULT 2,
    beta REAL NOT NULL DEFAULT 2,
    observations INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (project_scope, endpoint, task_type)
);

CREATE INDEX IF NOT EXISTS idx_route_log_scope_time
    ON route_log(project_scope, timestamp DESC, id DESC);

-- Older databases carried a destructive trigger; the application now
-- texturizes (summarize + archive) before evicting from the active window.
DROP TRIGGER IF EXISTS message_history_rolling_cap;
CREATE TABLE IF NOT EXISTS mission_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    node TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mission_nodes_thread ON mission_nodes(thread_id, position ASC);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_scope TEXT NOT NULL,
    thread_id INTEGER,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'waiting_input', 'done', 'failed', 'cancelled')),
    payload TEXT NOT NULL DEFAULT '{}',
    progress TEXT NOT NULL DEFAULT '{}',
    result TEXT NOT NULL DEFAULT '{}',
    question TEXT NOT NULL DEFAULT '',
    answer TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    worker TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    claimed_at REAL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_scope_status ON jobs(project_scope, status, id DESC);
CREATE TABLE IF NOT EXISTS job_secrets (
    job_id INTEGER PRIMARY KEY,
    blob BLOB NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS quota_usage (
    key TEXT NOT NULL,
    day TEXT NOT NULL,
    tokens INTEGER NOT NULL DEFAULT 0,
    requests INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (key, day)
);
CREATE TABLE IF NOT EXISTS settings (
    project_scope TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL,
    PRIMARY KEY (project_scope, key)
);
CREATE TABLE IF NOT EXISTS counters (
    key TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);
"""


def _open_database() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    # Readers (the UI polling a running job) never block the worker's writes.
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


class ScopeMismatch(ValueError):
    """A row exists but belongs to another visitor's scope; the caller must never see it."""


def _check_scope(connection: sqlite3.Connection, table: str, row_id: int, project_scope: Optional[str]) -> None:
    """Defence in depth for id-addressed rows: when a scope is given, the row must carry it."""
    if project_scope is None:
        return
    row = connection.execute(f"SELECT project_scope FROM {table} WHERE id = ?", (int(row_id),)).fetchone()
    if row is not None and str(row["project_scope"]) != str(project_scope).strip():
        raise ScopeMismatch(f"{table} {int(row_id)} belongs to another scope")


def initialize_database() -> None:
    """Create the local schema idempotently and migrate older vaults to threads."""
    with _open_database() as connection:
        connection.executescript(SCHEMA_SQL)
        for table in ("message_history", "message_archive", "summaries"):
            _ensure_column(connection, table, "thread_id", "INTEGER")
        _ensure_column(connection, "threads", "workspace", "TEXT NOT NULL DEFAULT 'normal_chat'")
        _ensure_column(connection, "message_history", "task_type", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "message_history", "finish", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "threads", "mission", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "message_archive", "task_type", "TEXT NOT NULL DEFAULT ''")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_message_thread ON message_history(thread_id, timestamp ASC, id ASC)"
        )
        _ensure_column(connection, "jobs", "run_after", "REAL NOT NULL DEFAULT 0")
        _ensure_column(connection, "jobs", "heartbeat_at", "REAL")
        _ensure_column(connection, "mission_nodes", "project_scope", "TEXT NOT NULL DEFAULT ''")
        from .society.store import SOCIETY_SCHEMA_SQL  # local import: the society package imports this module

        connection.executescript(SOCIETY_SCHEMA_SQL)
        for column, ddl in (("miss_weeks", "INTEGER NOT NULL DEFAULT 0"), ("miss_week", "TEXT NOT NULL DEFAULT ''"), ("idle_cycles", "INTEGER NOT NULL DEFAULT 0")):
            _ensure_column(connection, "seats", column, ddl)
        _ensure_column(connection, "agents", "thread_id", "INTEGER")
        _ensure_column(connection, "agents", "focus", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "companies", "wave_size", "INTEGER NOT NULL DEFAULT 6")
        _ensure_column(connection, "catalog", "brief", "TEXT NOT NULL DEFAULT ''")
        for column, ddl in (
            ("runner_up", "TEXT NOT NULL DEFAULT ''"), ("chaos_gain", "REAL NOT NULL DEFAULT 0"), ("chaos_profile", "TEXT NOT NULL DEFAULT ''"),
            ("jitter", "REAL NOT NULL DEFAULT 0"), ("outcome", "TEXT NOT NULL DEFAULT ''"), ("message_id", "INTEGER"), ("fragility", "REAL"),
            ("explored", "INTEGER NOT NULL DEFAULT 0"),
        ):
            _ensure_column(connection, "route_log", column, ddl)
        _ensure_recall_index(connection)
        # Backfill: every scope that has thread-less rows gets one "Main thread".
        scopes = {
            row[0]
            for table in ("message_history", "message_archive", "summaries")
            for row in connection.execute(f"SELECT DISTINCT project_scope FROM {table} WHERE thread_id IS NULL").fetchall()
        }
        for scope in scopes:
            thread_id = _active_thread_id(connection, scope, DEFAULT_WORKSPACE, create=True)
            for table in ("message_history", "message_archive", "summaries"):
                connection.execute(
                    f"UPDATE {table} SET thread_id = ? WHERE project_scope = ? AND thread_id IS NULL",
                    (thread_id, scope),
                )
        if _FTS["available"] and not connection.execute("SELECT 1 FROM recall_index LIMIT 1").fetchone():
            _rebuild_recall_index(connection)  # an older vault: index what it already holds, once
        connection.commit()


# =============================================================================
# Long-distance memory index (SQLite FTS5, BM25 ranked; zero provider quota)
# =============================================================================

_FTS: Dict[str, bool] = {"available": True}
RECALL_HALF_LIFE_DAYS = 30.0     # a recalled line loses half its weight every month
RECALL_SUPERSEDED_WEIGHT = 0.5   # lines from a migrated chat or an older digest rank behind fresh ones
_RECALL_FTS_SQL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS recall_index USING fts5("
    "line, project_scope UNINDEXED, source UNINDEXED, kind UNINDEXED, thread_id UNINDEXED, ref_id UNINDEXED, "
    "created_at UNINDEXED, superseded UNINDEXED, tokenize = \"unicode61 tokenchars '_'\")"
)


def _ensure_recall_index(connection: sqlite3.Connection) -> None:
    try:
        connection.execute(_RECALL_FTS_SQL)
        _FTS["available"] = True
    except sqlite3.OperationalError:
        _FTS["available"] = False  # SQLite built without FTS5: recall falls back to keyword matching


def recall_index_available() -> bool:
    return bool(_FTS["available"])


def _recall_lines(text: str) -> List[str]:
    out: List[str] = []
    for line in str(text or "").splitlines():
        stripped = line.strip().lstrip("-*#> ").strip()
        if len(stripped) < 12 or (stripped.startswith("[") and stripped.endswith("]")):
            continue
        out.append(stripped[:RECALL_LINE_MAX])
    return out


def _index_text(connection: sqlite3.Connection, scope: str, source: str, kind: str, thread_id: Optional[int], ref_id: int, text: str, created_at: float, superseded: int = 0) -> int:
    """(Re)index one source's lines; idempotent per (scope, kind, ref_id). Returns the lines indexed."""
    if not _FTS["available"]:
        return 0
    connection.execute("DELETE FROM recall_index WHERE project_scope = ? AND kind = ? AND ref_id = ?", (scope, kind, int(ref_id)))
    lines = _recall_lines(text)
    connection.executemany(
        "INSERT INTO recall_index (line, project_scope, source, kind, thread_id, ref_id, created_at, superseded) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(line, scope, source[:160], kind, int(thread_id) if thread_id is not None else None, int(ref_id), float(created_at), int(superseded)) for line in lines],
    )
    return len(lines)


def _thread_label(connection: sqlite3.Connection, thread_id: Optional[int]) -> str:
    row = connection.execute("SELECT title, workspace FROM threads WHERE id = ?", (int(thread_id or 0),)).fetchone()
    return f'chat "{row["title"]}" ({row["workspace"]})' if row else "earlier chat"


def _rebuild_recall_index(connection: sqlite3.Connection, project_scope: Optional[str] = None) -> int:
    if not _FTS["available"]:
        return 0
    where, params = ("WHERE project_scope = ?", (project_scope,)) if project_scope else ("", ())
    connection.execute(f"DELETE FROM recall_index {where}", params)
    migrated = {int(r["id"]) for r in connection.execute("SELECT id FROM threads WHERE status = 'migrated'").fetchall()}
    latest_digest: Dict[Tuple[str, str], int] = {}
    count = 0
    for row in connection.execute(f"SELECT id, project_scope, thread_id, content, created_at FROM summaries {where}", params).fetchall():
        count += _index_text(connection, row["project_scope"], _thread_label(connection, row["thread_id"]), "summary", row["thread_id"], int(row["id"]), row["content"], float(row["created_at"]), int(int(row["thread_id"] or 0) in migrated))
    for row in connection.execute(f"SELECT id, project_scope, title, mission FROM threads {where}", params).fetchall():
        if row["mission"]:
            count += _index_text(connection, row["project_scope"], f'mission of chat "{row["title"]}"', "mission", int(row["id"]), int(row["id"]), row["mission"], time.time())
    artifacts = connection.execute(f"SELECT id, project_scope, name, code_body, structural_summary, created_at FROM artifact_store {where} ORDER BY id ASC", params).fetchall()
    for row in artifacts:
        if str(row["name"]).endswith("-digest.md"):
            latest_digest[(row["project_scope"], row["name"])] = int(row["id"])
    for row in artifacts:
        name = str(row["name"])
        if name.endswith("-digest.md"):
            superseded = int(latest_digest.get((row["project_scope"], name)) != int(row["id"]))
            count += _index_text(connection, row["project_scope"], f"digest {name}", "digest", None, int(row["id"]), str(row["code_body"])[:8_000], float(row["created_at"]), superseded)
        elif row["structural_summary"]:
            count += _index_text(connection, row["project_scope"], f"artifact {name}", "artifact", None, int(row["id"]), row["structural_summary"], float(row["created_at"]))
    return count


def rebuild_recall_index(project_scope: Optional[str] = None) -> int:
    """Re-index every summary, mission, digest, and artifact summary (one scope, or all); returns lines indexed."""
    with _open_database() as connection:
        _ensure_recall_index(connection)
        count = _rebuild_recall_index(connection, (project_scope.strip() or "default") if project_scope else None)
        connection.commit()
    return count


def _fts_query(words: Sequence[str]) -> str:
    return " OR ".join('"' + word.replace('"', "") + '"*' for word in words if word)


def _recall_fts(connection: sqlite3.Connection, scope: str, words: Sequence[str], exclude_thread: Optional[int], needed: int, limit: int = 80) -> List[Tuple[float, str]]:
    """(score, formatted line) from the FTS index: BM25 relevance, decayed by age, halved when superseded."""
    try:
        rows = connection.execute(
            "SELECT line, source, kind, thread_id, ref_id, created_at, superseded, bm25(recall_index) AS rank "
            "FROM recall_index WHERE project_scope = ? AND recall_index MATCH ? ORDER BY rank LIMIT ?",
            (scope, _fts_query(words), int(limit)),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    live_summaries: set = set()
    if exclude_thread is not None:
        live_summaries = {int(r["id"]) for r in connection.execute(
            "SELECT id FROM summaries WHERE project_scope = ? AND thread_id = ? ORDER BY created_at DESC, id DESC LIMIT 5", (scope, exclude_thread)
        ).fetchall()}
    now = time.time()
    scored: List[Tuple[float, str]] = []
    seen: set = set()
    for row in rows:
        thread_id = int(row["thread_id"]) if row["thread_id"] is not None else None
        if exclude_thread is not None and thread_id == exclude_thread:
            if row["kind"] == "mission" or (row["kind"] == "summary" and int(row["ref_id"]) in live_summaries):
                continue  # the asking chat's own mission and live summaries are already in its context
        lowered = str(row["line"]).lower()
        if sum(1 for word in words if re.search(rf"(?<![a-z0-9]){re.escape(word)}", lowered)) < needed:
            continue  # word starts only ("pool" matches "pooling"); "t" inside "attributes" is not a match
        key = _normalized(str(row["line"]))
        if key in seen:
            continue
        seen.add(key)
        age_days = max(0.0, now - float(row["created_at"] or now)) / 86_400.0
        weight = (0.5 ** (age_days / RECALL_HALF_LIFE_DAYS)) * (RECALL_SUPERSEDED_WEIGHT if int(row["superseded"] or 0) else 1.0)
        score = max(0.01, -float(row["rank"])) * weight
        label = row["source"] if row["kind"] != "summary" or thread_id != exclude_thread else "this chat, older summary"
        scored.append((score, f"- ({label}) {row['line']}"))
    scored.sort(key=lambda item: -item[0])
    return scored


# =============================================================================
# Threads
# =============================================================================

def _active_thread_id(
    connection: sqlite3.Connection, scope: str, workspace: Optional[str] = None, create: bool = True
) -> Optional[int]:
    ws = _workspace(workspace)
    row = connection.execute(
        """
        SELECT id FROM threads WHERE project_scope = ? AND workspace = ? AND status IN ('active', 'migrated')
        ORDER BY updated_at DESC, id DESC LIMIT 1
        """,
        (scope, ws),
    ).fetchone()
    if row is not None:
        return int(row["id"])
    if not create:
        return None
    now = time.time()
    cursor = connection.execute(
        "INSERT INTO threads (project_scope, title, status, generation, workspace, created_at, updated_at) "
        "VALUES (?, ?, 'active', 1, ?, ?, ?)",
        (scope, "Main thread", ws, now, now),
    )
    return int(cursor.lastrowid)


def active_thread(project_scope: str, workspace: Optional[str] = None) -> sqlite3.Row:
    """The scope's current thread for a workspace, created on first use."""
    scope = project_scope.strip() or "default"
    with _open_database() as connection:
        thread_id = _active_thread_id(connection, scope, workspace, create=True)
        connection.commit()
        return connection.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()


def thread_by_id(thread_id: int, project_scope: Optional[str] = None) -> Optional[sqlite3.Row]:
    with _open_database() as connection:
        _check_scope(connection, "threads", thread_id, project_scope)
        return connection.execute("SELECT * FROM threads WHERE id = ?", (int(thread_id),)).fetchone()


def list_threads(
    project_scope: str, include_closed: bool = True, limit: int = 50, workspace: Optional[str] = None
) -> List[sqlite3.Row]:
    """Threads in a scope; ``workspace=None`` lists every workspace."""
    scope = project_scope.strip() or "default"
    with _open_database() as connection:
        query = "SELECT * FROM threads WHERE project_scope = ?"
        params: List[Any] = [scope]
        if workspace is not None:
            query += " AND workspace = ?"
            params.append(_workspace(workspace))
        if not include_closed:
            query += " AND status = 'active'"
        query += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        return list(connection.execute(query, params).fetchall())


def create_thread(
    project_scope: str, title: str = "", parent_thread_id: Optional[int] = None, workspace: Optional[str] = None
) -> int:
    """Start a fresh active thread in the scope/workspace; earlier threads keep their history."""
    scope = project_scope.strip() or "default"
    now = time.time()
    with _open_database() as connection:
        generation = 1
        ws = _workspace(workspace)
        if parent_thread_id is not None:
            parent = connection.execute(
                "SELECT generation, workspace FROM threads WHERE id = ?", (int(parent_thread_id),)
            ).fetchone()
            if parent:
                generation = int(parent["generation"]) + 1
                if workspace is None:
                    ws = _workspace(parent["workspace"])
        count = int(connection.execute(
            "SELECT COUNT(*) FROM threads WHERE project_scope = ? AND workspace = ?", (scope, ws)
        ).fetchone()[0])
        safe_title = redact_secrets(title.strip() or f"Thread {count + 1}")[:120]
        cursor = connection.execute(
            """
            INSERT INTO threads (project_scope, title, status, parent_thread_id, generation, workspace, created_at, updated_at)
            VALUES (?, ?, 'active', ?, ?, ?, ?, ?)
            """,
            (scope, safe_title, parent_thread_id, generation, ws, now, now),
        )
        connection.commit()
        return int(cursor.lastrowid)


def switch_thread(thread_id: int, project_scope: Optional[str] = None) -> None:
    """Make ``thread_id`` the workspace's current thread without touching its status.

    A migrated thread stays labelled migrated (its digest chain is not forked);
    it simply becomes the one the workspace reads and appends to.
    """
    with _open_database() as connection:
        _check_scope(connection, "threads", thread_id, project_scope)
        connection.execute("UPDATE threads SET updated_at = ? WHERE id = ?", (time.time(), int(thread_id)))


def rename_thread(thread_id: int, title: str, project_scope: Optional[str] = None) -> None:
    with _open_database() as connection:
        _check_scope(connection, "threads", thread_id, project_scope)
        connection.execute("UPDATE threads SET title = ? WHERE id = ?", (redact_secrets(title.strip() or "Untitled")[:120], int(thread_id)))


def set_thread_mission(thread_id: int, goal: str, project_scope: Optional[str] = None) -> None:
    """Pin the Task Finder mission to the thread so it outlives window eviction and migration."""
    with _open_database() as connection:
        _check_scope(connection, "threads", thread_id, project_scope)
        clean = redact_secrets((goal or "").strip())[:2000]
        connection.execute("UPDATE threads SET mission = ? WHERE id = ?", (clean, int(thread_id)))
        row = connection.execute("SELECT project_scope, title FROM threads WHERE id = ?", (int(thread_id),)).fetchone()
        if row is not None:
            _index_text(connection, row["project_scope"], f'mission of chat "{row["title"]}"', "mission", int(thread_id), int(thread_id), clean, time.time())


def set_thread_status(thread_id: int, status: str, project_scope: Optional[str] = None) -> None:
    if status not in {"active", "migrated", "archived"}:
        raise ValueError("invalid thread status")
    with _open_database() as connection:
        _check_scope(connection, "threads", thread_id, project_scope)
        connection.execute("UPDATE threads SET status = ?, updated_at = ? WHERE id = ?", (status, time.time(), int(thread_id)))


def _touch_thread(connection: sqlite3.Connection, thread_id: int) -> None:
    connection.execute("UPDATE threads SET updated_at = ? WHERE id = ?", (time.time(), int(thread_id)))


def clear_thread(thread_id: int, project_scope: Optional[str] = None) -> int:
    """Move every active message of the thread to the archive (raw text kept). Keys are untouched."""
    now = time.time()
    with _open_database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _check_scope(connection, "threads", thread_id, project_scope)
        rows = connection.execute("SELECT * FROM message_history WHERE thread_id = ?", (int(thread_id),)).fetchall()
        connection.executemany(
            """
            INSERT OR REPLACE INTO message_archive
                (id, role, content, timestamp, token_count, project_scope, provider, mode, archived_at, thread_id, task_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    int(row["id"]), row["role"], row["content"], row["timestamp"], row["token_count"],
                    row["project_scope"], row["provider"], row["mode"], now, int(thread_id),
                    row["task_type"] if "task_type" in row.keys() else "",
                )
                for row in rows
            ],
        )
        connection.execute("DELETE FROM message_history WHERE thread_id = ?", (int(thread_id),))
        connection.execute("UPDATE threads SET mission = '' WHERE id = ?", (int(thread_id),))  # a cleared chat starts over
        _touch_thread(connection, int(thread_id))
        connection.commit()
        return len(rows)


def delete_thread(thread_id: int, project_scope: Optional[str] = None) -> Dict[str, int]:
    """Remove a thread with its live window, archive, and summaries. Locked artifacts are project-level and stay.

    Clear keeps history in the archive; this is the operator's explicit "forget it" and is the only
    path that deletes anything. Successor threads keep their digest artifact and lose only the parent link.
    """
    with _open_database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT id FROM threads WHERE id = ?", (int(thread_id),)).fetchone() is None:
            connection.rollback()
            raise ValueError(f"thread {int(thread_id)} does not exist")
        _check_scope(connection, "threads", thread_id, project_scope)
        counts: Dict[str, int] = {}
        for table in ("message_history", "message_archive", "summaries", "mission_nodes"):
            cursor = connection.execute(f"DELETE FROM {table} WHERE thread_id = ?", (int(thread_id),))
            if table == "summaries" and _FTS["available"]:
                connection.execute("DELETE FROM recall_index WHERE thread_id = ? AND kind IN ('summary', 'mission')", (int(thread_id),))
            counts[table] = int(cursor.rowcount)
        # Finished jobs of the chat go with it; an active one keeps its row so the worker can end it cleanly.
        finished = connection.execute(
            "SELECT id FROM jobs WHERE thread_id = ? AND status IN ('done', 'failed', 'cancelled')", (int(thread_id),)
        ).fetchall()
        for job in finished:
            connection.execute("DELETE FROM job_secrets WHERE job_id = ?", (int(job["id"]),))
            connection.execute("DELETE FROM jobs WHERE id = ?", (int(job["id"]),))
        counts["jobs"] = len(finished)
        connection.execute("UPDATE threads SET parent_thread_id = NULL WHERE parent_thread_id = ?", (int(thread_id),))
        connection.execute("DELETE FROM threads WHERE id = ?", (int(thread_id),))
        connection.commit()
    return counts


def estimate_tokens(text: str) -> int:
    return max(1, len(text.strip()) // 4) if text.strip() else 0


_SECRET_SHAPES = (
    re.compile(r"(?i)(api[_ -]?key|client[_ -]?secret|access[_ -]?token|bearer)\s*[:=]\s*[^\s,;]+"),
    re.compile(
        r"\b(sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,}|AIza[A-Za-z0-9_-]{20,}|gsk_[A-Za-z0-9]{12,}"
        r"|nvapi-[A-Za-z0-9_-]{12,}|csk-[A-Za-z0-9_-]{12,}|hf_[A-Za-z0-9]{12,}|AQ\.[A-Za-z0-9_-]{20,})\b"
    ),
)


def redact_secrets(text: str) -> str:
    """Remove common pasted credential shapes before local persistence."""
    redacted = str(text)
    for pattern in _SECRET_SHAPES:
        redacted = pattern.sub("[REDACTED_SECRET]", redacted)
    return redacted


def append_message(
    project_scope: str,
    role: str,
    content: str,
    provider: str = "",
    mode: str = "normal",
    thread_id: Optional[int] = None,
    workspace: Optional[str] = None,
    task_type: str = "",
    finish: str = "",
) -> int:
    """Insert a message into the workspace's current (or given) thread, then texturize + archive past the window.

    ``finish`` is the vendor's reason the answer ended ("length" when the output budget cut it); it travels
    into the next prompt as a note, so the model learns that a page that stops was cut, not designed short.
    """
    safe_role = role if role in {"user", "assistant", "system"} else "user"
    safe_content = redact_secrets(content)
    scope = project_scope.strip() or "default"
    with _open_database() as connection:
        resolved_thread = (
            int(thread_id) if thread_id is not None else _active_thread_id(connection, scope, workspace, create=True)
        )
        cursor = connection.execute(
            """
            INSERT INTO message_history
                (role, content, timestamp, token_count, project_scope, provider, mode, thread_id, task_type, finish)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                safe_role,
                safe_content,
                time.time(),
                estimate_tokens(safe_content),
                scope,
                provider[:120],
                mode[:40],
                resolved_thread,
                (task_type or "")[:40],
                (finish or "")[:24],
            ),
        )
        message_id = int(cursor.lastrowid)
        _touch_thread(connection, resolved_thread)
        connection.commit()
    enforce_window(scope, resolved_thread)
    return message_id


TEXTURIZE_BATCH = 40  # evict in blocks so each summary covers a coherent stretch


def extractive_summary(rows: Sequence[sqlite3.Row], max_characters: int = 1_800) -> str:
    """Deterministic, zero-quota texturization of an evicted message block.

    Keeps the first sentence of every user turn and any lines that look like
    decisions, file names, or code identifiers, then truncates to a budget.
    An LLM pass can later replace this text via :func:`retexturize_summary`.
    """
    keep: List[str] = []
    for row in rows:
        content = str(row["content"]).strip()
        if not content:
            continue
        first_line = content.splitlines()[0].strip()
        first_sentence = re.split(r"(?<=[.!?])\s+", first_line, maxsplit=1)[0][:220]
        keep.append(f"{row['role']}: {first_sentence}")
        operator = row["role"] == "user" and not content.startswith(AUTOMATIC_TURN_PREFIX)
        for line in content.splitlines():
            stripped = line.strip()
            if stripped == first_sentence or _MARKUP_LINE_RE.match(stripped):
                continue
            # A model's own diagnosis ("the buttons fail because…") is not kept as a fact; the operator's words are.
            decisionish = operator and re.search(r"(?i)\b(?:decid\w*|agree\w*|must|never|always|todo|fix\w*|bug\w*|error\w*)\b", stripped)
            if decisionish or re.search(r"[\w/.-]+\.(py|js|ts|md|json|yml|yaml|toml|sql)\b", stripped):
                keep.append(f"  - {stripped[:160]}")
            if sum(len(item) + 1 for item in keep) > max_characters:
                break
    text = "\n".join(keep)
    return text[:max_characters]


def enforce_window(project_scope: str, thread_id: Optional[int] = None, workspace: Optional[str] = None) -> Optional[int]:
    """Texturize and archive the oldest messages once a thread exceeds the window.

    Returns the new summary id when eviction happened, else ``None``. Raw
    messages are never lost: they move to ``message_archive`` and a summary of
    the block is inserted into ``summaries`` in the same transaction.
    """
    scope = project_scope.strip() or "default"
    with _open_database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        resolved_thread = (
            int(thread_id) if thread_id is not None else _active_thread_id(connection, scope, workspace, create=True)
        )
        total = int(connection.execute(
            "SELECT COUNT(*) FROM message_history WHERE project_scope = ? AND thread_id = ?", (scope, resolved_thread)
        ).fetchone()[0])
        if total <= MESSAGE_WINDOW:
            connection.commit()
            return None
        overflow = total - MESSAGE_WINDOW
        batch = max(overflow, min(TEXTURIZE_BATCH, total))
        rows = connection.execute(
            """
            SELECT * FROM message_history
            WHERE project_scope = ? AND thread_id = ?
            ORDER BY timestamp ASC, id ASC
            LIMIT ?
            """,
            (scope, resolved_thread, batch),
        ).fetchall()
        if not rows:
            connection.commit()
            return None
        summary_text = extractive_summary(rows)
        ids = [int(row["id"]) for row in rows]
        cursor = connection.execute(
            """
            INSERT INTO summaries
                (project_scope, covers_from_id, covers_to_id, message_count, content, method, created_at, thread_id)
            VALUES (?, ?, ?, ?, ?, 'extractive', ?, ?)
            """,
            (scope, min(ids), max(ids), len(ids), summary_text, time.time(), resolved_thread),
        )
        summary_id = int(cursor.lastrowid)
        _index_text(connection, scope, _thread_label(connection, resolved_thread), "summary", resolved_thread, summary_id, summary_text, time.time())
        now = time.time()
        connection.executemany(
            """
            INSERT OR REPLACE INTO message_archive
                (id, role, content, timestamp, token_count, project_scope, provider, mode, archived_at, thread_id, task_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    int(row["id"]), row["role"], row["content"], row["timestamp"], row["token_count"],
                    row["project_scope"], row["provider"], row["mode"], now, resolved_thread,
                    row["task_type"] if "task_type" in row.keys() else "",
                )
                for row in rows
            ],
        )
        placeholders = ",".join("?" for _ in ids)
        connection.execute(f"DELETE FROM message_history WHERE id IN ({placeholders})", ids)
        connection.commit()
        return summary_id


def setting_get(project_scope: str, key: str, default: str = "") -> str:
    """A per-scope setting (a JSON or plain string) shared by the app and the worker."""
    with _open_database() as connection:
        row = connection.execute(
            "SELECT value FROM settings WHERE project_scope = ? AND key = ?", (project_scope.strip() or "default", key)
        ).fetchone()
    return str(row["value"]) if row is not None else default


def setting_set(project_scope: str, key: str, value: str) -> None:
    with _open_database() as connection:
        connection.execute(
            "INSERT INTO settings (project_scope, key, value, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(project_scope, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (project_scope.strip() or "default", key, str(value)[:20_000], time.time()),
        )


def bump_counter(key: str) -> int:
    """Atomically advance a named counter and return its new value (the pink-wave step, per scope and feature)."""
    with _open_database() as connection:
        row = connection.execute(
            "INSERT INTO counters (key, value) VALUES (?, 1) ON CONFLICT(key) DO UPDATE SET value = value + 1 RETURNING value",
            (key,),
        ).fetchone()
    return int(row[0])


def peek_counter(key: str) -> int:
    with _open_database() as connection:
        row = connection.execute("SELECT value FROM counters WHERE key = ?", (key,)).fetchone()
    return int(row[0]) if row is not None else 0


def _thread_filter(
    connection: sqlite3.Connection, scope: str, thread_id: Optional[int], workspace: Optional[str] = None
) -> Tuple[str, Tuple[Any, ...]]:
    """SQL fragment + params selecting one thread (default: the workspace's current one) or all threads (-1)."""
    if thread_id == -1:
        return "project_scope = ?", (scope,)
    resolved = int(thread_id) if thread_id is not None else _active_thread_id(connection, scope, workspace, create=True)
    return "project_scope = ? AND thread_id = ?", (scope, resolved)


def recent_summaries(
    project_scope: str, limit: int = 5, thread_id: Optional[int] = None, workspace: Optional[str] = None
) -> List[sqlite3.Row]:
    scope = project_scope.strip() or "default"
    with _open_database() as connection:
        where, params = _thread_filter(connection, scope, thread_id, workspace)
        return list(
            connection.execute(
                f"""
                SELECT * FROM (
                    SELECT * FROM summaries WHERE {where}
                    ORDER BY created_at DESC, id DESC LIMIT ?
                ) ORDER BY created_at ASC, id ASC
                """,
                (*params, max(1, min(int(limit), 50))),
            ).fetchall()
        )


def archived_messages(
    project_scope: str, limit: int = 500, thread_id: Optional[int] = None, workspace: Optional[str] = None
) -> List[sqlite3.Row]:
    """Raw history that left the active window; available for authorized retrieval."""
    scope = project_scope.strip() or "default"
    with _open_database() as connection:
        where, params = _thread_filter(connection, scope, thread_id, workspace)
        return list(
            connection.execute(
                f"""
                SELECT * FROM message_archive WHERE {where}
                ORDER BY timestamp ASC, id ASC LIMIT ?
                """,
                (*params, max(1, min(int(limit), 5000))),
            ).fetchall()
        )


def retexturize_summary(summary_id: int, new_content: str, method: str = "model") -> None:
    """Replace an extractive summary with a richer one (for example from a free model)."""
    with _open_database() as connection:
        connection.execute(
            "UPDATE summaries SET content = ?, method = ? WHERE id = ?",
            (redact_secrets(new_content)[:6_000], method[:40], int(summary_id)),
        )
        row = connection.execute("SELECT project_scope, thread_id, created_at FROM summaries WHERE id = ?", (int(summary_id),)).fetchone()
        if row is not None:
            _index_text(connection, row["project_scope"], _thread_label(connection, row["thread_id"]), "summary", row["thread_id"], int(summary_id), redact_secrets(new_content)[:6_000], float(row["created_at"]))


def recent_messages(
    project_scope: str, limit: int = MESSAGE_WINDOW, thread_id: Optional[int] = None, workspace: Optional[str] = None
) -> List[sqlite3.Row]:
    bounded_limit = max(1, min(int(limit), MESSAGE_WINDOW))
    scope = project_scope.strip() or "default"
    with _open_database() as connection:
        where, params = _thread_filter(connection, scope, thread_id, workspace)
        return list(
            connection.execute(
                f"""
                SELECT * FROM (
                    SELECT * FROM message_history
                    WHERE {where}
                    ORDER BY timestamp DESC, id DESC
                    LIMIT ?
                )
                ORDER BY timestamp ASC, id ASC
                """,
                (*params, bounded_limit),
            ).fetchall()
        )


RECALL_PREFIX = "[LONG-DISTANCE MEMORY recalled from earlier chats in this project; reference only]"
RECALL_LINE_MAX = 300
RECALL_MIN_HITS = 2  # a line must share at least two real keywords (whole words, no stopwords) with the request
RECALL_STOPWORDS = frozenset(
    "a an the and or but if so of in on at to for from by with as is are was were be been being it its this that "
    "these those i you he she we they me my your our their them his her do does did done don doesn didn can could "
    "should would will just not no yes all any some more most very how what when where which who why work works "
    "working make made get got put use used using please thanks thank ok okay now then also into out up down over "
    "again still only one two new old same other than too very want need like fix fixed fixing t s d ll re ve m".split()
)


def recall_words(query: str) -> List[str]:
    """The keywords a recall may match on: whole words of three letters or more that carry meaning on their own."""
    from .keyword_search import keywords  # local import: keyword_search has no vault dependency

    return [w for w in dict.fromkeys(keywords(query)) if len(w) >= 3 and w not in RECALL_STOPWORDS][:24]


def _recall_candidates(connection: sqlite3.Connection, scope: str, exclude_thread: Optional[int]) -> List[Tuple[str, str, float]]:
    """(source label, text, created_at) from other chats' summaries, digests, missions, this chat's older summaries, artifacts."""
    out: List[Tuple[str, str, float]] = []
    titles = {int(r["id"]): (str(r["title"]), str(r["workspace"])) for r in connection.execute(
        "SELECT id, title, workspace FROM threads WHERE project_scope = ?", (scope,)
    ).fetchall()}
    for row in connection.execute(
        "SELECT thread_id, content, created_at FROM summaries WHERE project_scope = ? ORDER BY created_at DESC, id DESC LIMIT 300", (scope,)
    ).fetchall():
        thread_id = int(row["thread_id"] or 0)
        if exclude_thread is not None and thread_id == exclude_thread:
            continue
        title, workspace = titles.get(thread_id, ("earlier chat", ""))
        out.append((f'chat "{title}"' + (f" ({workspace})" if workspace else ""), str(row["content"]), float(row["created_at"])))
    if exclude_thread is not None:
        older = connection.execute(
            "SELECT content, created_at FROM summaries WHERE project_scope = ? AND thread_id = ? ORDER BY created_at DESC, id DESC LIMIT 50 OFFSET 5",
            (scope, exclude_thread),
        ).fetchall()
        out.extend(("this chat, older summary", str(r["content"]), float(r["created_at"])) for r in older)
    for row in connection.execute(
        "SELECT id, title, mission FROM threads WHERE project_scope = ? AND mission != '' AND id != ?", (scope, int(exclude_thread or -1))
    ).fetchall():
        out.append((f'mission of chat "{row["title"]}"', str(row["mission"]), 0.0))
    for row in connection.execute(
        "SELECT name, code_body, structural_summary, created_at FROM artifact_store WHERE project_scope = ? ORDER BY created_at DESC, id DESC LIMIT 60",
        (scope,),
    ).fetchall():
        name = str(row["name"])
        if name.endswith("-digest.md"):
            out.append((f"digest {name}", str(row["code_body"])[:6_000], float(row["created_at"])))
        elif row["structural_summary"]:
            out.append((f"artifact {name}", str(row["structural_summary"]), float(row["created_at"])))
    return out


def recall_memory(project_scope: str, query: str, thread_id: Optional[int] = None, max_characters: int = 2_000) -> str:
    """Long-distance memory: lines from other chats' summaries, digests, missions, and artifacts of this scope that match ``query``.

    With FTS5 (the normal case) lines are BM25-ranked, decayed by age (half-life 30 days), and halved
    when they come from a migrated chat or an older digest; without FTS5 they are ranked by keyword
    overlap and recency. Bounded by ``max_characters``, never across scopes, empty when the request
    has no keywords or nothing in the project matches.
    """
    words = recall_words(query)
    if not words or max_characters < 80:
        return ""
    scope = project_scope.strip() or "default"
    needed = min(len(words), RECALL_MIN_HITS)
    exclude = int(thread_id) if thread_id is not None else None
    with _open_database() as connection:
        if _FTS["available"]:
            ranked = _recall_fts(connection, scope, words, exclude, needed)
            if not ranked:
                return ""
            lines = [RECALL_PREFIX]
            used = len(RECALL_PREFIX) + 1
            for _, line in ranked:
                if used + len(line) + 1 > max_characters:
                    continue
                lines.append(line)
                used += len(line) + 1
            return "\n".join(lines) if len(lines) > 1 else ""
        candidates = _recall_candidates(connection, scope, exclude)
    scored: List[Tuple[int, float, str]] = []
    seen: set = set()
    for label, text, created in candidates:
        for line in str(text).splitlines():
            stripped = line.strip().lstrip("-*# ").strip()
            if len(stripped) < 12 or stripped.startswith("[") and stripped.endswith("]"):
                continue
            lowered = stripped.lower()
            hits = sum(1 for word in words if re.search(rf"(?<![a-z0-9]){re.escape(word)}", lowered))
            if hits < needed:
                continue
            key = _normalized(stripped)
            if key in seen:
                continue
            seen.add(key)
            scored.append((hits, created, f"- ({label}) {stripped[:RECALL_LINE_MAX]}"))
    if not scored:
        return ""
    scored.sort(key=lambda item: (-item[0], -item[1]))
    lines = [RECALL_PREFIX]
    used = len(RECALL_PREFIX) + 1
    for _, _, line in scored:
        if used + len(line) + 1 > max_characters:
            continue
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines) if len(lines) > 1 else ""


def context_parts(
    project_scope: str,
    max_characters: int = 24_000,
    thread_id: Optional[int] = None,
    workspace: Optional[str] = None,
    recall_query: str = "",
    recall_share: float = 0.0,
) -> Dict[str, Any]:
    """Prompt memory for one thread, split for role-based prompting.

    ``memory`` holds the inherited digest, texturized summaries, long-distance recall, and system
    notes (for the system prompt); ``turns`` holds the live window as user/assistant turns in order.
    Budgets: digest <= 1/4, summaries <= 1/4, recall <= ``recall_share`` (at most 1/4) when a
    ``recall_query`` is given, the live window gets the rest and fills newest-first; a single
    oversized newest turn is truncated rather than dropped.
    """
    scope = project_scope.strip() or "default"
    thread = thread_by_id(thread_id) if thread_id is not None else active_thread(scope, workspace)
    resolved_thread = int(thread["id"]) if thread is not None else None
    rows = recent_messages(scope, MESSAGE_WINDOW, thread_id=resolved_thread)
    summaries = recent_summaries(scope, 5, thread_id=resolved_thread)
    recall = ""
    if recall_query.strip() and recall_share > 0.0:
        recall = recall_memory(scope, recall_query, resolved_thread, int(max_characters * max(0.0, min(0.25, float(recall_share)))))
    digest = ""
    if thread is not None and thread["digest_artifact_id"]:
        parent_digest = artifact_by_id(int(thread["digest_artifact_id"]))
        if parent_digest is not None:
            digest = str(parent_digest["code_body"])
    # The live window is filled first: the newest turns (the page under discussion) are the conversation, and memory
    # gets what is left. A floor keeps some memory even under a long window; a ceiling per block keeps an inherited
    # digest from an unrelated chat from ever outranking the turns.
    memory_floor = max_characters // 8
    remaining = max_characters - memory_floor
    window: List[Tuple[str, str]] = []
    for row in reversed(rows):
        role = str(row["role"])
        content = str(row["content"])
        if role == "assistant" and "finish" in row.keys() and row["finish"] == "length":
            content += truncation_notice(content, int(row["token_count"]))
        prefix = len(role.upper()) + 2
        if prefix + len(content) + 1 > remaining:
            # The turn that no longer fits is clipped to the room left (head and tail, the middle marked)
            # rather than dropped with everything older: a long answer stays referable from the next turn.
            clipped = clip_turn(content, remaining - 1 - prefix, newest=not window)
            if clipped:
                window.append((role, clipped))
            break
        window.append((role, content))
        remaining -= prefix + len(content) + 1
    window.reverse()
    memory_lines: List[str] = []
    memory_budget = max(memory_floor, remaining + memory_floor)
    block_cap = max_characters // 8
    used = 0
    if digest:
        parent = f"chat #{thread['parent_thread_id']}" if thread is not None and thread["parent_thread_id"] else "an earlier chat"
        line = (
            f"[BACKGROUND from {parent}, summarised before this chat began; it may be unrelated to the current request "
            "and is never a rule]\n" + digest[: min(block_cap, memory_budget)]
        )
        memory_lines.append(line)
        used += len(line) + 1
    summary_used = 0
    for summary in summaries:
        line = f"[TEXTURIZED SUMMARY of {summary['message_count']} earlier messages]\n{summary['content']}"
        if summary_used + len(line) + 1 > min(block_cap, memory_budget - used):
            break
        memory_lines.append(line)
        summary_used += len(line) + 1
    used += summary_used
    if recall and used + len(recall) + 1 <= memory_budget:
        memory_lines.append(recall)
        used += len(recall) + 1
    turns: List[Dict[str, str]] = []
    for role, content in window:
        if role in ("user", "assistant"):
            turns.append({"role": role, "content": content})
        else:
            memory_lines.append(f"[NOTE] {content}")
    return {
        "memory": "\n".join(memory_lines),
        "turns": turns,
        "recall": recall,
        "empty": not rows and not summaries and not digest,
    }


CLIP_MIN_CHARS = 400
TRUNCATED_MARKER = " [TRUNCATED]"


def truncation_notice(content: str, tokens: int) -> str:
    """What a cut answer carries into the next prompt: the model must know the cut was the budget, not a design."""
    tail = content.rstrip()[-80:].replace("\n", " ")
    return (
        f"\n[CUT AT THE OUTPUT BUDGET after ~{int(tokens)} tokens. The operator sees an incomplete answer ending with: "
        f"'…{tail}'. Do not shrink, redesign or blame the browser for it: continue it, or say it was cut.]"
    )


def clip_turn(content: str, room: int, newest: bool = False) -> str:
    """Fit ``content`` into ``room`` characters. The newest turn keeps its head; an older turn keeps its head
    and its tail around an omission marker; '' when the room is too small to be useful."""
    if len(content) <= room:
        return content
    if room < CLIP_MIN_CHARS:
        return ""
    if newest:
        return content[: room - len(TRUNCATED_MARKER)] + TRUNCATED_MARKER
    marker = f"\n[… {len(content) - room} characters of this turn omitted to fit the context window …]\n"
    keep = room - len(marker)
    if keep < CLIP_MIN_CHARS // 2:
        return ""
    head = int(keep * 0.7)
    return content[:head] + marker + content[len(content) - (keep - head):]


def alternating_turns(turns: Sequence[Mapping[str, str]], request: str) -> Tuple[str, List[Dict[str, str]]]:
    """Strict user/assistant alternation ending with ``request`` as the final user turn.

    Gemini rejects consecutive same-role turns and a leading model turn, so same-role
    neighbours are joined and a leading assistant reply is handed back for the memory block.
    The request is stored as the newest turn before the prompt is built, so an identical
    trailing user turn is dropped rather than sent twice.
    """
    merged: List[Dict[str, str]] = []
    items = list(turns)
    if items and str(items[-1]["role"]) == "user" and str(items[-1]["content"]).strip() == request.strip():
        items = items[:-1]
    for turn in items + [{"role": "user", "content": request}]:
        item = {"role": str(turn["role"]), "content": str(turn["content"])}
        if merged and merged[-1]["role"] == item["role"]:
            merged[-1] = {"role": item["role"], "content": merged[-1]["content"] + "\n\n" + item["content"]}
        else:
            merged.append(item)
    leading = ""
    if merged and merged[0]["role"] == "assistant":
        leading = merged.pop(0)["content"]
    return leading, merged


def context_block(
    project_scope: str, max_characters: int = 24_000, thread_id: Optional[int] = None, workspace: Optional[str] = None
) -> str:
    """Bounded prompt context as one text block: memory first, then the live window as ROLE: lines."""
    parts = context_parts(project_scope, max_characters=max_characters, thread_id=thread_id, workspace=workspace)
    if parts["empty"]:
        return "(no prior messages in this project scope)"
    lines = [parts["memory"]] if parts["memory"] else []
    lines.extend(f"{turn['role'].upper()}: {turn['content']}" for turn in parts["turns"])
    return "\n".join(lines)


def structural_texturization(code_body: str, language: str = "text") -> str:
    """Produce a deterministic structural summary for artifact retrieval."""
    body = str(code_body)
    lines = body.splitlines()
    non_empty = [line for line in lines if line.strip()]
    summary = [f"language={language or 'text'}", f"lines={len(lines)}", f"non_empty={len(non_empty)}"]
    if language.lower() in {"py", "python", "python3", "file: py"}:
        try:
            tree = ast.parse(body)
            functions = [node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
            classes = [node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
            imports = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.append(node.module)
            summary.extend(
                [
                    f"functions={','.join(functions[:20]) or '(none)'}",
                    f"classes={','.join(classes[:20]) or '(none)'}",
                    f"imports={','.join(imports[:20]) or '(none)'}",
                    "syntax=valid",
                ]
            )
        except SyntaxError as exc:
            summary.append(f"syntax=invalid line {exc.lineno}: {exc.msg}")
    else:
        definitions = re.findall(r"(?m)^\s*(?:function|class|def)\s+([A-Za-z_][\w-]*)", body)
        headings = re.findall(r"(?m)^\s*#{1,6}\s+(.+?)\s*$", body)
        if definitions:
            summary.append(f"definitions={','.join(definitions[:20])}")
        if headings:
            summary.append(f"headings={'; '.join(headings[:10])}")
    return " | ".join(summary)


def save_artifact(
    project_scope: str,
    name: str,
    file_path: str,
    code_body: str,
    language: str,
    source_message_id: Optional[int] = None,
) -> Tuple[int, int]:
    """Commit an immutable, versioned artifact in one SQLite transaction."""
    safe_scope = project_scope.strip() or "default"
    safe_name = (name.strip() or "untitled-artifact")[:240]
    safe_path = file_path.strip()[:500]
    safe_body = redact_secrets(code_body)
    digest = hashlib.sha256(safe_body.encode("utf-8")).hexdigest()
    summary = structural_texturization(safe_body, language)
    with _open_database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT COALESCE(MAX(version), 0) + 1 AS next_version
            FROM artifact_store
            WHERE project_scope = ? AND name = ? AND file_path = ?
            """,
            (safe_scope, safe_name, safe_path),
        ).fetchone()
        version = int(row["next_version"])
        cursor = connection.execute(
            """
            INSERT INTO artifact_store
                (name, file_path, code_body, structural_summary, project_scope,
                 content_hash, version, created_at, source_message_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                safe_name,
                safe_path,
                safe_body,
                summary,
                safe_scope,
                digest,
                version,
                time.time(),
                source_message_id,
            ),
        )
        artifact_id = int(cursor.lastrowid)
        if safe_name.endswith("-digest.md"):
            if _FTS["available"]:
                connection.execute("UPDATE recall_index SET superseded = 1 WHERE project_scope = ? AND kind = 'digest' AND source = ?", (safe_scope, f"digest {safe_name}"[:160]))
            _index_text(connection, safe_scope, f"digest {safe_name}", "digest", None, artifact_id, safe_body[:8_000], time.time())
        elif summary:
            _index_text(connection, safe_scope, f"artifact {safe_name}", "artifact", None, artifact_id, summary, time.time())
        connection.commit()
        return artifact_id, version


def artifact_by_id(artifact_id: int, project_scope: Optional[str] = None) -> Optional[sqlite3.Row]:
    with _open_database() as connection:
        _check_scope(connection, "artifact_store", artifact_id, project_scope)
        return connection.execute("SELECT * FROM artifact_store WHERE id = ?", (int(artifact_id),)).fetchone()


def search_artifacts(project_scope: str, query: str, limit: int = 20) -> List[sqlite3.Row]:
    """Keyword search over name, path, and structural summary: every word must match, in any field, any order."""
    from .keyword_search import like_clauses  # local import: keyword_search has no vault dependency

    clause, params = like_clauses(query, ("name", "file_path", "structural_summary"))
    with _open_database() as connection:
        return list(
            connection.execute(
                f"""
                SELECT id, name, file_path, version, structural_summary, created_at
                FROM artifact_store
                WHERE project_scope = ? AND {clause}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                [project_scope.strip() or "default", *params, max(1, min(int(limit), 100))],
            ).fetchall()
        )


def search_messages(project_scope: str, thread_id: int, query: str, limit: int = 20) -> List[sqlite3.Row]:
    """Keyword search inside one thread (every word must appear, any order); newest first."""
    from .keyword_search import like_clauses

    clause, params = like_clauses(query, ("content",))
    scope = project_scope.strip() or "default"
    with _open_database() as connection:
        return list(connection.execute(
            f"SELECT id, role, content, timestamp, provider FROM message_history WHERE project_scope = ? AND thread_id = ? AND {clause} "
            "ORDER BY timestamp DESC, id DESC LIMIT ?",
            (scope, int(thread_id), *params, max(1, min(int(limit), 100))),
        ).fetchall())


def messages_around(thread_id: int, message_id: int, before: int = 10, after: int = 10, project_scope: Optional[str] = None) -> List[sqlite3.Row]:
    """The messages surrounding one message in its thread, oldest first (the navigator's jump target)."""
    with _open_database() as connection:
        _check_scope(connection, "threads", thread_id, project_scope)
        earlier = connection.execute(
            "SELECT * FROM message_history WHERE thread_id = ? AND id <= ? ORDER BY id DESC LIMIT ?", (int(thread_id), int(message_id), int(before) + 1)
        ).fetchall()
        later = connection.execute(
            "SELECT * FROM message_history WHERE thread_id = ? AND id > ? ORDER BY id ASC LIMIT ?", (int(thread_id), int(message_id), int(after))
        ).fetchall()
    return list(reversed(earlier)) + list(later)


def thread_outline(thread_id: int, limit: int = 200, project_scope: Optional[str] = None) -> List[Dict[str, Any]]:
    """One line per turn for the navigator: id, role, first words."""
    with _open_database() as connection:
        _check_scope(connection, "threads", thread_id, project_scope)
        rows = connection.execute(
            "SELECT id, role, content, timestamp FROM message_history WHERE thread_id = ? ORDER BY id ASC LIMIT ?", (int(thread_id), int(limit))
        ).fetchall()
    outline = []
    for row in rows:
        first = " ".join(str(row["content"]).strip().split())[:90]
        outline.append({"id": int(row["id"]), "role": row["role"], "text": first, "timestamp": float(row["timestamp"])})
    return outline


def save_mission_nodes(thread_id: int, plan: Sequence[Mapping[str, Any]], project_scope: Optional[str] = None) -> int:
    """Store a chat's node graph (replacing the previous one) so it can be reopened and re-run."""
    now = time.time()
    with _open_database() as connection:
        _check_scope(connection, "threads", thread_id, project_scope)
        owner = connection.execute("SELECT project_scope FROM threads WHERE id = ?", (int(thread_id),)).fetchone()
        scope = str(project_scope or (owner["project_scope"] if owner else "") or "")
        connection.execute("DELETE FROM mission_nodes WHERE thread_id = ?", (int(thread_id),))
        for position, node in enumerate(plan):
            connection.execute(
                "INSERT INTO mission_nodes (thread_id, position, node, created_at, project_scope) VALUES (?, ?, ?, ?, ?)",
                (int(thread_id), position, redact_secrets(json.dumps(dict(node), default=str)), now, scope),
            )
    return len(plan)


def mission_nodes_for(thread_id: int, project_scope: Optional[str] = None) -> List[Dict[str, Any]]:
    with _open_database() as connection:
        _check_scope(connection, "threads", thread_id, project_scope)
        rows = connection.execute("SELECT node FROM mission_nodes WHERE thread_id = ? ORDER BY position ASC", (int(thread_id),)).fetchall()
    nodes = []
    for row in rows:
        try:
            nodes.append(json.loads(row["node"]))
        except ValueError:
            continue
    return nodes


def export_artifact(artifact_id: int, project_scope: Optional[str] = None) -> Tuple[str, str]:
    """Return (suggested_filename, body) for a download or copy-out."""
    row = artifact_by_id(artifact_id, project_scope)
    if row is None:
        raise KeyError(f"artifact {artifact_id} not found")
    filename = row["file_path"].split("/")[-1] if row["file_path"] else row["name"]
    if not filename:
        filename = f"artifact-{row['id']}.txt"
    stem, dot, ext = filename.rpartition(".")
    filename = f"{stem}.v{row['version']}.{ext}" if dot else f"{filename}.v{row['version']}"
    return filename, str(row["code_body"])


# =============================================================================
# Routing telemetry (persisted per send; the session table is a view of the same events)
# =============================================================================


ROUTE_OUTCOMES = ("up", "down", "locked")


def record_route(
    project_scope: str, workspace: str, task_type: str, route: str, mode: str, ms: int, finish: str = "", reason: str = "",
    runner_up: str = "", chaos: Optional[Mapping[str, Any]] = None, message_id: Optional[int] = None, fragility: Optional[float] = None,
    explored: bool = False,
) -> int:
    """One row per send: where it went, how long it took, how it ended, why the solver chose it, what it would have chosen
    otherwise (``runner_up``), and the pink-wave state (``chaos``: gain, profile, jitter); ``message_id`` lets an outcome
    (thumbs, a locked artifact) be attached later so chaos on and off can be compared on results, not beliefs."""
    scope = project_scope.strip() or "default"
    chaos = dict(chaos or {})
    with _open_database() as connection:
        cursor = connection.execute(
            """
            INSERT INTO route_log (timestamp, project_scope, workspace, task_type, route, mode, ms, finish, reason,
                                   runner_up, chaos_gain, chaos_profile, jitter, message_id, fragility, explored)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (time.time(), scope, (workspace or "")[:40], (task_type or "")[:40], (route or "")[:120], (mode or "normal")[:40],
             max(0, int(ms)), (finish or "")[:40], redact_secrets(reason or "")[:300], str(runner_up or "")[:120],
             float(chaos.get("gain", 0.0) or 0.0), str(chaos.get("profile", "") or "")[:20], float(chaos.get("jitter", 0.0) or 0.0),
             int(message_id) if message_id is not None else None, float(fragility) if fragility is not None else None, int(bool(explored))),
        )
        connection.commit()
        return int(cursor.lastrowid)


def routes_for_messages(project_scope: str, message_ids: Sequence[int]) -> Dict[int, sqlite3.Row]:
    """The send behind each answer (the newest route row per message id), so a thread can say why an answer came out as it did."""
    ids = sorted({int(value) for value in message_ids})
    if not ids:
        return {}
    scope = project_scope.strip() or "default"
    with _open_database() as connection:
        rows = connection.execute(
            f"SELECT * FROM route_log WHERE project_scope = ? AND message_id IN ({','.join('?' for _ in ids)}) ORDER BY id ASC",
            (scope, *ids),
        ).fetchall()
    return {int(row["message_id"]): row for row in rows}


FINISH_WORDS = {
    "": "ended normally", "stop": "ended normally", "length": "was cut at the answer length limit",
    "filtered": "was filtered by the provider's content policy", "failed": "failed",
}


def route_facts(row: Mapping[str, Any]) -> str:
    """One plain paragraph per send: where it went and why, the runner-up, how long it took, how it ended, the verdict."""
    route, mode = str(row["route"] or ""), str(row["mode"] or "normal")
    reason = " ".join(str(row["reason"] or "").split())
    if route == "failed":
        head = f"No endpoint answered this send in {mode} mode" + (f": {reason}" if reason else "") + "."
    else:
        head = f"Sent to {route} in {mode} mode" + (f" because {reason}" if reason else "") + "."
    parts = [head]
    if row["runner_up"]:
        parts.append(f"The solver's runner-up was {row['runner_up']}.")
    if row["explored"]:
        parts.append("This was an exploration send: a less-used endpoint was tried on purpose.")
    finish = str(row["finish"] or "")
    parts.append(f"It took {int(row['ms'] or 0) / 1000.0:.1f} s and {FINISH_WORDS.get(finish, 'ended with ' + finish)}.")
    if row["outcome"]:
        parts.append(f"Your verdict: {row['outcome']}.")
    return " ".join(parts)


def set_route_outcome(project_scope: str, message_id: int, outcome: str) -> int:
    """Attach the operator's verdict to the send that produced a message: 'up', 'down', or 'locked' (an artifact was kept)."""
    if outcome not in ROUTE_OUTCOMES:
        raise ValueError("unknown route outcome")
    scope = project_scope.strip() or "default"
    with _open_database() as connection:
        cursor = connection.execute(
            "UPDATE route_log SET outcome = ? WHERE project_scope = ? AND message_id = ?", (outcome, scope, int(message_id))
        )
        rows = connection.execute(
            "SELECT route, task_type FROM route_log WHERE project_scope = ? AND message_id = ?", (scope, int(message_id))
        ).fetchall()
        connection.commit()
    from . import learner  # local import: the learner reads this module lazily

    for row in rows:
        learner.observe_outcome(scope, str(row["route"]).split("/")[0], str(row["task_type"]), outcome)
    return int(cursor.rowcount)


def quality_priors_for(project_scope: str) -> Dict[Tuple[str, str], Tuple[float, float, int]]:
    scope = project_scope.strip() or "default"
    with _open_database() as connection:
        rows = connection.execute("SELECT endpoint, task_type, alpha, beta, observations FROM quality_priors WHERE project_scope = ?", (scope,)).fetchall()
    return {(str(r["endpoint"]), str(r["task_type"])): (float(r["alpha"]), float(r["beta"]), int(r["observations"])) for r in rows}


def quality_prior_update(project_scope: str, endpoint: str, task_type: str, delta_alpha: float, delta_beta: float) -> Tuple[float, float, int]:
    """Fold one verdict into a Beta(alpha, beta) prior; returns the posterior parameters and the count."""
    scope = project_scope.strip() or "default"
    with _open_database() as connection:
        connection.execute(
            "INSERT INTO quality_priors (project_scope, endpoint, task_type, alpha, beta, observations, updated_at) VALUES (?, ?, ?, 2, 2, 0, ?) "
            "ON CONFLICT(project_scope, endpoint, task_type) DO NOTHING",
            (scope, endpoint[:60], task_type[:40], time.time()),
        )
        connection.execute(
            "UPDATE quality_priors SET alpha = alpha + ?, beta = beta + ?, observations = observations + 1, updated_at = ? "
            "WHERE project_scope = ? AND endpoint = ? AND task_type = ?",
            (float(delta_alpha), float(delta_beta), time.time(), scope, endpoint[:60], task_type[:40]),
        )
        row = connection.execute(
            "SELECT alpha, beta, observations FROM quality_priors WHERE project_scope = ? AND endpoint = ? AND task_type = ?", (scope, endpoint[:60], task_type[:40])
        ).fetchone()
        connection.commit()
    return float(row["alpha"]), float(row["beta"]), int(row["observations"])


def quality_priors_reset(project_scope: str) -> int:
    with _open_database() as connection:
        cursor = connection.execute("DELETE FROM quality_priors WHERE project_scope = ?", (project_scope.strip() or "default",))
        connection.commit()
    return int(cursor.rowcount)


def endpoint_latency_stats(project_scope: str, hours: float = 72.0) -> Dict[str, Dict[str, float]]:
    """Per endpoint (the route's provider part), the number of answered sends and their p50 latency over the window."""
    buckets: Dict[str, List[int]] = {}
    for row in recent_routes(project_scope, limit=5000, hours=hours):
        route = str(row["route"])
        if route == "failed":
            continue
        buckets.setdefault(route.split("/")[0], []).append(int(row["ms"]))
    return {name: {"n": len(values), "p50_ms": float(_percentile(values, 0.5))} for name, values in buckets.items()}


def exploration_stats(project_scope: str, hours: float = 168.0) -> Dict[str, Any]:
    """How often the learner explored over the window, against the sends made with the wave on."""
    rows = recent_routes(project_scope, limit=5000, hours=hours)
    with_wave = [r for r in rows if "chaos_gain" in r.keys() and float(r["chaos_gain"] or 0.0) > 0.0 and str(r["route"]) != "failed"]
    explored = sum(1 for r in with_wave if "explored" in r.keys() and int(r["explored"] or 0))
    return {"sends_with_wave": len(with_wave), "explored": explored, "observed_rate": round(explored / len(with_wave), 4) if with_wave else 0.0}


def chaos_comparison(project_scope: str, hours: float = 168.0) -> List[Dict[str, Any]]:
    """Chaos on (gain > 0) versus off over the window: sends, failures, truncations, latency, verdicts, runner-up disagreements."""
    rows = recent_routes(project_scope, limit=5000, hours=hours)
    buckets: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        keys = row.keys()
        gain = float(row["chaos_gain"]) if "chaos_gain" in keys and row["chaos_gain"] is not None else 0.0
        bucket = buckets.setdefault("chaos on" if gain > 0 else "chaos off", {"sends": 0, "failed": 0, "truncated": 0, "ms": [], "up": 0, "down": 0, "locked": 0, "runner_up_differs": 0, "fragility": [], "explored": 0})
        bucket["sends"] += 1
        bucket["explored"] += int(row["explored"] or 0) if "explored" in keys else 0
        if "fragility" in keys and row["fragility"] is not None:
            bucket["fragility"].append(float(row["fragility"]))
        bucket["failed"] += int(str(row["route"]) == "failed")
        bucket["truncated"] += int(str(row["finish"]) == "length")
        bucket["ms"].append(int(row["ms"]))
        outcome = str(row["outcome"]) if "outcome" in keys and row["outcome"] else ""
        if outcome in bucket:
            bucket[outcome] += 1
        runner_up = str(row["runner_up"]) if "runner_up" in keys and row["runner_up"] else ""
        bucket["runner_up_differs"] += int(bool(runner_up) and not str(row["route"]).startswith(runner_up))
    out = []
    for name in ("chaos on", "chaos off"):
        bucket = buckets.get(name)
        if not bucket:
            continue
        sends = bucket["sends"] or 1
        out.append({
            "setting": name, "sends": bucket["sends"], "failed": bucket["failed"], "truncated": bucket["truncated"],
            "p50_ms": _percentile(bucket["ms"], 0.5), "p95_ms": _percentile(bucket["ms"], 0.95),
            "up": bucket["up"], "down": bucket["down"], "locked": bucket["locked"],
            "good_rate": round((bucket["up"] + bucket["locked"]) / sends, 3), "down_rate": round(bucket["down"] / sends, 3),
            "runner_up_differs": bucket["runner_up_differs"],
            "mean_fragility": round(sum(bucket["fragility"]) / len(bucket["fragility"]), 3) if bucket["fragility"] else None,
            "explored": bucket["explored"],
        })
    return out


def recent_routes(project_scope: str, limit: int = 200, hours: Optional[float] = None) -> List[sqlite3.Row]:
    scope = project_scope.strip() or "default"
    query = "SELECT * FROM route_log WHERE project_scope = ?"
    params: List[Any] = [scope]
    if hours is not None:
        query += " AND timestamp >= ?"
        params.append(time.time() - float(hours) * 3600.0)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 5000)))
    with _open_database() as connection:
        return list(connection.execute(query, params).fetchall())


def _percentile(values: Sequence[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return int(ordered[index])


def route_stats(project_scope: str, hours: float = 24.0) -> List[Dict[str, Any]]:
    """Per route (provider/model or 'failed'): sends, truncations, latency p50/p95, last seen. Pure arithmetic."""
    rows = recent_routes(project_scope, limit=5000, hours=hours)
    buckets: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        bucket = buckets.setdefault(str(row["route"]), {"route": str(row["route"]), "sends": 0, "truncated": 0, "filtered": 0, "ms": [], "last": 0.0})
        bucket["sends"] += 1
        bucket["truncated"] += int(row["finish"] == "length")
        bucket["filtered"] += int(row["finish"] == "filtered")
        bucket["ms"].append(int(row["ms"]))
        bucket["last"] = max(float(bucket["last"]), float(row["timestamp"]))
        outcome = str(row["outcome"]) if "outcome" in row.keys() and row["outcome"] else ""
        bucket["up"] = bucket.get("up", 0) + int(outcome == "up")
        bucket["down"] = bucket.get("down", 0) + int(outcome == "down")
        bucket["locked"] = bucket.get("locked", 0) + int(outcome == "locked")
    total = sum(b["sends"] for b in buckets.values()) or 1
    stats = []
    for bucket in buckets.values():
        stats.append({
            "route": bucket["route"],
            "sends": bucket["sends"],
            "share": round(bucket["sends"] / total, 3),
            "p50_ms": _percentile(bucket["ms"], 0.5),
            "p95_ms": _percentile(bucket["ms"], 0.95),
            "truncated": bucket["truncated"],
            "filtered": bucket["filtered"],
            "up": bucket.get("up", 0), "down": bucket.get("down", 0), "locked": bucket.get("locked", 0),
            "last_seen": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(bucket["last"])),
        })
    stats.sort(key=lambda item: (-item["sends"], item["route"]))
    return stats


def routes_csv(project_scope: str, limit: int = 5000) -> str:
    header = "id,timestamp_utc,workspace,task_type,route,mode,ms,finish,reason,runner_up,chaos_gain,chaos_profile,jitter,outcome,fragility,explored"
    lines = [header]
    for row in reversed(recent_routes(project_scope, limit=limit)):
        keys = row.keys()
        reason = str(row["reason"]).replace('"', "'").replace("\n", " ")
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(row["timestamp"])))
        extra = ",".join(str(row[k] if k in keys and row[k] is not None else "") for k in ("runner_up", "chaos_gain", "chaos_profile", "jitter", "outcome", "fragility", "explored"))
        lines.append(f"{row['id']},{stamp},{row['workspace']},{row['task_type']},{row['route']},{row['mode']},{row['ms']},{row['finish']},\"{reason}\",{extra}")
    return "\n".join(lines) + "\n"


# =============================================================================
# Jobs (background work claimed by the in-process runner or an external worker)
# =============================================================================

JOB_ACTIVE = ("queued", "running", "waiting_input")


def _job_json(text: Optional[str]) -> Dict[str, Any]:
    try:
        value = json.loads(text or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def enqueue_job(
    project_scope: str, kind: str, payload: Mapping[str, Any], thread_id: Optional[int] = None, run_after: float = 0.0
) -> int:
    """Queue one job. The payload is redacted like a message; secrets belong to the runner, never here.

    ``run_after`` (epoch seconds) holds the row back until then: the scheduler primitive behind chained cycles.
    """
    scope = project_scope.strip() or "default"
    now = time.time()
    body = redact_secrets(json.dumps(dict(payload), default=str))
    with _open_database() as connection:
        cursor = connection.execute(
            "INSERT INTO jobs (project_scope, thread_id, kind, status, payload, created_at, updated_at, run_after) "
            "VALUES (?, ?, ?, 'queued', ?, ?, ?, ?)",
            (scope, int(thread_id) if thread_id is not None else None, kind, body, now, now, float(run_after or 0.0)),
        )
        return int(cursor.lastrowid)


def claim_job(worker: str, kinds: Sequence[str]) -> Optional[sqlite3.Row]:
    """Atomically take the oldest queued job of the given kinds; None when the queue is empty."""
    if not kinds:
        return None
    marks = ",".join("?" for _ in kinds)
    with _open_database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            f"SELECT id FROM jobs WHERE status = 'queued' AND kind IN ({marks}) AND run_after <= ? ORDER BY id ASC LIMIT 1",
            (*kinds, time.time()),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        now = time.time()
        connection.execute(
            "UPDATE jobs SET status = 'running', worker = ?, claimed_at = ?, updated_at = ? WHERE id = ?",
            (worker, now, now, int(row["id"])),
        )
        connection.commit()
        return connection.execute("SELECT * FROM jobs WHERE id = ?", (int(row["id"]),)).fetchone()


def update_job_progress(job_id: int, fields: Mapping[str, Any]) -> None:
    """Merge ``fields`` into the job's progress JSON (redacted like every persisted string)."""
    with _open_database() as connection:
        row = connection.execute("SELECT progress FROM jobs WHERE id = ?", (int(job_id),)).fetchone()
        if row is None:
            return
        progress = _job_json(row["progress"])
        progress.update(dict(fields))
        connection.execute(
            "UPDATE jobs SET progress = ?, updated_at = ? WHERE id = ?", (redact_secrets(json.dumps(progress, default=str)), time.time(), int(job_id))
        )


def ask_job_question(job_id: int, question: str) -> None:
    with _open_database() as connection:
        connection.execute(
            "UPDATE jobs SET status = 'waiting_input', question = ?, answer = NULL, updated_at = ? WHERE id = ? AND status = 'running'",
            (redact_secrets(str(question))[:2000], time.time(), int(job_id)),
        )


def answer_job(job_id: int, answer: str, project_scope: Optional[str] = None) -> bool:
    """Operator's reply; True when the job was actually waiting."""
    with _open_database() as connection:
        _check_scope(connection, "jobs", job_id, project_scope)
        cursor = connection.execute(
            "UPDATE jobs SET status = 'running', answer = ?, updated_at = ? WHERE id = ? AND status = 'waiting_input'",
            (redact_secrets(str(answer)), time.time(), int(job_id)),
        )
        return cursor.rowcount == 1


def job_answer(job_id: int) -> Optional[str]:
    with _open_database() as connection:
        row = connection.execute("SELECT status, answer FROM jobs WHERE id = ?", (int(job_id),)).fetchone()
    if row is None or row["status"] != "running" or row["answer"] is None:
        return None
    return str(row["answer"])


def request_cancel(job_id: int, project_scope: Optional[str] = None) -> None:
    """Cooperative cancel: a queued job ends now, a running one at its next check."""
    now = time.time()
    with _open_database() as connection:
        _check_scope(connection, "jobs", job_id, project_scope)
        connection.execute(
            "UPDATE jobs SET status = 'cancelled', cancel_requested = 1, finished_at = ?, updated_at = ? WHERE id = ? AND status = 'queued'",
            (now, now, int(job_id)),
        )
        connection.execute(
            "UPDATE jobs SET cancel_requested = 1, updated_at = ? WHERE id = ? AND status IN ('running', 'waiting_input')",
            (now, int(job_id)),
        )


def cancel_requested(job_id: int) -> bool:
    with _open_database() as connection:
        row = connection.execute("SELECT cancel_requested FROM jobs WHERE id = ?", (int(job_id),)).fetchone()
    return bool(row and row["cancel_requested"])


def finish_job(job_id: int, status: str, result: Mapping[str, Any]) -> None:
    final = status if status in ("done", "failed", "cancelled") else "failed"
    now = time.time()
    with _open_database() as connection:
        connection.execute(
            "UPDATE jobs SET status = ?, result = ?, finished_at = ?, updated_at = ? WHERE id = ?",
            (final, redact_secrets(json.dumps(dict(result), default=str)), now, now, int(job_id)),
        )
        connection.execute("DELETE FROM job_secrets WHERE job_id = ?", (int(job_id),))


def job_by_id(job_id: int, project_scope: Optional[str] = None) -> Optional[sqlite3.Row]:
    with _open_database() as connection:
        _check_scope(connection, "jobs", job_id, project_scope)
        return connection.execute("SELECT * FROM jobs WHERE id = ?", (int(job_id),)).fetchone()


def store_job_secrets(job_id: int, blob: bytes) -> None:
    """Ciphertext only (see ``jobsecrets``); deleted when the worker claims the job or the job ends."""
    with _open_database() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO job_secrets (job_id, blob, created_at) VALUES (?, ?, ?)", (int(job_id), sqlite3.Binary(bytes(blob)), time.time())
        )


def take_job_secrets(job_id: int) -> Optional[bytes]:
    """Read and delete the blob in one transaction; None when there is none."""
    with _open_database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT blob FROM job_secrets WHERE job_id = ?", (int(job_id),)).fetchone()
        connection.execute("DELETE FROM job_secrets WHERE job_id = ?", (int(job_id),))
        connection.commit()
    return bytes(row["blob"]) if row is not None else None


def has_job_secrets(job_id: int) -> bool:
    with _open_database() as connection:
        return connection.execute("SELECT 1 FROM job_secrets WHERE job_id = ?", (int(job_id),)).fetchone() is not None


def purge_job_secrets(older_than_seconds: float = 3600.0) -> int:
    """Blobs nobody claimed (a job that never ran) are dropped after an hour."""
    with _open_database() as connection:
        cursor = connection.execute("DELETE FROM job_secrets WHERE created_at < ?", (time.time() - float(older_than_seconds),))
        return int(cursor.rowcount)


def quota_usage_load(key: str, day: str) -> Tuple[int, int]:
    with _open_database() as connection:
        row = connection.execute("SELECT tokens, requests FROM quota_usage WHERE key = ? AND day = ?", (str(key), str(day))).fetchone()
    return (int(row["tokens"]), int(row["requests"])) if row is not None else (0, 0)


def quota_usage_add(key: str, day: str, tokens: int, requests: int) -> None:
    with _open_database() as connection:
        connection.execute(
            "INSERT INTO quota_usage (key, day, tokens, requests, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(key, day) DO UPDATE SET tokens = tokens + excluded.tokens, requests = requests + excluded.requests, updated_at = excluded.updated_at",
            (str(key), str(day), int(tokens), int(requests), time.time()),
        )
        connection.execute("DELETE FROM quota_usage WHERE updated_at < ?", (time.time() - 3 * 86_400.0,))


def list_jobs(
    project_scope: str, statuses: Optional[Sequence[str]] = None, limit: int = 50,
    thread_id: Optional[int] = None, kind: Optional[str] = None,
) -> List[sqlite3.Row]:
    """Newest first for one scope, optionally narrowed by status, thread, or kind."""
    scope = project_scope.strip() or "default"
    clauses = ["project_scope = ?"]
    params: List[Any] = [scope]
    if statuses:
        clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")")
        params.extend(statuses)
    if thread_id is not None:
        clauses.append("thread_id = ?")
        params.append(int(thread_id))
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    params.append(int(limit))
    with _open_database() as connection:
        return connection.execute(
            f"SELECT * FROM jobs WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT ?", tuple(params)
        ).fetchall()


def reap_stale_jobs(note: str, queued_before: Optional[float] = None) -> int:
    """Fail every running or waiting job, plus queued ones created before ``queued_before``; returns the count.

    After a process restart their secrets are gone, so none of them can continue. Queued rows
    created after the restart (by this process) are left alone.
    """
    now = time.time()
    with _open_database() as connection:
        cursor = connection.execute(
            "UPDATE jobs SET status = 'failed', result = ?, finished_at = ?, updated_at = ? "
            "WHERE status IN ('running', 'waiting_input') OR (status = 'queued' AND created_at < ?)",
            (json.dumps({"error": note}), now, now, float(queued_before) if queued_before is not None else 0.0),
        )
        return int(cursor.rowcount)


def touch_heartbeat(job_ids: Sequence[int]) -> int:
    """A live worker stamps only the rows its threads are executing right now; stale stamps let another process reap them."""
    ids = [int(value) for value in job_ids]
    if not ids:
        return 0
    marks = ",".join("?" for _ in ids)
    with _open_database() as connection:
        cursor = connection.execute(
            f"UPDATE jobs SET heartbeat_at = ? WHERE id IN ({marks}) AND status IN ('running', 'waiting_input')", (time.time(), *ids)
        )
        return int(cursor.rowcount)


def fail_jobs_of_host(host_prefix: str, note: str, except_worker: str = "") -> int:
    """Fail rows an earlier process of this host left running (a restarted container reuses its hostname)."""
    now = time.time()
    with _open_database() as connection:
        cursor = connection.execute(
            "UPDATE jobs SET status = 'failed', result = ?, finished_at = ?, updated_at = ? "
            "WHERE status IN ('running', 'waiting_input') AND worker LIKE ? AND worker != ?",
            (json.dumps({"error": note}), now, now, f"{host_prefix}%", except_worker),
        )
        return int(cursor.rowcount)


def reap_stale_heartbeats(note: str, older_than_seconds: float = 180.0) -> int:
    """Fail running or waiting rows whose worker stopped stamping them (crashed thread or dead container)."""
    now = time.time()
    with _open_database() as connection:
        cursor = connection.execute(
            "UPDATE jobs SET status = 'failed', result = ?, finished_at = ?, updated_at = ? "
            "WHERE status IN ('running', 'waiting_input') AND COALESCE(heartbeat_at, claimed_at, created_at) < ?",
            (json.dumps({"error": note}), now, now, now - float(older_than_seconds)),
        )
        return int(cursor.rowcount)


def job_view(row: sqlite3.Row) -> Dict[str, Any]:
    """A job row with its JSON columns parsed, for display."""
    return {
        "id": int(row["id"]), "kind": str(row["kind"]), "status": str(row["status"]), "thread_id": row["thread_id"],
        "payload": _job_json(row["payload"]), "progress": _job_json(row["progress"]), "result": _job_json(row["result"]),
        "question": str(row["question"] or ""), "answer": row["answer"], "cancel_requested": bool(row["cancel_requested"]),
        "created_at": float(row["created_at"]), "updated_at": float(row["updated_at"]), "finished_at": row["finished_at"],
        "run_after": float(row["run_after"]) if "run_after" in row.keys() and row["run_after"] is not None else 0.0,
    }


def health_check() -> Dict[str, Any]:
    """Zero-cost liveness for the ?health=1 view and the smoke drive: schema present, counts readable."""
    try:
        with _open_database() as connection:
            threads = int(connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0])
            messages = int(connection.execute("SELECT COUNT(*) FROM message_history").fetchone()[0])
            artifacts = int(connection.execute("SELECT COUNT(*) FROM artifact_store").fetchone()[0])
            jobs = {str(status): int(count) for status, count in connection.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status")}
            routes = connection.execute("SELECT COUNT(*), MAX(timestamp) FROM route_log").fetchone()
        return {
            "ok": True, "path": str(database_path()), "threads": threads, "messages": messages, "artifacts": artifacts, "error": "",
            "jobs": jobs, "sends": int(routes[0] or 0), "last_send_at": float(routes[1] or 0.0),
        }
    except Exception as exc:
        return {
            "ok": False, "path": str(database_path()), "threads": 0, "messages": 0, "artifacts": 0,
            "error": f"{type(exc).__name__}: {exc}"[:200], "jobs": {}, "sends": 0, "last_send_at": 0.0,
        }


def export_thread(thread_id: int, project_scope: Optional[str] = None) -> Dict[str, Any]:
    """Everything one chat holds, for review or hand-off: thread row, archived + live messages in order, summaries, digest.

    Nothing is written; content was redacted when it was stored, so an export never carries a key.
    """
    thread = thread_by_id(int(thread_id), project_scope)
    if thread is None:
        raise ValueError(f"thread {int(thread_id)} does not exist")
    scope = str(thread["project_scope"])
    fields = ("id", "role", "content", "timestamp", "token_count", "provider", "mode", "task_type")

    def message(row: sqlite3.Row, archived: bool) -> Dict[str, Any]:
        item = {name: (row[name] if name in row.keys() else "") for name in fields}
        item["archived"] = archived
        return item

    messages = [message(row, True) for row in archived_messages(scope, 5000, thread_id=int(thread_id))]
    messages += [message(row, False) for row in recent_messages(scope, MESSAGE_WINDOW, thread_id=int(thread_id))]
    messages.sort(key=lambda item: int(item["id"]))
    summaries = [
        {name: row[name] for name in ("id", "covers_from_id", "covers_to_id", "message_count", "content", "method", "created_at")}
        for row in reversed(recent_summaries(scope, 50, thread_id=int(thread_id)))
    ]
    digest = ""
    if thread["digest_artifact_id"]:
        artifact = artifact_by_id(int(thread["digest_artifact_id"]))
        digest = str(artifact["code_body"]) if artifact is not None else ""
    route_fields = ("message_id", "timestamp", "route", "mode", "ms", "finish", "reason", "runner_up", "explored", "outcome")
    routes = [
        {name: row[name] for name in route_fields}
        for _, row in sorted(routes_for_messages(scope, [int(item["id"]) for item in messages]).items())
    ]
    return {
        "thread": {name: thread[name] for name in thread.keys()},
        "messages": messages,
        "summaries": summaries,
        "digest": digest,
        "routes": routes,  # why each answer came out as it did; a failed send is a route row on its system message
        "exported_at": time.time(),
    }


def _stamp(value: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(float(value)))
    except (TypeError, ValueError):
        return "-"


def thread_transcript(thread_id: int, fmt: str = "markdown", project_scope: Optional[str] = None) -> Tuple[str, str]:
    """Return (filename, body) for a chat download: ``markdown`` for reading, ``json`` for tooling."""
    payload = export_thread(thread_id, project_scope)
    thread = payload["thread"]
    stem = f"chat-{_workspace(thread.get('workspace'))}-{int(thread['id'])}"
    if fmt == "json":
        return f"{stem}.json", json.dumps(payload, indent=2, ensure_ascii=False)
    lines = [
        f"# Chat Johnson transcript · {_workspace(thread.get('workspace'))} · #{int(thread['id'])} {thread.get('title', '')}",
        "",
        f"- scope: {thread.get('project_scope', '')}",
        f"- status: {thread.get('status', '')} · generation {thread.get('generation', 1)}",
        f"- mission: {thread.get('mission') or '(none)'}",
        f"- exported: {_stamp(payload['exported_at'])}",
        "",
    ]
    if payload["digest"]:
        lines += ["## Inherited vision digest", "", payload["digest"], ""]
    if payload["summaries"]:
        lines += ["## Texturized summaries", ""]
        for summary in payload["summaries"]:
            lines += [
                f"### Summary #{summary['id']} · {summary['message_count']} messages "
                f"(ids {summary['covers_from_id']}-{summary['covers_to_id']}) · {summary['method']} · {_stamp(summary['created_at'])}",
                "",
                str(summary["content"]),
                "",
            ]
    lines += ["## Messages", ""]
    routes = {int(route["message_id"]): route for route in payload.get("routes", []) if route.get("message_id") is not None}
    for index, item in enumerate(payload["messages"], start=1):
        meta = [str(item["role"]).upper(), _stamp(item["timestamp"])]
        if item["provider"]:
            meta.append(str(item["provider"]))
        if item["task_type"]:
            meta.append(f"task {item['task_type']}")
        meta.append(str(item["mode"]))
        if item["archived"]:
            meta.append("archived")
        lines += [f"### {index}. " + " · ".join(meta), "", str(item["content"]), ""]
        route = routes.get(int(item["id"]))
        if route is not None:
            lines += [f"_Why: {route_facts(route)}_", ""]
    return f"{stem}.md", "\n".join(lines)


def recent_artifacts(project_scope: str, limit: int = 8) -> List[sqlite3.Row]:
    with _open_database() as connection:
        return list(
            connection.execute(
                """
                SELECT id, name, file_path, version, structural_summary, created_at
                FROM artifact_store
                WHERE project_scope = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (project_scope.strip() or "default", max(1, min(int(limit), 50))),
            ).fetchall()
        )


# =============================================================================
# Thread health sweep and optimized migration
# =============================================================================

HEALTH_MESSAGE_LIMIT = 300        # active + archived messages before a thread is considered heavy (> MESSAGE_WINDOW,
                                  # so at least one texturized block exists before migration)
HEALTH_TOKEN_LIMIT = 60_000       # estimated tokens in the live window (pages are big by design; the window clips before this)
HEALTH_SUMMARY_LIMIT = 2          # texturized blocks already stacked on this thread
MIN_DIGEST_MESSAGES = 4           # a thread needs this many real messages before it can be migrated at all
DIGEST_MAX_CHARACTERS = 5_000

_DECISION_RE = re.compile(r"(?i)\b(?:decid\w*|agree\w*|decision\w*|constraint\w*|requir\w*|polic\w*|approv\w*|must|never|always|rule)\b")
_MARKUP_LINE_RE = re.compile(r"^\s*(?:<|[{}]|[.#@][\w-]+\s*[{,]|[\w-]+\s*:\s*[^:]+;\s*$|//|/\*|\*/|\}|\)|;|```)")
AUTOMATIC_TURN_PREFIX = "AUTOMATIC "  # app-authored user turns (sandbox fixes, continuations) are not the operator's words
_OPEN_RE = re.compile(r"(?i)\b(todo|next step|open question|unresolved|pending|follow[- ]up|blocked)\b|\?\s*$")
_FACT_RE = re.compile(r"[\w/.-]+\.(py|js|ts|md|json|yml|yaml|toml|sql|html|css)\b|\b\d+(\.\d+)?\s*(%|rpm|tpm|tokens|ms|s)\b", re.I)


def _normalized(text: str) -> str:
    return re.sub(r"\W+", " ", text.lower()).strip()


MIN_MESSAGES_AFTER_MIGRATION = 40  # a fresh successor must earn some history before it can migrate again


def thread_health(project_scope: str, thread_id: Optional[int] = None, workspace: Optional[str] = None) -> Dict[str, Any]:
    """Cheap, deterministic sweep of one thread: load, repetition, and a migration recommendation."""
    scope = project_scope.strip() or "default"
    thread = thread_by_id(thread_id) if thread_id is not None else active_thread(scope, workspace)
    resolved = int(thread["id"])
    rows = [row for row in recent_messages(scope, MESSAGE_WINDOW, thread_id=resolved) if row["role"] != "system"]
    summaries = recent_summaries(scope, 50, thread_id=resolved)
    archived = archived_messages(scope, 5000, thread_id=resolved)
    # App-authored turns (automatic fixes carry a whole page each) are not load the operator created.
    spoken = [row for row in rows if not (row["role"] == "user" and str(row["content"]).startswith(AUTOMATIC_TURN_PREFIX))]
    tokens = sum(int(row["token_count"]) for row in spoken)
    user_turns = [_normalized(row["content"])[:200] for row in spoken if row["role"] == "user"]
    repeated = 0
    if len(user_turns) > 1:
        repeated = len(user_turns) - len(set(user_turns))
    repetition = repeated / len(user_turns) if user_turns else 0.0
    error_turns = sum(1 for row in rows if row["role"] == "assistant" and row["content"].startswith(("Provider error", "Task failed")))
    total_messages = len(rows) + len(archived)
    # Context load is the migration criterion; the gauge shows exactly this number.
    load_terms = {
        "messages": total_messages / HEALTH_MESSAGE_LIMIT,
        "tokens": tokens / HEALTH_TOKEN_LIMIT,
        "summaries": len(summaries) / HEALTH_SUMMARY_LIMIT,
    }
    pressure = max(load_terms.values())
    reasons: List[str] = []
    if total_messages >= HEALTH_MESSAGE_LIMIT:
        reasons.append(f"{total_messages} messages on this thread (live + archived)")
    if tokens >= HEALTH_TOKEN_LIMIT:
        reasons.append(f"~{tokens} tokens in the live window")
    if len(summaries) >= HEALTH_SUMMARY_LIMIT:
        reasons.append(f"{len(summaries)} texturized blocks stacked")
    # Diagnosis, not remedy: these never trigger a migration (which would clear the
    # window, reset the counters, and re-arm during the very outage they measure).
    advisories: List[str] = []
    if repetition >= 0.25 and len(user_turns) >= 8:
        advisories.append(f"{int(repetition * 100)}% repeated prompts; rephrase or start a new thread")
    if error_turns >= 5:
        advisories.append(f"{error_turns} provider errors in the window; check keys or switch provider")
    exempt = False
    if int(thread["generation"]) > 1 and len(rows) < MIN_MESSAGES_AFTER_MIGRATION:
        exempt = True  # a just-migrated successor is exempt until it has real history
    if len(rows) + len(archived) < MIN_DIGEST_MESSAGES:
        exempt = True  # nothing worth compressing yet
    return {
        "thread_id": resolved,
        "title": thread["title"],
        "workspace": thread["workspace"],
        "generation": int(thread["generation"]),
        "messages": len(rows),
        "tokens": tokens,
        "summaries": len(summaries),
        "archived": len(archived),
        "repetition": round(repetition, 3),
        "error_turns": error_turns,
        "pressure": round(min(pressure, 2.0), 3),
        "recommend_migration": bool(reasons) and not exempt,
        "can_migrate": not exempt,
        "exempt": exempt,
        "reasons": reasons,
        "advisories": advisories,
    }


def build_vision_digest(
    project_scope: str,
    thread_id: Optional[int] = None,
    max_characters: int = DIGEST_MAX_CHARACTERS,
    workspace: Optional[str] = None,
    recall_characters: Optional[int] = None,
) -> str:
    """Deterministic compression of a thread into the material a fresh thread needs.

    Sections: vision (how the thread started), decisions and constraints, key facts (files,
    numbers), open items, artifacts locked in the scope, the stacked summaries, and long-distance
    memory recalled from the project's other chats (``recall_characters``: the scope's pink-wave
    setting when None, 0 disables). Zero provider quota; a model may refine it afterwards.
    """
    scope = project_scope.strip() or "default"
    thread = thread_by_id(thread_id) if thread_id is not None else active_thread(scope, workspace)
    resolved = int(thread["id"])
    live = recent_messages(scope, MESSAGE_WINDOW, thread_id=resolved)
    older = archived_messages(scope, 5000, thread_id=resolved)
    rows = list(older) + list(live)
    summaries = recent_summaries(scope, 50, thread_id=resolved)
    inherited = ""
    if thread["digest_artifact_id"]:
        parent = artifact_by_id(int(thread["digest_artifact_id"]))
        inherited = str(parent["code_body"]) if parent else ""

    def unique(items: List[str], cap: int) -> List[str]:
        seen: set = set()
        out: List[str] = []
        for item in items:
            key = _normalized(item)
            if key and key not in seen:
                seen.add(key)
                out.append(item.strip())
            if len(out) >= cap:
                break
        return out

    spoken = [row for row in rows if row["role"] == "user" and not str(row["content"]).startswith(AUTOMATIC_TURN_PREFIX)]
    first_user = next((row["content"].strip() for row in spoken), "")
    decisions: List[str] = []
    facts: List[str] = []
    open_items: List[str] = []
    # Only the operator's own sentences can become decisions or open items: a model's explanation, a sandbox report
    # or a line of CSS harvested as a "constraint" is how a wrong diagnosis became a standing rule.
    for row in spoken:
        for line in str(row["content"]).splitlines():
            stripped = line.strip()
            if not stripped or len(stripped) > 400 or _MARKUP_LINE_RE.match(stripped):
                continue
            if _DECISION_RE.search(stripped):
                decisions.append(stripped)
            elif _OPEN_RE.search(stripped):
                open_items.append(stripped)
    for row in rows:
        if row["role"] == "system":
            continue
        for line in str(row["content"]).splitlines():
            stripped = line.strip()
            if not stripped or len(stripped) > 400 or _MARKUP_LINE_RE.match(stripped):
                continue
            if _FACT_RE.search(stripped) and not _DECISION_RE.search(stripped):
                facts.append(stripped)
    artifacts = [a for a in recent_artifacts(scope, 40) if not str(a["name"]).endswith("-digest.md")][:20]
    parts: List[str] = [f"# Vision digest · {thread['title']} (generation {int(thread['generation'])})"]
    if inherited:
        parts.append("## Inherited from earlier threads\n" + inherited[:1_200])
    if first_user:
        parts.append("## Vision (how this thread started)\n" + first_user[:600])
    if decisions:
        parts.append("## Decisions and constraints\n" + "\n".join(f"- {d}" for d in unique(decisions, 18)))
    if facts:
        parts.append("## Key facts\n" + "\n".join(f"- {f}" for f in unique(facts, 18)))
    if open_items:
        parts.append("## Open items\n" + "\n".join(f"- {o}" for o in unique(open_items, 12)))
    if artifacts:
        parts.append("## Locked artifacts in scope\n" + "\n".join(
            f"- {a['name']} v{a['version']} ({a['file_path'] or 'no path'})" for a in artifacts
        ))
    if summaries:
        parts.append("## Texturized summaries\n" + "\n\n".join(
            f"[{s['message_count']} msgs] {s['content'][:700]}" for s in summaries[-4:]
        ))
    if recall_characters is None:
        from .pinkwave import for_scope  # local import: pinkwave imports this module lazily

        recall_characters = for_scope(scope).digest_recall_chars()
    recalled = recall_memory(scope, " ".join([first_user[:400], *decisions[:3]]), resolved, int(recall_characters)) if recall_characters else ""
    if recalled:
        parts.append("## Long-distance memory (other chats in this project)\n" + "\n".join(recalled.splitlines()[1:]))
    digest = "\n\n".join(parts)
    return digest[:max_characters]


def migrate_thread(
    project_scope: str,
    thread_id: Optional[int] = None,
    refine: Optional[Any] = None,
    title: str = "",
    workspace: Optional[str] = None,
) -> Dict[str, Any]:
    """Close a heavy thread and open its optimized successor.

    1. build the deterministic vision digest (optionally refined by ``refine(text) -> text``);
    2. lock the digest as an immutable artifact so it never leaves the vault;
    3. create the successor thread pointing at that digest and seed it with one
       system message; 4. mark the old thread ``migrated``. Raw history stays.
    """
    scope = project_scope.strip() or "default"
    thread = thread_by_id(thread_id) if thread_id is not None else active_thread(scope, workspace)
    old_id = int(thread["id"])
    real_messages = [
        row for row in list(recent_messages(scope, MESSAGE_WINDOW, thread_id=old_id)) + list(archived_messages(scope, 5000, thread_id=old_id))
        if row["role"] != "system"
    ]
    if len(real_messages) < MIN_DIGEST_MESSAGES:
        raise ValueError(f"thread #{old_id} has only {len(real_messages)} message(s); nothing worth compressing yet")
    digest = build_vision_digest(scope, old_id)
    method = "extractive"
    if refine is not None and len(digest) < 400:
        refine = None
        method = "extractive (digest too short to spend a model call on)"
    if refine is not None:
        try:
            refined = str(refine(digest)).strip()
            if len(refined) >= 200:
                digest = f"{refined}\n\n---\n_Deterministic base digest retained below for audit._\n\n{digest}"[:DIGEST_MAX_CHARACTERS * 2]
                method = "model+extractive"
        except Exception:
            method = "extractive (model refinement failed)"
    artifact_id, version = save_artifact(
        scope, f"thread-{old_id}-digest.md", f"threads/thread-{old_id}-digest.md", digest, "markdown"
    )
    new_title = title.strip() or f"{thread['title']} · v{int(thread['generation']) + 1}"
    new_id = create_thread(scope, new_title, parent_thread_id=old_id, workspace=thread["workspace"])
    with _open_database() as connection:
        connection.execute("UPDATE threads SET digest_artifact_id = ? WHERE id = ?", (artifact_id, new_id))
        mission = str(thread["mission"]) if "mission" in thread.keys() and thread["mission"] else ""
        connection.execute("UPDATE threads SET mission = ? WHERE id = ?", (mission, new_id))
        connection.execute("UPDATE threads SET status = 'migrated', updated_at = ? WHERE id = ?", (time.time(), old_id))
        if _FTS["available"]:
            connection.execute("UPDATE recall_index SET superseded = 1 WHERE project_scope = ? AND thread_id = ? AND kind = 'summary'", (scope, old_id))
            # The digest belongs to the successor, so the successor never recalls its own background a second time.
            connection.execute("UPDATE recall_index SET thread_id = ? WHERE project_scope = ? AND kind = 'digest' AND ref_id = ?", (new_id, scope, artifact_id))
        connection.commit()
    append_message(
        scope,
        "system",
        f"This chat continues chat #{old_id} ({thread['title']}), which got long. Its summary (v{version}) is locked as artifact "
        f"{artifact_id} ({method}) and is shown to the model as background only, never as a rule.",
        thread_id=new_id,
    )
    return {"old_thread_id": old_id, "new_thread_id": new_id, "digest_artifact_id": artifact_id, "method": method, "digest": digest}
