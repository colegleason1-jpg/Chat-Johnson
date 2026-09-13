# Chat Johnson / Project Seth Recovery Audit

**Audit date:** 2026-09-13  
**Scope:** repository contents available after the save/crash boundary  
**Status:** baseline recorded; implementation should proceed in small, verified slices

## Executive finding

The repository is a Python 3.10+ Streamlit prototype for a BYOK, multi-provider coding orchestrator. The core orchestration path survived in the `orchestrator/` package. The README contains the larger Chat Johnson / Project Seth blueprint and a long Project Seth numerical experiment, but the requested product features are not yet implemented as a coherent application.

This is recoverable. The safest reconstruction strategy is to preserve the working pipeline, add explicit contracts and observability around it, and add external services only when a concrete feature needs them.

## Surviving implementation

| Area | Current evidence | Assessment |
|---|---|---|
| Provider registry | `orchestrator/config.py` | Six providers are registered: Gemini, Groq, NVIDIA NIM, OpenRouter, Cerebras, Mistral. Keys are read from environment variables. |
| HTTP adapters | `orchestrator/providers.py` | Requests-based OpenAI-compatible and Gemini REST clients exist, with retry handling. |
| Quota accounting | `orchestrator/quota.py` | Thread-safe RPM/TPM sliding windows plus daily token counters exist. Limits are conservative static estimates, not live provider metadata. |
| Routing | `orchestrator/router.py` | Keyword task classifier, provider ranking, context-window filtering, quota-aware fallback. No latency/quality telemetry or Monte Carlo optimizer yet. |
| Repo context | `orchestrator/repo_ingest.py` | Deterministic text-file walk with skip rules, importance ordering, and token budget. |
| Patch safety | `orchestrator/patches.py` | Safe relative paths, complete FILE blocks, unified diff support. |
| Sandbox | `orchestrator/sandbox.py` | Git worktree when possible, copy fallback otherwise, Python AST guardrail. |
| Test repair | `orchestrator/test_loop.py` | pytest execution and model-assisted repair loop. |
| Task memory | `orchestrator/memory.py` | Atomic JSON persistence of goal, summary, and step records. It is not yet a 200-message conversation store or artifact vault. |
| UI | `app.py` | Streamlit tabs for chat, task orchestration, and repo pipeline. Provider status and pipeline report are present. |
| CLI | `cli.py` | `status`, `chat`, and `run` commands. |
| Tests | `tests/test_orchestrator.py` | Focused tests cover quota, classification, patch safety, AST validation, and copy-mode sandboxing. |
| Project Seth math | `README.md` | A complete-looking Phase 3 scalar stochastic simulation is embedded in documentation, not implemented as an importable module or tested experiment package. |

## Important discrepancies and risks

1. **No `AGENT_GUIDE.md` is present.** `AGENTS.md` requires it, but the file is missing. This is a repository hygiene issue and should be restored from the intended source if available.
2. **Duplicate stale modules exist.** `App.py`, `Router.py`, and `Sandbox.py` are separate uppercase implementations and are not used by the documented `app.py` path. They must not be silently merged or deleted until ownership is confirmed.
3. **The UI does not implement BYOK input itself.** It reads environment/Keys-tab values; the sidebar is status-only. That is correct for secret handling in this environment but should be made explicit in the UI.
4. **Provider limits and model IDs can age out.** Static values in `config.py` are routing heuristics, not authoritative quotas. The system needs configurable limits and a visible “last checked / user override” policy.
5. **The current router is heuristic, not stochastic.** There is no measured latency, success-rate, quality score, entropy, pink-noise filter, or Monte Carlo simulation in the LLM path. Those should be treated as experimental ranking features, not prerequisites for safe routing.
6. **The README overstates security.** API keys are not encrypted local “vectors”; they are read from environment variables. No key vault or database persistence is implemented, which is preferable until a threat model and authenticated backend exist.
7. **The sandbox commits its isolated branch.** That protects the working tree, but lifecycle cleanup, branch retention, and explicit handoff/export need to be designed before automated GitHub delivery is added.
8. **Memory has no 200-message contract.** It stores orchestration steps and a summary, but not normalized messages, project scopes, artifacts, embeddings, or retention metadata.
9. **No connector bus exists.** Supabase, Neon, Upstash, MongoDB, Pinecone, D1, PlanetScale, DynamoDB, BigQuery, and “Toro” are README concepts only. “Toro” is also not sufficiently identified as a concrete service or API.
10. **No GitHub OAuth/SSH handshake exists.** The current code uses local Git commands in a sandbox. Ownership claims should be described as a design goal, not as an implemented guarantee.
11. **Heavy Mode is not implemented.** There is no mode policy, multi-pass contract, budget cap, approval gate, or UI toggle.
12. **The full README is not executable source.** It includes pasted assistant output, code fragments, and a large scientific experiment. Treat it as product evidence requiring normalization, not as a direct build script.

## Recovered product requirements

### Immediate Chat Johnson branch

- Normal Chat: low-latency single-pass response.
- Heavy Mode: explicit multi-pass planning, critique, and synthesis with a token/time budget and user-visible status; do not expose private chain-of-thought.
- Task Finder: typed plans, independent subtasks, progress, and result aggregation.
- Repository Work: ingest, patch, AST/test guardrails, reviewable diff, and explicit handoff.
- Chat Bot / developer mode: persistent project-scoped context, code blocks, files, and test feedback.
- Artifact Lock: save an output/file with title, project, provenance, and immutable version history.
- Context texturization: retain a rolling active window while producing summaries and preserving raw history for authorized retrieval.
- BYOK: provider keys supplied through environment/Keys UI; never write keys to JSON, logs, artifacts, or prompts.
- Free-tier safety: no paid endpoint or paid model should be selected accidentally; every provider needs an explicit billing policy and configurable limits.

### Long-range Project Seth branch

- Preserve the numerical experiment separately from production routing.
- Keep its scientific scope precise: simulated scalar stochastic observables only; no claim of propulsion, lift, or physical force.
- Add reproducible experiment files, tests, metadata, and output storage only after the Chat Johnson foundation is stable.

## Recommended implementation order

1. **Foundation and contracts:** normalize configuration, provider identity, route decision, error taxonomy, project/session/message/artifact records, redaction rules, and structured event logging.
2. **Chat modes:** implement Normal and Heavy policies behind one orchestration service, with budget/deadline/cancellation controls and tests using mocked providers.
3. **Memory and artifacts:** upgrade JSON memory to a versioned local store first; add 200-message active-window semantics, summaries, project scoping, artifact export, and recovery tests.
4. **Routing telemetry:** record latency, status, estimated/actual tokens, task type, and fallback reason. Use these measurements for deterministic ranking before experimenting with stochastic weighting.
5. **Repository safety:** improve patch validation, sandbox lifecycle, test commands, diff export, and explicit human approval. No background auto-commit or push by default.
6. **Task Finder:** introduce a bounded task graph, concurrency limits, cancellation, retries, and per-step budgets.
7. **Optional persistence connectors:** implement one canonical connector at a time behind an interface. Start with Supabase only if durable cross-device artifacts/auth are needed; otherwise local storage remains the zero-cost baseline.
8. **GitHub integration:** use a scoped GitHub App/OAuth flow with least-privilege permissions, never raw SSH key collection, and require explicit review before push/PR.
9. **Project Seth lab branch:** extract and test the documented simulation as a separate research module with reproducibility metadata.

## Definition of “recovered foundation”

The foundation is ready for feature work when:

- all provider calls are mockable and secrets never enter logs or persisted memory;
- Normal and Heavy modes have explicit, tested policies;
- route decisions explain provider, model, quota estimate, and fallback path;
- active chat retention is bounded and project-scoped;
- artifacts are versioned and exportable;
- repository changes remain isolated until human approval;
- tests cover failure paths, not only happy paths;
- connector additions do not make the core dependent on ten external services.
