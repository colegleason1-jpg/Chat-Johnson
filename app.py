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
import secrets as _secrets
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


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
    import requests  # noqa: F401  (the GitHub and provider adapters need it; a missing wheel should fail here)
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
from orchestrator.discovery import vendor_for
from orchestrator.errors import plain_error
from orchestrator.executor import Orchestrator
from orchestrator import mission_runner  # noqa: F401  (registers the mission job handler)
from orchestrator.jobs import ACTIVE_STATUSES, enqueue as enqueue_job, get_runner
from orchestrator import society
from orchestrator.society import store as society_store
from orchestrator.society.personas import board_persona
from orchestrator.prompting import build_prompt_messages as _build_prompt_messages
from orchestrator.quota import QuotaLedger
from orchestrator.quota_registry import get_quota_ledger, get_request_lock
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
    repository_context_chars,
    strip_reasoning_tags,
)


# =============================================================================
# Local source of truth (orchestrator/vault.py)
# =============================================================================

from orchestrator.connectors import ROADMAP_FEATURES, connector_status
from orchestrator.deploykit import CLOUDS, LANGUAGES, TARGETS, KitSpec, generate_kit, kit_zip, summarize, validate_kit
from orchestrator.github_push import GitHubPushError, GitHubWriter, PushRecord, branch_name_for
from orchestrator.github_repo import GitHubRepoError, collect_changed_files, fetch_tree, list_repositories, looks_like_owner_repo, qualify_repository, whoami
from orchestrator.repo_ingest import repo_prompt_context
from orchestrator.missions import (
    MAX_SECTIONS,
    MISSION_TEMPLATES,
    classify_mission,
    deliverable_slug,
    mission_hints,
    parse_length_target,
    task_plan,
    writing_sections,
)
from orchestrator.preview import extract_preview_source, safe_preview_document
from orchestrator.vault import (
    answer_job,
    job_view,
    list_jobs,
    request_cancel,
    MESSAGE_WINDOW,
    WORKSPACES as VAULT_WORKSPACES,
    active_thread,
    append_message,
    archived_messages,
    clear_thread,
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
    switch_thread,
    thread_health,
    thread_transcript,
)

initialize_database()
# Background workers start once per process (CHAT_JOHNSON_JOB_WORKERS=0 keeps them idle, as the tests do).
get_runner()


# =============================================================================
# Provider/runtime helpers
# =============================================================================

def get_task_request_lock() -> threading.Lock:
    """Serialize provider calls for this visitor's keys around quota check + record; background jobs share it."""
    return get_request_lock()


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


def build_prompt_messages(
    project_scope: str,
    user_prompt: str,
    injected_context: str = "",
    workspace: Optional[str] = None,
    thread_id: Optional[int] = None,
    extra_system: str = "",
) -> List[Dict[str, str]]:
    """The shared prompt builder with this session's output budget."""
    return _build_prompt_messages(
        project_scope, user_prompt, injected_context, workspace=workspace, thread_id=thread_id,
        max_tokens=int(st.session_state.get("max_tokens", 2048)), extra_system=extra_system,
    )


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
# Query params and visitor scope
# =============================================================================


def _query_value(name: str) -> str:
    try:
        query_params = st.query_params
        value = query_params.get(name, "")
        return str(value[0] if isinstance(value, list) else value)
    except AttributeError:
        values = st.experimental_get_query_params()
        return str(values.get(name, [""])[0])


SCOPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def resolve_visitor_scope() -> str:
    """A private scope per browser session: ``?scope=`` on the URL wins, else a fresh visitor id is minted."""
    requested = _query_value("scope").strip()
    if requested and SCOPE_RE.match(requested):
        return requested
    return f"visitor-{_secrets.token_hex(6)}"


def remember_scope(scope: str) -> None:
    """Keep the scope on the URL so a reload (and a bookmark) returns to the same chats."""
    try:
        if st.query_params.get("scope") != scope:
            st.query_params["scope"] = scope
    except Exception:  # very old Streamlit builds keep it in session state only
        pass


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
    if fetched and fetched.get("empty"):
        text = (
            f"REPOSITORY CONTEXT ({fetched['owner']}/{fetched['repo']}): connected but EMPTY, no commits and no files yet. "
            "Propose and build the structure: name folders and files with their purpose; the Repository Work pipeline creates "
            "them and the first push makes the initial commit."
        )
        return text, f"Repository in context: {fetched['owner']}/{fetched['repo']} · empty, nothing committed yet. Ask the chat to propose a structure, then run the pipeline to create it.", True
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
            if fetched.get("empty"):
                c1.success(
                    f"Connected {fetched['owner']}/{fetched['repo']} · empty repository (no commits yet). Describe the structure "
                    f"you want below; the pipeline creates the files, and the first push makes the initial commit on {fetched['ref']}."
                )
            else:
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
            settings.max_output_tokens = int(st.session_state.get("max_tokens", 2048))
            if not run_tests:
                settings.max_test_rounds = 0
            with st.status("Ingesting, planning, patching, and verifying… free-tier windows are waited for, not tripped.", expanded=True) as status:
                try:
                    report = Orchestrator(settings=settings, ledger=ledger).run(repo_goal, repo_path=repo_path)
                    failed_steps = int(report.get("failed_steps", 0))
                    if failed_steps:
                        status.update(label=f"Pipeline finished with {failed_steps} failed step(s); see the step table", state="error")
                    else:
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
    steps = report.get("steps") or []
    if steps:
        st.dataframe(
            [{"step": s["id"], "title": s["title"], "type": s["type"], "provider": s["provider"], "status": s["status"], "note": s["note"][:300]} for s in steps],
            hide_index=True, use_container_width=True,
        )
    ingest = report.get("ingest", {})
    st.caption(f"Ingested {ingest.get('file_count', 0)} file(s) (~{ingest.get('est_tokens', 0)} tokens) from the sandbox source.")
    diff = str(report.get("diff") or "")
    changed_pairs, changed_missing = collect_changed_files(str(report.get("sandbox") or ""), diff)
    if not changed_pairs and not changed_missing:
        failed = [s for s in steps if s["status"] == "failed"]
        if failed:
            st.warning(
                "No files were written. " + "; ".join(f"step {s['id']} ({s['title']}): {s['note'][:200]}" for s in failed)
                + ". Retry below asks the model for file blocks only."
            )
        else:
            st.info("The pipeline ran but changed no files: the plan had no code step, or the answers had no file blocks. Retry below asks for file blocks only.")
        if st.button("Retry, file blocks only", key="repo_retry_strict", type="primary",
                     help="Re-runs with an explicit instruction to answer with one ```file: path block per file and nothing else."):
            strict_goal = (
                last["goal"] + "\n\nRespond ONLY with fenced ```file: relative/path blocks, one per file, each with the complete "
                "file content. Create every folder through its files. No prose."
            )
            settings = get_settings()
            settings.max_output_tokens = int(st.session_state.get("max_tokens", 2048))
            if not st.session_state.get("repo_run_tests", False):
                settings.max_test_rounds = 0
            source_path = str(last["fetched"]["path"]) if last.get("fetched") else str(st.session_state.get("repo_path", ""))
            with st.status("Re-running with the strict file format…", expanded=True) as status:
                try:
                    report = Orchestrator(settings=settings, ledger=ledger).run(strict_goal, repo_path=source_path)
                    status.update(label="Pipeline completed", state="complete")
                    st.session_state.repo_last_report = {**last, "report": report}
                    st.rerun()
                except Exception as exc:
                    status.update(label="Pipeline stopped", state="error")
                    st.error(f"Repository pipeline failed: {exc}")
    else:
        pairs, missing = changed_pairs, changed_missing
        st.caption(f"Changed files: {', '.join(path for path, _ in pairs) or 'none readable'}" + (f" · not pushable: {', '.join(missing)}" if missing else ""))
        st.code(diff[:20_000], language="diff")
        st.download_button("⬇ Download patch (.diff)", data=diff, file_name="chat-johnson-change.diff", mime="text/x-diff", key="repo_patch_download")
        if last["source"] == "GitHub repository" and last.get("fetched") and pairs:
            if push_state["armed"] and push_state["connected"] and last["fetched"].get("empty"):
                render_initial_commit_push(pairs, f"Chat Johnson: {last['goal'][:60]}", "repo_init_push")
            elif push_state["armed"] and push_state["connected"]:
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


def render_initial_commit_push(pairs: Sequence[Tuple[str, str]], message: str, key: str) -> None:
    """Empty repository: the push creates the first commit on the default branch; there is no base for a pull request."""
    fetched = st.session_state.get("repo_fetched") or {}
    branch = str(fetched.get("ref") or "main")
    st.info(
        f"{fetched.get('owner')}/{fetched.get('repo')} has no commits, so this push creates the initial commit on **{branch}** "
        "directly. A pull request needs a base to compare against; after this first commit, every later push goes to a new "
        "branch with a pull request."
    )
    if st.button(f"Create the first commit on {branch} with {len(pairs)} file(s)", type="primary", key=key, use_container_width=True):
        token = str(st.session_state.get("github_push_token", ""))
        repo = f"{fetched.get('owner')}/{fetched.get('repo')}"
        try:
            with st.spinner("Creating the first commit…"):
                record = GitHubWriter(token, repo).initialize_repository(pairs, branch, message)
                refreshed = fetch_tree(repo, branch, token)
            st.session_state.setdefault("kit_pushes", []).append(record)
            st.session_state.repo_fetched = refreshed.as_dict()
            st.session_state.pop("repo_context_cache", None)
            st.success(f"Initial commit {record.commit_sha[:7]} on {branch} with {len(record.files)} file(s): {record.pr_url}. The repository is now connected at that commit.")
        except (GitHubPushError, GitHubRepoError) as exc:
            st.error(f"Push failed: {exc}")


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


def render_push_ledger() -> None:
    """Every push made this session (Deploy Kit or pipeline) with a revert action."""
    state = github_push_status()
    pushes: List[PushRecord] = st.session_state.setdefault("kit_pushes", [])
    if not pushes:
        st.caption("No pushes this session.")
        return
    for index, record in enumerate(pushes):
        label = {"revert": "revert", "init": "initial commit"}.get(record.kind, "push")
        target = f"PR #{record.pr_number}" if record.pr_number else "no pull request (first commit)"
        st.caption(f"{label} · {record.owner}/{record.repo} · branch {record.branch} · {target} · {record.pr_url}")
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

**Empty repositories** connect too: the sandbox starts blank, the chat proposes a structure, the pipeline creates the files, and the first push makes the initial commit on the default branch (later pushes get a branch and a pull request).

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
    elif (st.session_state.get("repo_fetched") or {}).get("empty"):
        render_initial_commit_push(pairs, f"Add deploy kit for {spec.app_name}", "kit_init_push")
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
    ("Company", "company"),
    ("Academy", "academy"),
)
assert set(key for _, key in WORKSPACE_TABS) <= set(VAULT_WORKSPACES)  # "society" and "academy" have no tab of their own
WORKSPACE_LABEL = {key: label for label, key in WORKSPACE_TABS}
DEFAULT_WORKSPACE_KEY = "normal_chat"
CHAT_FILE_TYPES = ["py", "js", "ts", "tsx", "jsx", "json", "md", "txt", "yml", "yaml", "toml", "css", "html"]
# Streamlit >= 1.43 puts a paperclip in the chat bar; older builds fall back to an uploader in the body.
CHAT_INPUT_ACCEPTS_FILES = "accept_file" in inspect.signature(st.chat_input).parameters
INJECTION_BUDGET_CHARS = 120_000
ROUTING_LOG_LIMIT = 40
MISSION_PREFIX = "MISSION: "
JOB_LABELS = {"mission": "Mission", "company_cycle": "Company cycle", "academy_cycle": "Academy cycle"}
BACKLOG_LINE = re.compile(r"^\s*BACKLOG:\s*(.+?)\s*::\s*(.+?)\s*$", re.M)
CHAT_MAX_WAIT_SECONDS = 65  # one free-tier window; longer waits surface as the plain error instead
KEY_GUIDES = (
    ("Google AI Studio (Gemini)", "https://aistudio.google.com/app/apikey", ("Sign in with a Google account", "Create API key", "Copy it into GEMINI_API_KEY")),
    ("Groq Cloud", "https://console.groq.com/keys", ("Sign up (free)", "Create API Key", "Copy it into GROQ_API_KEY")),
    ("Hugging Face", "https://huggingface.co/settings/tokens", ("Sign up (free)", "New token, read scope", "Copy it into HF_TOKEN")),
    ("NVIDIA NIM", "https://build.nvidia.com/", ("Sign in", "Get API key on any model page", "Copy it into NVIDIA_API_KEY")),
    ("OpenRouter", "https://openrouter.ai/keys", ("Sign up", "Create key (free models need no credit)", "Copy it into OPENROUTER_API_KEY")),
    ("Cerebras", "https://cloud.cerebras.ai/", ("Sign up", "API keys → Create", "Copy it into CEREBRAS_API_KEY")),
    ("Mistral", "https://console.mistral.ai/api-keys", ("Sign up, choose the free tier", "Create new key", "Copy it into MISTRAL_API_KEY")),
)
MODEL_OVERRIDE_FIELDS = (
    ("Gemini model", ("CORTEX_GEMINI_MODEL", "GEMINI_MODEL")),
    ("Groq model", ("CORTEX_GROQ_MODEL", "GROQ_MODEL")),
    ("Hugging Face model", ("CORTEX_HF_MODEL",)),
    ("NVIDIA model", ("NVIDIA_MODEL",)),
    ("OpenRouter model", ("OPENROUTER_MODEL",)),
    ("Cerebras model", ("CEREBRAS_MODEL",)),
    ("Mistral model", ("MISTRAL_MODEL",)),
)


def _short_count(value: float) -> str:
    value = float(value)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(int(value))


def usage_sentence(ledger: QuotaLedger, vendor: str) -> str:
    """Plain usage line per vendor: this minute's requests and tokens, today's tokens, and when a full window resets."""
    if not ledger.known(vendor):
        return "No calls yet this session."
    use = ledger.usage(vendor)
    parts = [f"this minute {use['rpm_used']}/{use['rpm_limit']} requests, {_short_count(use['tpm_used'])}/{_short_count(use['tpm_limit'])} tokens"]
    if use.get("daily_limit"):
        parts.append(f"today {_short_count(use['daily_tokens'])}/{_short_count(use['daily_limit'])} tokens")
    else:
        parts.append(f"today {_short_count(use['daily_tokens'])} tokens")
    wait = ledger.wait_seconds(vendor, 1)
    if wait > 0:
        parts.append(f"window full, resets in {int(wait) + 1} s")
    return " · ".join(parts)


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
    extra_system: str = "",
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
    messages = build_prompt_messages(project_scope, clean_prompt, injected_context, workspace=workspace, extra_system=extra_system)
    max_tokens = int(st.session_state.get("max_tokens", 2048))
    with st.chat_message("user"):
        st.markdown(clean_prompt)
        for note in attachment_notes or []:
            st.caption(f"Attached · {note}")
    scroll_to_bottom(user_message_id)
    live_box = st.empty()
    started = time.perf_counter()
    request_lock = get_task_request_lock()
    try:
        wait = cortex_wait_seconds(ledger, messages, max_tokens)
        if 0 < wait <= CHAT_MAX_WAIT_SECONDS:
            # Paced like missions and the pipeline: a full free-tier window is a short wait, not an error.
            with live_box.container():
                st.info(f"Free-tier window is full; sending in {int(wait) + 1} s…")
            time.sleep(wait + 0.5)
            live_box.empty()
        request_lock.acquire()
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
        # Shown in plain words; the raw vendor body stays in the session's provider events only.
        st.session_state.setdefault("provider_events", []).append(str(exc)[:600])
        log_route(workspace, task_type, "failed", mode, started, str(exc)[:160])
        st.error(plain_error(exc))
        with st.expander("Technical detail", expanded=False):
            st.code(str(exc)[:600])
        return user_message_id
    finally:
        if request_lock.locked():
            try:
                request_lock.release()
            except RuntimeError:
                pass
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
        for bucket in ("pending_missions",):
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
            for bucket in ("pending_missions",):
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
    if workspace == "company":
        return "Tell the Executive Assistant an idea, a directive, or a question…"
    if workspace == "academy":
        return "The academy has no chat: talk to a company's Executive Assistant in the Company workspace"
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


def launch_mission(project_scope: str, ledger: QuotaLedger, thread_id: int, goal: str, plan: Sequence[Dict[str, Any]]) -> int:
    """Queue the mission as a background job; the chat shows each result as it lands and the bar stays free."""
    migration = health_sweep(project_scope, ledger, workspace="task_finder")
    if migration:
        st.info(f"Thread health agent migrated to optimized chat #{migration['new_thread_id']} before launching.")
        thread_id = int(migration["new_thread_id"])
    task_mode = active_mode()
    append_message(project_scope, "user", f"{MISSION_PREFIX}{goal}", mode=task_mode, thread_id=thread_id, workspace="task_finder", task_type="plan")
    payload = {
        "goal": goal, "plan": [dict(step) for step in plan], "mode": task_mode,
        "max_tokens": int(st.session_state.get("max_tokens", 2048)),
        "paid_model": str(st.session_state.get("paid_slot_model", "") or ""),
        "paid_enabled": bool(st.session_state.get("paid_slot_enabled", False)),
    }
    # Keys go to the runner's in-memory stash for this job only; the row never holds them.
    job_secrets = dict(st.session_state.get("byok_keys", {}) or {})
    if task_mode == "heavy" and st.session_state.get("paid_slot_key"):
        job_secrets["paid_slot_key"] = str(st.session_state.get("paid_slot_key"))
    return enqueue_job(project_scope, mission_runner.KIND, payload, job_secrets, thread_id=thread_id)


def render_jobs_strip(project_scope: str) -> None:
    """Background jobs for this scope: progress, Cancel, and the answer box when one is waiting on the operator.

    Polls every two seconds while something is queued, running, or waiting; a full rerun follows
    each step so the chat underneath shows the new messages.
    """
    if not list_jobs(project_scope, ACTIVE_STATUSES, limit=1):
        st.session_state.pop("jobs_seen", None)
        return

    def body() -> None:
        jobs = [job_view(row) for row in list_jobs(project_scope, ACTIVE_STATUSES, limit=6)]
        snapshot = [(job["id"], job["status"], job["progress"].get("step")) for job in jobs]
        seen = st.session_state.get("jobs_seen")
        st.session_state.jobs_seen = snapshot
        if seen is not None and snapshot != seen:
            st.rerun(scope="app")
        for job in jobs:
            progress = job["progress"]
            total = int(progress.get("total") or 0)
            step = int(progress.get("step") or 0)
            with st.container(border=True):
                head, tail = st.columns([0.8, 0.2], gap="small")
                head.markdown(f"**{JOB_LABELS.get(job['kind'], job['kind'].title())} #{job['id']}** · {job['status'].replace('_', ' ')}")
                if tail.button("Cancel", key=f"cancel_job_{job['id']}", use_container_width=True, disabled=job["cancel_requested"]):
                    request_cancel(job["id"])
                st.progress(min(1.0, step / total) if total else 0.0, text=str(progress.get("text") or job["status"]))
                if job["status"] == "waiting_input":
                    st.info(job["question"])
                    answer = st.text_input("Your answer", key=f"job_answer_{job['id']}")
                    if st.button("Send answer", key=f"job_answer_btn_{job['id']}") and answer.strip():
                        answer_job(job["id"], answer.strip())

    fragment = getattr(st, "fragment", None)
    if fragment is not None:
        fragment(run_every=2)(body)()
    else:
        body()


def render_launch_summary(job: Dict[str, Any], thread_id: int, dismissed: set) -> None:
    """What a finished mission produced, read from its job row."""
    result = job["result"]
    if job["status"] == "cancelled":
        st.warning("Mission cancelled; the workstreams that finished are above and in this chat's memory.")
    elif job["status"] == "failed" or "succeeded" not in result:
        st.error(f"Mission failed: {result.get('error', 'unknown error')}")
    elif result["failed"] == 0:
        st.success(
            f"All {result['succeeded']} workstream(s) finished; the results are above and in this chat's memory. "
            "Continue the mission in the chat bar."
        )
    elif result["succeeded"] == 0:
        st.error(f"All {result['failed']} workstream(s) failed. Each error is below; fix keys or adjust the mission and send it again.")
    else:
        st.warning(f"{result['succeeded']} workstream(s) succeeded, {result['failed']} failed. The failed steps are below.")
    if result.get("deliverable_artifact"):
        measure = result.get("measure", {})
        target_note = f" against a target of {result['target_label']} (≈{result['target_words']} words)" if result.get("target_words") else ""
        st.info(
            f"Deliverable assembled from {result['sections']} section(s): {measure.get('lines', 0)} lines, "
            f"{measure.get('words', 0)} words{target_note}. Locked as artifact v{result['deliverable_version']}; "
            "it is also under Locked artifacts in the sidebar."
        )
        filename, body = export_artifact(int(result["deliverable_artifact"]))
        st.download_button("⬇ Download deliverable (.md)", data=body, file_name=filename, mime="text/markdown",
                           key=f"deliverable_{thread_id}_{result['deliverable_artifact']}")
    if result.get("truncated"):
        st.caption(f"{result['truncated']} workstream(s) stopped at the output budget; raise it in the sidebar for fuller results.")
    for title, error in result.get("failures", []):
        with st.expander(f"{title} · failed", expanded=False):
            st.code(error)
    if st.button("Dismiss", key=f"dismiss_launch_{job['id']}"):
        dismissed.add(job["id"])
        st.rerun()


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
            launch_mission(project_scope, ledger, thread_id, goal, plan)
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
    job_rows = list_jobs(project_scope, None, limit=1, thread_id=thread_id, kind=mission_runner.KIND)
    job = job_view(job_rows[0]) if job_rows else None
    running = bool(job and job["status"] in ACTIVE_STATUSES)
    continuing: Optional[ChatSubmission] = None
    if submission and submission.text.strip():
        if running:
            st.warning("A mission is running in this chat; wait for it to finish or cancel it above, then send again.")
        elif has_mission:
            continuing = submission
        else:
            pending[thread_id] = submission.text.strip()
    render_history(project_scope, "Mission conversation", workspace="task_finder")
    if continuing:
        dispatch_chat(project_scope, "task_finder", continuing, ledger)
    dismissed: set = st.session_state.setdefault("dismissed_jobs", set())
    if running:
        st.caption("Mission running in the background: each workstream lands in this chat as it finishes, and the other workspaces stay usable.")
    elif job and job["id"] not in dismissed:
        render_launch_summary(job, thread_id, dismissed)
    goal = pending.get(thread_id, "")
    if goal and not running:
        render_mission_panel(project_scope, ledger, thread_id, goal, pending)
    elif not has_mission and not running:
        st.caption("No mission in this chat yet. Type one in the chat bar to see its proposed workstreams before anything runs.")
    else:
        st.caption("Mission in progress: the chat bar continues it with every result in context. Start another mission with New chat.")



def job_secrets_for_session() -> Dict[str, str]:
    """Keys handed to a background job for its lifetime only; never written anywhere."""
    secrets = dict(st.session_state.get("byok_keys", {}) or {})
    if active_mode() == "heavy" and st.session_state.get("paid_slot_key"):
        secrets["paid_slot_key"] = str(st.session_state.get("paid_slot_key"))
    return secrets


def ingest_board_reply(project_scope: str, company_id: int, text: str) -> List[str]:
    """Work items the Executive Assistant filtered out of the board's message (BACKLOG lines)."""
    titles: List[str] = []
    for title, brief in BACKLOG_LINE.findall(text or ""):
        society_store.add_work_item(project_scope, company_id, title, brief, importance=3)
        titles.append(title)
    return titles


def company_facts(project_scope: str, company: Dict[str, Any]) -> List[str]:
    cid = int(company["id"])
    counts = {status: society_store.count("work_items", project_scope, "company_id = ? AND status = ?", (cid, status)) for status in ("backlog", "assigned", "review", "board", "done")}
    latest = society_store.cycles_for(project_scope, cid, limit=1)
    facts = [
        f"work items: {counts['backlog']} backlog, {counts['assigned']} assigned, {counts['review']} in review, {counts['board']} waiting for the board, {counts['done']} done",
        f"seats: {len(society_store.seats_for(project_scope, cid, 'filled'))} filled, {len(society_store.open_seats(project_scope, cid))} open",
        "open issues: " + (", ".join(i["title"] for i in society_store.rows("issues", project_scope, "company_id = ? AND status = 'open'", (cid,), limit=5)) or "none"),
        "rocks: " + (", ".join(r["title"] for r in society_store.rows("rocks", project_scope, "company_id = ?", (cid,), limit=5)) or "none"),
    ]
    if latest:
        facts.append(f"last cycle: {latest[0]['status']}, {latest[0]['calls']} calls, {latest[0]['tokens_used']} tokens")
    return facts


def render_company(project_scope: str, ledger: QuotaLedger, submission: Optional[ChatSubmission]) -> None:
    st.subheader("Company")
    companies = society_store.companies_for(project_scope)
    if not companies:
        st.caption(
            "Two companies run on Traction/EOS with seats, a scorecard, and Level 10 meetings; one agent society feeds both. "
            "Seed a company from its template: founding agents fill the active seats so a cycle can run before the academy exists."
        )
        left, right = st.columns(2)
        if left.button("Create AVS Studio", key="seed_avs", type="primary", use_container_width=True):
            society.seed_company(project_scope, "avs_studio")
            st.rerun()
        if right.button("Create AVS Software (3 products)", key="seed_sw", use_container_width=True):
            society.seed_company(project_scope, "software_co")
            st.rerun()
        return
    names = {int(c["id"]): str(c["name"]) for c in companies}
    company_id = int(st.selectbox("Company", list(names), format_func=names.get, key="company_pick"))
    company = society_store.row("companies", company_id) or {}
    st.caption(f"{company.get('core_focus', '')} · cycle every {int(company.get('interval_s', 21600)) // 3600} h · treasury share {int(float(company.get('daily_share', 0.35)) * 100)} %" + mode_caption())
    thread = render_thread_bar(project_scope, "company", ledger)
    thread_id = int(thread["id"])
    inbox, org, backlog_tab, rocks_tab, score_tab, cycle_tab, settings_tab = st.tabs(
        ["Board inbox", "Org chart", "Backlog & catalog", "Rocks & timeline", "Scorecard", "Cycle log", "Settings"]
    )
    with inbox:
        render_history(project_scope, "Board inbox", workspace="company")
        if submission and submission.text.strip():
            if not configured_provider_names():
                st.warning("Add a BYOK key in the sidebar to talk to the Executive Assistant.")
            else:
                answer_id = run_generation(
                    project_scope, submission.text, "chat", ledger, workspace="company",
                    extra_system=board_persona(company, company_facts(project_scope, company)),
                )
                if answer_id:
                    rows = recent_messages(project_scope, 2, thread_id=thread_id)
                    created = ingest_board_reply(project_scope, company_id, rows[-1]["content"] if rows else "")
                    if created:
                        st.info("Added to the backlog: " + "; ".join(created) + ". The CEO rates it in the next cycle.")
    with org:
        seats = society_store.seats_for(project_scope, company_id)
        agents = {int(a["id"]): a for a in society_store.agents_for(project_scope)}
        depts = {int(d["id"]): d for d in society_store.departments_for(project_scope, company_id)}
        teams = {int(t["id"]): t for t in society_store.teams_for(project_scope, company_id)}
        by_id = {int(s["id"]): s for s in seats}
        load: Dict[int, int] = {}
        for item in society_store.work_items_for(project_scope, company_id, ("assigned", "running"), limit=500):
            if item.get("seat_id"):
                load[int(item["seat_id"])] = load.get(int(item["seat_id"]), 0) + 1
        st.dataframe(
            [
                {
                    "department": depts.get(int(s["department_id"] or 0), {}).get("name", ""),
                    "team": teams.get(int(s["team_id"] or 0), {}).get("name", ""),
                    "seat": s["title"], "reports to": by_id.get(int(s["reports_to"] or 0), {}).get("title", "board"),
                    "roles": "; ".join(society_store.load_json(s.get("roles"), [])),
                    "agent": agents.get(int(s["agent_id"] or 0), {}).get("name", "open seat"),
                    "tier": agents.get(int(s["agent_id"] or 0), {}).get("tier", ""),
                    "balance": agents.get(int(s["agent_id"] or 0), {}).get("balance"),  # None for an open seat keeps the column numeric
                    "load": load.get(int(s["id"]), 0), "status": s["status"],
                }
                for s in seats
            ],
            hide_index=True, use_container_width=True,
        )
        st.caption(f"{len(seats)} seats · {len([s for s in seats if s['status'] == 'filled'])} filled · departments marked inactive open when wave 1 is published.")
        threaded = [s for s in seats if s.get("thread_id")]
        if threaded:
            pick = st.selectbox("Open a seat's working thread", threaded, format_func=lambda s: s["title"], key="seat_thread_pick")
            for row in recent_messages(project_scope, 6, thread_id=int(pick["thread_id"])):
                with st.chat_message("user" if row["role"] == "user" else "assistant"):
                    st.markdown(row["content"][:1500])
    with backlog_tab:
        st.markdown("**Catalog**")
        catalog = society_store.catalog_for(project_scope, company_id)
        edited = st.data_editor(
            [{"id": c["id"], "key": c["key"], "title": c["title"], "field": c["field"], "logline": c["logline"], "stage": c["stage"], "wave": c["release_wave"]} for c in catalog],
            hide_index=True, use_container_width=True, num_rows="fixed", key=f"catalog_editor_{company_id}",
            column_config={"id": st.column_config.NumberColumn(disabled=True, width="small"), "key": st.column_config.TextColumn(disabled=True), "stage": st.column_config.TextColumn(disabled=True), "wave": st.column_config.NumberColumn(disabled=True, width="small")},
        )
        if st.button("Save catalog titles and loglines", key=f"save_catalog_{company_id}"):
            for row in edited:
                society_store.update("catalog", int(row["id"]), title=str(row["title"])[:200], logline=str(row["logline"])[:1000], field=str(row["field"])[:40])
            st.success("Catalog saved.")
        st.markdown("**Backlog and work in flight**")
        items = society_store.work_items_for(project_scope, company_id, limit=300)
        by_id = {int(s["id"]): s for s in society_store.seats_for(project_scope, company_id)}
        editable = st.data_editor(
            [{"id": i["id"], "title": i["title"], "status": i["status"], "importance": i["importance"], "seat": by_id.get(int(i["seat_id"] or 0), {}).get("title", ""), "feedback": i["feedback"][:120]} for i in items],
            hide_index=True, use_container_width=True, num_rows="fixed", key=f"items_editor_{company_id}",
            column_config={"id": st.column_config.NumberColumn(disabled=True, width="small"), "title": st.column_config.TextColumn(disabled=True), "status": st.column_config.TextColumn(disabled=True), "seat": st.column_config.TextColumn(disabled=True), "feedback": st.column_config.TextColumn(disabled=True), "importance": st.column_config.NumberColumn(min_value=1, max_value=5, width="small")},
        )
        if st.button("Save importance", key=f"save_items_{company_id}"):
            for row in editable:
                society_store.update("work_items", int(row["id"]), importance=max(1, min(5, int(row["importance"]))))
            st.success("Importance saved; the board's rating overrides the CEO's.")
        with st.form(f"add_item_{company_id}", clear_on_submit=True):
            title = st.text_input("New work item title")
            brief = st.text_area("Brief", height=80)
            importance = st.slider("Importance", 1, 5, 3)
            if st.form_submit_button("Add to backlog") and title.strip():
                society_store.add_work_item(project_scope, company_id, title, brief, importance)
                st.rerun()
        waiting = [i for i in items if i["status"] == "board"]
        if waiting:
            st.markdown("**Waiting for the board**")
            for item in waiting:
                with st.expander(f"#{item['id']} · {item['title']}", expanded=False):
                    if item.get("artifact_id"):
                        filename, body = export_artifact(int(item["artifact_id"]))
                        st.markdown(body[:4000])
                        st.download_button("⬇ Download", data=body, file_name=filename, mime="text/markdown", key=f"dl_item_{item['id']}")
                    if item.get("feedback"):
                        st.caption(f"Editor notes: {item['feedback']}")
                    note = st.text_input("Feedback for the company", key=f"fb_{item['id']}")
                    ok, back = st.columns(2)
                    if ok.button("Approve", key=f"approve_{item['id']}", use_container_width=True):
                        society_store.set_work_status(int(item["id"]), "done", feedback=note or item["feedback"])
                        if note.strip():
                            society_store.insert("feedback", project_scope, company_id=company_id, catalog_id=item.get("catalog_id"), source="board", text=note.strip(), created_at=time.time())
                        if item.get("catalog_id"):
                            cat = society_store.row("catalog", int(item["catalog_id"]))
                            if cat and cat["stage"] == "preliminary_review":
                                society_store.update("catalog", int(cat["id"]), stage="board_feedback" if note.strip() else "final")
                        st.rerun()
                    if back.button("Return with feedback", key=f"return_{item['id']}", use_container_width=True):
                        society_store.set_work_status(int(item["id"]), "assigned", feedback=note or "returned by the board")
                        society_store.insert("feedback", project_scope, company_id=company_id, catalog_id=item.get("catalog_id"), source="board", text=note.strip() or "returned", created_at=time.time())
                        st.rerun()
    with rocks_tab:
        st.markdown("**Rocks (this quarter)**")
        st.dataframe([{"rock": r["title"], "status": r["status"], "due": time.strftime("%Y-%m-%d", time.gmtime(float(r["due_at"] or 0)))} for r in society_store.rows("rocks", project_scope, "company_id = ?", (company_id,))], hide_index=True, use_container_width=True)
        st.markdown("**Timeline**")
        st.dataframe([{"milestone": m["milestone"], "due": time.strftime("%Y-%m-%d", time.gmtime(float(m["due_at"] or 0))), "status": m["status"]} for m in society_store.rows("timeline", project_scope, "company_id = ?", (company_id,), order="due_at ASC")], hide_index=True, use_container_width=True)
        issues = society_store.rows("issues", project_scope, "company_id = ?", (company_id,), order="id DESC", limit=20)
        todos = society_store.rows("todos", project_scope, "company_id = ?", (company_id,), order="id DESC", limit=20)
        if issues:
            st.markdown("**Issues**")
            st.dataframe([{"issue": i["title"], "status": i["status"], "resolution": i["resolution"]} for i in issues], hide_index=True, use_container_width=True)
        if todos:
            st.markdown("**To-dos**")
            st.dataframe([{"to-do": t["text"], "seat": by_id.get(int(t["seat_id"] or 0), {}).get("title", ""), "done": bool(t["done"])} for t in todos], hide_index=True, use_container_width=True)
    with score_tab:
        rows = society_store.rows("scorecard", project_scope, "seat_id IN (SELECT id FROM seats WHERE company_id = ?)", (company_id,), order="id DESC", limit=200)
        if rows:
            st.dataframe([{"week": r["week"], "seat": by_id.get(int(r["seat_id"]), {}).get("title", r["seat_id"]), "kpi": r["kpi"], "target": r["target"], "actual": round(float(r["actual"]), 2), "met": bool(r["met"])} for r in rows], hide_index=True, use_container_width=True)
        else:
            st.caption("The scorecard fills in when the first cycle runs.")
    with cycle_tab:
        chain = st.checkbox("Keep cycling at the company interval while the app is awake", key=f"chain_{company_id}", value=False)
        left, right = st.columns(2)
        if left.button("Run a cycle now", key=f"run_cycle_{company_id}", type="primary", use_container_width=True, disabled=not configured_provider_names()):
            society.run_now(project_scope, company_id, job_secrets_for_session(), mode=active_mode(), call_tokens=min(int(st.session_state.get("max_tokens", 2048)), 1500), chain=chain, board_thread_id=thread_id)
            st.rerun()
        queued = [job_view(r) for r in list_jobs(project_scope, ("queued",), limit=50, kind=society.KIND_COMPANY) if job_view(r)["payload"].get("company_id") == company_id]
        if right.button("Pause chain (cancel queued cycles)", key=f"pause_{company_id}", use_container_width=True, disabled=not queued):
            for job in queued:
                request_cancel(job["id"])
            st.rerun()
        if queued:
            st.caption(f"{len(queued)} cycle(s) queued; next at {time.strftime('%H:%M UTC', time.gmtime(max(j['run_after'] for j in queued)))}.")
        cycles = society_store.cycles_for(project_scope, company_id, limit=20)
        if cycles:
            st.dataframe([{"cycle": c["id"], "status": c["status"], "started": time.strftime("%m-%d %H:%M", time.gmtime(float(c["started_at"]))), "calls": c["calls"], "tokens": c["tokens_used"], "budget": c["tokens_planned"]} for c in cycles], hide_index=True, use_container_width=True)
            with st.expander("Last cycle log", expanded=False):
                st.json(society_store.load_json(cycles[0]["log"], []), expanded=False)
        else:
            st.caption("No cycle yet. A cycle runs the L10, rates and delegates the backlog, produces deliverables, reviews them, and reports here.")
    with settings_tab:
        with st.form(f"company_settings_{company_id}"):
            vision = st.text_area("Vision", value=str(company.get("vision", "")), height=80)
            values = st.text_input("Core values (comma separated)", value=", ".join(society_store.load_json(company.get("core_values"), [])))
            focus = st.text_input("Core focus", value=str(company.get("core_focus", "")))
            ten = st.text_input("10-year target", value=str(company.get("ten_year", "")))
            three = st.text_input("3-year picture", value=str(company.get("three_year", "")))
            one = st.text_input("1-year plan", value=str(company.get("one_year", "")))
            hours = st.number_input("Cycle interval (hours)", min_value=1, max_value=168, value=max(1, int(company.get("interval_s", 21600)) // 3600))
            share = st.slider("Treasury share (% of today's tokens)", 5, 60, int(float(company.get("daily_share", 0.35)) * 100))
            if st.form_submit_button("Save V/TO and settings"):
                society_store.update("companies", company_id, vision=vision[:2000], core_values=[v.strip() for v in values.split(",") if v.strip()], core_focus=focus[:500], ten_year=ten[:500], three_year=three[:500], one_year=one[:500], interval_s=int(hours) * 3600, daily_share=share / 100)
                st.success("Saved.")
        if company.get("kind") == "software":
            with st.form(f"add_product_{company_id}", clear_on_submit=True):
                p_title = st.text_input("Product title")
                p_logline = st.text_input("One line on what it does")
                if st.form_submit_button("Add product (seat, catalog row, backlog)") and p_title.strip():
                    society.add_product(project_scope, company_id, deliverable_slug(p_title), p_title.strip(), p_logline.strip())
                    st.rerun()
        missing = [key for key in ("avs_studio", "software_co") if not society_store.company_by_key(project_scope, key)]
        for key in missing:
            if st.button(f"Create {'AVS Studio' if key == 'avs_studio' else 'AVS Software'}", key=f"seed_{key}_settings"):
                society.seed_company(project_scope, key)
                st.rerun()



def render_academy(project_scope: str, ledger: QuotaLedger, submission: Optional[ChatSubmission]) -> None:
    st.subheader("Academy")
    st.caption(
        "Plato's Republic as a training society: Producers do foundational work on a basic allowance, Auxiliaries grade and guard "
        "on a higher one, Philosophers pass the evaluation and graduate into open company seats. One society feeds both companies."
        + mode_caption()
    )
    if submission and submission.text.strip():
        st.info("The academy has no chat. Talk to a company's Executive Assistant in the Company workspace.")
    agents = society_store.agents_for(project_scope)
    tiers = {tier: sum(1 for a in agents if a["tier"] == tier) for tier in ("producer", "auxiliary", "philosopher")}
    seated = sum(1 for a in agents if a["employment"] == "seated")
    cols = st.columns(5)
    cols[0].metric("Agents", len(agents))
    cols[1].metric("Producers", tiers["producer"])
    cols[2].metric("Auxiliaries", tiers["auxiliary"])
    cols[3].metric("Philosophers", tiers["philosopher"])
    cols[4].metric("Seated", seated)
    seed_col, run_col, chain_col = st.columns([0.35, 0.35, 0.3])
    target = int(st.session_state.get("academy_target", 100))
    if seed_col.button(f"Seed the society to {target} agents", key="seed_academy", type="primary", use_container_width=True, disabled=len(agents) >= target):
        created = society.seed_academy(project_scope, target)
        st.success(f"{created} agent(s) created.")
        st.rerun()
    chain = chain_col.checkbox("Keep cycling (3 h)", key="academy_chain", value=False)
    if run_col.button("Run an academy cycle now", key="run_academy", use_container_width=True, disabled=not configured_provider_names() or not agents):
        society.run_academy_now(project_scope, job_secrets_for_session(), mode=active_mode(), call_tokens=min(int(st.session_state.get("max_tokens", 2048)), 900), chain=chain)
        st.rerun()
    queued = [job_view(r) for r in list_jobs(project_scope, ("queued",), limit=50, kind=society.KIND_ACADEMY)]
    if queued and st.button("Pause chain (cancel queued academy cycles)", key="pause_academy"):
        for job in queued:
            request_cancel(job["id"])
        st.rerun()
    st.number_input("Society size target", min_value=10, max_value=500, value=target, step=10, key="academy_target")
    classes, evals, personnel, cycle_tab = st.tabs(["Classes", "Evaluations", "Personnel log", "Academy cycles"])
    with classes:
        if agents:
            seats = {int(s["id"]): s for s in society_store.rows("seats", project_scope, limit=2000)}
            st.dataframe(
                [{"agent": a["name"], "tier": a["tier"], "employment": a["employment"], "seat": seats.get(int(a["seat_id"] or 0), {}).get("title", ""), "focus": a["focus"], "balance": a["balance"], "allowance": a["allowance"], "mode": a["mode"], "interest": a["interest"][:60]} for a in agents],
                hide_index=True, use_container_width=True,
            )
        else:
            st.caption("No agents yet. Seed the society, or create a company (its founding agents join the society).")
    with evals:
        rows = society_store.rows("evaluations", project_scope, order="id DESC", limit=60)
        names = {int(a["id"]): a["name"] for a in agents}
        if rows:
            st.dataframe([{"when": time.strftime("%m-%d %H:%M", time.gmtime(float(r["timestamp"]))), "agent": names.get(int(r["agent_id"]), r["agent_id"]), "kind": r["kind"], "prompt": r["prompt_key"], "score": r["score"], "passed": bool(r["passed"]), "grader": names.get(int(r["grader_agent_id"] or 0), "check only")} for r in rows], hide_index=True, use_container_width=True)
        else:
            st.caption("Evaluations appear when the first academy cycle grades producer work.")
    with personnel:
        rows = society_store.rows("personnel_log", project_scope, order="id DESC", limit=60)
        names = {int(a["id"]): a["name"] for a in agents}
        seats = {int(s["id"]): s for s in society_store.rows("seats", project_scope, limit=2000)}
        if rows:
            st.dataframe([{"when": time.strftime("%m-%d %H:%M", time.gmtime(float(r["created_at"]))), "event": r["event"], "seat": seats.get(int(r["seat_id"] or 0), {}).get("title", ""), "agent": names.get(int(r["agent_id"] or 0), ""), "reason": r["reason"][:120], "approved by": r["approved_by"]} for r in rows], hide_index=True, use_container_width=True)
        else:
            st.caption("Hires, fires, promotions, graduations, and seat changes are logged here.")
    with cycle_tab:
        cycles = [c for c in society_store.rows("cycles", project_scope, "kind = 'academy'", order="id DESC", limit=20)]
        if cycles:
            st.dataframe([{"cycle": c["id"], "status": c["status"], "started": time.strftime("%m-%d %H:%M", time.gmtime(float(c["started_at"]))), "calls": c["calls"], "tokens": c["tokens_used"], "budget": c["tokens_planned"]} for c in cycles], hide_index=True, use_container_width=True)
            with st.expander("Last academy cycle log", expanded=False):
                st.json(society_store.load_json(cycles[0]["log"], []), expanded=False)
        else:
            st.caption("An academy cycle pays allowances, runs producer tasks, grades them, promotes, examines one graduation candidate, and fills open seats.")


WORKSPACE_RENDERERS = {
    "task_finder": render_task_finder,
    "repository": render_repository_work,
    "chat_bot": render_chat_bot,
    "normal_chat": render_normal_chat,
    "company": render_company,
    "academy": render_academy,
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
    st.session_state.project_scope = resolve_visitor_scope()
remember_scope(st.session_state.project_scope)
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
            "Paste one or more free-tier keys. Each key is sent only to its own vendor, lives in this browser "
            "session's memory, and is never written to the vault, logs, artifacts, or git. Closing the tab forgets it."
        )
        with st.expander("How to get a free key (2 minutes each)", expanded=False):
            for vendor, url, steps in KEY_GUIDES:
                st.markdown(f"**{vendor}** · [open the key page]({url})")
                st.caption(" → ".join(steps))
            st.caption("Free tiers are metered per minute and per day; the app waits for the window instead of failing, "
                       "and 'Test keys' below confirms each key with one tiny call.")
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
                detail = row["detail"] if row["ok"] or row["detail"] == "no key configured" else plain_error(RuntimeError(f"{row['endpoint']} HTTP {row['status']}: {row['detail']}" if row["status"] else f"{row['endpoint']} {row['detail']}"))
                st.caption(f"{marker} **{row['endpoint']}** · {row['model']} · key {row['key']}{status} · {detail}")
        with st.expander("Model overrides (optional, session only)", expanded=False):
            st.caption("Leave blank to let discovery pick a live model per vendor. A blank field clears an override.")
            with st.form("model_override_form", clear_on_submit=False):
                chosen: Dict[str, str] = {}
                for label, env_names in MODEL_OVERRIDE_FIELDS:
                    chosen[label] = st.text_input(label, key=f"model_override_{env_names[0]}", placeholder="model id, e.g. from the vendor's model list")
                if st.form_submit_button("Apply overrides", use_container_width=True):
                    for label, env_names in MODEL_OVERRIDE_FIELDS:
                        value = chosen[label].strip()
                        for env_name in env_names:
                            if value:
                                st.session_state.byok_keys[env_name] = value
                            else:
                                st.session_state.byok_keys.pop(env_name, None)
                    bind_session_keys(st.session_state.byok_keys)
                    st.success("Model overrides applied for this session.")
    st.caption(
        f"Private scope `{st.session_state.project_scope}` · chats, artifacts, and jobs are visible only on this browser. "
        "Bookmark the URL to come back to them."
    )
    with st.expander("Open another scope", expanded=False):
        other_scope = st.text_input("Scope id", key="scope_switch_input", placeholder="visitor-…")
        if st.button("Open scope", key="scope_switch_button", use_container_width=True):
            if SCOPE_RE.match(other_scope.strip()):
                st.session_state.project_scope = other_scope.strip()
                remember_scope(st.session_state.project_scope)
                st.rerun()
            st.warning("Scope ids use letters, digits, dots, dashes, or underscores (up to 64 characters).")

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
        if configured:
            st.caption(usage_sentence(ledger, vendor_for(name)))
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
render_jobs_strip(scope)
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
