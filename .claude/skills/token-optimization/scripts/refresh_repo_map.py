#!/usr/bin/env python3
"""Regenerate references/repo-map.md from the code (run from the repository root).

Usage: python .claude/skills/token-optimization/scripts/refresh_repo_map.py
"""
from __future__ import annotations

import os
import re
import sys

ENTRIES = [
    ("app.py", "Streamlit studio: sidebar (keys, Heavy Mode, paid slot, artifacts, memory, roadmap stubs), server-side workspace switch (segmented control or radio, ?ws=), one chat bar pinned to the bottom and routed to the selected workspace, per-workspace thread row (New/Clear/Delete/More) rendered under the conversation so the bottom-pinned page keeps it on screen, Task Finder missions, routing log, preview canvas opened by a button (never by a reload), sticky per-chat page mode (set_page_mode / building_a_page) so follow-up edits keep the page, the rules and the budget (Run mode auto-selected for scripted pages, status line), truncation box (Continue / Raise the limit / Smaller page via queued sends), failed sends as system rows, per-answer route facts, restart and earlier-session-keys notices, ?health=1 with process/jobs/ledger/snapshot age"),
    ("orchestrator/router.py", "Cortex 1/2/3: BYOK status, endpoints + ceilings (incl. max_output_tokens), interface detection + output-need routing + effective_output_budget, automatic continuation and stitching, answer-shape check, gpt-oss reasoning control, vendor_wait_hint (Retry-After and reset headers block the vendor), stream usage from the last chunk, served model and pass latency on the decision, 1/f noise + SDE + telemetry penalty, MILP selection, resilient HTTP + streaming, probes, paid slot, Heavy Mode pipeline (page mode: full-budget draft, cut-aware critique, non-shortening synthesis, fallbacks carry finish), legacy generate/generate_mode"),
    ("orchestrator/discovery.py", "vendor model lists, preference order, retired/transient/unusable rules, backoff, validated discovery"),
    ("orchestrator/providers.py", "legacy OpenAI-compatible + Gemini client with retries, model recovery, key redaction"),
    ("orchestrator/config.py", "legacy provider registry, session key overlay (ContextVar), Settings, provider_model resolution"),
    ("orchestrator/quota.py", "QuotaLedger: RPM/TPM windows, daily tokens and daily requests, reservations while a request is in flight (reserve/settle), vendor blocks (block/blocked_for), tighten, record_attempt, record"),
    ("orchestrator/quiet.py", "quiet period after an operator send: note_chat_send, quiet_seconds, deferral (cycles re-queue for its end)"),
    ("orchestrator/learner.py", "measured speed, Beta quality priors (thumbs plus cut/failed/sandbox_error), pink-wave exploration inside the capacity rows, momentum per sending workspace (set_workspace)"),
    ("orchestrator/vault.py", "SQLite: threads per workspace, messages (with the vendor finish reason and the cut note), live window before memory (digest capped at 1/8 as background), texturize/archive, summaries, artifacts, health sweep (automatic turns excluded), vision digest (decisions from the operator only), migration, recall (stopwords, whole words), route facts per message (routes_for_messages, route_facts; exported with the transcript), health_check with jobs/sends"),
    ("orchestrator/missions.py", "deterministic mission classification + workstream templates for Task Finder (incl. the preview kind: preview.validate / preview.repair executors)"),
    ("orchestrator/mission_runner.py", "missions as background jobs: nodes in order, model steps with the launch page sent whole, headroom retry, failed steps posted as system rows, static page_check, bounded preview repair loop, the produced page locked as an artifact"),
    ("orchestrator/pagepatch.py", "patch mode for large canvas pages: PATCH_RULES, wants_patch_mode, parse_edits, apply_edits (exact then whitespace-tolerant, problems reported)"),
    ("orchestrator/prompting.py", "build_prompt_messages: persona, capability card, APP STATE, CANVAS RULES, current page turn (patch header in patch mode), skills, memory as background"),
    ("orchestrator/preview.py", "nh3 allowlist sanitizer + preview document + markup extraction (the last whole page fence wins; unclosed fence; data:text/html links) + page_completeness (fence, document, scripts, brace balance, finish) + external_resources + page_review (the deterministic stand-in for the Heavy critique on a page request)"),
    ("orchestrator/sandbox_preview.py", "Run-mode preview contract: child/component CSP, one-line talk-back shim (loud silent failures, WebRTC shadow, hazard sweep), wrapper-first document assembly, hazard stripping, report normalization, plain_script_error (browser errors in plain words), fix decision (rounds, same-place stop, incomplete pages never repaired, transient refusals), fix prompt + FIX_MARKER"),
    ("frontend/sandbox_preview/", "declared Streamlit component: index.html (COMPONENT_CSP verbatim), main.js (nested allow-scripts frame, one report per run, status precedence error > navigated > blocked > blank > ready > timeout, never relays), vendor/three.datauri.js (generated by scripts/build_three_datauri.py)"),
    ("orchestrator/github_auth.py", "signed, time-limited OAuth state"),
    ("orchestrator/connectors.py", "local SQLite connector + roadmap stubs (not enable-able)"),
    ("orchestrator/executor.py", "repository pipeline: decompose → sandbox → patches → AST/pytest → diff"),
    ("orchestrator/sandbox.py", "git worktree / copy-mode sandbox, copy-mode difflib diff, AST guardrail"),
    ("orchestrator/patches.py", "FILE-block / unified-diff parsing and safe application, changed_files"),
    ("orchestrator/decomposer.py", "LLM JSON step planner for the repository pipeline"),
    ("orchestrator/test_loop.py", "pytest + traceback repair loop"),
    ("orchestrator/repo_ingest.py", "token-budgeted repository serialization"),
    ("orchestrator/memory.py", "task memory JSON for the repository pipeline"),
    ("research/project_seth_phase3.py", "isolated Phase 3 research engine (no routing dependency)"),
    ("cli.py", "status / chat / run commands"),
    ("tests/", "test_cortex (routing), test_vault, test_threads, test_missions, test_preview, test_github_auth, test_executor_pipeline, test_session_keys, test_orchestrator, test_project_seth_phase3, test_sandbox_preview (Run-mode contract), test_sandbox_frontend (Playwright, skips without Chromium), test_batch_t_prompt, test_batch_t_ui (AppTest: Clear buttons, Run mode, auto-fix loop), test_batch_u (finished answers, honest memory), test_batch_u2 (operator truth: auto Run mode, truncation box, failed-send row, route facts, notices, health), test_batch_u3 (reservations, vendor blocks, daily requests, usage chunk, served model, priors, momentum, quiet period), test_batch_u4 (patch mode, preview missions, failed-step rows, headroom retry, page travel, Task Finder honesty), test_layout (controls reachable: thread row under the conversation, canvas closed on load), test_batch_v (sticky page mode, output ceiling in the waits, free page review)"),
]

RIPPLE = """## Dependency ripple (what else to touch when you change a file)

- `orchestrator/vault.py` signatures → `app.py` callers (`render_thread_bar`, `execute_mission`, `render_routing_log`, `run_generation`, `render_history`, Task Finder) and `tests/test_threads.py`, `tests/test_vault.py`.
- `orchestrator/router.py` public names → `app.py` imports block, `cli.py`, `orchestrator/decomposer.py`, `orchestrator/executor.py`, `tests/test_cortex.py`.
- `orchestrator/config.py` provider defaults/limits → `orchestrator/discovery.py` preferences, README model-id lines, `tests/test_cortex.py` default assertions.
- `orchestrator/discovery.py` rules → `orchestrator/router.py` `_resilient_post`, `orchestrator/providers.py` `_with_model_recovery`.
- `orchestrator/sandbox_preview.py` (insert, build_document, report shape, fix_decision tuple, FIX_MARKER) → `frontend/sandbox_preview/main.js` (buildDocument mirror, statuses), `app.py` (`render_preview_panel`, `run_sandbox_preview`, `consider_sandbox_fix`, `dispatch_sandbox_fix`, `render_user_turn`), `tests/test_sandbox_preview.py`, `tests/test_sandbox_frontend.py`, `tests/test_batch_t_ui.py`.
- Any new sidebar/tab label → README feature/roadmap tables; any new claim → code that earns it or a stub marked not-yet.

## Verification commands

```
ruff check app.py cli.py orchestrator research tests
python -m py_compile app.py orchestrator/*.py
pytest -q
```
"""


def main() -> int:
    out = [
        "# Repository map (cached; refresh with `python .claude/skills/token-optimization/scripts/refresh_repo_map.py` when structure changes)",
        "",
        "Consult this map instead of sweeping the tree. Read only the files named for the change at hand.",
        "",
        "| File | Responsibility | Key symbols |",
        "|---|---|---|",
    ]
    for path, responsibility in ENTRIES:
        symbols = ""
        if os.path.isfile(path):
            source = open(path, encoding="utf-8").read()
            names = [n for n in re.findall(r"^(?:def|class) ([A-Za-z_][A-Za-z0-9_]*)", source, re.M) if not n.startswith("_")][:14]
            symbols = ", ".join(names)
        out.append(f"| `{path}` | {responsibility} | {symbols} |")
    out += ["", RIPPLE]
    target = os.path.join(".claude", "skills", "token-optimization", "references", "repo-map.md")
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("\n".join(out))
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
