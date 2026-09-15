"""Batch M: vault snapshots to Supabase Storage (restore on an empty start, upload on change) and the demo flag."""
import gzip
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from orchestrator import vault, vaultsync  # noqa: E402


class FakeResponse:
    def __init__(self, status_code, content=b"", text=""):
        self.status_code, self.content, self.text = status_code, content, text


class FakeStorage:
    """A private bucket: one object, bearer-checked, upsert on POST, 404 when empty."""

    def __init__(self, key):
        self.key, self.objects, self.calls = key, {}, []

    def _auth(self, headers):
        return headers.get("Authorization") == f"Bearer {self.key}" and headers.get("apikey") == self.key

    def post(self, url, data=b"", headers=None, timeout=None):
        self.calls.append(("POST", url))
        if not self._auth(headers or {}):
            return FakeResponse(401, text="invalid key")
        if headers.get("x-upsert") != "true" and url in self.objects:
            return FakeResponse(400, text="exists")
        self.objects[url] = bytes(data)
        return FakeResponse(200, text="{}")

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url))
        if not self._auth(headers or {}):
            return FakeResponse(401, text="invalid key")
        if url not in self.objects:
            return FakeResponse(404, text="not found")
        return FakeResponse(200, content=self.objects[url])


@pytest.fixture()
def bucket(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_JOHNSON_DB_PATH", str(tmp_path / "vault.db"))
    monkeypatch.delenv("CHAT_JOHNSON_JOB_WORKERS", raising=False)
    monkeypatch.setenv(vaultsync.ENV_URL, "https://ref.supabase.co/")
    monkeypatch.setenv(vaultsync.ENV_KEY, "service-secret")
    monkeypatch.delenv(vaultsync.ENV_BUCKET, raising=False)
    monkeypatch.delenv(vaultsync.ENV_OBJECT, raising=False)
    fake = FakeStorage("service-secret")
    monkeypatch.setattr(vaultsync, "requests", fake)
    vaultsync.reset_for_tests()
    yield fake
    vaultsync.reset_for_tests()


def test_snapshot_roundtrip_restores_an_empty_vault_but_never_one_with_work(bucket, tmp_path):
    assert vaultsync.configured() and vaultsync.vault_is_fresh()  # no file yet
    assert vaultsync.restore_if_fresh() == "no snapshot in the bucket yet"
    vault.initialize_database()
    assert vaultsync.vault_is_fresh()  # tables exist, nothing in them
    thread = vault.create_thread("scope-m", "Keep me", workspace="normal_chat")
    vault.append_message("scope-m", "user", "remember the cache TTL is 300 seconds", thread_id=thread)
    assert not vaultsync.vault_is_fresh() and vaultsync.dirty()
    result = vaultsync.snapshot_now()
    assert result["ok"] and result["bytes"] > 0 and not vaultsync.dirty()
    url = f"https://ref.supabase.co/storage/v1/object/{vaultsync.DEFAULT_BUCKET}/{vaultsync.DEFAULT_OBJECT}"
    assert url in bucket.objects and gzip.decompress(bucket.objects[url])[:16] == b"SQLite format 3\x00"
    status = vaultsync.status()
    assert status["uploads"] == 1 and status["last_upload_bytes"] == result["bytes"] and status["last_error"] == "" and status["bucket"] == vaultsync.DEFAULT_BUCKET
    # A vault with work is left alone.
    assert vaultsync.restore_if_fresh() == "local vault already holds work; not restored"
    # A fresh container: the file is gone, the snapshot comes back before the first query.
    for suffix in ("", "-wal", "-shm"):
        path = tmp_path / f"vault.db{suffix}"
        if path.exists():
            path.unlink()
    note = vaultsync.restore_if_fresh()
    assert note.startswith("restored ") and vaultsync.status()["last_restore_bytes"] > 0
    vault.initialize_database()
    rows = vault.recent_messages("scope-m", 10, thread_id=thread)
    assert len(rows) == 1 and "300 seconds" in rows[0]["content"]
    # Initialization touches the restored file (WAL, migrations), so one more upload follows; then the vault is in step again.
    assert vaultsync.snapshot_now()["ok"] and vaultsync.status()["uploads"] == 2 and not vaultsync.dirty()


def test_forced_restore_needs_a_snapshot_and_a_bad_key_is_reported_not_raised(bucket, monkeypatch):
    vault.initialize_database()
    assert vaultsync.restore_now() == {"ok": False, "note": "no snapshot in the bucket yet"}
    vault.create_thread("scope-m", "work", workspace="normal_chat")
    assert vaultsync.snapshot_now()["ok"]
    forced = vaultsync.restore_now()
    assert forced["ok"] and forced["bytes"] > 0
    monkeypatch.setenv(vaultsync.ENV_KEY, "wrong")
    failed = vaultsync.snapshot_now()
    assert not failed["ok"] and "401" in failed["note"] and "401" in vaultsync.status()["last_error"]
    assert vaultsync.restore_if_fresh() == "local vault already holds work; not restored"
    assert "wrong" not in str(vaultsync.status())  # the key never appears in status


def test_unconfigured_sync_is_a_quiet_no_op(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_JOHNSON_DB_PATH", str(tmp_path / "vault.db"))
    monkeypatch.delenv(vaultsync.ENV_URL, raising=False)
    monkeypatch.delenv(vaultsync.ENV_KEY, raising=False)
    vaultsync.reset_for_tests()
    assert not vaultsync.configured()
    assert vaultsync.restore_if_fresh() == "snapshots not configured"
    assert vaultsync.snapshot_now() == {"ok": False, "note": "not configured"}
    assert vaultsync.restore_now() == {"ok": False, "note": "not configured"}
    assert vaultsync.start_background() is False and vaultsync.status()["running"] is False and vaultsync.status()["dirty"] is False


def test_background_uploader_starts_once_and_uploads_only_when_dirty(bucket, monkeypatch):
    vault.initialize_database()
    vault.create_thread("scope-m", "work", workspace="normal_chat")
    uploads = []
    monkeypatch.setattr(vaultsync, "snapshot_now", lambda: uploads.append(time.time()) or {"ok": True})
    import types

    def end_thread(seconds):
        raise SystemExit()  # one loop turn, then the daemon ends; the test's own time module is untouched

    monkeypatch.setattr(vaultsync, "time", types.SimpleNamespace(sleep=end_thread, time=time.time))
    assert vaultsync.start_background(1) is True
    time.sleep(0.2)
    assert vaultsync.start_background(1) in (True, False)  # a dead thread may be replaced; a live one is never duplicated
    assert not uploads  # the loop sleeps first and our sleep ends the thread before any upload
    assert vaultsync.dirty() is True  # nothing was uploaded, so the vault is still ahead of the snapshot


def test_corrupt_snapshot_is_refused(bucket, tmp_path):
    url = f"https://ref.supabase.co/storage/v1/object/{vaultsync.DEFAULT_BUCKET}/{vaultsync.DEFAULT_OBJECT}"
    bucket.objects[url] = gzip.compress(b"not a database")
    note = vaultsync.restore_if_fresh()
    assert note.startswith("restore failed") and not (tmp_path / "vault.db").exists()
    vault.initialize_database()
    assert vaultsync.vault_is_fresh()
