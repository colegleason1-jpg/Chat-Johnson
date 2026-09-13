# 🧠 Chat Johnson · Master Studio

A BYOK (bring-your-own-key), free-tier-only, multi-provider developer workbench. It routes each
piece of work to the free LLM endpoint best suited for it, keeps every code change in an isolated
git worktree until a human approves it, and stores chat history and locked artifacts in a local
SQLite vault.

Chat Johnson is the active branch of the Project Seth family. Project Seth's stochastic math is used
here strictly as an experimental routing signal; the research engine itself is parked in `research/`.
See `docs/PROJECT_BIBLE.md` for the full design bible and `docs/STRATEGIC_OUTLINE.md` for the
approved reconstruction plan.

## Architecture

```
                       [ TRI-PROCESSOR CORTEX  ·  orchestrator/router.py ]
                                              │
        ┌─────────────────────────────────────┼─────────────────────────────────────┐
        ▼                                     ▼                                     ▼
 [ CORTEX 1 · LLM CORE ]           [ CORTEX 2 · MILP CONTROLLER ]       [ CORTEX 3 · PHYSICS PROBE ]
 provider payloads, system         scipy.optimize.milp, binary 0/1      rFFT 1/f noise, Euler-Maruyama
 prompt append, SSE stream         decision under RPM/TPM ceilings      SDE, Shannon entropy penalty

 goal ─▶ classify ─▶ Cortex 3 entropy ─▶ Cortex 2 selects one endpoint ─▶ Cortex 1 streams
        └─ Heavy Mode: bounded draft → critique → synthesis over the same free endpoints

 repo work: git worktree sandbox → FILE blocks / unified diff → ast.parse guardrail → pytest
            → traceback fed back to the model → reviewable diff (never auto-applied)
```

## Modules

| Path | Responsibility |
|---|---|
| `app.py` | Streamlit studio: sidebar control deck, four environments, preview canvas, SQLite vault, Artifact Lock |
| `cli.py` | `status`, `chat`, `run` commands |
| `orchestrator/router.py` | BYOK vault, Cortex 1/2/3, Heavy Mode, legacy provider routing |
| `orchestrator/config.py` | Legacy provider registry and conservative free-tier limits |
| `orchestrator/quota.py` | RPM + TPM sliding windows, daily token counters |
| `orchestrator/providers.py` | HTTP client for OpenAI-compatible and Gemini REST endpoints |
| `orchestrator/decomposer.py` | Goal → typed steps |
| `orchestrator/repo_ingest.py` | Token-budgeted repository serialization |
| `orchestrator/sandbox.py` | `git worktree` staging, copy-mode fallback, AST guardrail |
| `orchestrator/patches.py` | Safe FILE-block and unified-diff application |
| `orchestrator/test_loop.py` | pytest + traceback repair loop |
| `orchestrator/executor.py` | goal → plan → route → verify → report |
| `orchestrator/memory.py` | Task memory persisted to disk |
| `research/project_seth_phase3.py` | Project Seth Phase 3 distribution and bias-sweep engine (research only) |
| `docs/` | Bible, recovery audit, implementation plan, strategic outline |

## Operational environments

- **Task Finder**: bounded multi-workstream execution with progress and per-step results.
- **Repository Work**: least-privilege GitHub OAuth skeleton plus the local sandboxed patch pipeline.
- **Chat Bot**: long-form developer chat with file uploads injected as context.
- **Normal Chat**: zero-overhead single-pass chat.

Controls: **Heavy Mode** toggle (multi-pass, more tokens, longer wait), **Artifact Lock** beside every
code block (versioned save to SQLite), output token budget slider, per-project scope.

## Setup

1. `pip install -r requirements.txt`
2. Supply keys either way:
   - **In the app:** open the sidebar "API keys" panel, paste any subset, click **Apply keys**.
     They live in process memory for the session only and override environment variables.
   - **In the environment / `.env`:** `GEMINI_API_KEY`, `GROQ_API_KEY`, `HUGGINGFACE_API_KEY`
     (or `HF_TOKEN`), `NVIDIA_API_KEY`, `OPENROUTER_API_KEY`, `CEREBRAS_API_KEY`, `MISTRAL_API_KEY`.
3. `python cli.py status` (reads environment keys; the CLI has no key panel)
4. Run:
   - Studio: `streamlit run app.py`
   - CLI chat: `python cli.py chat`
   - Repo pipeline: `python cli.py run "Add retry logic to client.py" --repo ./myrepo`

Keys are read from the process environment at call time. They are never written to SQLite, logs,
prompts, artifacts, or git.

Optional model-id overrides for the strict Cortex endpoints (useful when a vendor retires an id):
`CORTEX_GEMINI_MODEL`, `CORTEX_GROQ_MODEL`, `CORTEX_HF_MODEL`. The RPM/TPM ceilings stay fixed.

### Paid reasoning slot (optional, session-only)

The backend is free-tier only. Heavy Mode can optionally send its *review* pass to a paid
OpenAI-compatible reasoning model. The slot arms only when, in the current browser session, you
switch it on **and** paste a key. It is never persisted, never read from an environment variable,
and Normal mode never uses it. Close the tab and it is gone.

## Design rules

- **Free-tier only in the backend.** Any paid slot must be toggled on and have its key entered fresh
  every session. Nothing paid is reachable by default.
- **Never trust one model with a repo rewrite.** Goals are split into small typed steps; the patcher
  accepts only complete FILE blocks or unified diffs and rejects placeholders.
- **Quota ledger counts RPM and TPM.** Cortex 2 only selects endpoints with headroom; 429s fall
  through the ranked list.
- **Working tree is sacred.** Every edit lands in a `git worktree` sandbox; you get a verified diff.
- **Guardrails before handoff.** `ast.parse()` on every changed file, then pytest, then the traceback
  goes back to a reasoning model.
- **No hidden chain-of-thought.** Heavy Mode shows the final answer and a concise rationale only.

## Deploying on Streamlit Community Cloud

- Point the app at **this repository, the branch you want to run, and `app.py`**. The
  `main` branch does not yet carry the key panel; use `claude/ai-nonlinear-logic-arch-jakbpm`
  until it is merged.
- Dependencies come from the lowercase `requirements.txt`; `.python-version` requests 3.12.
- Keys pasted in the sidebar are scoped to your own browser session. Other visitors to the same
  app URL do not see them and must paste their own.

## Development

```
ruff check app.py cli.py orchestrator research tests
pytest -q
```

CI runs the same lint, compile, and test steps on every push and pull request.
