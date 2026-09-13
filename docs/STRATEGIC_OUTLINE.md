# Chat Johnson · Strategic Outline for Approval

**Date:** 2026-09-13
**Branch:** `claude/ai-nonlinear-logic-arch-jakbpm` (currently identical to `main`)
**Status:** approved 2026-09-13 (decisions A–D). Phase 0 executed in this branch.

---

## 1. What the repository actually contains today

The repo is not an empty shell. The last Freebuff commit (`910cd6f`, 2026-09-13 05:42 UTC,
"Update 6 files") landed roughly 2,400 lines that already cover most of the Genesis directive:

| Directive item | Where it lives | State |
|---|---|---|
| BYOK vault from `os.environ` | `orchestrator/router.py` `refresh_byok_vault` / `byok_status` | Done, redacted status API |
| Cortex 3: rFFT 1/f noise, low-frequency clamp, standardization, alpha fit, Shannon entropy | `router.py` `generate_one_over_f_noise`, `fit_one_over_f_alpha`, `shannon_entropy` | Done |
| Cortex 3: Euler-Maruyama step `x + [A(x-x^3)+C]dt + sigma(1+|x|)eta sqrt(dt)` | `router.py` `advance_stochastic_project_seth_step`, `simulate_project_seth_trajectory` | Done, exact stencil |
| Cortex 2: `scipy.optimize.milp` with sum(x)=1, RPM and TPM rows, no-key exclusion rows, entropy-weighted utility | `router.py` `select_milp_endpoint`, `_build_constraint_array` | Done, with deterministic fallback when SciPy is absent |
| Free-tier ceilings 2/32k, 30/15k, 60 RPM | `router.py` `CORTEX_ENDPOINTS` | Done |
| Cortex 1: provider payload formatting, system prompt append, SSE stream | `router.py` `build_cortex_request`, `cortex_stream`, `cortex_generate` | Done |
| Heavy Mode multi-pass (draft → critique → synthesis) | `router.py` `_heavy_pipeline`, `generate_mode` | Done, bounded |
| Tree sanitization of `App.py`, `Router.py`, `Sandbox.py` | `app.py` `_sanitize_duplicate_modules` | Done, runs at import |
| SQLite vault with `message_history` (200-row rolling trigger) and `artifact_store` | `app.py` `SCHEMA_SQL` | Done |
| Four environments, left panel | `app.py` `render_task_finder`, `render_repository_work`, `render_chat_bot`, `render_normal_chat` | Done |
| Right panel HTML preview iframe | `app.py` `render_preview_panel`, `safe_preview_document` | Done, sanitized |
| Heavy Mode sidebar toggle | `app.py` sidebar checkbox | Done |
| Artifact Lock button beside code blocks | `app.py` `render_output_with_artifacts`, `save_artifact` | Done, versioned by content hash |
| GitHub OAuth handshake skeleton | `app.py` `github_oauth_url`, `exchange_github_code` | Skeleton only, needs client id/secret env |
| Recovery audit and implementation plan | `docs/RECOVERY_AUDIT.md`, `docs/IMPLEMENTATION_PLAN.md` | Present and sound |

Older surviving pipeline (pre-crash, still used): `orchestrator/` package with providers,
quota ledger, decomposer, repo ingest, worktree sandbox, patches, pytest repair loop, executor, and `cli.py`.

## 2. What I verified in this session

Everything below was run in a clean container. No live provider calls were made because no API keys are present.

| Check | Result |
|---|---|
| `py_compile` on every module | All compile |
| `pytest` (9 tests, legacy modules) | 9 passed |
| `ruff check` | 9 findings, all cosmetic (unused imports, E402 from sanitizer-before-imports) |
| Cortex 3 probe, 1024 samples, target alpha 1.0 | fitted alpha 0.881, trajectory entropy 5.50 bits, 10 ms total |
| MILP routing with fake keys | chat → groq, context_load → gemini, code_patch → groq, all via `scipy.optimize.milp` |
| MILP with 40k-token context_load | Gemini excluded by its 32k TPM cap, falls to Hugging Face. Constraint works. |
| Headless Streamlit boot driven by Chromium | Page renders, all four environments switch, preview canvas renders |
| Sanitizer effect | `App.py`, `Router.py`, `Sandbox.py` deleted on first run; `chat_johnson_vault.db` created |

Conclusion: the two pillars of the Genesis directive exist and run. Rewriting them from scratch would
throw away working, tested code. The right move is to harden, clean, and extend.

## 3. Gaps and risks against the directive

1. **Stale uppercase modules are tracked in git.** `App.py`, `Router.py`, `Sandbox.py`, `Ledger.py`,
   `Guardrails.py`, `Config.py` are all committed. The runtime sanitizer deletes three of them, but every
   fresh clone brings them back, and `git status` shows deletions after each launch. Delete them from git.
2. **The vault database is not gitignored.** `chat_johnson_vault.db` will be committed by accident.
3. **README is a 78 KB dump.** It mixes the working doc, pasted chat transcripts, the Bible, and the full
   Phase 3 engine (about 1,000 lines of Python inside markdown). The Phase 3 engine is not importable or
   tested from there.
4. **No tests cover the new engine.** Cortex 1/2/3, Heavy Mode, SQLite window, Artifact Lock, redaction,
   and the GitHub OAuth state check all have zero tests. This is the main crash-recovery risk: without
   tests, a future corruption is invisible.
5. **Model IDs will age out.** `gemini-1.5-pro` has been retired by Google for new projects; the Cortex
   endpoint table hardcodes it with no env override. Same exposure for the Hugging Face router model.
   The 2 RPM / 32k TPM ceiling should stay as policy, the model id must be configurable.
6. **Heavy Mode spec conflict.** The directive names o1/o3-mini chains. OpenAI has no free tier, which
   violates the BYOK free-only invariant in the Bible. Current code runs Heavy Mode as a bounded
   draft → critique → synthesis loop over the free endpoints, which honors the invariant.
7. **The 200-message cap is destructive.** The SQLite trigger hard-deletes row 201+. The Bible calls for
   texturization (summarize, then evict). No `summaries` table exists yet.
8. **No CI.** Nothing runs tests on push, so a broken push is silent.
9. **Chat panels do not stream.** `cortex_stream` exists but the UI calls the blocking path.
10. **Task Finder is serialized.** A lock covers each request so free-tier keys are not double-spent.
    Correct for free tiers; the "asynchronous" label is optimistic.
11. **Crash resilience is the real pain point.** Work should be committed in small verified slices and
    pushed every time, with CI green as the gate.

## 4. Proposed phases

### Phase 0 · Hygiene and durability (one commit, this session on approval)
- Remove the six stale uppercase modules from git. Keep the runtime sanitizer as a safety net.
- Add `chat_johnson_vault.db`, `.orchestrator/`, and `generated/` to `.gitignore`.
- Fix the 9 ruff findings.
- Add GitHub Actions CI: `ruff check` + `pytest` on every push and PR.
- Split the README: clean `README.md`, `docs/PROJECT_BIBLE.md` (Bible + Tri-Processor spec, verbatim),
  `research/project_seth_phase3.py` (the Phase 3 engine as an importable module with the README's
  recommended smoke configuration as a test). Nothing is discarded, only moved.

### Phase 1 · Test armor for the existing engine
- Mocked-provider tests for Cortex 1 (payload shape per provider, system prompt append, SSE parsing,
  secret redaction in errors).
- MILP tests: exclusivity, RPM exhaustion, TPM exclusion, no-key exclusion, entropy penalty ordering,
  SciPy-absent fallback parity.
- Cortex 3 tests: alpha recovery within tolerance for alpha in {0.5, 1.0, 1.5}, DC bin is zero, unit
  variance, entropy bounds, exact one-step SDE value.
- Heavy Mode tests: call count is 3, degrades to draft on critique failure, never exposes hidden reasoning.
- SQLite tests: 201st message rolls, artifact versions are immutable, project scopes isolate.
- Env overrides for Cortex model ids (`CORTEX_GEMINI_MODEL`, etc.). Wire `st.write_stream` for chat panels.

### Phase 2 · Memory and texturization
- Add `summaries` table. Before eviction, texturize the oldest block into a compact summary via the
  cheapest free endpoint; keep summaries in the context block.
- Artifact export (download, copy to repo path) and artifact search in the sidebar.

### Phase 3 · First live validation
- With your keys in the environment: run `cli.py status`, one Normal Chat, one Heavy Mode pass, one
  Task Finder run. Record latency and status headers into the ledger.
- Decide the Heavy Mode reasoning slot (see decision B).

### Phase 4 · Task Finder and Repository Work depth
- Task DAG with persisted results, cancel and retry, per-step budgets.
- Repository Work: explicit apply / export / open-PR handoff after diff review. GitHub App or OAuth
  with `repo` scope only when you switch it on.

### Phase 5 · Opt-in connectors and autosave
- Supabase first (artifacts + auth), behind an interface, local SQLite stays authoritative.
- Bounded autosave: opt-in periodic commit to an `autosave/<scope>` branch, the safe form of the
  "Background Git-Streamer".

### Phase 6 · Project Seth lab branch (parked, per your ledger)
- Research module gets reproducibility metadata and CSV outputs. No routing dependency, no physical claims.

## 5. Decisions I need from you

- **A. Stale modules.** *Approved.* Delete `App.py`, `Router.py`, `Sandbox.py`, `Ledger.py`, `Guardrails.py`,
  `Config.py` from git and keep the runtime sanitizer? *Recommended: yes.*
- **B. Heavy Mode reasoning slot.** Keep free-only (NVIDIA NIM DeepSeek-R1, Gemini, Groq in a
  draft → critique → synthesis chain), with an optional, default-off `OPENAI_API_KEY` paid slot you can
  flip on later? *Recommended: free-only default, optional slot.*
  **Approved with a hard constraint:** everything in the backend is free-only. The paid slot must be
  toggled on *and* have its key entered fresh every session; it is never persisted, never read from a
  stored environment variable, and never on by default, so money cannot be spent by accident.
- **C. README split.** *Approved.* Approve moving the Bible to `docs/` and the Phase 3 engine to `research/` with
  a clean README? *Recommended: yes, verbatim moves.*
- **D. Execute Phase 0 now.** *Approved.* On approval I commit and push Phase 0 immediately, then start Phase 1.

## 6. Ground rules I will follow

- Every slice: tests green locally, ruff clean, then commit and push to this branch. Small commits, often.
- Keys stay in the environment. Never in SQLite, logs, prompts, artifacts, or git.
- No provider calls in tests. Live validation is a separate, explicit step with your keys.
- No auto-commit, push, or connector write without an explicit opt-in toggle.
- Project Seth math stays a routing signal with honest labeling, exactly as the current code states.
