"""Web QA: HTTP checks of a deployed URL anywhere, browser checks where Chromium exists (the VM worker)."""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence

import requests

URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")
TIMEOUT = 15


def first_url(text: str) -> str:
    match = URL_RE.search(text or "")
    return match.group(0).rstrip(".,;") if match else ""


def check_url(url: str, expect_status: int = 200, expect_text: str = "", timeout: int = TIMEOUT) -> Dict[str, Any]:
    """Status, latency, text presence, and the parsed health payload when the page is the ``?health=1`` view."""
    result: Dict[str, Any] = {"url": url, "ok": False, "status": None, "elapsed_ms": None, "text_found": None, "health": None, "error": ""}
    started = time.perf_counter()
    try:
        response = requests.get(url, timeout=timeout, headers={"User-Agent": "ChatJohnson-WebQA/1.0"})
    except requests.RequestException as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        return result
    result["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    result["status"] = int(response.status_code)
    body = response.text or ""
    if expect_text:
        result["text_found"] = expect_text in body
    stripped = body.strip()
    if stripped.startswith("{"):
        try:
            result["health"] = json.loads(stripped)
        except ValueError:
            result["health"] = None
    result["ok"] = result["status"] == int(expect_status) and (result["text_found"] is not False)
    return result


def browser_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except Exception:
        return False
    return True


def browser_check(url: str, steps: Sequence[Mapping[str, Any]], timeout: int = 60) -> Dict[str, Any]:
    """Drive a real browser through ``steps`` (goto, expect_text, click, fill, screenshot); honest when no browser exists."""
    if not browser_available():
        return {"available": False, "ok": False, "steps": [], "error": "Playwright is not installed here; browser checks run on the VM worker"}
    from playwright.sync_api import sync_playwright

    log: List[Dict[str, Any]] = []
    ok = True
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(args=["--no-sandbox"])
        except Exception:
            fallback = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE", "/opt/pw-browsers/chromium")
            browser = p.chromium.launch(executable_path=fallback, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1200, "height": 900})
        page.set_default_timeout(timeout * 1000)
        try:
            page.goto(url, wait_until="networkidle")
            for step in steps:
                entry = {"step": dict(step), "ok": True}
                try:
                    if "goto" in step:
                        page.goto(str(step["goto"]), wait_until="networkidle")
                    elif "expect_text" in step:
                        entry["ok"] = str(step["expect_text"]) in page.inner_text("body")
                    elif "click" in step:
                        page.click(str(step["click"]))
                    elif "fill" in step:
                        page.fill(str(step["fill"]), str(step.get("value", "")))
                    elif "screenshot" in step:
                        page.screenshot(path=str(step["screenshot"]), full_page=True)
                except Exception as exc:
                    entry["ok"] = False
                    entry["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
                ok = ok and entry["ok"]
                log.append(entry)
        except Exception as exc:
            ok = False
            log.append({"step": {"goto": url}, "ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
        finally:
            browser.close()
    return {"available": True, "ok": ok, "steps": log, "error": ""}


def check_markdown(result: Mapping[str, Any], browser: Optional[Mapping[str, Any]] = None) -> str:
    lines = [
        f"Web QA for {result.get('url', '')}: {'OK' if result.get('ok') else 'FAILED'}",
        "",
        "| check | value |", "|---|---|",
        f"| status | {result.get('status')} |", f"| latency | {result.get('elapsed_ms')} ms |",
        f"| expected text | {result.get('text_found') if result.get('text_found') is not None else 'not checked'} |",
        f"| health | {json.dumps(result['health'])[:200] if result.get('health') else 'not a health view'} |",
    ]
    if result.get("error"):
        lines.append(f"| error | {result['error']} |")
    if browser is not None:
        if not browser.get("available"):
            lines.append(f"| browser | unavailable: {browser.get('error', '')} |")
        else:
            lines.append(f"| browser | {'OK' if browser.get('ok') else 'FAILED'} over {len(browser.get('steps', []))} step(s) |")
    return "\n".join(lines)
