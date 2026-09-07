# 🧠 Freebuff Orchestrator — multi-provider free-LLM pipeline

A chat bot + execution pipeline that **surpasses any single free LLM** by
routing each chunk of a task to the free model that handles it best, tracking
overall progress, and verifying every code change in an isolated git worktree
before it ever touches your working tree.

## How it works

```
goal ─▶ decompose (reasoning model) ─▶ typed steps ─▶ router picks best free provider
        ├─ context_load  → Gemini (1M-token window ingests the whole repo)
        ├─ reasoning     → NVIDIA NIM / DeepSeek-class planners
        ├─ code_patch    → Groq / Cerebras (blazing-fast targeted diffs)
        ├─ quick_text    → Groq / Mistral (summaries, titles)
        └─ test_fix      → NIM / OpenRouter (traceback feedback loop)

each code step: worktree sandbox → FILE blocks/git apply → ast.parse guardrail
              → pytest → traceback fed back to the model until green
```

## Modules

| File | Responsibility |
|---|---|
| `orchestrator/config.py` | Provider registry, free-tier limits, env keys |
| `orchestrator/quota.py` | **Quota Ledger**: RPM + TPM sliding windows, daily tokens |
| `orchestrator/providers.py` | Unified HTTP client (OpenAI-compatible + Gemini REST) |
| `orchestrator/router.py` | Classify chunk → rank candidates → route with fallback |
| `orchestrator/memory.py` | Rolling task memory persisted to disk |
| `orchestrator/repo_ingest.py` | Serialize repo into a token-budgeted context |
| `orchestrator/sandbox.py` | `git worktree` staging + `ast.parse()` guardrails |
| `orchestrator/patches.py` | Deterministic FILE-block / unified-diff application |
| `orchestrator/test_loop.py` | pytest + traceback feedback repair loop |
| `orchestrator/executor.py` | The closed loop: goal → plan → route → verify → report |
| `app.py` | Streamlit interface (chat, task tracker, repo pipeline) |
| `cli.py` | `status`, `chat`, `run` commands |

## Setup

1. Add any subset of these keys (Keys/API keys tab or your own `.env`):
   `GEMINI_API_KEY`, `GROQ_API_KEY`, `NVIDIA_API_KEY`, `OPENROUTER_API_KEY`,
   `CEREBRAS_API_KEY`, `MISTRAL_API_KEY`.
2. Install: `pip install -r Requirements.txt`
3. Check: `python cli.py status`
4. Run:
   - UI: `streamlit run app.py`
   - CLI chat: `python cli.py chat`
   - Full pipeline: `python cli.py run "Add retry logic to client.py" --repo ./myrepo`

## Design rules baked in

- **Never trust one model with a repo rewrite** — the decomposer splits goals
  into small typed chunks; the patcher only accepts complete FILE blocks or
  unified diffs, and rejects placeholders.
- **Quota ledger counts RPM *and* TPM** — free tiers throttle on volume, and
  the router only picks providers with headroom, falling through the ranked
  candidate list on 429s.
- **Working tree is sacred** — all edits land in a `git worktree` sandbox
  (copy-mode fallback for non-git folders); you get a verified diff back.
- **Guardrails before handoff** — `ast.parse()` on every changed file, then
  pytest; failures go back to a reasoning model with the exact traceback.
