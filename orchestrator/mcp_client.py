"""A dependency-free MCP client over stdio (JSON-RPC 2.0, newline-delimited).

Servers are declared in ``mcp_servers.yaml`` and run where the process runs (the VM worker
container). A session spawns the server, performs the initialize handshake, lists or calls tools,
and closes it. Errors are redacted before they are shown.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from collections import deque
from typing import Any, Deque, Dict, List, Mapping, Optional, Sequence

from .config import resolve_secret
from .envsafe import minimal_env
from .vault import redact_secrets

PROTOCOL_VERSION = "2024-11-05"
STARTUP_TIMEOUT = 60.0  # first-run downloads (npx -y …) take longer than a tool call
DEFAULT_SERVERS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mcp_servers.yaml")
DEFAULT_TIMEOUT = 30.0


class MCPError(RuntimeError):
    pass


def load_servers(path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Server declarations: name, command, args, env (literal values or ``env:NAME`` resolved at call time)."""
    target = path or os.environ.get("CHAT_JOHNSON_MCP_SERVERS", "").strip() or DEFAULT_SERVERS_PATH
    if not os.path.isfile(target):
        return []
    try:
        import yaml
    except ImportError:
        return []
    with open(target, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    servers = []
    for raw in data.get("servers", []) or []:
        if not isinstance(raw, Mapping) or not raw.get("name") or not raw.get("command"):
            continue
        servers.append({
            "name": str(raw["name"]), "command": str(raw["command"]), "args": [str(a) for a in (raw.get("args") or [])],
            "env": {str(k): str(v) for k, v in (raw.get("env") or {}).items()}, "description": str(raw.get("description") or ""),
        })
    return servers


def _resolve_env(env: Mapping[str, str]) -> Dict[str, str]:
    resolved: Dict[str, str] = {}
    for name, value in env.items():
        resolved[name] = resolve_secret(value[4:]) if value.startswith("env:") else value
    return resolved


class MCPSession:
    """One server process; use as a context manager."""

    def __init__(self, server: Mapping[str, Any], timeout: float = DEFAULT_TIMEOUT, startup_timeout: float = STARTUP_TIMEOUT) -> None:
        self.server = dict(server)
        self.timeout = float(timeout)
        self.startup_timeout = float(startup_timeout)
        self._next_id = 0
        self._lines: "queue.Queue[Optional[str]]" = queue.Queue()
        self.stderr_tail: Deque[str] = deque(maxlen=40)
        # A server starts from a minimal environment: only what its declaration names reaches it, never the worker's keys.
        env = minimal_env(_resolve_env(self.server.get("env") or {}))
        try:
            self.process = subprocess.Popen(
                [self.server["command"], *self.server.get("args", [])], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, bufsize=1, env=env,
            )
        except OSError as exc:
            raise MCPError(f"cannot start MCP server {self.server.get('name')}: {redact_secrets(str(exc))[:200]}") from exc
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()
        self._err_reader = threading.Thread(target=self._pump_stderr, daemon=True)
        self._err_reader.start()
        self.server_info: Dict[str, Any] = {}

    def _pump(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def _pump_stderr(self) -> None:
        assert self.process.stderr is not None
        for line in self.process.stderr:
            self.stderr_tail.append(line.rstrip()[:300])

    def _stderr_note(self) -> str:
        tail = " | ".join(line for line in list(self.stderr_tail)[-5:] if line.strip())
        return f" (server stderr: {redact_secrets(tail)[:400]})" if tail else ""

    def _send(self, message: Mapping[str, Any]) -> None:
        assert self.process.stdin is not None
        try:
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise MCPError(f"MCP server {self.server.get('name')} closed its input: {exc}") from exc

    def notify(self, method: str, params: Optional[Mapping[str, Any]] = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": dict(params or {})})

    def request(self, method: str, params: Optional[Mapping[str, Any]] = None, timeout: Optional[float] = None) -> Dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        wait = float(timeout if timeout is not None else self.timeout)
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params or {})})
        while True:
            try:
                line = self._lines.get(timeout=wait)
            except queue.Empty as exc:
                raise MCPError(f"MCP server {self.server.get('name')} did not answer {method} within {int(wait)} s{self._stderr_note()}") from exc
            if line is None:
                raise MCPError(f"MCP server {self.server.get('name')} exited during {method}{self._stderr_note()}")
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue  # not JSON-RPC (a log line on stdout); skip it
            if message.get("id") != request_id:
                continue  # a notification or another id
            if "error" in message:
                error = message["error"] or {}
                raise MCPError(f"{method} failed: {redact_secrets(str(error.get('message', error)))[:300]}")
            result = message.get("result")
            return dict(result) if isinstance(result, Mapping) else {"value": result}

    def initialize(self) -> Dict[str, Any]:
        self.server_info = self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "chat-johnson", "version": "1.0"},
        }, timeout=self.startup_timeout)
        self.notify("notifications/initialized")
        return self.server_info

    def list_tools(self) -> List[Dict[str, Any]]:
        return [dict(t) for t in self.request("tools/list").get("tools", [])]

    def call_tool(self, name: str, arguments: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        return self.request("tools/call", {"name": name, "arguments": dict(arguments or {})})

    def close(self) -> None:
        try:
            if self.process.stdin:
                self.process.stdin.close()
            self.process.terminate()
            self.process.wait(timeout=5)
        except Exception:
            try:
                self.process.kill()
            except Exception:
                pass

    def __enter__(self) -> "MCPSession":
        try:
            self.initialize()
        except BaseException:
            self.close()  # a failed handshake must not leave the server process behind
            raise
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def result_text(result: Mapping[str, Any]) -> str:
    """The text content of a tools/call result, joined; other content types are named."""
    parts: List[str] = []
    for item in result.get("content", []) or []:
        if isinstance(item, Mapping):
            if item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            else:
                parts.append(f"[{item.get('type', 'content')}]")
    if not parts and "value" in result:
        parts.append(json.dumps(result["value"])[:2000])
    return "\n".join(parts).strip()


def find_server(name: str, servers: Optional[Sequence[Mapping[str, Any]]] = None) -> Dict[str, Any]:
    for server in servers if servers is not None else load_servers():
        if server.get("name") == name:
            return dict(server)
    raise MCPError(f"no MCP server named {name!r} is declared")


def call(server_name: str, tool: str, arguments: Optional[Mapping[str, Any]] = None, servers: Optional[Sequence[Mapping[str, Any]]] = None, timeout: float = DEFAULT_TIMEOUT, startup_timeout: float = STARTUP_TIMEOUT) -> Dict[str, Any]:
    """Spawn the server, call one tool, close it; returns the raw result plus its joined text."""
    with MCPSession(find_server(server_name, servers), timeout=timeout, startup_timeout=startup_timeout) as session:
        result = session.call_tool(tool, arguments)
    return {"result": result, "text": result_text(result), "is_error": bool(result.get("isError"))}


def probe(server: Mapping[str, Any], timeout: float = 15.0) -> Dict[str, Any]:
    try:
        with MCPSession(server, timeout=timeout, startup_timeout=max(timeout, 30.0)) as session:
            tools = session.list_tools()
        return {"ok": True, "tools": [str(t.get("name", "")) for t in tools], "error": ""}
    except MCPError as exc:
        return {"ok": False, "tools": [], "error": str(exc)}
