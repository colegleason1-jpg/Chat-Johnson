# Chat Johnson Recovery Implementation Plan

## Guiding decisions

1. **Core first, connectors second.** A ten-service fan-out increases failure modes, secrets, cost exposure, and debugging surface. The core must work with local disk and mocked providers before any cloud connector is enabled.
2. **BYOK means explicit provider policy.** Every provider has a key name, model policy, quota policy, billing class (`free_only`, `user_allowed_paid`, or `disabled`), and redacted telemetry. No key is ever stored in task memory, artifacts, logs, prompts, or README output.
3. **Heavy Mode is a policy, not chain-of-thought display.** It may use planning, independent critique, structured verification, and synthesis, but only the final answer and concise rationale are shown. The app must enforce a request/token/time budget.
4. **Telemetry before stochastic routing.** Collect real latency, success, rate-limit, token, and task-outcome measurements first. A deterministic score is auditable; Monte Carlo/pink-noise weighting can later be added as an optional experiment and must not claim scientific optimality.
5. **Human approval before external side effects.** Repository edits are sandboxed; commits, pushes, artifact publication, connector writes, and GitHub PRs require explicit user actions.
6. **Project Seth is isolated.** The stochastic experiment is research code and documentation, not an inference-routing dependency and not evidence of physical propulsion.

## Target module map

```text
chat_johnson/
  domain/
    models.py              # Provider, mode, project, message, artifact, task, event
    policies.py             # Normal/Heavy/Task Finder/Repo mode budgets and invariants
    errors.py               # Typed provider/quota/patch/connector errors
  providers/
    base.py                # ProviderAdapter protocol and normalized response
    registry.py             # Config + enabled/billing policy + model capabilities
    http.py                 # requests transport, timeout, retry-after, redaction
    adapters.py             # Gemini/OpenAI-compatible adapters
  routing/
    classifier.py           # task classification; deterministic and testable
    scorer.py               # quota/latency/quality/fallback score
    ledger.py               # RPM/TPM/RPD/TPD and provider response headers
    service.py              # route, execute, fallback, explain decision
  memory/
    store.py                # local versioned store with project/session scopes
    window.py               # 200-message active window and summary boundaries
    texturizer.py           # compression interface; no raw history deletion
    artifacts.py            # immutable artifact versions and export metadata
    redact.py               # secret/prompt/log redaction
  workspace/
    ingest.py               # repo inventory, budgets, ignore rules
    patches.py              # safe FILE blocks/diffs and placeholder rejection
    sandbox.py              # worktree/copy lifecycle and cleanup
    verify.py               # AST, configured test/lint/typecheck commands
    handoff.py              # reviewable diff and explicit apply/export
  modes/
    chat.py                 # Normal single-pass request
    heavy.py                # bounded plan -> draft -> critique -> synthesis
    tasks.py                # bounded DAG, progress, cancellation
    repository.py            # ingest -> patch -> verify -> review
  connectors/
    protocol.py             # optional connector contract and health state
    local.py                # default durable local implementation
    supabase.py              # only when durable cross-device artifacts/auth needed
    upstash.py               # optional cache/queue, never source of truth
  ui/
    streamlit_app.py        # keep current app shell until extraction is justified
  research/
    project_seth_phase3.py  # extracted numerical experiment, separate lifecycle
```

## Feature slices and gates

### Slice 1 — contracts and safety

- Add normalized provider response/error structures.
- Separate configured, available, quota-eligible, and billing-allowed states.
- Record actual response headers (`Retry-After`, provider remaining/reset headers) when available.
- Fix model selection reporting to use `provider_model(cfg)` rather than the un-overridden default.
- Add secret redaction tests for API-key-shaped strings and environment names.
- Add deterministic provider mocks so tests do not make network calls.

**Gate:** existing tests plus mocked provider contract tests pass; no network needed.

### Slice 2 — Normal and Heavy modes

Normal:

```text
classify -> route -> one provider call -> normalized answer -> telemetry -> memory
```

Heavy:

```text
classify -> route plan -> bounded independent draft/analysis calls
         -> structured critique/checks -> final synthesis -> telemetry -> memory
```

Controls:

- `mode`: `normal | heavy`
- `max_provider_calls`
- `max_output_tokens`
- `deadline_seconds`
- `require_approval_for_code`
- `fallback_policy`: `next_provider | stop | ask_user`

Heavy Mode must degrade cleanly to Normal when only one provider is available or the budget cannot support multiple passes. It must never expose hidden chain-of-thought or claim “flawless” output.

**Gate:** mocked tests verify call counts, fallback behavior, budget refusal, and no-key behavior.

### Slice 3 — memory, 200-message window, and artifacts

Use a local JSON or SQLite implementation first; do not require Supabase to run the app.

Records:

- `Project(id, name, branch, created_at)`
- `Session(id, project_id, mode, created_at)`
- `Message(id, session_id, role, content, provider, token_count, created_at)`
- `Summary(id, session_id, covers_message_ids, content, created_at)`
- `Artifact(id, project_id, title, kind, content_hash, content, source_message_id, version, created_at)`
- `ExecutionEvent(id, session_id, event_type, redacted_payload, created_at)`

Policy:

- Keep the latest 200 messages active by message count and token budget, whichever is stricter.
- Summarize before eviction; never delete raw history automatically.
- Artifacts are immutable versions with explicit user labels and export.
- Raw repository content is not uploaded to a connector unless the user enables that project policy.

**Gate:** restart/recovery tests, 201-message rollover tests, artifact immutability tests, and redaction tests pass.

### Slice 4 — telemetry-led routing

Metrics per provider/model/task:

- attempted/succeeded/failed calls;
- HTTP status and typed error;
- input/output/total tokens;
- latency and timeout;
- quota header snapshots;
- patch parse success, AST success, test success;
- user-selected quality signal when available.

Start with an explainable score:

```text
utility = task_fit
        + quality_signal
        - latency_penalty
        - failure_penalty
        - quota_pressure
        - estimated_cost_penalty
```

The score must be bounded and persisted as aggregate telemetry only. A later experimental stochastic scorer can be toggled on and compared against the deterministic baseline.

**Gate:** routing decisions include a reason and candidates considered; replay tests produce the same result for the same telemetry snapshot.

### Slice 5 — repository work and Task Finder

Repository pipeline:

1. inventory and consented file selection;
2. typed plan;
3. sandbox creation;
4. bounded patch application;
5. AST/type/lint/test verification;
6. repair loop with maximum rounds;
7. diff/report;
8. explicit human apply/export/commit action.

Task Finder:

- represent tasks as a DAG, not unconstrained agents;
- cap concurrency and total calls;
- isolate subtask context;
- require structured result schema;
- aggregate results with conflict detection;
- support cancel/retry/resume from persisted events.

**Gate:** a failing test cannot be reported as green; sandbox cleanup occurs on success and failure; no push happens automatically.

### Slice 6 — optional connectors

Recommended order:

1. **Local store:** default, zero external dependency.
2. **Supabase:** first cloud connector if cross-device authentication, artifact storage, or durable project data is required. It offers Postgres, storage, and auth in one service, but free projects can pause after inactivity and have bounded quotas.
3. **Upstash Redis:** optional active cache/queue only. Do not make it the source of truth; free tier limits are finite and resets/availability must be observable.
4. **Vector search:** only after retrieval tests prove a need. Start with Postgres/pgvector or a local index before adding Pinecone/Turbopuffer/etc.
5. **Neon/MongoDB/D1/PlanetScale/DynamoDB/BigQuery:** do not implement in parallel. Each needs a concrete workload, schema, retention policy, and failure behavior.
6. **“Toro”:** do not implement until the exact product/API is identified.

Every connector implements:

```text
health() -> ConnectorHealth
put(record) -> ConnectorResult
get(key) -> ConnectorResult
list(scope) -> ConnectorResult
delete/version policy
close()
```

Connector writes are asynchronous/best-effort where possible and must never erase the local source of truth.

### Slice 7 — GitHub and delivery

- Use a scoped GitHub App/OAuth integration, not raw SSH-key collection.
- Store only short-lived/session-scoped credentials through the host platform’s secret mechanism.
- Default permissions: read repository metadata and contents; write/push/PR only after explicit enablement.
- Show changed files, tests, and diff before any commit/PR operation.
- Preserve existing Freebuff-managed Git behavior; the app should not bypass the workspace’s credential boundary.

**Gate:** permission matrix tests and a mocked GitHub client pass; local sandbox remains usable without GitHub.

## Research-backed provider policy

The provider registry must treat published quotas as moving configuration. Current official documentation indicates:

- Gemini quotas are per project, vary by model and usage tier, and include RPM, input TPM, and RPD. Exact active limits are visible in AI Studio and are not guaranteed.
- Groq measures RPM, RPD, TPM/TPD, and in some organizations separate input/output token limits; current exact values are account/model-specific and response headers expose remaining/reset data.
- Cerebras uses model/organization-level request limits and separate uncached/total token buckets; its current documentation describes a credit-bounded Free Trial rather than a permanently renewing free tier.
- OpenRouter free variants have platform request caps and can also fail due to upstream provider capacity; the key endpoint exposes remaining credits/usage and responses expose rate-limit hints.
- Hugging Face Inference Providers includes small monthly credits for free users; routed usage and custom provider keys have different billing semantics, so it must not be labeled “free-only” without an explicit policy.
- Mistral exposes usage, organization/workspace spending limits, and API limits through its admin panel; exact limits are account-specific.

Therefore the app should ship with conservative defaults, visible configuration, response-header updates, and a hard `free_only` billing guard. It must not promise a fixed free quota in product copy.

## Verification strategy

- Unit tests: pure classifier, scorer, quota, redaction, memory window, artifact versioning, patch parser.
- Contract tests: mocked adapters for success, malformed response, timeout, 429, 402, 5xx, retry-after, and partial stream.
- Integration tests: local end-to-end Normal/Heavy/Repo flows with fake provider and temp worktree.
- Property tests where useful: path traversal, budget limits, message-window bounds.
- Manual smoke checks: Streamlit launch, no-key state, mocked provider state, Heavy Mode status, diff review.
- Research checks: fixed seeds, configuration output, null controls, confidence intervals, no physical claims.

## Non-goals for the first recovery milestone

- Ten simultaneous databases.
- Automatic background GitHub commits/pushes.
- Encrypting user provider keys into the application database.
- Exposing model chain-of-thought.
- Treating pink-noise weighting as validated optimization or physical evidence.
- Claiming repository changes are “flawless” or automatically preserve legal ownership without reviewing permissions and terms.
