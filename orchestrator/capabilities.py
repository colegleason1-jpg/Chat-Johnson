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
    ("Normal Chat", "single-pass answers routed by a MILP solver across the operator's free-tier keys "
                    "(Gemini, Groq, Hugging Face; NVIDIA, OpenRouter, Cerebras, Mistral as fallback)"),
    ("Chat Bot", "developer chat with explicit file attachments; fenced file blocks can be locked as versioned artifacts"),
    ("Task Finder", "deterministic decomposition of a mission into typed workstreams run in order under free-tier limits; "
                    "results stay in the chat and the mission continues as a conversation"),
    ("Repository Work", "sandboxed pipeline on a GitHub repository fetched through the API (public, or private with the armed session "
                        "token) or a local path: ingest, plan, patch, optional pytest repair loop, reviewable diff and patch download; "
                        "the result can be pushed as a branch plus pull request through the session-only slot; the repository "
                        "conversation sees the fetched tree (file map and the highest-value files within the token budget) and "
                        "says so when nothing is loaded"),
    ("Memory", "SQLite vault per project: per-workspace chats, 200-message windows, texturized summaries, vision digests, "
               "locked artifacts, Markdown/JSON transcript export"),
    ("Heavy Mode", "draft, review, synthesis passes; an optional paid slot serves only the review pass and is armed per session"),
    ("Live preview canvas", "sanitized rendering of HTML/CSS mockups"),
)

BOUNDARIES: Tuple[str, ...] = (
    "deployed on Streamlit Community Cloud from the GitHub main branch; no CI/CD pipeline, Kubernetes, Terraform, Helm, "
    "cloud account, or cloud credentials are attached to the app",
    "never writes to GitHub on its own; the only write path is the session-only GitHub push slot (a token pasted per "
    "session, never stored), which pushes one commit to a new branch and opens a pull request when the operator presses "
    "the button; the default branch is never written to and a revert pull request can undo any push from the session",
    "cannot execute Docker, Terraform, Helm, kubectl, or cloud CLIs; it can write such files and check their syntax offline",
    "free-tier only: every provider call is metered against per-vendor RPM and TPM ceilings",
)


def capability_card() -> str:
    """Compact, factual statement of features, boundaries, and roadmap for the system prompt."""
    lines = ["CHAT JOHNSON CAPABILITY CARD (answer questions about yourself from this card; never invent executors or integrations):"]
    lines.append("Implemented:")
    lines.extend(f"- {name}: {detail}" for name, detail in IMPLEMENTED)
    lines.append("Boundaries:")
    lines.extend(f"- {item}" for item in BOUNDARIES)
    lines.append("Roadmap (not usable today):")
    lines.extend(f"- {name} ({status})" for name, status, _ in ROADMAP_FEATURES)
    lines.extend(f"- {name} connector (not implemented)" for name, _ in ROADMAP_CONNECTORS)
    return "\n".join(lines)
