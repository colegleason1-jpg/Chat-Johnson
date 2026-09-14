"""Connector nodes: built-in actions a mission node can run without a model call, and the offline validator.

Every connector is ``fn(ctx, config, outputs, extras) -> str``; ``required`` names the config keys the
validator checks before Launch, ``needs_token`` marks GitHub writes that need the session token.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import mcp_client, vault
from .deploykit import KitSpec, generate_kit, summarize, validate_kit
from .github_push import GitHubWriter, PushRecord, branch_name_for
from .github_repo import fetch_tree
from .webqa import browser_available, browser_check, check_markdown, check_url

Outputs = Sequence[Tuple[str, str]]


def _last_output(outputs: Outputs) -> str:
    return outputs[-1][1] if outputs else ""


def deploy_kit_generate(ctx: Any, config: Mapping[str, Any], outputs: Outputs, extras: Dict[str, Any]) -> str:
    allowed = {k: v for k, v in config.items() if k in KitSpec.__dataclass_fields__}
    spec = KitSpec(**allowed).normalized()
    files = generate_kit(spec)
    findings = validate_kit(files)
    counts = summarize(findings)
    locked: List[int] = []
    if config.get("lock", True):
        for item in files:
            artifact_id, _ = vault.save_artifact(ctx.project_scope, item.path.rsplit("/", 1)[-1], item.path, item.body, item.language)
            locked.append(int(artifact_id))
    extras.setdefault("kit_files", {})[spec.app_name] = [(item.path, item.body) for item in files]
    extras["kit_artifacts"] = locked
    lines = [f"Deploy kit for {spec.app_name} ({spec.target}): {len(files)} file(s), {counts['ok']} ok, {counts['warn']} warning(s), {counts['error']} error(s)."]
    lines += [f"- {item.path} · {item.purpose}" for item in files]
    return "\n".join(lines)


def github_fetch(ctx: Any, config: Mapping[str, Any], outputs: Outputs, extras: Dict[str, Any]) -> str:
    fetched = fetch_tree(str(config["owner_repo"]), ref=str(config.get("ref") or ""), token=str(ctx.secrets.get("github_token", "")))
    extras["repo_path"] = fetched.path
    extras["repo_fetched"] = fetched.as_dict()
    return f"Fetched {fetched.owner}/{fetched.repo} @ {fetched.ref} ({fetched.sha[:7]}): {fetched.files} file(s), {fetched.size_bytes} bytes" + (" · empty repository" if fetched.empty else "")


def _files_for_push(config: Mapping[str, Any], outputs: Outputs, extras: Dict[str, Any]) -> List[Tuple[str, str]]:
    files: List[Tuple[str, str]] = [(str(f["path"]), str(f["body"])) for f in config.get("files", []) or [] if isinstance(f, Mapping) and f.get("path")]
    for artifact_id in config.get("from_artifacts", []) or []:
        filename, body = vault.export_artifact(int(artifact_id))
        row = vault.artifact_by_id(int(artifact_id))
        files.append((str(row["file_path"]) if row is not None and row["file_path"] else filename, body))
    kit = config.get("from_kit")
    if kit and kit in extras.get("kit_files", {}):
        files.extend(extras["kit_files"][kit])
    return files


def github_push(ctx: Any, config: Mapping[str, Any], outputs: Outputs, extras: Dict[str, Any]) -> str:
    token = str(ctx.secrets.get("github_token", ""))
    if not token:
        raise ValueError("the push node needs the session GitHub token: arm GitHub push in the sidebar before launching")
    files = _files_for_push(config, outputs, extras)
    if not files:
        raise ValueError("the push node has no files: give it files, from_artifacts, or from_kit")
    owner_repo = str(config["owner_repo"])
    branch = str(config.get("branch") or branch_name_for(str(config.get("app_name") or "mission"), files))
    writer = GitHubWriter(token, owner_repo)
    record = writer.push_files(files, branch, str(config.get("message") or "Chat Johnson mission push"), str(config.get("pr_title") or "Chat Johnson mission push"), str(config.get("pr_body") or "Pushed by a mission node; review before merging."))
    extras.setdefault("push_records", []).append(asdict(record))
    return f"Pushed {len(files)} file(s) to {owner_repo} on branch {record.branch}: pull request #{record.pr_number} {record.pr_url}"


def github_revert(ctx: Any, config: Mapping[str, Any], outputs: Outputs, extras: Dict[str, Any]) -> str:
    token = str(ctx.secrets.get("github_token", ""))
    if not token:
        raise ValueError("the revert node needs the session GitHub token")
    raw = config.get("record") or (extras.get("push_records") or [None])[-1]
    if not raw:
        raise ValueError("nothing to revert: no push record in this mission or config")
    record = PushRecord(**raw)
    result = GitHubWriter(token, f"{record.owner}/{record.repo}").open_revert(record)
    return f"Revert pull request #{result.pr_number} opened: {result.pr_url}"


def repository_run(ctx: Any, config: Mapping[str, Any], outputs: Outputs, extras: Dict[str, Any]) -> str:
    from .config import Settings
    from .executor import Orchestrator

    repo_path = str(config.get("path") or extras.get("repo_path") or "")
    if not repo_path:
        raise ValueError("repository.run needs a fetched repository (a github.fetch node before it) or a path")
    settings = Settings(max_test_rounds=int(config.get("test_rounds", 0)), max_output_tokens=int(config.get("max_tokens", 2048)))
    report = Orchestrator(settings=settings, ledger=ctx.ledger).run(str(config["goal"]), repo_path=repo_path)
    extras["pipeline_report"] = {"branch": report.get("branch"), "failed_steps": report.get("failed_steps"), "steps": report.get("steps")}
    lines = [f"Pipeline on {repo_path}: {len(report.get('steps', []))} step(s), {report.get('failed_steps', 0)} failed."]
    lines += [f"- {s['title']}: {s['status']} · {s['note'][:120]}" for s in report.get("steps", [])]
    if report.get("diff"):
        lines.append("\n```diff\n" + str(report["diff"])[:4000] + "\n```")
    return "\n".join(lines)


def vault_export_thread(ctx: Any, config: Mapping[str, Any], outputs: Outputs, extras: Dict[str, Any]) -> str:
    thread_id = int(config.get("thread_id") or ctx.thread_id or 0)
    filename, body = vault.thread_transcript(thread_id, "markdown")
    return f"Transcript {filename} ({len(body)} chars):\n\n" + body[:8000]


def vault_save_artifact(ctx: Any, config: Mapping[str, Any], outputs: Outputs, extras: Dict[str, Any]) -> str:
    body = str(config.get("body") or _last_output(outputs))
    if not body.strip():
        raise ValueError("vault.save_artifact has nothing to save: no body and no earlier output")
    name = str(config.get("name") or "mission-output.md")
    artifact_id, version = vault.save_artifact(ctx.project_scope, name, str(config.get("path") or f"missions/{name}"), body, str(config.get("language") or "markdown"))
    extras.setdefault("saved_artifacts", []).append(int(artifact_id))
    return f"Locked artifact {name} v{version} (id {artifact_id})."


def webqa_check(ctx: Any, config: Mapping[str, Any], outputs: Outputs, extras: Dict[str, Any]) -> str:
    url = str(config["url"])
    result = check_url(url, expect_status=int(config.get("expect_status", 200)), expect_text=str(config.get("expect_text") or ""))
    browser = browser_check(url, [{"expect_text": str(config["expect_text"])}] if config.get("expect_text") else []) if config.get("browser") and browser_available() else None
    extras["webqa"] = {"http": result, "browser": browser}
    return check_markdown(result, browser)


def mcp_call(ctx: Any, config: Mapping[str, Any], outputs: Outputs, extras: Dict[str, Any]) -> str:
    arguments = config.get("arguments") or {}
    if isinstance(arguments, str):
        arguments = json.loads(arguments)
    result = mcp_client.call(str(config["server"]), str(config["tool"]), arguments)
    extras.setdefault("mcp_results", []).append(result["result"])
    if result["is_error"]:
        raise RuntimeError(f"MCP tool {config['tool']} reported an error: {result['text'][:300]}")
    return result["text"] or "(the tool returned no text)"


Connector = Callable[[Any, Mapping[str, Any], Outputs, Dict[str, Any]], str]
CONNECTORS: Dict[str, Dict[str, Any]] = {
    "deploy_kit.generate": {"fn": deploy_kit_generate, "required": (), "needs_token": False, "description": "generate and lock a Deploy Kit (config: app_name, target, cloud, …)"},
    "github.fetch": {"fn": github_fetch, "required": ("owner_repo",), "needs_token": False, "description": "fetch a repository tree (config: owner_repo, ref)"},
    "github.push": {"fn": github_push, "required": ("owner_repo",), "needs_token": True, "description": "push files as a branch + pull request (config: owner_repo, files | from_artifacts | from_kit, message)"},
    "github.revert": {"fn": github_revert, "required": (), "needs_token": True, "description": "open a revert pull request for the last push"},
    "repository.run": {"fn": repository_run, "required": ("goal",), "needs_token": False, "description": "run the sandboxed pipeline on a fetched repository (config: goal, test_rounds)"},
    "vault.export_thread": {"fn": vault_export_thread, "required": (), "needs_token": False, "description": "export this chat's transcript"},
    "vault.save_artifact": {"fn": vault_save_artifact, "required": (), "needs_token": False, "description": "lock the previous output as an artifact (config: name, path, language)"},
    "webqa.check": {"fn": webqa_check, "required": ("url",), "needs_token": False, "description": "HTTP (and browser) check of a URL (config: url, expect_text, browser)"},
    "mcp.call": {"fn": mcp_call, "required": ("server", "tool"), "needs_token": False, "description": "call a tool on a declared MCP server (config: server, tool, arguments)"},
}


def run_connector(ctx: Any, name: str, config: Mapping[str, Any], outputs: Outputs, extras: Dict[str, Any]) -> str:
    entry = CONNECTORS.get(name)
    if entry is None:
        raise ValueError(f"unknown connector {name!r}")
    return entry["fn"](ctx, config, outputs, extras)


def validate_nodes(plan: Sequence[Mapping[str, Any]], push_armed: bool, mcp_servers: Optional[Sequence[str]] = None) -> List[str]:
    """Reasons a plan cannot launch (empty when it can). Offline: no calls, no side effects."""
    from .missions import EXECUTORS, FAILURE_POLICIES, OUTPUTS

    reasons: List[str] = []
    ids = {int(node.get("id", i + 1)) for i, node in enumerate(plan)}
    for index, node in enumerate(plan, start=1):
        label = f"step {node.get('id', index)} ({node.get('title', '')})"
        executor = str(node.get("executor") or "model")
        if executor not in EXECUTORS:
            reasons.append(f"{label}: unknown executor {executor!r}")
            continue
        config = node.get("config") or {}
        if executor == "connector":
            name = str(config.get("connector") or "")
            entry = CONNECTORS.get(name)
            if entry is None:
                reasons.append(f"{label}: unknown connector {name!r}")
                continue
            missing = [key for key in entry["required"] if not config.get(key)]
            if missing:
                reasons.append(f"{label}: {name} needs {', '.join(missing)}")
            if entry["needs_token"] and not push_armed:
                reasons.append(f"{label}: {name} needs the GitHub push slot armed in the sidebar")
            if name == "mcp.call" and mcp_servers is not None and config.get("server") not in mcp_servers:
                reasons.append(f"{label}: no MCP server named {config.get('server')!r} is declared")
        if executor == "sub_mission" and not str(config.get("statement") or "").strip():
            reasons.append(f"{label}: the sub-mission needs a statement")
        if str(node.get("output") or "chat") not in OUTPUTS:
            reasons.append(f"{label}: output must be one of {', '.join(OUTPUTS)}")
        if str(node.get("on_failure") or "stop") not in FAILURE_POLICIES:
            reasons.append(f"{label}: on_failure must be one of {', '.join(FAILURE_POLICIES)}")
        for ref in node.get("inputs") or []:
            try:
                ref_id = int(ref)
            except (TypeError, ValueError):
                reasons.append(f"{label}: inputs must be step numbers")
                continue
            if ref_id not in ids or ref_id >= int(node.get("id", index)):
                reasons.append(f"{label}: input {ref_id} must be an earlier step")
    return reasons
