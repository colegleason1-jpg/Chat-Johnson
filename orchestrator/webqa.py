"""Web QA: HTTP checks of a deployed URL anywhere, browser checks where Chromium exists (the VM worker)."""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import tempfile
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.parse import urljoin, urlsplit

import requests

URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")
TIMEOUT = 15
MAX_REDIRECTS = 5
_BLOCKED_SUFFIXES = (".internal", ".local", ".localhost", ".lan", ".home", ".corp")


def unsafe_target(url: str, allow_private: bool = False) -> str:
    """Why a URL must not be fetched from here ("" when it may be): only public http(s) hosts are checked.

    A deployed app can reach the VM's own services (Ollama, the worker), cloud metadata, and the
    loopback interface; a check pointed there would be a scanner, not QA.
    """
    try:
        parts = urlsplit(str(url or "").strip())
    except ValueError:
        return "not a valid URL"
    if parts.scheme not in ("http", "https"):
        return f"only http(s) URLs are checked, not {parts.scheme or 'a bare path'}"
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return "the URL has no host"
    if not allow_private and (host == "localhost" or host.endswith(_BLOCKED_SUFFIXES) or "." not in host):
        return f"{host} is a private or local name"
    if parts.username or parts.password:
        return "credentials in the URL are not allowed"
    if allow_private:
        return ""  # the operator's own VM may check its own services; tests use a loopback server
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return f"{host} does not resolve"
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved or address.is_multicast or address.is_unspecified:
            return f"{host} resolves to a private or local address"
    return ""


def first_url(text: str) -> str:
    match = URL_RE.search(text or "")
    return match.group(0).rstrip(".,;") if match else ""


def check_url(url: str, expect_status: int = 200, expect_text: str = "", timeout: int = TIMEOUT, allow_private: bool = False) -> Dict[str, Any]:
    """Status, latency, text presence, and the parsed health payload when the page is the ``?health=1`` view."""
    result: Dict[str, Any] = {"url": url, "ok": False, "status": None, "elapsed_ms": None, "text_found": None, "health": None, "error": ""}
    reason = unsafe_target(url, allow_private)
    if reason:
        result["error"] = f"refused: {reason}"
        return result
    started = time.perf_counter()
    try:
        current = url
        response = None
        for _ in range(MAX_REDIRECTS + 1):
            # Redirects are followed by hand so every hop passes the same host check.
            response = requests.get(current, timeout=timeout, headers={"User-Agent": "ChatJohnson-WebQA/1.0"}, allow_redirects=False)
            location = response.headers.get("Location") if 300 <= response.status_code < 400 else None
            if not location:
                break
            current = urljoin(current, location)
            reason = unsafe_target(current, allow_private)
            if reason:
                result["error"] = f"refused redirect to {current}: {reason}"
                return result
        assert response is not None
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


def _screenshot_path(name: Any) -> str:
    """Screenshots land under one temp directory, never at a path the step names."""
    folder = os.path.join(tempfile.gettempdir(), "chat-johnson-webqa")
    os.makedirs(folder, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.basename(str(name)))[:80] or "page"
    return os.path.join(folder, safe if safe.endswith(".png") else safe + ".png")


def browser_check(url: str, steps: Sequence[Mapping[str, Any]], timeout: int = 60, allow_private: bool = False) -> Dict[str, Any]:
    """Drive a real browser through ``steps`` (goto, expect_text, click, fill, screenshot); honest when no browser exists."""
    reason = unsafe_target(url, allow_private)
    if reason:
        return {"available": browser_available(), "ok": False, "steps": [], "error": f"refused: {reason}"}
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
                        blocked = unsafe_target(str(step["goto"]), allow_private)
                        if blocked:
                            raise ValueError(f"refused: {blocked}")
                        page.goto(str(step["goto"]), wait_until="networkidle")
                    elif "expect_text" in step:
                        entry["ok"] = str(step["expect_text"]) in page.inner_text("body")
                    elif "click" in step:
                        page.click(str(step["click"]))
                    elif "fill" in step:
                        page.fill(str(step["fill"]), str(step.get("value", "")))
                    elif "screenshot" in step:
                        page.screenshot(path=_screenshot_path(step["screenshot"]), full_page=True)
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
