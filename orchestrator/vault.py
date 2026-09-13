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
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import List, Optional, Tuple


DEFAULT_DB_PATH = "chat_johnson_vault.db"
MESSAGE_WINDOW = 200


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

CREATE TRIGGER IF NOT EXISTS message_history_rolling_cap
AFTER INSERT ON message_history
BEGIN
    DELETE FROM message_history
    WHERE id IN (
        SELECT id
        FROM message_history
        WHERE project_scope = NEW.project_scope
        ORDER BY timestamp DESC, id DESC
        LIMIT -1 OFFSET 200
    );
END;
"""


def _open_database() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def initialize_database() -> None:
    """Create the local schema and rolling-window trigger idempotently."""
    with _open_database() as connection:
        connection.executescript(SCHEMA_SQL)


def estimate_tokens(text: str) -> int:
    return max(1, len(text.strip()) // 4) if text.strip() else 0


_SECRET_SHAPES = (
    re.compile(r"(?i)(api[_ -]?key|client[_ -]?secret|access[_ -]?token|bearer)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"\b(sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,}|AIza[A-Za-z0-9_-]{20,})\b"),
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
) -> int:
    """Insert a scoped message; the SQLite trigger retains the newest 200."""
    safe_role = role if role in {"user", "assistant", "system"} else "user"
    safe_content = redact_secrets(content)
    with _open_database() as connection:
        cursor = connection.execute(
            """
            INSERT INTO message_history
                (role, content, timestamp, token_count, project_scope, provider, mode)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                safe_role,
                safe_content,
                time.time(),
                estimate_tokens(safe_content),
                project_scope.strip() or "default",
                provider[:120],
                mode[:40],
            ),
        )
        return int(cursor.lastrowid)


def recent_messages(project_scope: str, limit: int = MESSAGE_WINDOW) -> List[sqlite3.Row]:
    bounded_limit = max(1, min(int(limit), MESSAGE_WINDOW))
    with _open_database() as connection:
        return list(
            connection.execute(
                """
                SELECT * FROM (
                    SELECT * FROM message_history
                    WHERE project_scope = ?
                    ORDER BY timestamp DESC, id DESC
                    LIMIT ?
                )
                ORDER BY timestamp ASC, id ASC
                """,
                (project_scope.strip() or "default", bounded_limit),
            ).fetchall()
        )


def context_block(project_scope: str, max_characters: int = 24_000) -> str:
    """Build a bounded prompt context from the active project's local window."""
    rows = recent_messages(project_scope, MESSAGE_WINDOW)
    if not rows:
        return "(no prior messages in this project scope)"
    lines: List[str] = []
    used = 0
    for row in rows:
        line = f"{row['role'].upper()}: {row['content']}"
        if used + len(line) + 1 > max_characters:
            break
        lines.append(line)
        used += len(line) + 1
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
