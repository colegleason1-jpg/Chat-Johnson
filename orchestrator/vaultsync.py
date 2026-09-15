"""Vault snapshots to Supabase Storage: the SQLite vault survives a redeploy that wipes the container.

Streamlit Community Cloud replaces the container, and its disk, on every push. This module keeps a
gzip snapshot of the vault in a private Supabase Storage bucket and restores it when the app starts
on an empty vault, so the learner's priors, the outcome log, the recall index, and every chat carry
over. On the VM the same path is the offsite backup.

- The snapshot is taken with SQLite's online backup API, so it is consistent under WAL while the app
  is writing; it is gzipped and uploaded with ``x-upsert`` to one object.
- Restore happens only when the local vault is fresh (no file, or no threads and no artifacts), never
  over a vault that already holds work; ``restore_now`` forces it from the sidebar after confirmation.
- A daemon thread uploads every ``CHAT_JOHNSON_VAULT_SNAPSHOT_MINUTES`` (default 10) when the vault
  changed since the last upload; the app and the worker each run one.
- The service key is read from the environment (Streamlit secrets are mapped to it by the app) and
  never logged, stored, or shown; the bucket must be private.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import requests

ENV_URL = "CHAT_JOHNSON_SUPABASE_URL"
ENV_KEY = "CHAT_JOHNSON_SUPABASE_KEY"
ENV_BUCKET = "CHAT_JOHNSON_VAULT_BUCKET"
ENV_OBJECT = "CHAT_JOHNSON_VAULT_OBJECT"
ENV_INTERVAL = "CHAT_JOHNSON_VAULT_SNAPSHOT_MINUTES"
ENV_EXPORT_TABLE = "CHAT_JOHNSON_CHAT_EXPORT_TABLE"
DEFAULT_EXPORT_TABLE = "chat_exports"
DEFAULT_BUCKET = "chat-johnson-vault"
DEFAULT_OBJECT = "vault/chat_johnson_vault.db.gz"
DEFAULT_INTERVAL_MINUTES = 10
TIMEOUT_SECONDS = 60
MAX_SNAPSHOT_BYTES = 200 * 1024 * 1024

_STATE: Dict[str, Any] = {
    "last_upload_at": 0.0, "last_upload_bytes": 0, "last_restore_at": 0.0, "last_restore_bytes": 0,
    "last_error": "", "uploads": 0, "thread": None, "snapshot_mtime": 0.0, "last_export_at": 0.0, "last_export_threads": 0,
}
_LOCK = threading.Lock()


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def configured() -> bool:
    return bool(_env(ENV_URL)) and bool(_env(ENV_KEY))


def _database_path() -> Path:
    from .vault import database_path

    return database_path()


def _object_url(kind: str = "") -> str:
    base = _env(ENV_URL).rstrip("/")
    bucket = _env(ENV_BUCKET, DEFAULT_BUCKET)
    key = _env(ENV_OBJECT, DEFAULT_OBJECT).lstrip("/")
    return f"{base}/storage/v1/object/{kind}{bucket}/{key}" if kind else f"{base}/storage/v1/object/{bucket}/{key}"


def _headers(content_type: Optional[str] = None) -> Dict[str, str]:
    key = _env(ENV_KEY)
    headers = {"Authorization": f"Bearer {key}", "apikey": key}
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def snapshot_bytes() -> bytes:
    """A consistent gzip copy of the vault via SQLite's online backup (safe while the app writes)."""
    source = _database_path()
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "snapshot.db"
        src = sqlite3.connect(str(source), timeout=30.0)
        try:
            dst = sqlite3.connect(str(copy))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        raw = copy.read_bytes()
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as gz:
        gz.write(raw)
    return buffer.getvalue()


def vault_is_fresh() -> bool:
    """True when nothing worth keeping exists locally: no file, or no threads and no artifacts."""
    path = _database_path()
    if not path.exists():
        return True
    try:
        connection = sqlite3.connect(str(path), timeout=30.0)
        try:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
            if "threads" not in tables:
                return True
            threads = int(connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0])
            artifacts = int(connection.execute("SELECT COUNT(*) FROM artifact_store").fetchone()[0]) if "artifact_store" in tables else 0
            return threads == 0 and artifacts == 0
        finally:
            connection.close()
    except sqlite3.Error:
        return False


def upload(data: bytes) -> Dict[str, Any]:
    if len(data) > MAX_SNAPSHOT_BYTES:
        raise ValueError(f"snapshot is {len(data)} bytes; the limit is {MAX_SNAPSHOT_BYTES}")
    headers = _headers("application/gzip")
    headers["x-upsert"] = "true"
    response = requests.post(_object_url(), data=data, headers=headers, timeout=TIMEOUT_SECONDS)
    if response.status_code >= 300:
        raise RuntimeError(f"Supabase Storage upload failed: HTTP {response.status_code}: {response.text[:200]}")
    with _LOCK:
        _STATE["last_upload_at"] = time.time()
        _STATE["last_upload_bytes"] = len(data)
        _STATE["uploads"] = int(_STATE["uploads"]) + 1
        _STATE["last_error"] = ""
        _STATE["snapshot_mtime"] = _mtime()
    return {"bytes": len(data), "at": _STATE["last_upload_at"]}


def download() -> Optional[bytes]:
    """The stored snapshot, or None when the bucket holds none yet."""
    response = requests.get(_object_url(), headers=_headers(), timeout=TIMEOUT_SECONDS)
    if response.status_code in (400, 404):
        return None
    if response.status_code >= 300:
        raise RuntimeError(f"Supabase Storage download failed: HTTP {response.status_code}: {response.text[:200]}")
    return response.content


def _mtime() -> float:
    path = _database_path()
    stamps = [p.stat().st_mtime for p in (path, Path(str(path) + "-wal")) if p.exists()]
    return max(stamps) if stamps else 0.0


def snapshot_now() -> Dict[str, Any]:
    """Upload a snapshot immediately; the result carries the error text instead of raising, so callers can show it."""
    if not configured():
        return {"ok": False, "note": "not configured"}
    try:
        result = upload(snapshot_bytes())
    except Exception as exc:  # the vault keeps working when the bucket does not
        with _LOCK:
            _STATE["last_error"] = str(exc)[:300]
        return {"ok": False, "note": str(exc)[:300]}
    return {"ok": True, **result}


def _write_restore(data: bytes) -> int:
    raw = gzip.decompress(data)
    path = _database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(str(path) + ".restore")
    temp.write_bytes(raw)
    check = sqlite3.connect(str(temp))
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("the snapshot failed SQLite's integrity check")
    finally:
        check.close()
    for suffix in ("-wal", "-shm"):
        side = Path(str(path) + suffix)
        if side.exists():
            side.unlink()
    os.replace(str(temp), str(path))
    with _LOCK:
        _STATE["last_restore_at"] = time.time()
        _STATE["last_restore_bytes"] = len(raw)
        _STATE["snapshot_mtime"] = _mtime()
    return len(raw)


def restore_if_fresh() -> str:
    """At startup: bring the stored snapshot in when the local vault is fresh; a note says what happened."""
    if not configured():
        return "snapshots not configured"
    if not vault_is_fresh():
        return "local vault already holds work; not restored"
    try:
        data = download()
        if data is None:
            return "no snapshot in the bucket yet"
        size = _write_restore(data)
    except Exception as exc:
        with _LOCK:
            _STATE["last_error"] = str(exc)[:300]
        return f"restore failed: {str(exc)[:200]}"
    return f"restored {size} bytes from the bucket"


def restore_now() -> Dict[str, Any]:
    """Force a restore over the local vault (the sidebar asks for confirmation first); the app must reload afterwards."""
    if not configured():
        return {"ok": False, "note": "not configured"}
    try:
        data = download()
        if data is None:
            return {"ok": False, "note": "no snapshot in the bucket yet"}
        size = _write_restore(data)
    except Exception as exc:
        with _LOCK:
            _STATE["last_error"] = str(exc)[:300]
        return {"ok": False, "note": str(exc)[:300]}
    return {"ok": True, "bytes": size}


def export_chats(project_scope: str, limit: int = 500) -> Dict[str, Any]:
    """Send every chat of a scope to the ``chat_exports`` table (one row per thread, upserted on scope + thread id)
    so it can be audited with SQL from outside the app. The payload is the chat download's JSON; nothing is deleted."""
    if not configured():
        return {"ok": False, "note": "not configured"}
    from .vault import export_thread, list_threads

    rows = []
    for thread in list_threads(project_scope, include_closed=True, limit=limit):
        payload = export_thread(int(thread["id"]), project_scope)
        rows.append({
            "scope": project_scope, "thread_id": int(thread["id"]), "workspace": str(thread["workspace"] or ""),
            "title": str(thread["title"] or ""), "message_count": len(payload["messages"]), "payload": payload,
        })
    table = _env(ENV_EXPORT_TABLE, DEFAULT_EXPORT_TABLE)
    if not rows:
        return {"ok": True, "threads": 0, "table": table, "note": "no chats in this scope"}
    headers = _headers("application/json")
    headers["Prefer"] = "resolution=merge-duplicates,return=minimal"
    try:
        response = requests.post(
            f"{_env(ENV_URL).rstrip('/')}/rest/v1/{table}", data=json.dumps(rows).encode("utf-8"), headers=headers, timeout=TIMEOUT_SECONDS
        )
        if response.status_code >= 300:
            raise RuntimeError(f"chat export failed: HTTP {response.status_code}: {response.text[:200]}")
    except Exception as exc:
        with _LOCK:
            _STATE["last_error"] = str(exc)[:300]
        return {"ok": False, "note": str(exc)[:300]}
    with _LOCK:
        _STATE["last_export_at"] = time.time()
        _STATE["last_export_threads"] = len(rows)
        _STATE["last_error"] = ""
    return {"ok": True, "threads": len(rows), "table": table}


def dirty() -> bool:
    return _mtime() > float(_STATE["snapshot_mtime"])


def start_background(interval_minutes: Optional[int] = None) -> bool:
    """One daemon uploader per process: every interval, upload when the vault changed. False when already running or not configured."""
    if not configured():
        return False
    with _LOCK:
        if _STATE["thread"] is not None and _STATE["thread"].is_alive():
            return False
    minutes = int(interval_minutes or int(_env(ENV_INTERVAL, str(DEFAULT_INTERVAL_MINUTES)) or DEFAULT_INTERVAL_MINUTES))
    interval = max(60.0, minutes * 60.0)

    def loop() -> None:
        while True:
            time.sleep(interval)
            if dirty():
                snapshot_now()

    thread = threading.Thread(target=loop, name="vault-snapshots", daemon=True)
    with _LOCK:
        _STATE["thread"] = thread
    thread.start()
    return True


def status() -> Dict[str, Any]:
    with _LOCK:
        return {
            "configured": configured(), "bucket": _env(ENV_BUCKET, DEFAULT_BUCKET), "object": _env(ENV_OBJECT, DEFAULT_OBJECT),
            "last_upload_at": float(_STATE["last_upload_at"]), "last_upload_bytes": int(_STATE["last_upload_bytes"]),
            "last_restore_at": float(_STATE["last_restore_at"]), "last_restore_bytes": int(_STATE["last_restore_bytes"]),
            "uploads": int(_STATE["uploads"]), "last_error": str(_STATE["last_error"]),
            "running": bool(_STATE["thread"] is not None and _STATE["thread"].is_alive()), "dirty": dirty() if configured() else False,
            "last_export_at": float(_STATE["last_export_at"]), "last_export_threads": int(_STATE["last_export_threads"]),
        }


def reset_for_tests() -> None:
    with _LOCK:
        _STATE.update({"last_upload_at": 0.0, "last_upload_bytes": 0, "last_restore_at": 0.0, "last_restore_bytes": 0, "last_error": "", "uploads": 0, "thread": None, "snapshot_mtime": 0.0, "last_export_at": 0.0, "last_export_threads": 0})
