#!/usr/bin/env python3
"""Regenerate references/repo-map.md from the code (run from the repository root).

Usage: python .claude/skills/token-optimization/scripts/refresh_repo_map.py
"""
from __future__ import annotations

import os
import re
import sys

ENTRIES = [
    ("app.py", "Streamlit studio: sidebar (keys, Heavy Mode, paid slot, artifacts, memory, roadmap stubs), four workspace tabs, per-workspace thread bar, Task Finder, preview canvas"),
    ("orchestrator/router.py", "Cortex 1/2/3: BYOK status, endpoints + ceilings, 1/f noise + SDE + telemetry penalty, MILP selection, resilient HTTP + streaming, probes, paid slot, Heavy Mode pipeline, legacy generate/generate_mode"),
    ("orchestrator/discovery.py", "vendor model lists, preference order, retired/transient/unusable rules, backoff, validated discovery"),
    ("orchestrator/providers.py", "legacy OpenAI-compatible + Gemini client with retries, model recovery, key redaction"),
    ("orchestrator/config.py", "legacy provider registry, session key overlay (ContextVar), Settings, provider_model resolution"),
    ("orchestrator/quota.py", "QuotaLedger: RPM/TPM windows, tighten, record_attempt, record"),
    ("orchestrator/vault.py", "SQLite: threads per workspace, messages, window/texturize/archive, summaries, artifacts, health sweep, vision digest, migration"),
    ("orchestrator/missions.py", "deterministic mission classification + workstream templates for Task Finder"),
    ("orchestrator/preview.py", "nh3 allowlist sanitizer + preview document + markup extraction"),
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
    ("tests/", "test_cortex (routing), test_vault, test_threads, test_missions, test_preview, test_github_auth, test_executor_pipeline, test_session_keys, test_orchestrator, test_project_seth_phase3"),
]

RIPPLE = """## Dependency ripple (what else to touch when you change a file)

- `orchestrator/vault.py` signatures → `app.py` callers (`render_thread_controls`, `run_generation`, `render_history`, Task Finder) and `tests/test_threads.py`, `tests/test_vault.py`.
- `orchestrator/router.py` public names → `app.py` imports block, `cli.py`, `orchestrator/decomposer.py`, `orchestrator/executor.py`, `tests/test_cortex.py`.
- `orchestrator/config.py` provider defaults/limits → `orchestrator/discovery.py` preferences, README model-id lines, `tests/test_cortex.py` default assertions.
- `orchestrator/discovery.py` rules → `orchestrator/router.py` `_resilient_post`, `orchestrator/providers.py` `_with_model_recovery`.
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
