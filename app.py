"""Chat Johnson / Project Seth Master Studio Streamlit dashboard.

The app is intentionally local-first:

* SQLite is the source of truth for scoped messages and immutable artifacts.
* Provider credentials are BYOK environment values and are never persisted.
* Normal mode is a single bounded request; Heavy mode is a bounded
  draft/review/synthesis workflow. Private model reasoning is never displayed.
* Repository work remains reviewable and sandboxed. GitHub authorization is a
  read-oriented OAuth skeleton; it does not collect SSH private keys or push
  changes automatically.
* Generated HTML previews are rendered in an isolated component after removing
  scripts, remote frames, forms, and event-handler attributes.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import platform
import sqlite3
import os
import re
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlencode


# Keep the startup cleanup deliberately narrow and before any application
# services are initialized. Only these three retired case-variant modules are
# targets; the active lowercase app.py and orchestrator package are untouched.
def _sanitize_duplicate_modules() -> List[str]:
    root = Path(__file__).resolve().parent
    removed: List[str] = []
    for filename in ("App.py", "Router.py", "Sandbox.py"):
        candidate = root / filename
        try:
            os.remove(candidate)
            removed.append(filename)
        except FileNotFoundError:
            continue
        except OSError:
            continue
    return removed


SANITIZED_DUPLICATES = _sanitize_duplicate_modules()

import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(page_title="Chat Johnson · Master Studio", page_icon="🧠", layout="wide")

try:
    import requests
    import numpy  # noqa: F401  (surface a broken scientific stack early, with a clear message)
except ImportError as _import_error:  # pragma: no cover - only reachable on a broken deploy
    st.error(
        "A required dependency failed to import: "
        f"{getattr(_import_error, 'name', None) or _import_error}. "
        "Install with `pip install -r requirements.txt` (lowercase file name; hosted platforms "
        "such as Streamlit Community Cloud only detect that exact name) and use Python 3.10+."
    )
    st.stop()

from orchestrator.config import PROVIDERS, bind_session_keys, get_settings, provider_model, resolve_secret
from orchestrator.executor import Orchestrator
from orchestrator.quota import QuotaLedger
from orchestrator.router import (
    CORTEX_ENDPOINTS,
    TASK_TYPES,
    CortexStream,
    PaidReasoningSlot,
    ProviderError,
    byok_status,
    classify,
    cortex_available,
    cortex_wait_seconds,
    endpoint_model,
    generate_mode,
    probe_all_endpoints,
    prompt_context_chars,
    repository_context_chars,
    strip_reasoning_tags,
)


# =============================================================================
# Local source of truth (orchestrator/vault.py)
# =============================================================================

from orchestrator.capabilities import capability_card
from orchestrator.connectors import ROADMAP_FEATURES, connector_status
from orchestrator.deploykit import CLOUDS, LANGUAGES, TARGETS, KitSpec, generate_kit, kit_zip, summarize, validate_kit
from orchestrator.github_auth import mint_state, verify_state
from orchestrator.github_push import GitHubPushError, GitHubWriter, PushRecord, branch_name_for
from orchestrator.github_repo import GitHubRepoError, collect_changed_files, fetch_tree, list_repositories, looks_like_owner_repo, qualify_repository, whoami
from orchestrator.repo_ingest import repo_prompt_context
from orchestrator.missions import (
    MAX_SECTIONS,
    MISSION_TEMPLATES,
    assemble_deliverable,
    classify_mission,
    deliverable_slug,
    mission_hints,
    parse_length_target,
    task_plan,
    text_measure,
    writing_sections,
)
from orchestrator.preview import extract_preview_source, safe_preview_document
from orchestrator.vault import (
    MESSAGE_WINDOW,
    WORKSPACES as VAULT_WORKSPACES,
    active_thread,
    alternating_turns,
    append_message,
    archived_messages,
    clear_thread,
    context_parts,
    delete_thread,
    create_thread,
    export_artifact,
    health_check,
    initialize_database,
    list_threads,
    migrate_thread,
    recent_artifacts,
    recent_messages,
    recent_routes,
    recent_summaries,
    record_route,
    route_stats,
    routes_csv,
    rename_thread,
    save_artifact,
    search_artifacts,
    set_thread_mission,
    switch_thread,
    thread_health,
    thread_transcript,
)

initialize_database()


# =============================================================================
# Provider/runtime helpers
# =============================================================================

@st.cache_resource
def _ledger_registry() -> Dict[str, Tuple[QuotaLedger, threading.Lock]]:
    """Process-wide registry of ledgers, one per distinct credential set."""
    return {}


def credential_fingerprint() -> str:
    """Non-reversible id of the keys in effect for this session (overlay + environment)."""
    from orchestrator.config import resolve_secret as _resolve
    from orchestrator.router import BYOK_ENV_KEYS

    material = "|".join(
        f"{env}:{hashlib.sha256(_resolve(env).encode()).hexdigest()[:16]}"
        for names in BYOK_ENV_KEYS.values() for env in names if _resolve(env)
    )
    return hashlib.sha256(material.encode()).hexdigest()[:16] if material else "no-keys"


def get_quota_ledger() -> QuotaLedger:
    """The ledger for THIS visitor's keys. Two visitors with different keys never share a bucket.

    Buckets are keyed by vendor (one credential = one bucket) and start from
    the legacy registry's ceilings; Cortex tightens them to its stricter policy.
    """
    registry = _ledger_registry()
    fingerprint = credential_fingerprint()
    if fingerprint not in registry:
        from orchestrator.discovery import vendor_for

        limits: Dict[str, Tuple[int, int]] = {}
        for name, cfg in PROVIDERS.items():
            vendor = vendor_for(name)
            rpm, tpm = limits.get(vendor, (cfg.rpm_limit, cfg.tpm_limit))
            limits[vendor] = (min(rpm, cfg.rpm_limit), min(tpm, cfg.tpm_limit))
        registry[fingerprint] = (QuotaLedger(limits), threading.Lock())
    return registry[fingerprint][0]


def get_task_request_lock() -> threading.Lock:
    """Serialize Task Finder provider calls for this visitor's keys around quota check + record."""
    registry = _ledger_registry()
    get_quota_ledger()
    return registry[credential_fingerprint()][1]


@st.cache_resource
def build_marker() -> str:
    """Short git commit of the running checkout, so a deploy can be verified at a glance.

    CHAT_JOHNSON_BUILD wins when set (platforms that strip .git, or a deploy job that knows the sha).
    """
    pinned = os.environ.get("CHAT_JOHNSON_BUILD", "").strip()
    if pinned:
        return pinned[:12]
    try:
        import subprocess

        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        return sha or "unknown"
    except Exception:
        return "unknown"


def configured_provider_names() -> List[str]:
    status = byok_status()
    return [name for name, row in status.items() if row["configured"]]


def active_mode() -> str:
    return "heavy" if st.session_state.get("heavy_mode", False) else "normal"


def session_paid_slot() -> PaidReasoningSlot:
    """Build the per-session paid slot from session state only.

    The key is never read from the environment and never persisted. Both the
    toggle and the key must be supplied in this browser session for the slot
    to arm, and the slot is consulted only by the Heavy Mode critique pass.
    """
    return PaidReasoningSlot(
        api_key=str(st.session_state.get("paid_slot_key", "") or ""),
        model=str(st.session_state.get("paid_slot_model", "o3-mini") or "o3-mini").strip() or "o3-mini",
        enabled=bool(st.session_state.get("paid_slot_enabled", False)),
    )


def github_push_status() -> Dict[str, Any]:
    """Armed when the toggle is on and a token was pasted this session; the target is the connected repository."""
    enabled = bool(st.session_state.get("github_push_enabled", False))
    token = str(st.session_state.get("github_push_token", "") or "").strip()
    fetched = st.session_state.get("repo_fetched") or {}
    repo = f"{fetched['owner']}/{fetched['repo']}" if fetched else ""
    return {"enabled": enabled, "repo": repo, "armed": enabled and bool(token), "connected": bool(repo), "login": github_login(token if enabled else "")}


def github_login(token: str) -> str:
    """The token's GitHub login, looked up once per token (one API call) and remembered for the session."""
    if not token:
        return ""
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
    cache: Dict[str, str] = st.session_state.setdefault("github_login_cache", {})
    if digest not in cache:
        try:
            cache[digest] = whoami(token)
        except GitHubRepoError:
            cache[digest] = ""
    return cache[digest]


def provider_status_rows() -> List[Tuple[str, str, str, bool]]:
    status = byok_status()
    rows: List[Tuple[str, str, str, bool]] = []
    for name, label, env_name in (
        ("google_ai_studio", "Google AI Studio", "GEMINI_API_KEY"),
        ("groq", "Groq Cloud", "GROQ_API_KEY"),
        ("huggingface", "Hugging Face Serverless", "HUGGINGFACE_API_KEY / HF_TOKEN"),
    ):
        model = endpoint_model(CORTEX_ENDPOINTS[name])
        configured = bool(status.get(name, {}).get("configured", False))
        if name == "google_ai_studio":
            configured = bool(status.get("gemini", {}).get("configured", configured))
        rows.append((name, label, f"{model} · {env_name}", configured))
    for name, cfg in PROVIDERS.items():
        if name in {"gemini", "groq"}:
            continue
        configured = bool(resolve_secret(cfg.env_key))
        rows.append((
            name, cfg.label,
            f"{provider_model(cfg)} · {cfg.env_key} · fallback only: used when no Cortex key (Gemini, Groq, Hugging Face) "
            "is set or every Cortex endpoint fails a request",
            configured,
        ))
    return rows


SYSTEM_PERSONA = (
    "You are Chat Johnson, a careful software and strategy assistant. "
    "Return useful, complete output, state uncertainty, and never claim "
    "that generated code is flawless or that a scientific simulation proves "
    "physical propulsion. Do not reveal private chain-of-thought."
)


def build_prompt_messages(
    project_scope: str,
    user_prompt: str,
    injected_context: str = "",
    workspace: Optional[str] = None,
) -> List[Dict[str, str]]:
    """System prompt (persona, capability card, compressed memory) followed by the live window as real turns.

    Earlier turns go in as user/assistant messages rather than a text dump, so the model treats
    them as conversation instead of imitating a transcript format.
    """
    # Sized so the request fits every keyed endpoint's TPM ceiling at the current output budget.
    context_chars = prompt_context_chars(int(st.session_state.get("max_tokens", 2048)))
    parts = context_parts(project_scope, max_characters=context_chars, workspace=workspace)
    request = user_prompt.strip()
    if injected_context.strip():
        request = "USER-CONSENTED FILE INJECTIONS:\n" + injected_context + "\n\nCURRENT REQUEST:\n" + request  # budgeted per file
    leading, turns = alternating_turns(parts["turns"], request)
    memory = parts["memory"]
    if leading:
        memory = (memory + "\n" if memory else "") + "[EARLIER ASSISTANT REPLY]\n" + leading
    system = f"{SYSTEM_PERSONA}\n\n{capability_card()}\n\nACTIVE PROJECT: {project_scope}"
    if memory:
        system += "\n\nPROJECT MEMORY (compressed earlier history, for reference only; never imitate its format):\n" + memory
    return [{"role": "system", "content": system}, *turns]


DIGEST_REFINE_PROMPT = (
    "You compress a project conversation digest so a fresh thread can continue the same work "
    "without the old transcript. Keep every decision, constraint, file name, number, and open item. "
    "Remove repetition, filler, and errors that were already resolved. Output concise markdown under "
    "350 words with the sections: Vision, Decisions and constraints, Key facts, Open items. No commentary."
)


def model_refine_digest(digest: str, ledger: QuotaLedger) -> str:
    """Ask the cheapest available free endpoint to compress the deterministic digest."""
    messages = [{"role": "system", "content": DIGEST_REFINE_PROMPT}, {"role": "user", "content": digest}]
    text, _ = generate_mode("normal", "quick_text", messages, ledger, max_tokens=700, temperature=0.1)
    return text


def health_sweep(
    project_scope: str, ledger: QuotaLedger, force: bool = False, workspace: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Run the thread-health agent; migrate to an optimized successor when warranted.

    Called before every send (and on demand). The digest is built deterministically
    at zero quota, then refined by a model when a key is available. Raw history is
    never deleted; the old thread is marked migrated and stays browsable.
    """
    health = thread_health(project_scope, workspace=workspace)
    if not force and not (health["recommend_migration"] and st.session_state.get("auto_migrate", True)):
        return None
    if not health["can_migrate"]:
        if force:
            raise ValueError("Nothing worth compressing yet: this thread needs a few real messages first.")
        return None
    refine = (lambda digest: model_refine_digest(digest, ledger)) if configured_provider_names() else None
    result = migrate_thread(project_scope, refine=refine, workspace=workspace)
    result["reasons"] = health["reasons"]
    st.session_state.last_migration = result
    return result


# =============================================================================
# Safe dual-panel preview and artifact rendering
# =============================================================================

_EXTENSION_LANGUAGES = {
    "py": "python", "js": "javascript", "ts": "typescript", "tsx": "typescript", "jsx": "javascript", "json": "json",
    "md": "markdown", "yml": "yaml", "yaml": "yaml", "toml": "toml", "css": "css", "html": "html", "sql": "sql",
    "sh": "bash", "bash": "bash", "txt": "text",
}


def display_language(language: str, body: str) -> str:
    """Prism language for st.code: 'file: path.py' fences derive it from the extension."""
    label = language.strip()
    if label.lower().startswith("file:"):
        path = infer_artifact_path(label, body)
        return _EXTENSION_LANGUAGES.get(path.rsplit(".", 1)[-1].lower(), "text") if "." in path else "text"
    return label.split(":", 1)[0].strip() or "text"


def infer_artifact_path(language: str, body: str) -> str:
    label = language.strip()
    if label.lower().startswith("file:"):
        return label.split(":", 1)[1].strip()
    first_line = body.splitlines()[0].strip() if body.splitlines() else ""
    match = re.match(r"(?:#|//|<!--)\s*file\s*:\s*([^>]+?)(?:-->)?$", first_line, re.I)
    return match.group(1).strip() if match else ""


def render_output_with_artifacts(
    text: str,
    project_scope: str,
    source_message_id: Optional[int],
    artifact_prefix: str,
) -> None:
    """Render markdown/code output with an adjacent Artifact Lock action."""
    if not text:
        st.info("The provider returned an empty response.")
        return
    pattern = re.compile(r"```(?P<language>[^\n`]*)\n(?P<body>.*?)```", re.DOTALL)
    cursor = 0
    found = False
    for index, match in enumerate(pattern.finditer(text)):
        found = True
        before = text[cursor:match.start()]
        if before.strip():
            st.markdown(before)
        language = match.group("language").strip() or "text"
        body = match.group("body")
        left, right = st.columns([0.88, 0.12], gap="small")
        with left:
            st.code(body, language=display_language(language, body))
        with right:
            st.caption("Artifact")
            key_material = f"{artifact_prefix}:{index}:{hashlib.sha256(body.encode()).hexdigest()[:12]}"
            if st.button("🔒 Lock", key=key_material, help="Save an immutable local artifact version"):
                file_path = infer_artifact_path(language, body)
                artifact_name = Path(file_path).name if file_path else f"{artifact_prefix}-block-{index + 1}"
                artifact_id, version = save_artifact(
                    project_scope,
                    artifact_name,
                    file_path,
                    body,
                    language,
                    source_message_id,
                )
                st.success(f"Locked artifact v{version} (id {artifact_id})")
        cursor = match.end()
    remainder = text[cursor:]
    if remainder.strip():
        st.markdown(remainder)
    if not found:
        st.markdown(text)


def render_preview_panel() -> None:
    st.caption("HTML/CSS mockups are isolated and sanitized before rendering.")
    if "preview_editor" not in st.session_state:
        st.session_state.preview_editor = st.session_state.get("preview_source", "")
    source = st.text_area(
        "Preview markup",
        key="preview_editor",
        height=170,
        label_visibility="collapsed",
        placeholder="Paste HTML/CSS here or generate an interface in Chat Bot mode…",
    )
    render_clicked = st.button("Render sanitized preview", key="render_preview", type="secondary")
    if render_clicked:
        st.session_state.preview_source = source
    effective_source = st.session_state.get("preview_source", "") or source
    st.components.v1.html(safe_preview_document(effective_source), height=410, scrolling=True)


# =============================================================================
# GitHub OAuth/read-only repository skeleton
# =============================================================================

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_API_URL = "https://api.github.com"


def _query_value(name: str) -> str:
    try:
        query_params = st.query_params
        value = query_params.get(name, "")
        return str(value[0] if isinstance(value, list) else value)
    except AttributeError:
        values = st.experimental_get_query_params()
        return str(values.get(name, [""])[0])


def github_oauth_url() -> str:
    client_id = os.environ.get("GITHUB_CLIENT_ID", "").strip()
    redirect_uri = os.environ.get("GITHUB_REDIRECT_URI", "").strip()
    scope = os.environ.get("GITHUB_OAUTH_SCOPE", "read:user").strip() or "read:user"
    # Signed, time-limited state: the redirect back from GitHub lands in a fresh
    # Streamlit session, so session memory cannot be the CSRF check.
    state = mint_state(os.environ.get("GITHUB_CLIENT_SECRET", "").strip())
    return GITHUB_AUTHORIZE_URL + "?" + urlencode(
        {"client_id": client_id, "redirect_uri": redirect_uri, "scope": scope, "state": state}
    )


def exchange_github_code(code: str, expected_state: str, received_state: str) -> str:
    client_id = os.environ.get("GITHUB_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GITHUB_CLIENT_SECRET", "").strip()
    valid, why = verify_state(client_secret, received_state)
    if not valid:
        raise ValueError(f"GitHub OAuth state validation failed: {why}")
    redirect_uri = os.environ.get("GITHUB_REDIRECT_URI", "").strip()
    if not client_id or not client_secret or not redirect_uri:
        raise ValueError("GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, and GITHUB_REDIRECT_URI are required")
    response = requests.post(
        GITHUB_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        },
        headers={"Accept": "application/json"},
        timeout=20,
    )
    if response.status_code >= 400:
        raise ValueError(f"GitHub token exchange failed with HTTP {response.status_code}")
    payload = response.json()
    token = str(payload.get("access_token", ""))
    if not token:
        raise ValueError(str(payload.get("error_description", "GitHub did not return an access token")))
    return token


def github_api_get(token: str, path: str) -> Any:
    if not path.startswith("/") or ".." in path:
        raise ValueError("invalid GitHub API path")
    response = requests.get(
        GITHUB_API_URL + path,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=20,
    )
    if response.status_code >= 400:
        raise ValueError(f"GitHub API returned HTTP {response.status_code}")
    return response.json()


def render_repository_work(project_scope: str, ledger: QuotaLedger, submission: Optional[ChatSubmission]) -> None:
    st.subheader("Repository Work")
    st.caption("Sandboxed changes on a GitHub or local repository, reviewable diffs, and an explicit human handoff." + mode_caption())
    render_thread_bar(project_scope, "repository", ledger)
    work_tab, kit_tab, github_tab, directions_tab = st.tabs(["Work", "Deploy Kit", "GitHub", "Directions"])
    with work_tab:
        render_repo_work_tab(project_scope, ledger)
    with kit_tab:
        render_deploy_kit(project_scope)
    with github_tab:
        render_github_tab()
    with directions_tab:
        render_repo_directions()
    st.divider()
    context, note, loaded = repository_context(int(st.session_state.get("max_tokens", 2048)))
    (st.caption if loaded else st.info)(note)
    render_history(project_scope, "Repository conversation", workspace="repository")
    dispatch_chat(project_scope, "repository", submission, ledger, context, [note] if loaded else ())


NO_REPOSITORY_CONTEXT = (
    "REPOSITORY CONTEXT: none loaded. You cannot reach GitHub or the operator's disk on your own. If asked about "
    "a repository, say that nothing is loaded and point to the Work tab: Fetch repository (GitHub) or a local path."
)


def repository_context(max_tokens: int) -> Tuple[str, str, bool]:
    """(context for the prompt, note for the operator, loaded?) for the repository conversation.

    The fetched tree (or the local path) is serialized once per (path, sha, budget) and cached in the
    session, so a send never re-walks the tree.
    """
    fetched: Optional[Dict[str, Any]] = st.session_state.get("repo_fetched")
    path = str(fetched["path"]) if fetched else str(st.session_state.get("repo_path", "") or "").strip()
    if not path or not os.path.isdir(path):
        slot = github_push_status()
        hint = (
            "pick one from your list in the Work tab and press Connect" if slot["armed"]
            else "arm the GitHub token in the sidebar to pick from your repositories, or enter a public owner/name in the Work tab and press Connect"
        )
        return NO_REPOSITORY_CONTEXT, f"No repository connected: {hint}. The chat cannot reach GitHub on its own.", False
    label = f"{fetched['owner']}/{fetched['repo']}@{str(fetched['sha'])[:7]}" if fetched else os.path.basename(path.rstrip("/"))
    budget = repository_context_chars(max_tokens)
    key = (path, str(fetched["sha"]) if fetched else "", budget)
    cache = st.session_state.setdefault("repo_context_cache", {})
    if cache.get("key") != key:
        text, stats = repo_prompt_context(path, budget, label)
        cache.clear()
        cache.update({"key": key, "text": text, "stats": stats})
    stats = cache["stats"]
    note = f"Repository in context: {label} · {stats['file_count']} files · ~{stats['est_tokens']} tokens of file map and top files go with every message here."
    return str(cache["text"]), note, True


def render_repo_work_tab(project_scope: str, ledger: QuotaLedger) -> None:
    """Fetch (GitHub) or point at (local) a repository, run the sandbox pipeline, review, push."""
    push_state = github_push_status()
    source = st.radio("Source", ["GitHub repository", "Local path"], horizontal=True, key="repo_source_kind")
    fetched: Optional[Dict[str, Any]] = st.session_state.get("repo_fetched")
    repo_path = ""
    if source == "GitHub repository":
        token = str(st.session_state.get("github_push_token", "")) if push_state["armed"] else ""
        if fetched:
            c1, c2 = st.columns([0.8, 0.2], gap="small")
            c1.success(
                f"Connected {fetched['owner']}/{fetched['repo']} @ {fetched['ref']} ({str(fetched['sha'])[:7]}) · "
                f"{fetched['files']} file(s) · {int(fetched['size_bytes']) // 1024} KB. The conversation below sees it; pushes go here."
            )
            if c2.button("Disconnect", key="repo_disconnect", use_container_width=True):
                for key in ("repo_fetched", "repo_context_cache", "repo_last_report"):
                    st.session_state.pop(key, None)
                st.rerun()
            repo_path = str(fetched["path"])
        else:
            if token:
                # The token's repositories are listed once per session (one API call); the picker filters as you type.
                if "repo_choices" not in st.session_state:
                    try:
                        st.session_state.repo_choices = list_repositories(token)
                    except GitHubRepoError as exc:
                        st.session_state.repo_choices = []
                        st.error(f"Could not list repositories: {exc}")
                choices: List[str] = st.session_state.get("repo_choices", [])
                if choices:
                    pick = st.selectbox("Repository", choices, key="repo_pick", help="Type to filter. Newest activity first.")
                else:
                    pick = st.text_input("Repository", key="repo_manual", placeholder="name, or owner/name" if push_state["login"] else "owner/repo")
                if st.button("Refresh list", key="repo_list"):
                    st.session_state.pop("repo_choices", None)
                    st.rerun()
            else:
                pick = st.text_input("Public repository (owner/name)", key="repo_manual", placeholder="owner/repo")
                st.caption("Arm GitHub push in the sidebar to pick from your repositories and read private ones.")
            with st.expander("Advanced: branch, tag, or commit", expanded=False):
                ref = st.text_input("Ref", value="", key="repo_fetch_ref", placeholder="default branch")
            pick = qualify_repository(pick, push_state["login"])  # a bare name means one of yours
            valid = looks_like_owner_repo(pick)
            if pick.strip() and not valid:
                st.warning("Enter owner/name, for example colegleason1-jpg/Chat-Johnson.")
            if st.button(f"Connect {pick.strip()}" if valid else "Connect", key="repo_fetch", type="primary", disabled=not valid):
                try:
                    with st.spinner("Downloading the tree through the GitHub API…"):
                        tree = fetch_tree(pick, ref, token)
                    st.session_state.repo_fetched = tree.as_dict()
                    for key in ("repo_last_report", "repo_context_cache"):
                        st.session_state.pop(key, None)
                    st.rerun()
                except (GitHubRepoError, GitHubPushError) as exc:
                    st.error(f"Connect failed: {exc}")
    else:
        repo_path = st.text_input(
            "Repository path", value="", key="repo_path",
            placeholder="Absolute path to a local git repository (never the deployed app's own checkout)",
        )
    repo_goal = st.text_area(
        "Requested repository change", height=100, key="repo_goal",
        placeholder="Describe a reviewable change; generated edits stay in an isolated sandbox.",
    )
    run_tests = st.checkbox(
        "Run the repository's tests in the repair loop (executes its code in this app's container)",
        value=(source == "Local path"), key="repo_run_tests",
    )
    ready = bool(repo_goal.strip() and repo_path.strip() and configured_provider_names())
    if st.button("Run sandboxed pipeline", type="primary", key="run_repo_pipeline", disabled=not ready,
                 help="Needs a fetched or local repository, a requested change, and at least one provider key."):
        app_root = str(Path(__file__).resolve().parent)
        if not os.path.isdir(repo_path):
            st.error("Repository path does not exist.")
        elif source == "Local path" and os.path.realpath(repo_path) == os.path.realpath(app_root):
            st.error("Refusing to run the pipeline on the studio's own checkout; point it at another repository.")
        else:
            settings = get_settings()
            if not run_tests:
                settings.max_test_rounds = 0
            with st.status("Ingesting, patching, and verifying…", expanded=True) as status:
                try:
                    report = Orchestrator(settings=settings, ledger=ledger).run(repo_goal, repo_path=repo_path)
                    status.update(label="Pipeline completed", state="complete")
                    st.session_state.repo_last_report = {"report": report, "source": source, "fetched": fetched, "goal": repo_goal.strip()}
                except Exception as exc:
                    status.update(label="Pipeline stopped", state="error")
                    st.error(f"Repository pipeline failed: {exc}")
    last = st.session_state.get("repo_last_report")
    if not last:
        return
    report = last["report"]
    st.markdown(f"**Last run** · {last['goal'][:120]}")
    if report.get("branch") == "(copy-mode)":
        st.caption("Sandbox is a copied tree under the temp directory (no git worktree); the diff is computed against the source tree. Nothing was committed.")
    else:
        st.caption(f"Sandbox branch: {report.get('branch')}")
    st.json(report.get("ingest", {}), expanded=False)
    diff = str(report.get("diff") or "")
    if not diff.strip():
        st.info("The pipeline produced no file changes. Rephrase the request with the files or behaviour you expect to change.")
    else:
        pairs, missing = collect_changed_files(str(report.get("sandbox") or ""), diff)
        st.caption(f"Changed files: {', '.join(path for path, _ in pairs) or 'none readable'}" + (f" · not pushable: {', '.join(missing)}" if missing else ""))
        st.code(diff[:20_000], language="diff")
        st.download_button("⬇ Download patch (.diff)", data=diff, file_name="chat-johnson-change.diff", mime="text/x-diff", key="repo_patch_download")
        if last["source"] == "GitHub repository" and last.get("fetched") and pairs:
            if push_state["armed"] and push_state["connected"]:
                base = str(last["fetched"]["ref"])
                default_branch = f"chat-johnson/{deliverable_slug(last['goal'])}-{str(last['fetched']['sha'])[:7]}"
                branch = st.text_input("Branch name", value=default_branch, key="repo_push_branch")
                if st.button(f"Push {len(pairs)} changed file(s) as a branch and open a pull request", type="primary", key="repo_push"):
                    title = f"Chat Johnson: {last['goal'][:70]}"
                    body = (
                        f"Requested change: {last['goal']}\n\nGenerated in Chat Johnson's sandbox pipeline from "
                        f"`{last['fetched']['owner']}/{last['fetched']['repo']}@{str(last['fetched']['sha'])[:7]}`. "
                        "Review the diff; nothing was executed against the default branch."
                        + (f"\n\nNot included (deleted or binary): {', '.join(missing)}" if missing else "")
                    )
                    try:
                        with st.spinner("Pushing the branch and opening the pull request…"):
                            record = GitHubWriter(str(st.session_state.get("github_push_token", "")), push_state["repo"]).push_files(
                                pairs, branch.strip() or default_branch, f"Chat Johnson: {last['goal'][:60]}", title, body, base_branch=base,
                            )
                        st.session_state.setdefault("kit_pushes", []).append(record)
                        st.success(f"Pushed to {record.branch} and opened pull request #{record.pr_number}: {record.pr_url}")
                    except GitHubPushError as exc:
                        st.error(f"Push failed: {exc}")
            else:
                st.caption("Arm **GitHub push (session only)** in the sidebar (and keep the repository connected) to push this change as a branch with a pull request.")
    with st.expander("Execution memory", expanded=False):
        st.text(report.get("memory", "") or "(empty)")


def render_github_tab() -> None:
    state = github_push_status()
    st.markdown("**Session-only push slot**")
    if state["armed"] and state["connected"]:
        st.success(f"Armed for {state['repo']}: pushes create a new branch and a pull request; the default branch is never written.")
    elif state["armed"]:
        st.info("Token armed. Connect a repository in the Work tab; pushes go to the connected repository.")
    else:
        st.caption("Disarmed. Arm it in the sidebar (toggle + token) to list and read private repositories and to push results.")
    render_push_ledger()
    st.divider()
    code = _query_value("code")
    received_state = _query_value("state")
    token = st.session_state.get("github_token", "")
    with st.expander("GitHub identity (optional, profile-only OAuth)", expanded=bool(token or (code and received_state))):
        st.caption(
            "Least-privilege OAuth skeleton: the token lives in session memory only, no SSH keys are collected, "
            "and nothing is committed or pushed by it."
        )
        oauth_ready = all(os.environ.get(key, "").strip() for key in ("GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET", "GITHUB_REDIRECT_URI"))
        if code and received_state and "github_token" not in st.session_state:
            try:
                st.session_state.github_token = exchange_github_code(code, str(st.session_state.get("github_oauth_state", "")), received_state)
                token = st.session_state.github_token
                st.success("GitHub authorization completed for this session.")
            except (ValueError, requests.RequestException) as exc:
                st.error(f"GitHub authorization was not completed: {exc}")
        if oauth_ready:
            url = github_oauth_url()
            try:
                st.link_button("Authorize GitHub identity check (scope read:user, profile only)", url)
            except AttributeError:
                st.markdown(f"[Authorize GitHub identity check (scope read:user, profile only)]({url})")
            st.caption("This reads your GitHub profile only.")
        else:
            st.caption("Set GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, and GITHUB_REDIRECT_URI in the environment to enable it.")
        if token:
            st.success("Session-only GitHub connection is active.")
            if st.button("Verify GitHub identity (profile only)", key="github_identity"):
                try:
                    identity = github_api_get(token, "/user")
                    st.json({"login": identity.get("login"), "name": identity.get("name"), "public_repos": identity.get("public_repos")})
                except (ValueError, requests.RequestException) as exc:
                    st.error(f"GitHub request failed: {exc}")
            if st.button("Forget session connection", key="github_forget"):
                st.session_state.pop("github_token", None)
                st.session_state.pop("github_oauth_state", None)
                st.rerun()


def render_push_ledger() -> None:
    """Every push made this session (Deploy Kit or pipeline) with a revert action."""
    state = github_push_status()
    pushes: List[PushRecord] = st.session_state.setdefault("kit_pushes", [])
    if not pushes:
        st.caption("No pushes this session.")
        return
    for index, record in enumerate(pushes):
        label = "revert" if record.kind == "revert" else "push"
        st.caption(f"{label} · {record.owner}/{record.repo} · branch {record.branch} · PR #{record.pr_number} · {record.pr_url}")
        if record.kind == "push" and state["armed"] and st.button("Open revert PR", key=f"ledger_revert_{index}"):
            try:
                reverted = GitHubWriter(str(st.session_state.get("github_push_token", "")), f"{record.owner}/{record.repo}").open_revert(record)
                pushes.append(reverted)
                st.success(f"Revert pull request #{reverted.pr_number}: {reverted.pr_url}")
                st.rerun()
            except GitHubPushError as exc:
                st.error(f"Revert failed: {exc}")


REPO_DIRECTIONS = """
**What this workspace does.** It changes a repository for you inside an isolated sandbox and hands you a
reviewable result. It never touches your default branch, never stores a token, and never runs cloud CLIs.

**The four tabs**
1. **Work** · pick a source, describe the change, run the pipeline, review the diff, push it as a branch with a pull request.
2. **Deploy Kit** · generate CI/CD, container, Helm, Terraform, serverless, observability, rollback, and runbook files for a target; validate them offline; download or push them.
3. **GitHub** · the state of the session-only push slot, every push made this session with a one-click revert pull request, and the optional profile-only identity check.
4. **Directions** · this page.

**Sequence for a GitHub repository**
1. In the sidebar, open *GitHub push (session only)*: switch it on and paste a fine-grained token with Contents and Pull requests write access (read access is enough to connect a private repository). Nothing is stored.
2. *Work* → Source *GitHub repository*: your repositories are listed; pick one (type to filter) and press **Connect**. The tree is downloaded through the GitHub API at one commit into a temporary sandbox; the commit and file count are shown. Without a token, enter a public `owner/name` instead. Pushes go to the connected repository.
3. The conversation below the tabs now sees the repository (file map and the highest-value files within the token budget): ask it to inspect, explain, or plan. Changes are made by the pipeline, not by the chat.
4. Describe the change and press **Run sandboxed pipeline**. The pipeline ingests the tree within a token budget, plans typed steps, asks the routed model for complete file blocks or unified diffs, applies them in the sandbox, and validates Python syntax. Tick *Run the repository's tests* only when you accept that the repository's own test suite executes here.
5. Review the diff and the changed-file list. **Download patch** gives you the unified diff; **Push … and open a pull request** creates one commit on a new branch off the fetched ref and opens the pull request.
6. The GitHub tab lists the push; **Open revert PR** restores the touched paths.

**Local path** works the same on a self-hosted run: point at a repository on the machine that runs the app; a git worktree is used when possible.

**When the diff is empty** the model answered without file blocks. Name the files or behaviour you expect to change and run again; the *Execution memory* shows what each step returned.

**When the pipeline fails** the error names the provider or step. Free-tier limits are paced automatically; a retired model id is rediscovered on the next call.

**What never happens here** · writes to the default branch · stored tokens · Docker, Terraform, Helm, or cloud CLI execution · running your tests unless you tick the box · any push without your button press.
"""


def render_repo_directions() -> None:
    st.markdown(REPO_DIRECTIONS)


_KIT_CODE_LANGUAGE = {"dockerfile": "docker", "bash": "bash", "yaml": "yaml", "json": "json", "hcl": "hcl", "markdown": "markdown", "text": "text"}


def render_deploy_kit(project_scope: str) -> None:
    """Generate, validate, download, or lock deployment artefacts. Nothing is pushed or applied here."""
    kit = st.session_state.get("deploy_kit")
    with st.container(border=True):
        st.markdown("**Deploy Kit** · CI/CD, containers, Helm, Terraform, serverless, observability, rollback, runbooks")
        st.caption(
            "Generated from templates with zero provider calls and checked offline (YAML, JSON, shell, Dockerfile, HCL). "
            "Download the zip or lock the files as artifacts; the target repository's CI runs them once the listed "
            "secrets exist. This app never pushes, builds, or applies anything."
        )
        defaults = KitSpec()
        with st.form("deploy_kit_form"):
            c1, c2 = st.columns(2)
            app_name = c1.text_input("App name", value=defaults.app_name)
            target = c2.selectbox("Target", list(TARGETS), index=list(TARGETS).index(defaults.target), format_func=lambda key: TARGETS[key])
            c3, c4, c5 = st.columns(3)
            language = c3.selectbox("Language", LANGUAGES)
            runtime_version = c4.text_input("Runtime version", value=defaults.runtime_version)
            port = c5.number_input("Port", min_value=1, max_value=65535, value=defaults.port)
            c6, c7 = st.columns(2)
            fetched_repo = st.session_state.get("repo_fetched") or {}
            registry_default = f"ghcr.io/{fetched_repo['owner']}/{fetched_repo['repo']}".lower() if fetched_repo else defaults.registry
            registry = c6.text_input("Image registry / namespace", value=registry_default)
            cloud = c7.selectbox("Cloud", CLOUDS, index=CLOUDS.index(defaults.cloud), help="Terraform skeleton and the serverless flavour follow this choice.")
            c8, c9 = st.columns(2)
            health_path = c8.text_input("Health path", value=defaults.health_path)
            deploy_url = c9.text_input("Deployed URL (post-deploy smoke test)", value="", placeholder="https://your-app.example")
            entrypoint = st.text_input("Container entrypoint", value=defaults.entrypoint)
            c10, c11 = st.columns(2)
            lint_command = c10.text_input("Lint command", value=defaults.lint_command)
            test_command = c11.text_input("Test command", value=defaults.test_command)
            generate = st.form_submit_button("Generate kit", type="primary")
        if generate:
            spec = KitSpec(
                app_name=app_name, target=target, language=language, runtime_version=runtime_version.strip() or defaults.runtime_version,
                port=int(port), registry=registry, cloud=cloud, health_path=health_path, lint_command=lint_command.strip() or defaults.lint_command,
                test_command=test_command.strip() or defaults.test_command, entrypoint=entrypoint.strip() or defaults.entrypoint, deploy_url=deploy_url,
            ).normalized()
            files = generate_kit(spec)
            kit = {"spec": spec, "files": files, "findings": validate_kit(files)}
            st.session_state.deploy_kit = kit
        if not kit:
            return
        spec, files, findings = kit["spec"], kit["files"], kit["findings"]
        counts = summarize(findings)
        line = (
            f"{len(files)} file(s) for {TARGETS[spec.target].split(' (')[0]} · {counts['ok']} ok · {counts['warn']} warning(s) · "
            f"{counts['error']} error(s) · {counts['skipped']} skipped check(s)"
        )
        (st.success if counts["error"] == 0 else st.error)(line)
        st.dataframe([asdict(finding) for finding in findings], hide_index=True, use_container_width=True)
        chosen = st.selectbox("Preview a file", [item.path for item in files], key="kit_preview")
        preview = next(item for item in files if item.path == chosen)
        st.caption(preview.purpose)
        st.code(preview.body, language=_KIT_CODE_LANGUAGE.get(preview.language, "text"))
        d1, d2 = st.columns(2)
        d1.download_button(
            "⬇ Download kit (.zip)", data=kit_zip(files), file_name=f"{spec.app_name}-deploy-kit.zip", mime="application/zip",
            key="kit_zip", use_container_width=True,
        )
        if d2.button("Lock all files as artifacts", key="kit_lock", use_container_width=True,
                     help="Versioned copies in the vault; find them under Locked artifacts in the sidebar."):
            for item in files:
                save_artifact(project_scope, item.path.rsplit("/", 1)[-1], item.path, item.body, item.language)
            st.success(f"{len(files)} file(s) locked as artifacts in scope {project_scope}.")
        render_kit_push(spec, files)


def render_kit_push(spec: KitSpec, files: Sequence[Any]) -> None:
    """Push the kit as a branch + pull request through the session-only slot; list pushes with a revert option."""
    state = github_push_status()
    pairs = [(item.path, item.body) for item in files]
    pushes: List[PushRecord] = st.session_state.setdefault("kit_pushes", [])
    if not (state["armed"] and state["connected"]):
        st.caption("To push this kit as a branch with a pull request, arm **GitHub push (session only)** in the sidebar and connect a repository in the Work tab.")
    else:
        st.markdown(f"**Push to {state['repo']}** · one commit on a new branch, then a pull request. Nothing touches the default branch.")
        branch = st.text_input("Branch name", value=branch_name_for(spec.app_name, pairs), key="kit_branch")
        if st.button(f"Push {len(files)} file(s) and open a pull request", type="primary", key="kit_push", use_container_width=True):
            title = f"Deploy kit for {spec.app_name} ({TARGETS[spec.target].split(' (')[0]})"
            body = (
                "Generated by Chat Johnson Deploy Kit. Nothing runs until the secrets and variables listed in "
                "docs/RUNBOOK.md exist in this repository.\n\nFiles:\n" + "\n".join(f"- `{item.path}` · {item.purpose}" for item in files)
            )
            try:
                with st.spinner("Creating blobs, tree, commit, branch, and pull request…"):
                    record = GitHubWriter(str(st.session_state.get("github_push_token", "")), state["repo"]).push_files(
                        pairs, branch.strip() or branch_name_for(spec.app_name, pairs), f"Add deploy kit for {spec.app_name}", title, body,
                    )
                pushes.append(record)
                st.success(f"Pushed {len(record.files)} file(s) to {record.branch} and opened pull request #{record.pr_number}: {record.pr_url}")
            except GitHubPushError as exc:
                st.error(f"GitHub push failed: {exc}")
    for index, record in enumerate(pushes):
        label = "revert" if record.kind == "revert" else "push"
        st.caption(f"{label} · {record.owner}/{record.repo} · branch {record.branch} · PR #{record.pr_number} · {record.pr_url}")
        if record.kind == "push" and state["armed"] and st.button("Open revert PR", key=f"kit_revert_{index}"):
            try:
                reverted = GitHubWriter(str(st.session_state.get("github_push_token", "")), f"{record.owner}/{record.repo}").open_revert(record)
                pushes.append(reverted)
                st.success(f"Revert pull request #{reverted.pr_number}: {reverted.pr_url}")
                st.rerun()
            except GitHubPushError as exc:
                st.error(f"Revert failed: {exc}")


# =============================================================================
# Chat surfaces: one pinned chat bar, per-workspace chats
# =============================================================================


WORKSPACE_TABS = (
    ("Task Finder", "task_finder"),
    ("Repository Work", "repository"),
    ("Chat Bot", "chat_bot"),
    ("Normal Chat", "normal_chat"),
)
assert tuple(key for _, key in WORKSPACE_TABS) == VAULT_WORKSPACES
WORKSPACE_LABEL = {key: label for label, key in WORKSPACE_TABS}
DEFAULT_WORKSPACE_KEY = "normal_chat"
CHAT_FILE_TYPES = ["py", "js", "ts", "tsx", "jsx", "json", "md", "txt", "yml", "yaml", "toml", "css", "html"]
# Streamlit >= 1.43 puts a paperclip in the chat bar; older builds fall back to an uploader in the body.
CHAT_INPUT_ACCEPTS_FILES = "accept_file" in inspect.signature(st.chat_input).parameters
INJECTION_BUDGET_CHARS = 120_000
ROUTING_LOG_LIMIT = 40
MISSION_PREFIX = "MISSION: "
MISSION_MAX_WAIT_SECONDS = 65  # one free-tier window; longer waits surface as a failed step instead


def heavy_pass_tokens(budget: int) -> int:
    """Output tokens one Heavy Mode send can use: draft b/2, critique b/3, synthesis b (256 floor each)."""
    return max(256, budget // 2) + max(256, budget // 3) + budget


@dataclass
class ChatSubmission:
    text: str
    files: List[Any]


def uploaded_file_context(files: Sequence[Any], budget: int = INJECTION_BUDGET_CHARS) -> Tuple[str, List[str]]:
    """Join uploaded files under one prompt budget, splitting it evenly and marking every truncation."""
    chunks: List[str] = []
    notes: List[str] = []
    valid = [uploaded for uploaded in files if hasattr(uploaded, "getvalue")]
    if not valid:
        return "", notes
    per_file = max(1_000, budget // len(valid))
    for uploaded in valid:
        try:
            decoded = uploaded.getvalue().decode("utf-8", errors="replace")
        except (AttributeError, UnicodeError):
            notes.append(f"{uploaded.name}: could not be read as text; skipped")
            continue
        if len(decoded) > per_file:
            dropped = len(decoded) - per_file
            decoded = decoded[:per_file] + f"\n[TRUNCATED: {dropped} characters of {uploaded.name} not sent]"
            notes.append(f"{uploaded.name}: sent {per_file} of {per_file + dropped} characters")
        else:
            notes.append(f"{uploaded.name}: sent {len(decoded)} characters in full")
        chunks.append(f"===== INJECTED FILE: {uploaded.name} =====\n{decoded}")
    return "\n\n".join(chunks), notes


def thread_mission(thread: sqlite3.Row) -> str:
    """The mission pinned to a Task Finder chat; survives window eviction, migration, and a reload."""
    return str(thread["mission"]).strip() if "mission" in thread.keys() and thread["mission"] else ""


def log_route(workspace: Optional[str], task_type: str, route: str, mode: str, started: float, reason: str, finish: str = "") -> None:
    """Trace of every send (what was asked, where it went, how long, how it ended, why): session table plus the vault."""
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    log: List[Dict[str, Any]] = st.session_state.setdefault("routing_log", [])
    log.append(
        {
            "time": time.strftime("%H:%M:%S"),
            "workspace": WORKSPACE_LABEL.get(workspace or "", workspace or "-"),
            "task": task_type,
            "route": route,
            "mode": mode,
            "ms": elapsed_ms,
            "finish": finish,
            "reason": (reason or "")[:160],
        }
    )
    del log[:-ROUTING_LOG_LIMIT]
    try:
        record_route(str(st.session_state.get("project_scope", "chat-johnson")), workspace or "", task_type, route, mode, elapsed_ms, finish, reason)
    except Exception:  # telemetry must never break a send
        pass


_SCROLL_SNIPPET = (
    "<!-- NONCE --><script>(function(){try{var d=window.parent.document;"
    "var m=d.querySelector('section[data-testid=\"stAppScrollToBottomContainer\"]')||d.querySelector('section.stMain')"
    "||d.querySelector('[data-testid=\"ScrollToBottomContainer\"]')||d.querySelector('section[data-testid=\"stMain\"]')"
    "||d.querySelector('section.main')||d.querySelector('.main');"
    "var msgs=d.querySelectorAll('[data-testid=\"stChatMessage\"]');var last=msgs.length?msgs[msgs.length-1]:null;"
    "var go=function(){if(last){last.scrollIntoView({block:'start',behavior:'smooth'});}"
    "else if(m){m.scrollTo({top:m.scrollHeight,behavior:'smooth'});}else{window.parent.scrollTo(0,d.body.scrollHeight);}};"
    "go();setTimeout(go,400);setTimeout(go,1500);}catch(e){}})();</script>"
)


def scroll_to_bottom(nonce: Any) -> None:
    """Scroll the message just sent to the top of the view so the answer streams in below it; the pinned bar never moves."""
    components.html(_SCROLL_SNIPPET.replace("NONCE", str(nonce)), height=0)


def run_generation(
    project_scope: str,
    prompt: str,
    task_type: str,
    ledger: QuotaLedger,
    injected_context: str = "",
    workspace: Optional[str] = None,
    attachment_notes: Optional[Sequence[str]] = None,
) -> Optional[int]:
    clean_prompt = prompt.strip()
    if not clean_prompt:
        return None
    mode = active_mode()
    migration = health_sweep(project_scope, ledger, workspace=workspace)
    if migration:
        st.info(
            f"Thread health agent migrated to an optimized chat (#{migration['new_thread_id']}) before sending: "
            + ", ".join(migration["reasons"])
            + f". Digest locked as artifact {migration['digest_artifact_id']} ({migration['method']})."
        )
    user_message_id = append_message(project_scope, "user", clean_prompt, mode=mode, workspace=workspace, task_type=task_type)
    messages = build_prompt_messages(project_scope, clean_prompt, injected_context, workspace=workspace)
    max_tokens = int(st.session_state.get("max_tokens", 2048))
    with st.chat_message("user"):
        st.markdown(clean_prompt)
        for note in attachment_notes or []:
            st.caption(f"Attached · {note}")
    scroll_to_bottom(user_message_id)
    live_box = st.empty()
    started = time.perf_counter()
    try:
        if mode == "normal" and cortex_available():
            # Normal mode streams token-by-token from the MILP-selected endpoint.
            try:
                stream = CortexStream(task_type, messages, ledger, max_tokens=max_tokens, temperature=0.35)
                with live_box.container():
                    with st.chat_message("assistant"):
                        st.caption(f"{stream.decision.provider}/{stream.decision.model} · {task_type} · streaming")
                        st.write_stream(stream)
                answer, decision = stream.text, stream.decision
            except ProviderError:
                # Strict endpoint failed mid-flight; use the blocking path with fallback.
                live_box.empty()
                answer, decision = generate_mode(mode, task_type, messages, ledger, max_tokens=max_tokens, temperature=0.35)
        else:
            with live_box.container():
                st.info("Heavy Mode: draft → review → synthesis in progress…" if mode == "heavy" else "Generating…")
            answer, decision = generate_mode(
                mode,
                task_type,
                messages,
                ledger,
                max_tokens=max_tokens,
                temperature=0.2 if mode == "heavy" else 0.35,
                paid_slot=session_paid_slot() if mode == "heavy" else None,
            )
    except Exception as exc:
        live_box.empty()
        # Shown, not persisted: vendor error bodies must never be re-injected into later prompts.
        st.session_state.setdefault("provider_events", []).append(str(exc)[:600])
        log_route(workspace, task_type, "failed", mode, started, str(exc)[:160])
        st.error(f"Generation failed: {exc}")
        return user_message_id
    live_box.empty()
    answer = strip_reasoning_tags(answer)
    assistant_id = append_message(
        project_scope,
        "assistant",
        answer,
        provider=f"{decision.provider}/{decision.model}",
        mode=mode,
        workspace=workspace,
        task_type=task_type,
    )
    log_route(workspace, task_type, f"{decision.provider}/{decision.model}", mode, started, decision.reason, decision.finish)
    with st.chat_message("assistant"):
        st.caption(f"{decision.provider}/{decision.model} · {task_type} · {mode}")
        render_output_with_artifacts(answer, project_scope, assistant_id, f"message-{assistant_id}")
    if decision.finish == "length":
        st.warning(
            f"This answer stopped at the output budget ({max_tokens} tokens). Raise 'Output token budget' in the "
            "sidebar for longer answers, or send 'continue'."
        )
    elif decision.finish == "filtered":
        st.info("The provider filtered part of this answer under its content policy.")
    extracted = extract_preview_source(answer)
    if extracted:  # never wipe what the operator typed into the canvas
        st.session_state.preview_source = extracted
        st.session_state.preview_editor = extracted
    st.session_state.last_decision = decision
    return assistant_id


def dispatch_chat(
    project_scope: str,
    workspace: str,
    submission: Optional[ChatSubmission],
    ledger: QuotaLedger,
    injected_context: str = "",
    injection_notes: Sequence[str] = (),
) -> None:
    """Send what the chat bar delivered to this workspace's current chat."""
    if submission is None or not submission.text.strip():
        return
    if not configured_provider_names():
        st.warning("Add at least one BYOK provider key in the sidebar API keys panel before sending a request.")
        return
    run_generation(
        project_scope,
        submission.text,
        classify(submission.text),
        ledger,
        injected_context,
        workspace=workspace,
        attachment_notes=list(injection_notes),
    )


def render_history(project_scope: str, heading: str, workspace: Optional[str] = None, limit: int = 24) -> None:
    st.markdown(f"#### {heading}")
    rows = recent_messages(project_scope, MESSAGE_WINDOW, workspace=workspace)
    if not rows:
        st.caption("No messages yet in this chat.")
        return
    if len(rows) > limit:
        st.caption(f"Showing the last {limit} of {len(rows)} messages in the active window.")
    for row in rows[-limit:]:
        role = row["role"] if row["role"] in {"user", "assistant"} else "assistant"
        with st.chat_message(role):
            if row["provider"]:
                task = row["task_type"] if "task_type" in row.keys() and row["task_type"] else ""
                st.caption(f"{row['provider']} · {task or row['mode']} · {row['token_count']} estimated tokens")
            if role == "assistant":
                render_output_with_artifacts(
                    row["content"],
                    project_scope,
                    int(row["id"]),
                    f"message-{row['id']}",
                )
            else:
                st.markdown(row["content"])


def render_thread_bar(project_scope: str, workspace: str, ledger: QuotaLedger) -> sqlite3.Row:
    """One row per workspace: pick a chat, New chat, Clear chat, Delete chat, More (rename, migrate, load).

    Keys are never touched here. Clear archives the messages (they leave the context);
    Delete removes the chat after a confirmation.
    """
    current = active_thread(project_scope, workspace)
    rows = list_threads(project_scope, workspace=workspace)
    ids = [int(row["id"]) for row in rows]
    labels = {
        int(row["id"]): f"#{row['id']} · {row['title']}" + ("" if row["status"] == "active" else f" · {row['status']}")
        for row in rows
    }
    select_key = f"thread_select_{workspace}"
    st.session_state[select_key] = int(current["id"])

    def _on_select(key: str = select_key) -> None:
        switch_thread(int(st.session_state[key]))

    st.selectbox(
        "Chat", ids, format_func=lambda value: labels.get(value, str(value)), key=select_key,
        on_change=_on_select, label_visibility="collapsed", help="Type in the box to filter chats by keyword.",
    )
    new, clear, delete, more = st.columns(4, gap="small")  # four equal buttons stay readable at iPad width
    if new.button("New chat", key=f"new_thread_{workspace}", use_container_width=True,
                  help="Start a fresh chat in this workspace. Keys and other chats stay as they are."):
        create_thread(project_scope, workspace=workspace)
        st.rerun()
    if clear.button("Clear chat", key=f"clear_thread_{workspace}", use_container_width=True,
                    help="Empty this chat. The messages move to the archive and leave the context; keys are untouched."):
        moved = clear_thread(int(current["id"]))
        for bucket in ("pending_missions", "task_launches"):
            st.session_state.get(bucket, {}).pop(int(current["id"]), None)
        st.session_state[f"notice_{workspace}"] = f"Chat cleared: {moved} message(s) archived and out of context."
        st.rerun()
    if delete.button("Delete", key=f"delete_thread_{workspace}", use_container_width=True,
                     help="Remove this chat, its archive, and its summaries after a confirmation. Locked artifacts stay."):
        st.session_state[f"confirm_delete_{workspace}"] = int(current["id"])
        st.rerun()
    with more.popover("More", use_container_width=True):
        title_key = f"thread_title_{workspace}_{current['id']}"
        new_title = st.text_input("Rename this chat", value=current["title"], key=title_key)
        if st.button("Save name", key=f"save_title_{workspace}") and new_title.strip() and new_title.strip() != current["title"]:
            rename_thread(int(current["id"]), new_title)
            st.rerun()
        health = thread_health(project_scope, workspace=workspace)
        st.progress(min(float(health["pressure"]), 1.0), text=f"Context load {min(health['pressure'], 1.0):.0%} of the migration threshold")
        st.caption(
            f"gen {health['generation']} · {health['messages']} msgs · ~{health['tokens']} tokens · "
            f"{health['summaries']} summaries · {health['archived']} archived"
        )
        if st.button("Migrate now", key=f"migrate_{workspace}", use_container_width=True, disabled=not health["can_migrate"],
                     help="Compress this chat into a locked vision digest and continue in a fresh optimized chat. "
                          "Disabled until the chat has a few real messages (or right after a migration)."):
            try:
                if health_sweep(project_scope, ledger, force=True, workspace=workspace):
                    st.rerun()
            except ValueError as exc:
                st.info(str(exc))
        if health["recommend_migration"]:
            st.warning("Migration recommended: " + ", ".join(health["reasons"]))
        for note in health.get("advisories", []):
            st.info(note)
        last_migration = st.session_state.get("last_migration")
        if last_migration and int(last_migration.get("new_thread_id", -1)) == int(current["id"]):
            st.caption(
                f"Optimized from #{last_migration['old_thread_id']} · digest artifact "
                f"{last_migration['digest_artifact_id']} · {last_migration['method']}"
            )
        st.caption("Download this chat (archive, summaries, and digest included) for review or hand-off.")
        md_name, md_body = thread_transcript(int(current["id"]), "markdown")
        js_name, js_body = thread_transcript(int(current["id"]), "json")
        st.download_button("⬇ Chat (.md)", data=md_body, file_name=md_name, mime="text/markdown",
                           key=f"download_md_{workspace}_{current['id']}", use_container_width=True)
        st.download_button("⬇ Chat (.json)", data=js_body, file_name=js_name, mime="application/json",
                           key=f"download_json_{workspace}_{current['id']}", use_container_width=True)

    pending_delete = st.session_state.get(f"confirm_delete_{workspace}")
    if pending_delete == int(current["id"]):
        st.warning(
            f"Delete “{current['title']}” for good? Its messages, archive, and summaries are removed. "
            "Locked artifacts and every other chat stay."
        )
        yes, no = st.columns(2, gap="small")
        if yes.button("Yes, delete this chat", type="primary", key=f"delete_yes_{workspace}", use_container_width=True):
            counts = delete_thread(int(current["id"]))
            for bucket in ("pending_missions", "task_launches"):
                st.session_state.get(bucket, {}).pop(int(current["id"]), None)
            st.session_state.pop(f"confirm_delete_{workspace}", None)
            st.session_state[f"notice_{workspace}"] = (
                f"Chat deleted: {counts['message_history'] + counts['message_archive']} message(s) and "
                f"{counts['summaries']} summary block(s) removed."
            )
            st.rerun()
        if no.button("Keep it", key=f"delete_no_{workspace}", use_container_width=True):
            st.session_state.pop(f"confirm_delete_{workspace}", None)
            st.rerun()
    elif pending_delete is not None:
        st.session_state.pop(f"confirm_delete_{workspace}", None)
    notice = st.session_state.pop(f"notice_{workspace}", None)
    if notice:
        st.caption(notice)
    return current


def send_label(base: str) -> str:
    """Buttons say what they will do: Heavy Mode turns any send into three passes."""
    return f"{base} · Heavy Mode (up to 3 passes)" if active_mode() == "heavy" else base


def chat_placeholder(workspace: str, ready: bool) -> str:
    """Constant per workspace: on Streamlit 1.32 the widget id includes the placeholder, so a changing one wipes an unsent draft."""
    if not ready:  # no draft can exist while the bar is disabled
        return "Paste an API key in the sidebar (🔑 API keys) to start chatting"
    if workspace == "task_finder":
        return "Describe a mission, or continue the current one…"
    if workspace == "chat_bot":
        return "Ask the developer bot" + (" · attach files with the paperclip" if CHAT_INPUT_ACCEPTS_FILES else "") + "…"
    if workspace == "repository":
        return "Discuss the repository work: the diff, the next change, a review…"
    return "Message Normal Chat…"


def mode_caption() -> str:
    return " Heavy Mode is on: each send runs up to 3 passes (draft → review → synthesis)." if active_mode() == "heavy" else ""


def render_chat_bar(workspace: str) -> Optional[ChatSubmission]:
    """The one chat bar, pinned to the bottom of the screen; it always sends to the selected workspace.

    It must be created at the top level of the script (not inside a column or tab) to stay pinned.
    """
    ready = bool(configured_provider_names())
    extra: Dict[str, Any] = {}
    if workspace == "chat_bot" and CHAT_INPUT_ACCEPTS_FILES:
        extra = {"accept_file": "multiple", "file_type": CHAT_FILE_TYPES}
    value = st.chat_input(chat_placeholder(workspace, ready), key=f"chat_bar_{workspace}", disabled=not ready, **extra)
    if not value:
        return None
    if isinstance(value, str):
        return ChatSubmission(value, [])
    return ChatSubmission(str(getattr(value, "text", "") or ""), list(getattr(value, "files", None) or []))


def _remember_workspace() -> None:
    chosen = st.session_state.get("workspace_select")
    if chosen in WORKSPACE_LABEL:
        st.session_state.workspace_last = chosen
        try:
            st.query_params["ws"] = chosen
        except Exception:  # pragma: no cover - older Streamlit without query_params
            pass


def render_workspace_switch() -> str:
    """Server-side workspace choice, so the pinned chat bar knows where a message goes and a reload keeps the tab."""
    keys = list(WORKSPACE_LABEL)
    if st.session_state.get("workspace_select") not in keys:
        # First run honours ?ws=…; a deselect click on the segmented control lands here too and keeps the last workspace.
        remembered = st.session_state.get("workspace_last") or _query_value("ws")
        st.session_state.workspace_select = remembered if remembered in keys else DEFAULT_WORKSPACE_KEY
    if hasattr(st, "segmented_control"):
        st.segmented_control(
            "Workspace", keys, format_func=WORKSPACE_LABEL.get, key="workspace_select",
            on_change=_remember_workspace, label_visibility="collapsed",
        )
    else:
        st.radio(
            "Workspace", keys, format_func=WORKSPACE_LABEL.get, key="workspace_select",
            on_change=_remember_workspace, horizontal=True, label_visibility="collapsed",
        )
    chosen = st.session_state.get("workspace_select")
    if chosen not in keys:
        chosen = st.session_state.get("workspace_last", DEFAULT_WORKSPACE_KEY)
    st.session_state.workspace_last = chosen
    return chosen


def render_normal_chat(project_scope: str, ledger: QuotaLedger, submission: Optional[ChatSubmission]) -> None:
    st.subheader("Normal Chat")
    st.caption("A single-pass terminal for quick text, planning, and coding questions." + mode_caption())
    render_thread_bar(project_scope, "normal_chat", ledger)
    render_history(project_scope, "Conversation", workspace="normal_chat")
    dispatch_chat(project_scope, "normal_chat", submission, ledger)


def render_chat_bot(project_scope: str, ledger: QuotaLedger, submission: Optional[ChatSubmission]) -> None:
    st.subheader("Chat Bot")
    st.caption("Continuous developer mode with explicit, consented codebase file injections." + mode_caption())
    render_thread_bar(project_scope, "chat_bot", ledger)
    staged: List[Any] = []
    if CHAT_INPUT_ACCEPTS_FILES:
        st.caption("Attach text/code files with the paperclip in the chat bar; they are injected into that message only and never stored.")
    else:
        staged = st.file_uploader(
            "Attach text/code files to your next message",
            accept_multiple_files=True,
            type=CHAT_FILE_TYPES,
            key="chat_bot_files",
        ) or []
        if staged:
            st.caption(f"{len(staged)} file(s) staged in memory only; they go with your next message. Use Artifact Lock to persist an output.")
    render_history(project_scope, "Developer conversation", workspace="chat_bot")
    files = list(submission.files) if submission and submission.files else list(staged)
    if submission is not None and files and not submission.text.strip():
        # The paperclip lets a send go out with files only; give that send an explicit request.
        submission = ChatSubmission("Review the attached file(s): explain what they do and flag any problems.", files)
    injected, injection_notes = uploaded_file_context(files)
    dispatch_chat(project_scope, "chat_bot", submission, ledger, injected, injection_notes)


def execute_mission(
    project_scope: str, ledger: QuotaLedger, thread_id: int, goal: str, plan: Sequence[Dict[str, Any]]
) -> Dict[str, Any]:
    """Run the workstreams strictly in order; each one sees the results before it. Failures are shown, never stored."""
    migration = health_sweep(project_scope, ledger, workspace="task_finder")
    if migration:
        st.info(f"Thread health agent migrated to optimized chat #{migration['new_thread_id']} before launching.")
        thread_id = int(migration["new_thread_id"])
    task_mode = active_mode()
    append_message(project_scope, "user", f"{MISSION_PREFIX}{goal}", mode=task_mode, workspace="task_finder", task_type="plan")
    progress = st.progress(0.0, text="Starting workstreams…")
    request_lock = get_task_request_lock()
    token_budget = int(st.session_state.get("max_tokens", 2048))
    paid_slot = session_paid_slot() if task_mode == "heavy" else None
    succeeded = failed = truncated = 0
    failures: List[Tuple[str, str]] = []
    outputs: List[Tuple[str, str]] = []
    for finished, step in enumerate(plan, start=1):
        started = time.perf_counter()
        step_type = str(step["type"])
        try:
            # The prompt is built right before the call so this step sees every result before it.
            messages = build_prompt_messages(project_scope, str(step["description"]), workspace="task_finder")
            wait = cortex_wait_seconds(ledger, messages, token_budget)
            if 0 < wait <= MISSION_MAX_WAIT_SECONDS:
                # Free-tier RPM windows (Gemini: 2 per minute) are paced, not tripped.
                progress.progress((finished - 1) / len(plan), text=f"Waiting {int(wait) + 1}s for a free-tier window before step {finished}…")
                time.sleep(wait + 0.5)
            # The lock covers the full selection/request/ledger-record cycle so a
            # concurrent send from another tab cannot overspend one free-tier key.
            with request_lock:
                answer, decision = generate_mode(
                    task_mode, step_type, messages, ledger,
                    max_tokens=token_budget, temperature=0.2, paid_slot=paid_slot,
                )
            answer = strip_reasoning_tags(answer)
            append_message(
                project_scope, "user", f"[{step['title']}] {step['description']}",
                mode=task_mode, workspace="task_finder", task_type=step_type,
            )
            append_message(
                project_scope, "assistant", answer, provider=f"{decision.provider}/{decision.model}",
                mode=task_mode, workspace="task_finder", task_type=step_type,
            )
            log_route("task_finder", step_type, f"{decision.provider}/{decision.model}", task_mode, started, decision.reason, decision.finish)
            st.session_state.last_decision = decision
            outputs.append((str(step["title"]), answer))
            succeeded += 1
            truncated += int(decision.finish == "length")
        except Exception as exc:
            failed += 1
            failures.append((str(step["title"]), str(exc)[:600]))
            log_route("task_finder", step_type, "failed", task_mode, started, str(exc)[:160])
        progress.progress(finished / len(plan), text=f"{succeeded} succeeded · {failed} failed · {finished}/{len(plan)} done")
    if succeeded:
        # Pinned only once there is something to continue from; an all-failed launch leaves the next message free to be a new mission.
        set_thread_mission(thread_id, goal)
    summary: Dict[str, Any] = {
        "thread_id": thread_id, "goal": goal, "steps": len(plan), "succeeded": succeeded, "failed": failed,
        "truncated": truncated, "failures": failures,
    }
    sections = [answer for title, answer in outputs if title.lower().startswith("draft section")]
    if plan and plan[0].get("kind") == "writing" and sections:
        # The deliverable is assembled deterministically and locked; the chat keeps the per-section record.
        deliverable = assemble_deliverable(goal, sections)
        slug = deliverable_slug(goal)
        artifact_id, version = save_artifact(
            project_scope, f"mission-{thread_id}-{slug}.md", f"missions/mission-{thread_id}-{slug}.md", deliverable, "markdown"
        )
        target = parse_length_target(goal)
        summary.update({
            "deliverable_artifact": int(artifact_id), "deliverable_version": int(version), "sections": len(sections),
            "measure": text_measure(deliverable), "target_words": target["words"] if target else None,
            "target_label": f"{target['amount']} {target['unit']}(s)" if target else "",
        })
    return summary


def render_mission_panel(project_scope: str, ledger: QuotaLedger, thread_id: int, goal: str, pending: Dict[int, str]) -> None:
    kind = classify_mission(goal)
    budget = int(st.session_state.get("max_tokens", 2048))
    with st.container(border=True):
        st.markdown(
            f"**Proposed mission** · type `{kind}` · edit the workstreams, then launch. "
            "Typing another message replaces this mission until it is launched."
        )
        st.markdown("> " + goal.replace("\n", "\n> "))
        if kind == "writing":
            target = parse_length_target(goal)
            target_words = target["words"] if target else None
            suggested = writing_sections(target_words or 800, budget)
            count = int(st.number_input(
                "Sections to draft", min_value=1, max_value=MAX_SECTIONS, value=suggested, key=f"task_sections_{thread_id}",
                help="One drafting call per section, sized to the output token budget; a brief step and editor's notes are added around them.",
            ))
            st.caption(
                (f"Length target: {target['amount']} {target['unit']}(s) ≈ {target_words} words. " if target else "No length named; about 800 words assumed. ")
                + f"At a {budget}-token budget each section holds roughly {max(150, int(budget * 0.55))} words, so {suggested} section(s) are suggested."
            )
        else:
            max_steps = len(MISSION_TEMPLATES[kind][1])
            count = st.slider("Workstreams", min_value=1, max_value=max_steps, value=min(3, max_steps), key=f"task_count_{thread_id}_{kind}")
        for hint in mission_hints(goal, kind, count):
            st.caption(f"Note · {hint}")
        plan = task_plan(goal, count, max_tokens=budget)
        edited = st.data_editor(
            [{"#": step["id"], "workstream": step["title"], "type": step["type"], "instruction": step["description"]} for step in plan],
            hide_index=True,
            use_container_width=True,
            num_rows="fixed",
            column_config={
                "#": st.column_config.NumberColumn(disabled=True, width="small"),
                "type": st.column_config.SelectboxColumn(options=list(TASK_TYPES), required=True, width="small"),
                "instruction": st.column_config.TextColumn(width="large"),
            },
            key=f"plan_editor_{thread_id}_{kind}_{count}",
        )
        for step, row in zip(plan, edited):
            step["title"] = str(row.get("workstream") or step["title"])
            step["type"] = str(row.get("type") or step["type"])
            step["description"] = str(row.get("instruction") or step["description"])
        heavy = active_mode() == "heavy"
        passes = 3 if heavy else 1
        calls = len(plan) * passes
        per_step = heavy_pass_tokens(budget) if heavy else budget
        upto = "up to " if heavy else ""
        st.caption(
            f"Cost preview: {len(plan)} workstream(s) × {upto}{passes} pass(es) = {upto}{calls} provider call(s), "
            f"up to ~{len(plan) * per_step} output tokens"
            + (" · Heavy Mode: draft b/2 + critique b/3 + synthesis b per workstream" if heavy else "")
        )
        launch_col, discard_col = st.columns([0.7, 0.3], gap="small")
        if launch_col.button(
            send_label("Launch workstreams"), type="primary", key=f"launch_{thread_id}", use_container_width=True,
            disabled=not configured_provider_names(),
            help="Runs the workstreams strictly in order, each one seeing the results before it; results are saved to this chat.",
        ):
            summary = execute_mission(project_scope, ledger, thread_id, goal, plan)
            st.session_state.setdefault("task_launches", {})[int(summary["thread_id"])] = summary
            pending.pop(thread_id, None)
            st.rerun()
        if discard_col.button("Discard", key=f"discard_{thread_id}", use_container_width=True):
            pending.pop(thread_id, None)
            st.rerun()


def render_task_finder(project_scope: str, ledger: QuotaLedger, submission: Optional[ChatSubmission]) -> None:
    st.subheader("Task Finder")
    st.caption(
        "Describe a mission in the chat bar; it is decomposed into workstreams you can edit before launch. "
        "Steps run one at a time to respect free-tier limits, and every result lands in this chat so the mission "
        "continues as a conversation." + mode_caption()
    )
    thread = render_thread_bar(project_scope, "task_finder", ledger)
    thread_id = int(thread["id"])
    mission = thread_mission(thread)
    has_mission = bool(mission)
    if mission:
        st.caption(f"Mission · {mission[:240]}")
    pending: Dict[int, str] = st.session_state.setdefault("pending_missions", {})
    continuing: Optional[ChatSubmission] = None
    if submission and submission.text.strip():
        if has_mission:
            continuing = submission
        else:
            pending[thread_id] = submission.text.strip()
    render_history(project_scope, "Mission conversation", workspace="task_finder")
    if continuing:
        dispatch_chat(project_scope, "task_finder", continuing, ledger)
    launches: Dict[int, Dict[str, Any]] = st.session_state.setdefault("task_launches", {})
    launch = launches.get(thread_id)
    if launch:
        if launch["failed"] == 0:
            st.success(
                f"All {launch['succeeded']} workstream(s) finished; the results are above and in this chat's memory. "
                "Continue the mission in the chat bar."
            )
        elif launch["succeeded"] == 0:
            st.error(f"All {launch['failed']} workstream(s) failed. Each error is below; fix keys or adjust the mission and send it again.")
        else:
            st.warning(f"{launch['succeeded']} workstream(s) succeeded, {launch['failed']} failed. The failed steps are below.")
        if launch.get("deliverable_artifact"):
            measure = launch.get("measure", {})
            target_note = f" against a target of {launch['target_label']} (≈{launch['target_words']} words)" if launch.get("target_words") else ""
            st.info(
                f"Deliverable assembled from {launch['sections']} section(s): {measure.get('lines', 0)} lines, "
                f"{measure.get('words', 0)} words{target_note}. Locked as artifact v{launch['deliverable_version']}; "
                "it is also under Locked artifacts in the sidebar."
            )
            filename, body = export_artifact(int(launch["deliverable_artifact"]))
            st.download_button("⬇ Download deliverable (.md)", data=body, file_name=filename, mime="text/markdown",
                               key=f"deliverable_{thread_id}_{launch['deliverable_artifact']}")
        if launch.get("truncated"):
            st.caption(f"{launch['truncated']} workstream(s) stopped at the output budget; raise it in the sidebar for fuller results.")
        for title, error in launch["failures"]:
            with st.expander(f"{title} · failed", expanded=False):
                st.code(error)
        if st.button("Dismiss", key=f"dismiss_launch_{thread_id}"):
            launches.pop(thread_id, None)
            st.rerun()
    goal = pending.get(thread_id, "")
    if goal:
        render_mission_panel(project_scope, ledger, thread_id, goal, pending)
    elif not has_mission:
        st.caption("No mission in this chat yet. Type one in the chat bar to see its proposed workstreams before anything runs.")
    else:
        st.caption("Mission in progress: the chat bar continues it with every result in context. Start another mission with New chat.")


WORKSPACE_RENDERERS = {
    "task_finder": render_task_finder,
    "repository": render_repository_work,
    "chat_bot": render_chat_bot,
    "normal_chat": render_normal_chat,
}


def render_routing_log() -> None:
    scope = str(st.session_state.get("project_scope", "chat-johnson"))
    st.markdown("#### Routing log (this session)")
    entries = st.session_state.get("routing_log", [])
    if entries:
        st.dataframe(list(reversed(entries)), hide_index=True, use_container_width=True)
    else:
        st.caption(
            "Every send is recorded here: workspace → task type → provider/model, latency, finish reason, and the "
            "solver's reason, so routing can be judged against the project's vision over a long session."
        )
    st.markdown("#### Ops · last 24 hours (persisted in the vault)")
    stats = route_stats(scope, hours=24.0)
    if stats:
        st.dataframe(stats, hide_index=True, use_container_width=True)
        st.caption("share = fraction of sends; p50/p95 = latency percentiles in ms; 'failed' rows are sends no endpoint answered.")
    else:
        st.caption("No sends recorded yet in this project scope.")
    if recent_routes(scope, limit=1):
        st.download_button(
            "⬇ Routing log (.csv, up to 5000 rows)", data=routes_csv(scope), file_name=f"routing-log-{scope}.csv", mime="text/csv",
            key="routes_csv", use_container_width=True,
        )


# =============================================================================
# Page layout
# =============================================================================

st.markdown(
    """
    <style>
      .stApp { background: radial-gradient(circle at 12% 0%, #172b4d 0, #0a1220 42%, #070d17 100%); }
      section[data-testid="stSidebar"] { background: #0b1527; border-right: 1px solid #203452; }
      h1, h2, h3 { color: #f3f7ff; letter-spacing: .2px; }
      .eyebrow { color: #67e8c2; font-size: .72rem; font-weight: 800; letter-spacing: .18em; text-transform: uppercase; }
      .subtitle { color: #aebdd6; font-size: 1rem; }
      .status-ready { color: #70efc4; }
      .status-off { color: #8190aa; }
      div[data-testid="stForm"] { border: 1px solid #233959; border-radius: 16px; padding: 12px; background: rgba(17, 29, 49, .72); }
      div[data-testid="stBottom"] > div, div[data-testid="stBottomBlockContainer"] { background: rgba(7, 13, 23, .94); }
    </style>
    """,
    unsafe_allow_html=True,
)

if "project_scope" not in st.session_state:
    st.session_state.project_scope = "chat-johnson"
if "byok_keys" not in st.session_state:
    st.session_state.byok_keys = {}
# Keys pasted in this browser session are bound to this script run only.
bind_session_keys(st.session_state.byok_keys)
if "heavy_mode" not in st.session_state:
    st.session_state.heavy_mode = False

if _query_value("health") == "1":
    # Machine-readable liveness for the smoke drive and a human glance; no keys, no provider calls.
    vault_state = health_check()
    st.code(
        json.dumps(
            {
                "status": "ok" if vault_state["ok"] else "degraded",
                "build": build_marker(),
                "streamlit": st.__version__,
                "python": platform.python_version(),
                "vault": vault_state,
                "keyed_vendors": configured_provider_names(),
                "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            indent=2,
        ),
        language="json",
    )
    st.stop()

ledger = get_quota_ledger()
with st.sidebar:
    st.markdown(f"<div class='eyebrow'>Chat Johnson · Gen 2 · build {build_marker()}</div>", unsafe_allow_html=True)
    st.title("Control deck")
    if "byok_expanded" not in st.session_state:
        st.session_state.byok_expanded = not configured_provider_names()
    with st.expander("🔑 API keys (BYOK, session only)", expanded=st.session_state.byok_expanded):
        st.caption(
            "Paste free-tier keys here to use the studio like a normal user. They are scoped to "
            "this browser session only, override environment variables, and are never written "
            "to SQLite, logs, artifacts, or git."
        )
        key_fields = (
            ("GEMINI_API_KEY", "Google AI Studio (Gemini)"),
            ("GROQ_API_KEY", "Groq Cloud"),
            ("HF_TOKEN", "Hugging Face"),
            ("NVIDIA_API_KEY", "NVIDIA NIM"),
            ("OPENROUTER_API_KEY", "OpenRouter"),
            ("CEREBRAS_API_KEY", "Cerebras"),
            ("MISTRAL_API_KEY", "Mistral"),
        )
        with st.form("byok_form", clear_on_submit=False):
            entered: Dict[str, str] = {}
            for env_name, label in key_fields:
                entered[env_name] = st.text_input(label, type="password", key=f"byok_{env_name}", placeholder=env_name)
            apply_col, clear_col = st.columns(2)
            apply_clicked = apply_col.form_submit_button("Apply keys", type="primary", use_container_width=True)
            clear_clicked = clear_col.form_submit_button("Clear all", use_container_width=True)
        if apply_clicked:
            added = updated = removed = 0
            for env_name, value in entered.items():
                cleaned = value.strip()
                previous = st.session_state.byok_keys.get(env_name)
                if cleaned and previous is None:
                    st.session_state.byok_keys[env_name] = cleaned
                    added += 1
                elif cleaned and cleaned != previous:
                    st.session_state.byok_keys[env_name] = cleaned
                    updated += 1
                elif not cleaned and previous is not None:
                    st.session_state.byok_keys.pop(env_name, None)
                    removed += 1
            bind_session_keys(st.session_state.byok_keys)
            if added or updated or removed:
                st.success(f"Keys: {added} added, {updated} updated, {removed} removed (blank a field and Apply to remove one).")
            else:
                st.info("No key changes.")
        if clear_clicked:
            st.session_state.byok_keys = {}
            bind_session_keys({})
            for env_name, _ in key_fields:
                st.session_state.pop(f"byok_{env_name}", None)
            st.info("Session keys cleared. Environment variables, if any, remain in effect.")
            st.rerun()
        if st.button("Test keys (1 small call per configured provider; every retry is metered)", key="probe_keys", use_container_width=True):
            with st.spinner("Probing endpoints…"):
                probe_rows = probe_all_endpoints(ledger=get_quota_ledger())
            for row in probe_rows:
                marker = "✅" if row["ok"] else ("⚪" if row["detail"] == "no key configured" else "❌")
                status = f" · HTTP {row['status']}" if row["status"] else ""
                st.caption(f"{marker} **{row['endpoint']}** · {row['model']} · key {row['key']}{status} · {row['detail']}")
    project_input = st.text_input("Active project scope", value=st.session_state.project_scope, key="project_scope_input")
    st.session_state.project_scope = project_input.strip() or "chat-johnson"

    st.subheader("Thread health agent")
    st.checkbox(
        "Auto-migrate heavy threads",
        key="auto_migrate",
        value=True,
        help="Applies to every workspace. Before each send the health agent checks the active chat's load, repetition, and "
        "error loops. When a threshold trips, it compresses the chat into a locked vision digest and continues in a fresh "
        "optimized chat. Thresholds: 300 messages, ~18k live tokens, 2 stacked summaries; repetition and error loops are advisories.",
    )
    st.caption(
        "Each workspace has its own chats: New chat, Clear chat, Delete chat, and More (rename, context load, Migrate now). "
        "The chat bar at the bottom of the screen always sends to the workspace selected at the top."
    )
    st.checkbox(
        "Heavy Mode",
        key="heavy_mode",
        help="Uses a bounded draft → review → synthesis workflow. Private reasoning is not displayed; token usage and latency are higher.",
    )
    st.session_state.max_tokens = st.slider("Output token budget", 256, 8192, 2048, 256, key="output_token_budget")
    st.caption(
        "Heavy Mode is an auditable multi-pass policy, not an exposed chain-of-thought channel. "
        "It uses only configured BYOK providers."
    )
    with st.expander("Paid reasoning slot (Heavy Mode critique only)", expanded=False):
        st.caption(
            "Backend is free-tier only. This optional slot must be switched on AND given a key "
            "every session; it is never saved, never read from the environment, and only the "
            "Heavy Mode review pass uses it."
        )
        st.checkbox("Enable paid slot for this session", key="paid_slot_enabled", value=False)
        st.text_input("Paid slot API key (session memory only)", key="paid_slot_key", type="password", value="")
        st.text_input("Paid slot model", key="paid_slot_model", value="o3-mini")
        slot_status = session_paid_slot().status()
        if slot_status["armed"]:
            st.warning(f"ARMED: {slot_status['model']} will be billed for Heavy Mode review passes this session.")
        elif slot_status["enabled"] and not slot_status["key_present"]:
            st.info("Toggle is on but no key was entered; the slot stays disarmed.")
        else:
            st.caption("Disarmed. Nothing paid is reachable.")
    with st.expander("GitHub push (session only)", expanded=False):
        st.caption(
            "Off by default. Switch it on AND paste a fine-grained token with Contents and Pull requests write access "
            "every session; nothing is stored. A push is one commit on a new branch plus an opened pull request, only "
            "when you press the button in Repository Work. The default branch is never written to."
        )
        st.checkbox("Enable GitHub push for this session", key="github_push_enabled", value=False)
        st.text_input("GitHub token (session memory only)", key="github_push_token", type="password", value="")
        push_state = github_push_status()
        if push_state["armed"] and push_state["login"]:
            st.caption(f"Signed in as **{push_state['login']}**.")
        elif push_state["armed"]:
            st.caption("Token accepted, but it cannot read its own account (needs at least read access to your profile).")
        if push_state["armed"] and push_state["connected"]:
            st.warning(f"ARMED: pushes go to {push_state['repo']} as new branches with pull requests.")
        elif push_state["armed"]:
            st.info("Token armed. Pick and connect a repository in Repository Work → Work; pushes go there.")
        elif push_state["enabled"]:
            st.info("Toggle is on but no token was pasted; the slot stays disarmed.")
        else:
            st.caption("Disarmed. Nothing can be written to GitHub.")
    st.divider()
    st.subheader("BYOK channels")
    for name, label, detail, configured in provider_status_rows():
        marker = "●" if configured else "○"
        css_class = "status-ready" if configured else "status-off"
        st.markdown(f"<span class='{css_class}'>{marker}</span> **{label}**", unsafe_allow_html=True)
        st.caption(detail)
    if not configured_provider_names():
        st.warning("No provider keys detected. Paste them in the API keys panel above or set environment variables; this app never stores them in SQLite.")
    st.divider()
    st.subheader("Locked artifacts")
    artifact_query = st.text_input("Search artifacts", key="artifact_query", placeholder="keywords, any order: name, path, or summary")
    artifacts = (
        search_artifacts(st.session_state.project_scope, artifact_query, 12)
        if artifact_query.strip()
        else recent_artifacts(st.session_state.project_scope, 6)
    )
    if artifacts:
        for artifact in artifacts:
            filename, body = export_artifact(int(artifact["id"]))
            st.caption(f"v{artifact['version']} · {artifact['name']} · {artifact['structural_summary'][:100]}")
            st.download_button(
                f"⬇ {filename}",
                data=body,
                file_name=filename,
                key=f"download-artifact-{artifact['id']}",
                use_container_width=True,
            )
    else:
        st.caption("No artifacts in this scope yet.")
    st.divider()
    st.subheader("Memory")
    active_count = len(recent_messages(st.session_state.project_scope, MESSAGE_WINDOW, thread_id=-1))
    archive_count = len(archived_messages(st.session_state.project_scope, 5000, thread_id=-1))
    summary_rows = recent_summaries(st.session_state.project_scope, 50, thread_id=-1)
    thread_rows_all = list_threads(st.session_state.project_scope, limit=500)
    per_workspace = {key: sum(1 for row in thread_rows_all if row["workspace"] == key) for _, key in WORKSPACE_TABS}
    st.caption(
        f"Active messages {active_count} · archived {archive_count} · summaries {len(summary_rows)} across "
        + ", ".join(f"{label} {per_workspace[key]} chat(s)" for label, key in WORKSPACE_TABS)
        + ". Raw history is texturized and archived; only Delete chat removes anything."
    )
    st.divider()
    with st.expander("Roadmap & stubs (not yet built)", expanded=False):
        st.caption("Honest status of bible features that are not implemented. Nothing here can be switched on.")
        for row in connector_status():
            marker = "●" if row.status == "healthy" else "○"
            note = " · source of truth" if row.source_of_truth else ""
            st.markdown(f"{marker} **{row.name}** · {row.status}{note}")
            st.caption(row.detail)
        for feature, status, detail in ROADMAP_FEATURES:
            st.toggle(f"{feature} · {status}", value=False, disabled=True, key=f"roadmap_{feature}", help=detail)

st.markdown("<div class='eyebrow'>Sovereign local-first execution workspace</div>", unsafe_allow_html=True)
st.title("Chat Johnson Master Studio")
st.caption(
    "A CVO workbench for routing focused work, preserving project context, and keeping every code handoff reviewable. "
    "Project Seth's stochastic signal is an experimental routing feature only; it does not establish propulsion, lift, "
    "or a physical mechanism."
)

scope = st.session_state.project_scope
workspace = render_workspace_switch()
# Created at the top level on purpose: inside a column or tab the chat bar would render inline instead of pinned.
submission = render_chat_bar(workspace)

WORKSPACE_RENDERERS[workspace](scope, ledger, submission)

st.divider()
# One column: a side panel squeezed the chat to a sliver at iPad width. The canvas opens itself when markup arrives.
with st.expander("Live preview canvas", expanded=bool(st.session_state.get("preview_source"))):
    render_preview_panel()
with st.expander("Routing log, Ops stats & last decision", expanded=False):
    render_routing_log()
    decision = st.session_state.get("last_decision")
    if decision:
        st.markdown("#### Last routing decision")
        st.json(
            {
                "provider": decision.provider,
                "model": decision.model,
                "task_type": decision.task_type,
                "solver": decision.solver,
                "reason": decision.reason,
                "decision_vector": decision.decision_vector,
            },
            expanded=False,
        )
    st.markdown("#### Operating boundaries")
    st.caption(
        "Local SQLite remains authoritative. GitHub is written only through the session-only push slot (new branch + pull "
        "request, never the default branch). Cloud connectors and artifact publication still require explicit future opt-in."
    )
