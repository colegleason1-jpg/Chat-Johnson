#!/usr/bin/env python
"""Post-deploy smoke test for Chat Johnson: the live app answers, the build matches, the chat surface is there.

Usage: DEPLOY_URL=https://your-app.streamlit.app [EXPECTED_SHA=<commit>] python scripts/smoke_drive.py
Exit 0 on success, 1 on a failed check, 2 when DEPLOY_URL is unset (the CI job treats 2 as "not configured").
Needs playwright with chromium (pip install playwright && playwright install --with-deps chromium).
"""
from __future__ import annotations

import json
import os
import sys
import time


def check_health_payload(text: str, expected_sha: str) -> list[str]:
    """Pure check on the JSON the ?health=1 view prints; returns human-readable failures."""
    failures: list[str] = []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return [f"health view did not print JSON: {exc}"]
    if payload.get("status") != "ok":
        failures.append(f"status is {payload.get('status')!r}: {payload.get('vault', {}).get('error', '')}")
    build = str(payload.get("build", ""))
    if expected_sha:
        if build == "unknown":
            failures.append("build marker is 'unknown' (set CHAT_JOHNSON_BUILD in the deploy environment to verify versions)")
        elif not expected_sha.startswith(build) and not build.startswith(expected_sha[:7]):
            failures.append(f"build {build} does not match expected {expected_sha[:7]}")
    return failures


def main() -> int:
    url = os.environ.get("DEPLOY_URL", "").strip().rstrip("/")
    expected = os.environ.get("EXPECTED_SHA", "").strip()
    if not url:
        print("DEPLOY_URL is not set; nothing to smoke-test.")
        return 2
    from playwright.sync_api import sync_playwright  # imported late so the unset case needs no browser

    failures: list[str] = []
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(args=["--no-sandbox"])
        except Exception:
            # A preinstalled browser that does not match the package's expected build (container images).
            fallback = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE", "/opt/pw-browsers/chromium")
            browser = p.chromium.launch(executable_path=fallback, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1200, "height": 900})
        started = time.time()
        page.goto(f"{url}/?health=1", wait_until="networkidle", timeout=90_000)
        page.wait_for_selector("[data-testid='stCode'], pre", timeout=60_000)
        text = page.locator("[data-testid='stCode'], pre").first.inner_text()
        failures += check_health_payload(text, expected)
        print(f"health view answered in {time.time() - started:.1f}s: {text[:300]}")
        page.goto(url, wait_until="networkidle", timeout=90_000)
        page.wait_for_selector("text=Chat Johnson Master Studio", timeout=60_000)
        switch = page.locator("[data-testid='stSegmentedControl'] button, [data-testid='stButtonGroup'] button, [data-testid='stRadio'] label").count()
        if switch < 4:
            failures.append(f"workspace switch shows {switch} options, expected 4")
        if page.locator("[data-testid='stChatInput'] textarea").count() != 1:
            failures.append("pinned chat bar is missing")
        browser.close()
    for failure in failures:
        print(f"FAIL {failure}")
    print("smoke test " + ("passed" if not failures else f"failed with {len(failures)} problem(s)"))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
