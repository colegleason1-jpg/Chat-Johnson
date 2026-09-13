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

A workspace switch across the top of the main panel and one chat bar pinned to the bottom of the
screen. The bar always sends to the selected workspace, so the input is never out of reach while
reading. Each workspace owns its own chats (selector, New chat, Clear chat, Delete chat, and a More
menu with Rename, context load, and Migrate now); keys are never touched by any of these.

- **Task Finder**: a mission is classified (research, code, analysis, plan, general) with no provider
  call and expanded into typed workstreams you can edit before launch. Steps run one at a time through
  the router to respect free-tier limits; every result lands in the workspace's chat, and a
  chat bar keeps the conversation going with the results in context.
- **Repository Work**: least-privilege GitHub OAuth skeleton, the local sandboxed patch pipeline, and a
  discussion thread for the change.
- **Chat Bot**: long-form developer chat with file uploads injected as context.
- **Normal Chat**: single-pass chat.

## Implemented vs roadmap

| Bible feature | Status |
|---|---|
| BYOK key panel, session-scoped, Test keys for all 7 providers | Implemented |
| Tri-Processor Cortex (1/f probe, MILP selection, streaming) with self-healing model ids | Implemented (routing signal only) |
| Heavy Mode (draft → review → synthesis) with optional per-session paid review slot | Implemented |
| SQLite vault: per-workspace threads, 200-message windows, texturize-then-archive, artifacts | Implemented |
| Thread-health agent with vision-digest migration | Implemented |
| Repository sandbox pipeline with AST + pytest repair loop | Implemented (local) |
| GitHub OAuth handshake | Skeleton (read-only identity check) |
| 10-cloud connector fabric | Roadmap · local SQLite is the only store; stubs listed in the sidebar |
| Background Git-Streamer | Roadmap · not implemented |
| Live SDK Document Scraper | Roadmap · not implemented |
| Self-Correcting Execution Sandbox for chat output | Roadmap · exists only inside the repository pipeline |
| Cross-thread semantic search | Roadmap · artifact search is text matching |
| Capability card in every prompt (what the app can and cannot do) and memory as real chat turns | Implemented |
| Repository Work hub connector: fetch a GitHub repository through the API (public, or private with the armed token) into a temporary sandbox, run the pipeline, download the patch, push the change as a branch plus pull request; four tabs (Work, Deploy Kit, GitHub, Directions) | Implemented · tests of the fetched repository run only when ticked |
| Deploy Kit (Repository Work): CI/CD workflow, Dockerfile, Helm chart, Terraform skeleton, serverless template, observability, rollback script, runbook; offline validation; zip download; lock as artifacts | Implemented · generation only, nothing is pushed or applied |
| Post-deploy checks for this app: `?health=1` JSON view, `scripts/smoke_drive.py`, `post-deploy-smoke` workflow, `docs/RUNBOOK.md` | Implemented · set the `DEPLOY_URL` repository variable to arm the workflow |
| Mission nodes (sub-agents, connectors, APIs per workstream) and chat → Task Finder handoff | Planned · `docs/MISSION_NODES_DESIGN.md` |
| Session-only GitHub push: token and repo armed per session, one commit on a new branch plus an opened pull request, revert PR for any push from the session | Implemented · never the default branch |
| App observability: every send persisted to `route_log`, Ops view (sends, share, p50/p95, truncations) and CSV export under the routing expander | Implemented |

Controls: **Heavy Mode** toggle (multi-pass, more tokens, longer wait), **Artifact Lock** beside every
code block (versioned save to SQLite), output token budget slider, per-project scope.

## Threads and the thread-health agent

Every workspace holds any number of **chats** (threads). The row at the top of each workspace lets you
switch, start a new one, **Clear chat** (messages move to the archive and leave the context; keys are
untouched), or **Delete chat** (the chat, its archive, and its summaries are removed after a
confirmation; locked artifacts stay). Rename, Migrate now, and **Download this chat** (Markdown or JSON,
with the archive, summaries, and inherited digest) live under More. Commit a download to a `transcripts/`
folder in the repository to hand a full conversation to the assistant for an audit without pasting it.

Every send is traced in the **Routing log** on the right (workspace, task type, provider/model,
latency, solver reason) and stored with its task type on the message, so routing can be judged
against the project's vision over a long session.
Each thread has its own 200-message window and its own texturized summaries.

Before every send, a zero-quota **health sweep** measures the active thread: message count, estimated
tokens in the live window, stacked summaries, repeated prompts, and error loops. When a threshold
trips (or you press *Migrate now*), the agent:

1. compresses the whole thread, archive included, into a **vision digest**: how it started, decisions
   and constraints, key facts (files, numbers), open items, locked artifacts, and the summaries;
2. asks the cheapest available free model to refine that digest when a key is present (the
   deterministic base is kept underneath for audit);
3. locks the digest as an immutable artifact, opens a successor thread that injects the digest into
   every prompt, and marks the old thread *migrated*. Nothing raw is deleted.

The result is a fresh thread that carries the original vision in a re-optimized, token-light form.
Switch **Auto-migrate heavy threads** off in the sidebar to keep migrations manual.

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

### Model ids and retirements

Vendors retire model ids without warning (Groq shut down `llama-3.3-70b-versatile` on 2026-08-16;
Google retired `gemini-1.5-pro` and `gemini-2.0-flash`). The router resolves each endpoint's model
at call time in this order:

1. an env override: `CORTEX_GEMINI_MODEL`, `CORTEX_GROQ_MODEL`, `CORTEX_HF_MODEL`;
2. a live id discovered from the vendor's model list after a "retired / not found" error
   (cached for the process, preferring the newest Flash / gpt-oss ids);
3. the built-in default (`gemini-3.6-flash`, `openai/gpt-oss-120b`, `Qwen/Qwen2.5-Coder-32B-Instruct`; legacy NVIDIA default `meta/llama-3.3-70b-instruct`).

The RPM/TPM ceilings never change with the id. The sidebar **Test keys** button sends one small
request per configured provider (plus one model lookup if the id is retired) and reports the real
HTTP status, key fingerprint, and any auto-switch. Probes count toward the vendor's request ceiling.

### Routing signals, honestly labelled

- **Cortex 2** selects one endpoint with `scipy.optimize.milp`; its constraint rows (exclusivity, RPM
  capacity, TPM capacity, key present) decide feasibility. When nothing fits, the error names the
  blocking ceiling per endpoint. Without SciPy the identical rows are evaluated in Python.
- **Cortex 3** turns *observed* telemetry into a penalty: every real HTTP attempt records latency and
  outcome per endpoint; the Project Seth SDE is driven by the observed failure rate (bias term) and
  latency (noise gate), seeded by the endpoint name so it is reproducible, and the penalty is the
  entropy gained over the undriven baseline. An endpoint with no observations gets no penalty. This
  is a routing signal, not a physical claim.
- **Legacy providers** (NVIDIA NIM, OpenRouter, Cerebras, Mistral) are a fallback only: they serve a
  request when no Cortex key (Gemini, Groq, Hugging Face) is set or when every Cortex endpoint fails
  it. Their retries, sibling-model attempts, and rediscovery calls are metered like everything else.
- **Prompt context sizing**: the project-memory block is sized so the request fits every keyed
  endpoint's TPM ceiling at the current output budget (4 chars per token, 500-token reserve, never
  below 8k or above 24k characters), so a long thread does not silently lock out the fastest
  endpoint. The live window fills newest-first, so a follow-up always sees the latest results.
- **Free-tier pacing in Task Finder**: before each workstream the ledger is consulted; if every keyed
  vendor is inside its RPM/TPM window the step waits (up to 65 s) instead of failing. Missions are
  pinned to the chat, so they survive window eviction and thread migration.
- **Quota ledger**: one bucket per vendor credential; every HTTP attempt (retries, rediscovery,
  probes) counts toward RPM; tokens are charged on success. A visitor's ledger is keyed by a
  non-reversible fingerprint of their applied keys, so visitors never throttle each other.

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

- Point the app at **this repository, the branch you want to run, and `app.py`**. The working
  branch `claude/ai-nonlinear-logic-arch-jakbpm` is ahead of `main` until its pull request merges.
- Dependencies come from the lowercase `requirements.txt`; `.python-version` requests 3.12.
- Keys pasted in the sidebar are scoped to your own browser session. Other visitors to the same
  app URL do not see them and must paste their own.

## Development

```
ruff check app.py cli.py orchestrator research tests
pytest -q
```

CI runs the same lint, compile, and test steps on every push and pull request.
