# Repository map (cached; refresh with `python .claude/skills/token-optimization/scripts/refresh_repo_map.py` when structure changes)

Consult this map instead of sweeping the tree. Read only the files named for the change at hand.

| File | Responsibility | Key symbols |
|---|---|---|
| `app.py` | Streamlit studio: sidebar (keys, Heavy Mode, paid slot, artifacts, memory, roadmap stubs), server-side workspace switch (segmented control or radio, ?ws=), one chat bar pinned to the bottom and routed to the selected workspace, per-workspace thread row (New/Clear/Delete/More), Task Finder missions, routing log, preview canvas | credential_fingerprint, get_quota_ledger, get_task_request_lock, build_marker, configured_provider_names, active_mode, session_paid_slot, github_push_status, provider_status_rows, build_prompt_messages, model_refine_digest, health_sweep, display_language, infer_artifact_path |
| `orchestrator/router.py` | Cortex 1/2/3: BYOK status, endpoints + ceilings, 1/f noise + SDE + telemetry penalty, MILP selection, resilient HTTP + streaming, probes, paid slot, Heavy Mode pipeline, legacy generate/generate_mode | refresh_byok_vault, byok_status, CortexEndpoint, endpoint_model, PinkNoiseResult, shannon_entropy, fit_one_over_f_alpha, generate_one_over_f_noise, advance_stochastic_project_seth_step, simulate_project_seth_trajectory, record_telemetry, telemetry_snapshot, project_seth_routing_entropy, MILPDecision |
| `orchestrator/discovery.py` | vendor model lists, preference order, retired/transient/unusable rules, backoff, validated discovery | vendor_for, discovered, looks_like_retired_model, is_transient, retry_delay, sleep, list_models, rank_models, looks_like_unusable_model, discover, alternates |
| `orchestrator/providers.py` | legacy OpenAI-compatible + Gemini client with retries, model recovery, key redaction | ProviderError, metered, body_text, chat |
| `orchestrator/config.py` | legacy provider registry, session key overlay (ContextVar), Settings, provider_model resolution | session_keys, bind_session_keys, set_session_key, clear_session_keys, resolve_secret, ProviderConfig, provider_api_key, provider_model, Settings, get_settings |
| `orchestrator/quota.py` | QuotaLedger: RPM/TPM windows, tighten, record_attempt, record | Bucket, QuotaLedger |
| `orchestrator/vault.py` | SQLite: threads per workspace, messages, window/texturize/archive, summaries, artifacts, health sweep, vision digest, migration | database_path, initialize_database, active_thread, thread_by_id, list_threads, create_thread, switch_thread, rename_thread, set_thread_mission, set_thread_status, clear_thread, delete_thread, estimate_tokens, redact_secrets |
| `orchestrator/missions.py` | deterministic mission classification + workstream templates for Task Finder | classify_mission, parse_length_target, words_per_step, writing_sections, mission_hints, task_plan, deliverable_slug, assemble_deliverable, text_measure |
| `orchestrator/preview.py` | nh3 allowlist sanitizer + preview document + markup extraction | sanitize_markup, looks_like_markup, extract_preview_source, safe_preview_document |
| `orchestrator/github_auth.py` | signed, time-limited OAuth state | mint_state, verify_state |
| `orchestrator/connectors.py` | local SQLite connector + roadmap stubs (not enable-able) | ConnectorHealth, Connector, LocalSQLiteConnector, connector_status |
| `orchestrator/executor.py` | repository pipeline: decompose → sandbox → patches → AST/pytest → diff | Orchestrator |
| `orchestrator/sandbox.py` | git worktree / copy-mode sandbox, copy-mode difflib diff, AST guardrail | SandboxError, create_worktree, commit_sandbox, diff_vs_base, cleanup_worktree, validate_python_files |
| `orchestrator/patches.py` | FILE-block / unified-diff parsing and safe application, changed_files | parse_file_blocks, parse_diff_blocks, apply_file_blocks, apply_unified_diffs, changed_files |
| `orchestrator/decomposer.py` | LLM JSON step planner for the repository pipeline | decompose |
| `orchestrator/test_loop.py` | pytest + traceback repair loop | run_pytest, repair_loop |
| `orchestrator/repo_ingest.py` | token-budgeted repository serialization | walk_repo, serialize_repo |
| `orchestrator/memory.py` | task memory JSON for the repository pipeline | StepRecord, TaskMemory |
| `research/project_seth_phase3.py` | isolated Phase 3 research engine (no routing dependency) | ProjectSethConfig, TrajectoryResult, TrackSummary, PairedDelta, SweepAggregate, validate_config, confidence_interval_95, format_float, control_coefficient, drift, noise_gate, quintic_smootherstep, transition_fraction, transition_rate |
| `cli.py` | status / chat / run commands | cmd_status, cmd_chat, cmd_run, main |
| `tests/` | test_cortex (routing), test_vault, test_threads, test_missions, test_preview, test_github_auth, test_executor_pipeline, test_session_keys, test_orchestrator, test_project_seth_phase3 |  |

## Dependency ripple (what else to touch when you change a file)

- `orchestrator/vault.py` signatures → `app.py` callers (`render_thread_bar`, `execute_mission`, `render_routing_log`, `run_generation`, `render_history`, Task Finder) and `tests/test_threads.py`, `tests/test_vault.py`.
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
