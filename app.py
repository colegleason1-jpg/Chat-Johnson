"""Freebuff Orchestrator — multi-provider free-LLM pipeline UI."""
from __future__ import annotations

import json
import os

import streamlit as st

from orchestrator.config import PROVIDERS, provider_api_key, provider_model
from orchestrator.executor import Orchestrator
from orchestrator.memory import TaskMemory
from orchestrator.quota import QuotaLedger
from orchestrator.router import classify, generate

st.set_page_config(page_title="Freebuff Orchestrator", page_icon="🧠", layout="wide")

st.markdown(
    """
    <style>
      .stApp { background: #0b1020; color: #e6e9f5; }
      section[data-testid="stSidebar"] { background: #0e1430; }
      h1, h2, h3 { color: #f2f4ff; letter-spacing: .3px; }
      .provider-chip { display:inline-block; padding:2px 10px; border-radius:999px;
        margin:2px 4px 2px 0; font-size:.8rem; background:#1b2350; border:1px solid #2e3a78; }
      .provider-chip.ready { border-color:#3ddc97; color:#bdf5dc; }
      .provider-chip.off { opacity:.55; }
      div[data-testid="stStatusWidget"] { visibility:hidden; }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_resource
def get_ledger() -> QuotaLedger:
    return QuotaLedger({n: (c.rpm_limit, c.tpm_limit) for n, c in PROVIDERS.items()})


ledger = get_ledger()
available = [n for n, c in PROVIDERS.items() if provider_api_key(c)]

st.title("🧠 Freebuff Orchestrator")
st.caption("Task → typed chunks → routed to the best free model → sandboxed, tested, verified.")

# ---------- sidebar: providers + quota ----------
with st.sidebar:
    st.header("Providers")
    for name, cfg in PROVIDERS.items():
        ready = name in available
        cls = "provider-chip ready" if ready else "provider-chip off"
        u = ledger.usage(name)
        st.markdown(
            f'<span class="{cls}">{cfg.label}</span>',
            unsafe_allow_html=True,
        )
        st.caption(
            f"`{cfg.env_key}` · {provider_model(cfg)} · "
            f"ctx {cfg.context_window:,} · rpm {u['rpm_used']}/{u['rpm_limit']} · "
            f"tpm {int(u['tpm_used']):,}/{u['tpm_limit']:,}"
        )
    if not available:
        st.warning("No API keys detected. Add them in the Keys/API keys tab, then restart the preview.")

tab_chat, tab_run, tab_repo = st.tabs(["💬 Chat", "🧩 Task Orchestrator", "📦 Repo Pipeline"])

# ---------- chat ----------
with tab_chat:
    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []
    if "chat_memory" not in st.session_state:
        st.session_state.chat_memory = TaskMemory(
            ".orchestrator/chat_memory.json", goal="interactive chat"
        )
    user = st.chat_input("Ask anything — routed to the best free model…")
    if user and available:
        with st.chat_message("user"):
            st.markdown(user)
        messages = [
            {"role": "system", "content": "You are a helpful coding assistant."},
            {"role": "user", "content": st.session_state.chat_memory.context_block() + f"\n\nUSER: {user}"},
        ]
        try:
            text, decision = generate(classify(user), messages, ledger, max_tokens=2048)
            st.session_state.chat_history += [(user, f"**[{decision.provider}]** {text}")]
        except Exception as exc:
            st.session_state.chat_history += [(user, f"⚠️ {exc}")]
    for q, a in st.session_state.chat_history[-12:]:
        with st.chat_message("user"):
            st.markdown(q)
        with st.chat_message("assistant"):
            st.markdown(a)

# ---------- task orchestrator ----------
with tab_run:
    st.subheader("Break a task into chunks, route each chunk, track progress")
    goal = st.text_area(
        "Task / goal",
        placeholder="e.g. Add retry-with-backoff to the sync client and cover it with pytest tests",
        height=90,
    )
    col1, col2 = st.columns([1, 3])
    with col1:
        go = st.button("🚀 Run pipeline", type="primary", disabled=not (goal and available))
    if go:
        orch = Orchestrator()
        with st.status("Running pipeline…", expanded=True) as status:
            st.write("Ingesting / planning…")
            try:
                report = orch.run(goal)
            except Exception as exc:
                st.error(f"Pipeline failed: {exc}")
                report = None
            if report:
                for ev in orch.log:
                    if ev["event"] in ("plan", "sandbox", "step_done", "step_failed"):
                        st.write(f"`{ev['event']}` " + json.dumps(
                            {k: v for k, v in ev.items() if k not in ("ts", "event")}, default=str)[:220]
                        )
                status.update(label="Pipeline complete", state="complete")
        if report:
            st.success("Report ready")
            st.markdown(f"**Branch:** `{report['branch']}` · **Sandbox:** `{report['sandbox']}`")
            st.text_area("Memory", report["memory"], height=180)
            if report["diff"]:
                st.code(report["diff"][:8000], language="diff")
            st.json({p: report["ledger"][p] for p in list(report["ledger"])[:6]}, expanded=False)

# ---------- repo pipeline ----------
with tab_repo:
    st.subheader("Repository ingestion & execution pipeline")
    st.caption("Worktree-sandboxed edits with ast.parse + pytest guardrails and a traceback feedback loop.")
    repo_path = st.text_input("Repository path", value=os.getcwd(), help="Folder containing the target codebase")
    repo_goal = st.text_area(
        "What should change?",
        placeholder="e.g. Split utils.py into io.py and math.py; keep tests green",
        height=80,
    )
    run_repo = st.button("🧪 Run repo pipeline", type="primary", disabled=not (repo_goal and available))
    if run_repo:
        if not os.path.isdir(repo_path):
            st.error("Repository path not found.")
        else:
            orch = Orchestrator()
            with st.spinner("Serialize → plan → patch → test…"):
                try:
                    report = orch.run(repo_goal, repo_path=repo_path)
                    st.success(f"Done — branch `{report['branch']}`, ingest {report['ingest']}")
                    st.markdown(f"**Sandbox:** `{report['sandbox']}` (your working tree was never touched)")
                    if report["diff"]:
                        st.code(report["diff"][:10000], language="diff")
                    st.text_area("Step memory", report["memory"], height=200)
                except Exception as exc:
                    st.error(f"Pipeline failed: {exc}")
