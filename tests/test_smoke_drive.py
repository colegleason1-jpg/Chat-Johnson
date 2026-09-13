"""The smoke drive's health-payload check is pure and testable without a browser."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.smoke_drive import check_health_payload


def test_health_payload_checks():
    good = json.dumps({"status": "ok", "build": "abc1234", "vault": {"ok": True}})
    assert check_health_payload(good, "abc1234def") == []
    assert check_health_payload(good, "") == []
    assert any("does not match" in f for f in check_health_payload(good, "fffffff0"))
    unknown = json.dumps({"status": "ok", "build": "unknown"})
    assert any("CHAT_JOHNSON_BUILD" in f for f in check_health_payload(unknown, "abc1234"))
    degraded = json.dumps({"status": "degraded", "build": "abc1234", "vault": {"error": "no such table"}})
    assert any("no such table" in f for f in check_health_payload(degraded, ""))
    assert check_health_payload("not json", "")[0].startswith("health view did not print JSON")
