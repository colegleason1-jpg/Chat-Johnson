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
WORKSPACES = ("task_finder", "repository", "chat_bot", "normal_chat")
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
    reason TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_route_log_scope_time
    ON route_log(project_scope, timestamp DESC, id DESC);

-- Older databases carried a destructive trigger; the application now
-- texturizes (summarize + archive) before evicting from the active window.
DROP TRIGGER IF EXISTS message_history_rolling_cap;
"""


def _open_database() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def initialize_database() -> None:
    """Create the local schema idempotently and migrate older vaults to threads."""
    with _open_database() as connection:
        connection.executescript(SCHEMA_SQL)
        for table in ("message_history", "message_archive", "summaries"):
            _ensure_column(connection, table, "thread_id", "INTEGER")
        _ensure_column(connection, "threads", "workspace", "TEXT NOT NULL DEFAULT 'normal_chat'")
        _ensure_column(connection, "message_history", "task_type", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "threads", "mission", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "message_archive", "task_type", "TEXT NOT NULL DEFAULT ''")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_message_thread ON message_history(thread_id, timestamp ASC, id ASC)"
        )
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
        connection.commit()


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


def thread_by_id(thread_id: int) -> Optional[sqlite3.Row]:
    with _open_database() as connection:
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
        safe_title = (title.strip() or f"Thread {count + 1}")[:120]
        cursor = connection.execute(
            """
            INSERT INTO threads (project_scope, title, status, parent_thread_id, generation, workspace, created_at, updated_at)
            VALUES (?, ?, 'active', ?, ?, ?, ?, ?)
            """,
            (scope, safe_title, parent_thread_id, generation, ws, now, now),
        )
        connection.commit()
        return int(cursor.lastrowid)


def switch_thread(thread_id: int) -> None:
    """Make ``thread_id`` the workspace's current thread without touching its status.

    A migrated thread stays labelled migrated (its digest chain is not forked);
    it simply becomes the one the workspace reads and appends to.
    """
    with _open_database() as connection:
        connection.execute("UPDATE threads SET updated_at = ? WHERE id = ?", (time.time(), int(thread_id)))


def rename_thread(thread_id: int, title: str) -> None:
    with _open_database() as connection:
        connection.execute("UPDATE threads SET title = ? WHERE id = ?", ((title.strip() or "Untitled")[:120], int(thread_id)))


def set_thread_mission(thread_id: int, goal: str) -> None:
    """Pin the Task Finder mission to the thread so it outlives window eviction and migration."""
    with _open_database() as connection:
        connection.execute("UPDATE threads SET mission = ? WHERE id = ?", ((goal or "").strip()[:2000], int(thread_id)))


def set_thread_status(thread_id: int, status: str) -> None:
    if status not in {"active", "migrated", "archived"}:
        raise ValueError("invalid thread status")
    with _open_database() as connection:
        connection.execute("UPDATE threads SET status = ?, updated_at = ? WHERE id = ?", (status, time.time(), int(thread_id)))


def _touch_thread(connection: sqlite3.Connection, thread_id: int) -> None:
    connection.execute("UPDATE threads SET updated_at = ? WHERE id = ?", (time.time(), int(thread_id)))


def clear_thread(thread_id: int) -> int:
    """Move every active message of the thread to the archive (raw text kept). Keys are untouched."""
    now = time.time()
    with _open_database() as connection:
        connection.execute("BEGIN IMMEDIATE")
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


def delete_thread(thread_id: int) -> Dict[str, int]:
    """Remove a thread with its live window, archive, and summaries. Locked artifacts are project-level and stay.

    Clear keeps history in the archive; this is the operator's explicit "forget it" and is the only
    path that deletes anything. Successor threads keep their digest artifact and lose only the parent link.
    """
    with _open_database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT id FROM threads WHERE id = ?", (int(thread_id),)).fetchone() is None:
            connection.rollback()
            raise ValueError(f"thread {int(thread_id)} does not exist")
        counts: Dict[str, int] = {}
        for table in ("message_history", "message_archive", "summaries"):
            cursor = connection.execute(f"DELETE FROM {table} WHERE thread_id = ?", (int(thread_id),))
            counts[table] = int(cursor.rowcount)
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
) -> int:
    """Insert a message into the workspace's current (or given) thread, then texturize + archive past the window."""
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
                (role, content, timestamp, token_count, project_scope, provider, mode, thread_id, task_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        for line in content.splitlines():
            stripped = line.strip()
            if stripped == first_sentence:
                continue
            if re.search(r"(?i)\b(?:decid\w*|agree\w*|must|never|always|todo|fix\w*|bug\w*|error\w*)\b", stripped) or re.search(
                r"[\w/.-]+\.(py|js|ts|md|json|yml|yaml|toml|sql)\b", stripped
            ):
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


def context_parts(
    project_scope: str, max_characters: int = 24_000, thread_id: Optional[int] = None, workspace: Optional[str] = None
) -> Dict[str, Any]:
    """Prompt memory for one thread, split for role-based prompting.

    ``memory`` holds the inherited digest, texturized summaries, and system notes (for the
    system prompt); ``turns`` holds the live window as user/assistant turns in order.
    Budgets: digest <= 1/4, summaries <= 1/4, the live window gets the rest and fills
    newest-first; a single oversized newest turn is truncated rather than dropped.
    """
    scope = project_scope.strip() or "default"
    thread = thread_by_id(thread_id) if thread_id is not None else active_thread(scope, workspace)
    resolved_thread = int(thread["id"]) if thread is not None else None
    rows = recent_messages(scope, MESSAGE_WINDOW, thread_id=resolved_thread)
    summaries = recent_summaries(scope, 5, thread_id=resolved_thread)
    digest = ""
    if thread is not None and thread["digest_artifact_id"]:
        parent_digest = artifact_by_id(int(thread["digest_artifact_id"]))
        if parent_digest is not None:
            digest = str(parent_digest["code_body"])
    memory_lines: List[str] = []
    used = 0
    digest_budget = max_characters // 4
    summary_budget = max_characters // 4
    if digest:
        line = "[THREAD VISION DIGEST inherited from the previous thread]\n" + digest[:digest_budget]
        memory_lines.append(line)
        used += len(line) + 1
    summary_used = 0
    for summary in summaries:
        line = f"[TEXTURIZED SUMMARY of {summary['message_count']} earlier messages]\n{summary['content']}"
        if summary_used + len(line) + 1 > summary_budget:
            break
        memory_lines.append(line)
        summary_used += len(line) + 1
    used += summary_used
    remaining = max_characters - used
    window: List[Tuple[str, str]] = []
    for row in reversed(rows):
        role = str(row["role"])
        content = str(row["content"])
        prefix = len(role.upper()) + 2
        if prefix + len(content) + 1 > remaining:
            marker = " [TRUNCATED]"
            if not window and remaining > 200 + len(marker):
                window.append((role, content[: remaining - 1 - prefix - len(marker)] + marker))
            break
        window.append((role, content))
        remaining -= prefix + len(content) + 1
    window.reverse()
    turns: List[Dict[str, str]] = []
    for role, content in window:
        if role in ("user", "assistant"):
            turns.append({"role": role, "content": content})
        else:
            memory_lines.append(f"[NOTE] {content}")
    return {
        "memory": "\n".join(memory_lines),
        "turns": turns,
        "empty": not rows and not summaries and not digest,
    }


def alternating_turns(turns: Sequence[Mapping[str, str]], request: str) -> Tuple[str, List[Dict[str, str]]]:
    """Strict user/assistant alternation ending with ``request`` as the final user turn.

    Gemini rejects consecutive same-role turns and a leading model turn, so same-role
    neighbours are joined and a leading assistant reply is handed back for the memory block.
    """
    merged: List[Dict[str, str]] = []
    for turn in list(turns) + [{"role": "user", "content": request}]:
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
        connection.commit()
        return int(cursor.lastrowid), version


def artifact_by_id(artifact_id: int) -> Optional[sqlite3.Row]:
    with _open_database() as connection:
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


def export_artifact(artifact_id: int) -> Tuple[str, str]:
    """Return (suggested_filename, body) for a download or copy-out."""
    row = artifact_by_id(artifact_id)
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


def record_route(
    project_scope: str, workspace: str, task_type: str, route: str, mode: str, ms: int, finish: str = "", reason: str = ""
) -> int:
    """One row per send: where it went, how long it took, how it ended, why the solver chose it."""
    scope = project_scope.strip() or "default"
    with _open_database() as connection:
        cursor = connection.execute(
            """
            INSERT INTO route_log (timestamp, project_scope, workspace, task_type, route, mode, ms, finish, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (time.time(), scope, (workspace or "")[:40], (task_type or "")[:40], (route or "")[:120], (mode or "normal")[:40],
             max(0, int(ms)), (finish or "")[:40], redact_secrets(reason or "")[:300]),
        )
        connection.commit()
        return int(cursor.lastrowid)


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
            "last_seen": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(bucket["last"])),
        })
    stats.sort(key=lambda item: (-item["sends"], item["route"]))
    return stats


def routes_csv(project_scope: str, limit: int = 5000) -> str:
    header = "id,timestamp_utc,workspace,task_type,route,mode,ms,finish,reason"
    lines = [header]
    for row in reversed(recent_routes(project_scope, limit=limit)):
        reason = str(row["reason"]).replace('"', "'").replace("\n", " ")
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(row["timestamp"])))
        lines.append(f"{row['id']},{stamp},{row['workspace']},{row['task_type']},{row['route']},{row['mode']},{row['ms']},{row['finish']},\"{reason}\"")
    return "\n".join(lines) + "\n"


def health_check() -> Dict[str, Any]:
    """Zero-cost liveness for the ?health=1 view and the smoke drive: schema present, counts readable."""
    try:
        with _open_database() as connection:
            threads = int(connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0])
            messages = int(connection.execute("SELECT COUNT(*) FROM message_history").fetchone()[0])
            artifacts = int(connection.execute("SELECT COUNT(*) FROM artifact_store").fetchone()[0])
        return {"ok": True, "path": str(database_path()), "threads": threads, "messages": messages, "artifacts": artifacts, "error": ""}
    except Exception as exc:
        return {"ok": False, "path": str(database_path()), "threads": 0, "messages": 0, "artifacts": 0, "error": f"{type(exc).__name__}: {exc}"[:200]}


def export_thread(thread_id: int) -> Dict[str, Any]:
    """Everything one chat holds, for review or hand-off: thread row, archived + live messages in order, summaries, digest.

    Nothing is written; content was redacted when it was stored, so an export never carries a key.
    """
    thread = thread_by_id(int(thread_id))
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
    return {
        "thread": {name: thread[name] for name in thread.keys()},
        "messages": messages,
        "summaries": summaries,
        "digest": digest,
        "exported_at": time.time(),
    }


def _stamp(value: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(float(value)))
    except (TypeError, ValueError):
        return "-"


def thread_transcript(thread_id: int, fmt: str = "markdown") -> Tuple[str, str]:
    """Return (filename, body) for a chat download: ``markdown`` for reading, ``json`` for tooling."""
    payload = export_thread(thread_id)
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
HEALTH_TOKEN_LIMIT = 18_000       # estimated tokens in the live window
HEALTH_SUMMARY_LIMIT = 2          # texturized blocks already stacked on this thread
MIN_DIGEST_MESSAGES = 4           # a thread needs this many real messages before it can be migrated at all
DIGEST_MAX_CHARACTERS = 5_000

_DECISION_RE = re.compile(r"(?i)\b(?:decid\w*|agree\w*|decision\w*|constraint\w*|requir\w*|polic\w*|approv\w*|must|never|always|rule)\b")
_OPEN_RE = re.compile(r"(?i)\b(todo|next step|open question|unresolved|pending|follow[- ]up|blocked)\b|\?\s*$")
_FACT_RE = re.compile(r"[\w/.-]+\.(py|js|ts|md|json|yml|yaml|toml|sql|html|css)\b|\b\d+(\.\d+)?\s*(%|rpm|tpm|tokens|ms|s)\b", re.I)


def _normalized(text: str) -> str:
    return re.sub(r"\W+", " ", text.lower()).strip()


MIN_MESSAGES_AFTER_MIGRATION = 12  # a fresh successor must earn some history before it can migrate again


def thread_health(project_scope: str, thread_id: Optional[int] = None, workspace: Optional[str] = None) -> Dict[str, Any]:
    """Cheap, deterministic sweep of one thread: load, repetition, and a migration recommendation."""
    scope = project_scope.strip() or "default"
    thread = thread_by_id(thread_id) if thread_id is not None else active_thread(scope, workspace)
    resolved = int(thread["id"])
    rows = [row for row in recent_messages(scope, MESSAGE_WINDOW, thread_id=resolved) if row["role"] != "system"]
    summaries = recent_summaries(scope, 50, thread_id=resolved)
    archived = archived_messages(scope, 5000, thread_id=resolved)
    tokens = sum(int(row["token_count"]) for row in rows)
    user_turns = [_normalized(row["content"])[:200] for row in rows if row["role"] == "user"]
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
    project_scope: str, thread_id: Optional[int] = None, max_characters: int = DIGEST_MAX_CHARACTERS, workspace: Optional[str] = None
) -> str:
    """Deterministic compression of a thread into the material a fresh thread needs.

    Sections: vision (how the thread started), decisions and constraints, key
    facts (files, numbers), open items, artifacts locked in the scope, and the
    stacked summaries. Zero provider quota; a model may refine it afterwards.
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

    first_user = next((row["content"].strip() for row in rows if row["role"] == "user"), "")
    decisions: List[str] = []
    facts: List[str] = []
    open_items: List[str] = []
    for row in rows:
        for line in str(row["content"]).splitlines():
            stripped = line.strip()
            if not stripped or len(stripped) > 400:
                continue
            if _DECISION_RE.search(stripped):
                decisions.append(stripped)
            elif _OPEN_RE.search(stripped):
                open_items.append(stripped)
            elif _FACT_RE.search(stripped):
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
        connection.commit()
    append_message(
        scope,
        "system",
        f"Thread migrated from #{old_id} ({thread['title']}). Vision digest v{version} locked as artifact {artifact_id} "
        f"({method}); it is injected into every prompt on this thread.",
        thread_id=new_id,
    )
    return {"old_thread_id": old_id, "new_thread_id": new_id, "digest_artifact_id": artifact_id, "method": method, "digest": digest}
