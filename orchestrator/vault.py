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
from typing import List, Optional, Sequence, Tuple


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
    """Insert a scoped message, then texturize + archive anything beyond the window."""
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
        message_id = int(cursor.lastrowid)
    enforce_window(project_scope)
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
            if re.search(r"(?i)\b(decid|agree|must|never|always|todo|fix|bug|error)\b", stripped) or re.search(
                r"[\w/.-]+\.(py|js|ts|md|json|yml|yaml|toml|sql)\b", stripped
            ):
                keep.append(f"  - {stripped[:160]}")
            if sum(len(item) + 1 for item in keep) > max_characters:
                break
    text = "\n".join(keep)
    return text[:max_characters]


def enforce_window(project_scope: str) -> Optional[int]:
    """Texturize and archive the oldest messages once the scope exceeds the window.

    Returns the new summary id when eviction happened, else ``None``. Raw
    messages are never lost: they move to ``message_archive`` and a summary of
    the block is inserted into ``summaries`` in the same transaction.
    """
    scope = project_scope.strip() or "default"
    with _open_database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        total = int(connection.execute(
            "SELECT COUNT(*) FROM message_history WHERE project_scope = ?", (scope,)
        ).fetchone()[0])
        if total <= MESSAGE_WINDOW:
            connection.commit()
            return None
        overflow = total - MESSAGE_WINDOW
        batch = max(overflow, min(TEXTURIZE_BATCH, total))
        rows = connection.execute(
            """
            SELECT * FROM message_history
            WHERE project_scope = ?
            ORDER BY timestamp ASC, id ASC
            LIMIT ?
            """,
            (scope, batch),
        ).fetchall()
        if not rows:
            connection.commit()
            return None
        summary_text = extractive_summary(rows)
        ids = [int(row["id"]) for row in rows]
        cursor = connection.execute(
            """
            INSERT INTO summaries
                (project_scope, covers_from_id, covers_to_id, message_count, content, method, created_at)
            VALUES (?, ?, ?, ?, ?, 'extractive', ?)
            """,
            (scope, min(ids), max(ids), len(ids), summary_text, time.time()),
        )
        summary_id = int(cursor.lastrowid)
        now = time.time()
        connection.executemany(
            """
            INSERT OR REPLACE INTO message_archive
                (id, role, content, timestamp, token_count, project_scope, provider, mode, archived_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    int(row["id"]), row["role"], row["content"], row["timestamp"], row["token_count"],
                    row["project_scope"], row["provider"], row["mode"], now,
                )
                for row in rows
            ],
        )
        placeholders = ",".join("?" for _ in ids)
        connection.execute(f"DELETE FROM message_history WHERE id IN ({placeholders})", ids)
        connection.commit()
        return summary_id


def recent_summaries(project_scope: str, limit: int = 5) -> List[sqlite3.Row]:
    with _open_database() as connection:
        return list(
            connection.execute(
                """
                SELECT * FROM (
                    SELECT * FROM summaries WHERE project_scope = ?
                    ORDER BY created_at DESC, id DESC LIMIT ?
                ) ORDER BY created_at ASC, id ASC
                """,
                (project_scope.strip() or "default", max(1, min(int(limit), 50))),
            ).fetchall()
        )


def archived_messages(project_scope: str, limit: int = 500) -> List[sqlite3.Row]:
    """Raw history that left the active window; available for authorized retrieval."""
    with _open_database() as connection:
        return list(
            connection.execute(
                """
                SELECT * FROM message_archive WHERE project_scope = ?
                ORDER BY timestamp ASC, id ASC LIMIT ?
                """,
                (project_scope.strip() or "default", max(1, min(int(limit), 5000))),
            ).fetchall()
        )


def retexturize_summary(summary_id: int, new_content: str, method: str = "model") -> None:
    """Replace an extractive summary with a richer one (for example from a free model)."""
    with _open_database() as connection:
        connection.execute(
            "UPDATE summaries SET content = ?, method = ? WHERE id = ?",
            (redact_secrets(new_content)[:6_000], method[:40], int(summary_id)),
        )


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
    summaries = recent_summaries(project_scope, 5)
    if not rows and not summaries:
        return "(no prior messages in this project scope)"
    lines: List[str] = []
    used = 0
    for summary in summaries:
        line = f"[TEXTURIZED SUMMARY of {summary['message_count']} earlier messages]\n{summary['content']}"
        if used + len(line) + 1 > max_characters // 3:
            break
        lines.append(line)
        used += len(line) + 1
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


def artifact_by_id(artifact_id: int) -> Optional[sqlite3.Row]:
    with _open_database() as connection:
        return connection.execute("SELECT * FROM artifact_store WHERE id = ?", (int(artifact_id),)).fetchone()


def search_artifacts(project_scope: str, query: str, limit: int = 20) -> List[sqlite3.Row]:
    """Case-insensitive search over name, path, and structural summary."""
    needle = f"%{query.strip()}%"
    with _open_database() as connection:
        return list(
            connection.execute(
                """
                SELECT id, name, file_path, version, structural_summary, created_at
                FROM artifact_store
                WHERE project_scope = ?
                  AND (name LIKE ? OR file_path LIKE ? OR structural_summary LIKE ?)
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (project_scope.strip() or "default", needle, needle, needle, max(1, min(int(limit), 100))),
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
