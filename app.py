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

import contextvars
import hashlib
import hmac
import html
import os
import re
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
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
    CortexStream,
    PaidReasoningSlot,
    ProviderError,
    byok_status,
    classify,
    cortex_available,
    generate_mode,
    probe_all_endpoints,
)


# =============================================================================
# Local source of truth (orchestrator/vault.py)
# =============================================================================

from orchestrator.vault import (
    MESSAGE_WINDOW,
    append_message,
    archived_messages,
    context_block,
    export_artifact,
    initialize_database,
    recent_artifacts,
    recent_messages,
    recent_summaries,
    save_artifact,
    search_artifacts,
)

initialize_database()


# =============================================================================
# Provider/runtime helpers
# =============================================================================

@st.cache_resource
def get_quota_ledger() -> QuotaLedger:
    return QuotaLedger({name: (cfg.rpm_limit, cfg.tpm_limit) for name, cfg in PROVIDERS.items()})


@st.cache_resource
def get_task_request_lock() -> threading.Lock:
    """Serialize task-finder provider calls around quota check + record.

    Futures still provide independent task lifecycle/progress, while this
    narrow lock prevents concurrent workers from observing the same RPM/TPM
    headroom and collectively exceeding the hard free-tier policy.
    """
    return threading.Lock()


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
    for name, label, model, env_name in (
        ("google_ai_studio", "Google AI Studio", "Gemini 1.5 Pro", "GEMINI_API_KEY"),
        ("groq", "Groq Cloud", "Llama 3.3 70B", "GROQ_API_KEY"),
        ("huggingface", "Hugging Face Serverless", "Qwen2.5-Coder-32B", "HUGGINGFACE_API_KEY / HF_TOKEN"),
    ):
        configured = bool(status.get(name, {}).get("configured", False))
        if name == "google_ai_studio":
            configured = bool(status.get("gemini", {}).get("configured", configured))
        rows.append((name, label, f"{model} · {env_name}", configured))
    for name, cfg in PROVIDERS.items():
        if name in {"gemini", "groq"}:
            continue
        configured = bool(resolve_secret(cfg.env_key))
        rows.append((name, cfg.label, f"{provider_model(cfg)} · {cfg.env_key}", configured))
    return rows


def build_prompt_messages(
    project_scope: str,
    user_prompt: str,
    injected_context: str = "",
) -> List[Dict[str, str]]:
    system = (
        "You are Chat Johnson, a careful software and strategy assistant. "
        "Return useful, complete output, state uncertainty, and never claim "
        "that generated code is flawless or that a scientific simulation proves "
        "physical propulsion. Do not reveal private chain-of-thought."
    )
    context = context_block(project_scope)
    parts = [f"ACTIVE PROJECT: {project_scope}", "PROJECT MEMORY:\n" + context]
    if injected_context.strip():
        parts.append("USER-CONSENTED FILE INJECTIONS:\n" + injected_context[:120_000])
    parts.append("CURRENT REQUEST:\n" + user_prompt.strip())
    return [{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join(parts)}]


def extract_preview_source(text: str) -> str:
    blocks = re.findall(r"```(?P<language>[^\n`]*)\n(?P<body>.*?)```", text, flags=re.DOTALL)
    for language, body in blocks:
        normalized = language.strip().lower()
        if normalized.startswith(("html", "htm", "css", "javascript", "js")) or re.search(r"<\s*(?:!doctype|html|main|section|div|button)\b", body, re.I):
            return body.strip()
    if re.search(r"<\s*(?:!doctype|html|main|section|div|button)\b", text, re.I):
        return text.strip()
    return ""


# =============================================================================
# Safe dual-panel preview and artifact rendering
# =============================================================================

_PREVIEW_CSS = """
:root { color-scheme: dark; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }
* { box-sizing: border-box; }
body { margin: 0; min-height: 100vh; padding: 22px; color: #e8eefc; background: #10182a; }
.preview-shell { max-width: 900px; margin: 0 auto; padding: 26px; border: 1px solid #263858;
  border-radius: 20px; background: linear-gradient(145deg, #172541, #111a2c);
  box-shadow: 0 18px 45px rgba(0,0,0,.24); }
.preview-shell h1, .preview-shell h2 { margin-top: 0; color: #f4f7ff; }
.preview-shell button { border: 0; border-radius: 10px; padding: 10px 16px; color: #08111f;
  background: #67e8c2; font-weight: 700; cursor: pointer; }
.preview-shell button:hover { background: #9af5db; }
.notice { margin-top: 16px; color: #a9b7d3; font-size: 12px; }
"""


def safe_preview_document(source: str) -> str:
    """Create a no-network HTML preview document from model/user markup."""
    value = source.strip()
    if not value:
        value = (
            "<div class='preview-shell'><h1>Preview canvas</h1>"
            "<p>Generated interface markup will appear here.</p>"
            "<button type='button'>Example control</button></div>"
        )
    value = re.sub(r"(?is)<(script|iframe|object|embed|form|base|link)\b[^>]*>.*?</\1\s*>", "", value)
    value = re.sub(r"(?is)<(script|iframe|object|embed|form|base|link)\b[^>]*/?>", "", value)
    value = re.sub(r"(?is)\s+on[a-z0-9_-]+\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", "", value)
    value = re.sub(r"(?is)\s+(?:href|src)\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", "", value)
    if not re.search(r"<\s*(?:!doctype|html|body|main|section|div|article|style)\b", value, re.I):
        value = f"<pre>{html.escape(value)}</pre>"
    if not re.search(r"<\s*style\b", value, re.I):
        value = f"<style>{_PREVIEW_CSS}</style>{value}"
    notice = "<p class='notice' data-preview-status>Preview sandbox: generated scripts, remote resources, frames, forms, and event handlers are disabled. Local preview controls remain available.</p>"
    runtime = """
    <script>
    (() => {
      const status = document.querySelector('[data-preview-status]');
      document.querySelectorAll('button').forEach((button) => {
        button.addEventListener('click', () => {
          if (status) status.textContent = 'Local preview interaction captured.';
        });
      });
    })();
    </script>
    """
    if "preview-shell" not in value:
        value = f"<div class='preview-shell'>{value}{notice}</div>"
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; connect-src 'none'; frame-src 'none'\">"
        f"</head><body>{value}{runtime}</body></html>"
    )


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
            st.code(body, language=language.split(":", 1)[0].strip() or "text")
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
    with st.container(border=True):
        st.subheader("Live preview canvas")
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
    state = st.session_state.get("github_oauth_state")
    if not state:
        state = secrets.token_urlsafe(32)
        st.session_state.github_oauth_state = state
    return GITHUB_AUTHORIZE_URL + "?" + urlencode(
        {"client_id": client_id, "redirect_uri": redirect_uri, "scope": scope, "state": state}
    )


def exchange_github_code(code: str, expected_state: str, received_state: str) -> str:
    if not expected_state or not received_state or not hmac.compare_digest(expected_state, received_state):
        raise ValueError("GitHub OAuth state validation failed")
    client_id = os.environ.get("GITHUB_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GITHUB_CLIENT_SECRET", "").strip()
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


def render_repository_work(project_scope: str, ledger: QuotaLedger) -> None:
    st.subheader("Repository Work")
    st.caption("Sandboxed local changes, reviewable diffs, and an explicit human handoff.")
    st.info(
        "The GitHub connection below is a least-privilege OAuth skeleton. It keeps the token in session memory only, "
        "does not collect SSH private keys, and never commits or pushes automatically."
    )
    oauth_ready = all(
        os.environ.get(key, "").strip()
        for key in ("GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET", "GITHUB_REDIRECT_URI")
    )
    code = _query_value("code")
    received_state = _query_value("state")
    if code and received_state and "github_token" not in st.session_state:
        try:
            st.session_state.github_token = exchange_github_code(
                code,
                str(st.session_state.get("github_oauth_state", "")),
                received_state,
            )
            st.success("GitHub authorization completed for this session.")
        except (ValueError, requests.RequestException) as exc:
            st.error(f"GitHub authorization was not completed: {exc}")
    if oauth_ready:
        url = github_oauth_url()
        try:
            st.link_button("Authorize read-only GitHub access", url)
        except AttributeError:
            st.markdown(f"[Authorize read-only GitHub access]({url})")
    else:
        st.warning("Configure GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, and GITHUB_REDIRECT_URI in the environment to enable the OAuth skeleton.")

    token = st.session_state.get("github_token", "")
    if token:
        st.success("Session-only GitHub connection is active.")
        if st.button("Check GitHub identity", key="github_identity"):
            try:
                identity = github_api_get(token, "/user")
                st.json({"login": identity.get("login"), "name": identity.get("name"), "public_repos": identity.get("public_repos")})
            except (ValueError, requests.RequestException) as exc:
                st.error(f"GitHub request failed: {exc}")
        if st.button("Forget session connection", key="github_forget"):
            st.session_state.pop("github_token", None)
            st.session_state.pop("github_oauth_state", None)

    st.divider()
    st.markdown("**Local sandbox pipeline**")
    repo_path = st.text_input("Repository path", value=os.getcwd(), key="repo_path")
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
        disabled=not bool(repo_goal.strip() and configured_provider_names()),
    )
    if run_repo:
        if not os.path.isdir(repo_path):
            st.error("Repository path does not exist.")
        else:
            with st.status("Ingesting, patching, and verifying…", expanded=True) as status:
                try:
                    report = Orchestrator(ledger=ledger).run(repo_goal, repo_path=repo_path)
                    status.update(label="Pipeline completed", state="complete")
                    st.success(f"Sandbox branch: {report['branch']}")
                    st.json(report["ingest"], expanded=False)
                    if report.get("diff"):
                        st.code(report["diff"][:20_000], language="diff")
                    st.text_area("Execution memory", report.get("memory", ""), height=180)
                except Exception as exc:
                    status.update(label="Pipeline stopped", state="error")
                    st.error(f"Repository pipeline failed: {exc}")


# =============================================================================
# Chat and Task Finder environments
# =============================================================================


def uploaded_file_context(files: Sequence[Any]) -> str:
    chunks: List[str] = []
    for uploaded in files:
        try:
            raw = uploaded.getvalue()
            if len(raw) > 120_000:
                raw = raw[:120_000] + b"\n...[file truncated by UI budget]"
            decoded = raw.decode("utf-8", errors="replace")
            chunks.append(f"===== INJECTED FILE: {uploaded.name} =====\n{decoded}")
        except (AttributeError, UnicodeError):
            continue
    return "\n\n".join(chunks)


def run_generation(
    project_scope: str,
    prompt: str,
    task_type: str,
    ledger: QuotaLedger,
    injected_context: str = "",
) -> Optional[int]:
    clean_prompt = prompt.strip()
    if not clean_prompt:
        return None
    mode = active_mode()
    user_message_id = append_message(project_scope, "user", clean_prompt, mode=mode)
    messages = build_prompt_messages(project_scope, clean_prompt, injected_context)
    max_tokens = int(st.session_state.get("max_tokens", 2048))
    live_box = st.empty()
    try:
        if mode == "normal" and cortex_available():
            # Normal mode streams token-by-token from the MILP-selected endpoint.
            try:
                stream = CortexStream(task_type, messages, ledger, max_tokens=max_tokens, temperature=0.35)
                with live_box.container():
                    with st.chat_message("assistant"):
                        st.caption(f"{stream.decision.provider}/{stream.decision.model} · streaming")
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
        append_message(project_scope, "assistant", f"Provider error: {exc}", mode=mode)
        st.error(f"Generation failed: {exc}")
        return user_message_id
    live_box.empty()
    assistant_id = append_message(
        project_scope,
        "assistant",
        answer,
        provider=f"{decision.provider}/{decision.model}",
        mode=mode,
    )
    st.session_state.preview_source = extract_preview_source(answer)
    st.session_state.preview_editor = st.session_state.preview_source
    st.session_state.last_decision = decision
    return assistant_id


def render_history(project_scope: str, heading: str) -> None:
    st.markdown(f"#### {heading}")
    rows = recent_messages(project_scope, MESSAGE_WINDOW)
    if not rows:
        st.caption("No messages yet in this project scope.")
        return
    for row in rows[-24:]:
        role = row["role"] if row["role"] in {"user", "assistant"} else "assistant"
        with st.chat_message(role):
            if row["provider"]:
                st.caption(f"{row['provider']} · {row['mode']} · {row['token_count']} estimated tokens")
            if role == "assistant":
                render_output_with_artifacts(
                    row["content"],
                    project_scope,
                    int(row["id"]),
                    f"message-{row['id']}",
                )
            else:
                st.markdown(row["content"])


def render_normal_chat(project_scope: str, ledger: QuotaLedger) -> None:
    st.subheader("Normal Chat")
    st.caption("A low-overhead single-pass terminal for quick text, planning, and coding questions.")
    with st.form("normal_chat_form", clear_on_submit=True):
        prompt = st.text_area("Message", height=120, placeholder="Ask a focused question…")
        submitted = st.form_submit_button("Send normal request", type="primary")
    if submitted:
        if not configured_provider_names():
            st.warning("Add at least one BYOK provider key in the sidebar API keys panel before sending a request.")
        else:
            run_generation(project_scope, prompt, classify(prompt), ledger)
    render_history(project_scope, "Project conversation")


def render_chat_bot(project_scope: str, ledger: QuotaLedger) -> None:
    st.subheader("Chat Bot")
    st.caption("Continuous developer mode with explicit, consented codebase file injections.")
    files = st.file_uploader(
        "Inject text/code files into the next prompt",
        accept_multiple_files=True,
        type=["py", "js", "ts", "tsx", "jsx", "json", "md", "txt", "yml", "yaml", "toml", "css", "html"],
        key="chat_bot_files",
    )
    injected = uploaded_file_context(files or [])
    if files:
        st.caption(f"{len(files)} file(s) staged in memory only; use Artifact Lock to persist an output.")
    with st.form("chat_bot_form", clear_on_submit=True):
        prompt = st.text_area(
            "Developer request",
            height=130,
            placeholder="Review the injected files, explain the issue, or emit complete fenced file blocks.",
        )
        submitted = st.form_submit_button("Send to Chat Bot", type="primary")
    if submitted:
        if not configured_provider_names():
            st.warning("Add at least one BYOK provider key in the sidebar API keys panel before sending a request.")
        else:
            run_generation(project_scope, prompt, classify(prompt), ledger, injected)
    render_history(project_scope, "Developer conversation")


def task_plan(goal: str, count: int) -> List[Dict[str, Any]]:
    """Create a bounded deterministic task graph without spending provider quota."""
    pieces = [piece.strip() for piece in re.split(r"[\n;]+", goal) if piece.strip()]
    templates = [
        ("Scope and constraints", "reasoning", "Identify acceptance criteria, risks, and a minimal execution boundary for: {goal}"),
        ("Repository/context analysis", "context_load", "Map the relevant files, interfaces, dependencies, and existing tests for: {goal}"),
        ("Implementation approach", "code_patch", "Propose a concrete implementation for: {goal}. Emit complete file blocks only if files are supplied."),
        ("Verification plan", "test_fix", "Define tests and failure checks that validate: {goal}. Do not weaken existing tests."),
        ("Operator handoff", "quick_text", "Write a concise review checklist and handoff summary for: {goal}"),
    ]
    goals = pieces[:count] if len(pieces) >= count else [goal] * count
    return [
        {
            "id": index + 1,
            "title": templates[index % len(templates)][0],
            "type": templates[index % len(templates)][1],
            "description": templates[index % len(templates)][2].format(goal=goals[index]),
            "status": "queued",
        }
        for index in range(max(1, min(count, 6)))
    ]


def render_task_finder(project_scope: str, ledger: QuotaLedger) -> None:
    st.subheader("Task Finder")
    st.caption("Bounded multi-agent task futures with progress, shared project scope, and quota-safe execution.")
    goal = st.text_area("Mission", height=100, placeholder="Break a complex project objective into independently reviewable workstreams.", key="task_goal")
    count = st.slider("Workstreams", min_value=2, max_value=6, value=3, key="task_count")
    plan = task_plan(goal, count) if goal.strip() else []
    if plan:
        st.markdown("**Proposed work graph**")
        st.dataframe(
            [{"#": step["id"], "workstream": step["title"], "type": step["type"], "status": step["status"]} for step in plan],
            hide_index=True,
            use_container_width=True,
        )
    execute = st.button(
        "Launch bounded task futures",
        type="primary",
        key="launch_task_futures",
        disabled=not bool(goal.strip() and configured_provider_names()),
    )
    if execute and plan:
        progress = st.progress(0.0, text="Starting workstreams…")
        results: Dict[int, Tuple[str, str, str]] = {}
        request_lock = get_task_request_lock()
        task_mode = active_mode()
        task_token_budget = int(st.session_state.get("max_tokens", 1536))
        task_paid_slot = session_paid_slot() if task_mode == "heavy" else None

        def worker(step: Mapping[str, Any]) -> Tuple[int, str, str, str]:
            messages = build_prompt_messages(project_scope, str(step["description"]))
            # The lock covers the full selection/request/ledger-record cycle.
            # This is essential when multiple futures share one free-tier key.
            with request_lock:
                answer, decision = generate_mode(
                    task_mode,
                    str(step["type"]),
                    messages,
                    ledger,
                    max_tokens=task_token_budget,
                    temperature=0.2,
                    paid_slot=task_paid_slot,
                )
            return int(step["id"]), answer, decision.provider, decision.model

        with ThreadPoolExecutor(max_workers=min(3, len(plan))) as executor:
            futures = {executor.submit(contextvars.copy_context().run, worker, step): step for step in plan}
            for completed, future in enumerate(as_completed(futures), start=1):
                step = futures[future]
                try:
                    step_id, answer, provider, model = future.result()
                    results[step_id] = (answer, provider, model)
                    step["status"] = "complete"
                    append_message(project_scope, "user", step["description"], mode=active_mode())
                    append_message(project_scope, "assistant", answer, provider=f"{provider}/{model}", mode=active_mode())
                except Exception as exc:
                    step["status"] = "failed"
                    results[int(step["id"])] = (f"Task failed: {exc}", "", "")
                progress.progress(completed / len(plan), text=f"Completed {completed}/{len(plan)} workstreams")
        st.session_state.task_results = results
        st.success("Task futures finished; review each result before applying any code.")
    for step in plan:
        result = st.session_state.get("task_results", {}).get(int(step["id"]))
        if result:
            answer, provider, model = result
            with st.expander(f"{step['id']}. {step['title']} · {step['status']}", expanded=True):
                if provider:
                    st.caption(f"{provider}/{model}")
                render_output_with_artifacts(answer, project_scope, None, f"task-{step['id']}")


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
    st.markdown("<div class='eyebrow'>Chat Johnson · Gen 2</div>", unsafe_allow_html=True)
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
            applied = 0
            for env_name, value in entered.items():
                if value.strip():
                    st.session_state.byok_keys[env_name] = value.strip()
                    applied += 1
            bind_session_keys(st.session_state.byok_keys)
            if applied:
                st.success(f"{applied} key(s) applied for this browser session.")
            else:
                st.info("No key values entered.")
        if clear_clicked:
            st.session_state.byok_keys = {}
            bind_session_keys({})
            for env_name, _ in key_fields:
                st.session_state.pop(f"byok_{env_name}", None)
            st.info("Session keys cleared. Environment variables, if any, remain in effect.")
            st.rerun()
        if st.button("Test keys (one tiny request per configured endpoint)", key="probe_keys", use_container_width=True):
            with st.spinner("Probing endpoints…"):
                probe_rows = probe_all_endpoints()
            for row in probe_rows:
                marker = "✅" if row["ok"] else ("⚪" if row["detail"] == "no key configured" else "❌")
                status = f" · HTTP {row['status']}" if row["status"] else ""
                st.caption(f"{marker} **{row['endpoint']}** · {row['model']}{status} · {row['detail']}")
    project_input = st.text_input("Active project scope", value=st.session_state.project_scope, key="project_scope_input")
    st.session_state.project_scope = project_input.strip() or "chat-johnson"
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
    active_count = len(recent_messages(st.session_state.project_scope, MESSAGE_WINDOW))
    archive_count = len(archived_messages(st.session_state.project_scope, 5000))
    summary_rows = recent_summaries(st.session_state.project_scope, 50)
    st.caption(
        f"Active window {active_count}/{MESSAGE_WINDOW} · archived {archive_count} · "
        f"summaries {len(summary_rows)}. Raw history is texturized and archived, never deleted."
    )

st.markdown("<div class='eyebrow'>Sovereign local-first execution workspace</div>", unsafe_allow_html=True)
st.title("Chat Johnson Master Studio")
st.markdown(
    "<div class='subtitle'>A CVO workbench for routing focused work, preserving project context, and keeping every code handoff reviewable.</div>",
    unsafe_allow_html=True,
)
st.caption("Project Seth's stochastic signal is an experimental routing feature only; it does not establish propulsion, lift, or a physical mechanism.")

left_panel, right_panel = st.columns([0.48, 0.52], gap="large")
with left_panel:
    st.markdown("### Operational environments")
    environment = st.radio(
        "Choose a workspace",
        ("Task Finder", "Repository Work", "Chat Bot", "Normal Chat"),
        key="environment",
    )
    scope = st.session_state.project_scope
    if environment == "Task Finder":
        render_task_finder(scope, ledger)
    elif environment == "Repository Work":
        render_repository_work(scope, ledger)
    elif environment == "Chat Bot":
        render_chat_bot(scope, ledger)
    else:
        render_normal_chat(scope, ledger)

with right_panel:
    render_preview_panel()
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
