"""What Chat Johnson can and cannot do, stated deterministically for every prompt.

The model has no self-knowledge beyond its system prompt; without this card a question
such as "what can Chat Johnson automate" is answered by guessing from the name. The card
costs a few hundred tokens per request and is built from the same tables the README and
sidebar use, so it cannot drift from the code.
"""
from __future__ import annotations

from typing import Tuple

from .connectors import ROADMAP_CONNECTORS, ROADMAP_FEATURES

IMPLEMENTED: Tuple[Tuple[str, str], ...] = (
    ("Normal Chat", "single-pass answers routed by a MILP solver across free-tier keys "
                    "(Gemini, Groq, Hugging Face; NVIDIA, OpenRouter, Cerebras, Mistral as fallback)"),
    ("Chat Bot", "developer chat with explicit file attachments; fenced file blocks lock as versioned artifacts"),
    ("Task Finder", "deterministic decomposition of a mission into typed workstreams run in order as a background job; "
                    "results stream into the chat"),
    ("Background jobs", "missions run on worker threads with progress, cancel, and questions to the operator"),
    ("Repository Work", "sandboxed pipeline on a GitHub repository or a local path: ingest, plan, patch, "
                        "optional pytest repair loop, reviewable diff; pushable as a branch plus pull request through the session-only slot"),
    ("Spatial layout", "a scene spec becomes a solver-resolved layout with a 3D preview"),
    ("Web QA", "HTTP checks of a deployed URL (status, latency, text, health JSON); browser checks where Chromium exists"),
    ("Mission nodes", "steps are nodes: executor model/solver/webqa/connector/sub_mission, output chat/artifact/both, "
                      "on_failure; connectors deploy_kit, github (fetch/push/revert), repository, vault, webqa, mcp"),
    ("Memory", "SQLite vault, private per visitor: per-workspace chats, 200-message windows, summaries, digests, "
               "locked artifacts, exports, keyword recall from the project's other chats"),
    ("Heavy Mode", "draft, review, synthesis passes; an optional paid slot serves only the review pass, per session"),
    ("Preview canvas", "Preview only (buttons off) for HTML/CSS mockups, or Run the page: it executes offline in a sealed frame and, "
                       "in Heavy Mode, script errors come back for up to 2 automatic fix rounds"),
)

BOUNDARIES: Tuple[str, ...] = (
    "deployed on Streamlit Community Cloud (or the operator's VM) from the configured GitHub branch; no CI/CD, Kubernetes, "
    "Terraform, cloud account, or cloud credentials are attached",
    "never writes to GitHub on its own; the only write path is the session-only GitHub push slot (a token pasted per "
    "session, never stored): one commit to a new branch plus a pull request when the operator presses the button; the "
    "default branch is never written; a revert pull request can undo any push",
    "cannot execute Docker, Terraform, Helm, kubectl, or cloud CLIs; it can write such files and check their syntax offline",
    "free-tier only: every provider call is metered against per-vendor RPM, TPM, and daily token ceilings",
)


def capability_card() -> str:
    """Compact, factual statement of features, boundaries, and roadmap for the system prompt."""
    lines = ["CHAT JOHNSON CAPABILITY CARD (answer questions about yourself from this card; never invent executors or integrations):"]
    lines.append("Implemented:")
    lines.extend(f"- {name}: {detail}" for name, detail in IMPLEMENTED)
    lines.append("Boundaries:")
    lines.extend(f"- {item}" for item in BOUNDARIES)
    lines.append("Handoff: asked to turn an idea into a mission, end with a ```mission YAML block (statement; nodes with "
                 "title, executor, task_type, instruction; optional config, inputs, output, on_failure).")
    lines.append("Roadmap (not usable today):")
    lines.extend(f"- {name} ({status})" for name, status, _ in ROADMAP_FEATURES)
    lines.append("- data connectors not implemented: " + ", ".join(name for name, _ in ROADMAP_CONNECTORS))
    return "\n".join(lines)
