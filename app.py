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
import sqlite3
import os
import re
import threading
import time
from dataclasses import dataclass
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

from orchestrator.config import PROVIDERS, bind_session_keys, provider_model, resolve_secret
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
    strip_reasoning_tags,
)


# =============================================================================
# Local source of truth (orchestrator/vault.py)
# =============================================================================

from orchestrator.capabilities import capability_card
from orchestrator.connectors import ROADMAP_FEATURES, connector_status
from orchestrator.github_auth import mint_state, verify_state
from orchestrator.missions import MISSION_TEMPLATES, classify_mission, task_plan
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
    initialize_database,
    list_threads,
    migrate_thread,
    recent_artifacts,
    recent_messages,
    recent_summaries,
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
    """Short git commit of the running checkout, so a deploy can be verified at a glance."""
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
    st.caption("Sandboxed local changes, reviewable diffs, and an explicit human handoff." + mode_caption())
    render_thread_bar(project_scope, "repository", ledger)
    code = _query_value("code")
    received_state = _query_value("state")
    token = st.session_state.get("github_token", "")
    with st.expander("GitHub identity (optional, profile-only OAuth)", expanded=bool(token or (code and received_state))):
        st.caption(
            "Least-privilege OAuth skeleton: the token lives in session memory only, no SSH keys are collected, "
            "and nothing is committed or pushed automatically."
        )
        oauth_ready = all(
            os.environ.get(key, "").strip()
            for key in ("GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET", "GITHUB_REDIRECT_URI")
        )
        if code and received_state and "github_token" not in st.session_state:
            try:
                st.session_state.github_token = exchange_github_code(
                    code,
                    str(st.session_state.get("github_oauth_state", "")),
                    received_state,
                )
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
            st.caption("This reads your GitHub profile only. No repository is read, written, committed, or pushed by this app.")
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

    st.markdown("**Local sandbox pipeline**")
    repo_path = st.text_input(
        "Repository path", value="", key="repo_path",
        placeholder="Absolute path to a local git repository (never the deployed app's own checkout)",
    )
    repo_goal = st.text_area(
        "Requested repository change",
        height=100,
        key="repo_goal",
        placeholder="Describe a reviewable change; generated edits stay in an isolated sandbox.",
    )
    run_repo = st.button(
        "Run sandboxed repository pipeline",
        type="primary",
        key="run_repo_pipeline",
        disabled=not bool(repo_goal.strip() and repo_path.strip() and configured_provider_names()),
    )
    if run_repo:
        app_root = str(Path(__file__).resolve().parent)
        if not os.path.isdir(repo_path):
            st.error("Repository path does not exist.")
        elif os.path.realpath(repo_path) == os.path.realpath(app_root):
            st.error("Refusing to run the pipeline on the studio's own checkout; point it at another repository.")
        else:
            with st.status("Ingesting, patching, and verifying…", expanded=True) as status:
                try:
                    report = Orchestrator(ledger=ledger).run(repo_goal, repo_path=repo_path)
                    status.update(label="Pipeline completed", state="complete")
                    if report["branch"] == "(copy-mode)":
                        st.warning(
                            "git worktree was unavailable, so the sandbox is a copied tree under the temp directory. "
                            "The diff below is computed against your source tree; nothing was committed."
                        )
                    else:
                        st.success(f"Sandbox branch: {report['branch']}")
                    st.json(report["ingest"], expanded=False)
                    if report.get("diff"):
                        st.code(report["diff"][:20_000], language="diff")
                    st.text_area("Execution memory", report.get("memory", ""), height=180)
                except Exception as exc:
                    status.update(label="Pipeline stopped", state="error")
                    st.error(f"Repository pipeline failed: {exc}")
    st.divider()
    render_history(project_scope, "Repository conversation", workspace="repository")
    dispatch_chat(project_scope, "repository", submission, ledger)


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


def log_route(workspace: Optional[str], task_type: str, route: str, mode: str, started: float, reason: str) -> None:
    """Session-only trace of every send: what was asked, where it went, how long it took, and why."""
    log: List[Dict[str, Any]] = st.session_state.setdefault("routing_log", [])
    log.append(
        {
            "time": time.strftime("%H:%M:%S"),
            "workspace": WORKSPACE_LABEL.get(workspace or "", workspace or "-"),
            "task": task_type,
            "route": route,
            "mode": mode,
            "ms": int((time.perf_counter() - started) * 1000),
            "reason": (reason or "")[:160],
        }
    )
    del log[:-ROUTING_LOG_LIMIT]


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
    log_route(workspace, task_type, f"{decision.provider}/{decision.model}", mode, started,
              decision.reason + (f"; finish={decision.finish}" if decision.finish else ""))
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
        on_change=_on_select, label_visibility="collapsed",
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
            log_route("task_finder", step_type, f"{decision.provider}/{decision.model}", task_mode, started, decision.reason)
            st.session_state.last_decision = decision
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
    return {
        "thread_id": thread_id, "goal": goal, "steps": len(plan), "succeeded": succeeded, "failed": failed,
        "truncated": truncated, "failures": failures,
    }


def render_mission_panel(project_scope: str, ledger: QuotaLedger, thread_id: int, goal: str, pending: Dict[int, str]) -> None:
    kind = classify_mission(goal)
    max_steps = len(MISSION_TEMPLATES[kind][1])
    with st.container(border=True):
        st.markdown(
            f"**Proposed mission** · type `{kind}` · edit the workstreams, then launch. "
            "Typing another message replaces this mission until it is launched."
        )
        st.markdown("> " + goal.replace("\n", "\n> "))
        count = st.slider("Workstreams", min_value=1, max_value=max_steps, value=min(3, max_steps), key=f"task_count_{thread_id}_{kind}")
        plan = task_plan(goal, count)
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
        budget = int(st.session_state.get("max_tokens", 2048))
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
    st.markdown("#### Routing log (this session)")
    entries = st.session_state.get("routing_log", [])
    if not entries:
        st.caption(
            "Every send is recorded here: workspace → task type → provider/model, latency, and the solver's reason, "
            "so routing can be judged against the project's vision over a long session."
        )
        return
    st.dataframe(list(reversed(entries)), hide_index=True, use_container_width=True)


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
    artifact_query = st.text_input("Search artifacts", key="artifact_query", placeholder="name, path, or summary")
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
with st.expander("Routing log & last decision", expanded=False):
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
        "Local SQLite remains authoritative. Cloud connectors, GitHub writes, commits, pushes, and artifact publication require explicit future opt-in."
    )
