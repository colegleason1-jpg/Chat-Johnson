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
| `app.py` | Streamlit studio: sidebar control deck, six workspaces, preview canvas (Preview only, or Run the page with automatic fixes), a Clear button per text field, SQLite vault, Artifact Lock |
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
| `orchestrator/missions.py` | Mission templates, node model (`normalise_plan`, `parse_mission_block`, `mission_block`) |
| `orchestrator/mission_runner.py` | The mission job: nodes in order, connector and sub-mission executors, outputs, failure policies |
| `orchestrator/connectors_nodes.py` | Connector registry for nodes and the offline validator (`validate_nodes`) |
| `orchestrator/mcp_client.py` | JSON-RPC 2.0 stdio MCP client (`mcp_servers.yaml`, `CHAT_JOHNSON_MCP_SERVERS`); servers start from a minimal environment |
| `orchestrator/jobsecrets.py` | Encrypted per-job hand-off of session secrets between the app and the worker (`CHAT_JOHNSON_JOB_KEY`) |
| `orchestrator/envsafe.py` | Minimal environment for child processes; the self-hosted switch |
| `orchestrator/memory.py` | Task memory persisted to disk (one file per project scope) |
| `orchestrator/pinkwave.py` | Controlled chaos: the 1/f signal walked per use at three frequency profiles; bounded nudges for routing, Heavy Mode, recall, digests |
| `orchestrator/proctor.py` | Monte Carlo proctor: routing fragility over pink-wave realizations, bursty budget forecast, the tick's deferral rule |
| `orchestrator/learner.py` | Cortex 2 as a learner: measured speed, Bayesian quality priors from verdicts, pink-wave exploration, per-scope settings |
| `orchestrator/vaultsync.py` | Vault snapshots to Supabase Storage: restore on an empty start, upload on change; the VM's offsite backup |
| `orchestrator/dynamics.py` | Pairwise statistics between endpoint latency series (built-in or pyspi) and the bounded constraint-law modulation |
| `orchestrator/treasury_plan.py` | Plan of the day: the daily token treasury allocated by the supply-chain resilience engine (`scrcae`, optional): most value within today's tokens, goal checked in target mode with the shortfall named, saturation budget, correlated tail risk, vendor stress fitted from the route log; fixed shares without it |
| `orchestrator/sandbox_preview.py` | Run-mode preview contract: the sealed-frame CSP, the one-line talk-back shim that makes silent sandbox failures loud, document assembly that keeps every page line number, report normalization, the bounded fix decision and the fix prompt |
| `frontend/sandbox_preview/` | Declared Streamlit component hosting the sealed frame (opaque origin, no network, vendored Three.js as a data: URL); it forwards the page's report and never relays its messages |
| `orchestrator/society/` | Two companies on Traction/EOS (cycles, EOS scorecard, release waves, manuscripts) and the agent society (academy, tick, leisure) |
| `research/project_seth_phase3.py` | Project Seth Phase 3 distribution and bias-sweep engine (research only) |
| `docs/` | Bible, recovery audit, implementation plan, strategic outline |

## Operational environments

A workspace switch across the top of the main panel and one chat bar pinned to the bottom of the
screen. The bar always sends to the selected workspace, so the input is never out of reach while
reading. Each workspace owns its own chats (selector, New chat, Clear chat, Delete chat, and a More
menu with Rename, context load, and Migrate now); keys are never touched by any of these.

- **Task Finder**: a mission is classified (research, code, analysis, plan, general) with no provider
  call and expanded into typed workstreams you can edit before launch. Launch queues the mission as a
  **background job**: steps run one at a time through the router on a worker thread, every result
  lands in the workspace's chat as it finishes, and the chat bar and the other workspaces stay usable.
  A jobs strip above the workspace shows progress, a Cancel button, and an answer box when a job
  asks the operator a question.
- **Company workspace (agent society, batch S1)**: two companies run on Traction/EOS from templates,
  AVS Studio (17-project IP catalog, six-work first wave, editorial/research/production/art seats)
  and AVS Software (one product manager per product, docs, support, marketing, sales, release, QA).
  Every seat has 3–5 roles and KPIs; founding agents fill the active seats until the academy
  graduates replacements. A **company cycle** is a background job: scorecard over a rolling seven-day
  window (one row per seat, week, and KPI) → Level 10 meeting (minutes locked as an artifact; issues
  and to-dos recorded; ROCK and DONE lines update rocks and close to-dos; timeline milestones complete
  from catalog stages) → the CEO rates the backlog →
  the Executive Assistant delegates by role and load → analytics breaks large items down → seats
  produce deliverables in their own threads → the managing editor (or QA reviewer) passes or
  returns them with a deterministic check alongside → a report lands in the Board inbox. The chat
  bar in the Company workspace talks to the Executive Assistant to the Board, whose BACKLOG lines
  become work items. Cycles are bounded by calls and by a treasury share of today's real token
  budget, and can chain at the company interval while the app is awake.
- **Academy workspace (batch S2)**: the society on Plato's Republic. "Seed the society" creates agents up
  to a target (100 by default): Producers on a basic allowance, Auxiliaries (guardians and teachers)
  on a higher one, Philosophers ready to graduate. An **academy cycle** pays allowances, gives the
  least-tried Producers a foundational task, has Auxiliaries grade and audit each one with the
  deterministic check alongside, promotes Producers after three graded passes, examines one Auxiliary
  whose gradings agreed with the checker three times (a three-rubric evaluation graded by a
  Philosopher), and lets graduates fill open seats in both companies by seat importance. The company
  cycle's **personnel routine** counts missed scorecard weeks per seat, asks the CEO to APPROVE or HOLD
  a release after two, returns released agents to the society with a cooldown, adds a seat where a
  department's load exceeds twice its seats, retires worker seats idle for four cycles, and hires
  graduates into every open seat. Every event is in the personnel log.
- **Leisure, dream bank, and the society tick (batch S3)**: off the clock, an agent with enough
  balance chooses an inquiry on a free knowledge source (Wikipedia, Project Gutenberg, arXiv, Open
  Library, Hacker News, or an operator-declared API), the fetch is free, the notes call is charged
  to its balance, and the notes land in its **dream bank**; the agent names its next interest and
  how long it sleeps. The next time it works, the keyword-ranked excerpt of its own research rides in
  its persona. The **society tick** is one chained job (every 30 minutes by default) that wakes due
  agents for leisure and queues company and academy cycles on their intervals; it stops with the
  app process and runs around the clock on the VM worker; a cycle that fails (a provider outage)
  is recorded as failed and the tick doubles its wait for every consecutive failure. The **release
  loop**: a work's **manuscript** is assembled from every finished item (story bible, chapters, the
  final edit last); the studio's six wave-1 works go to the board as one **release wave** once all
  are final, and the board approves it (every manuscript published, marketing and sales opened,
  positioning, landing copy, launch plan, outreach, and pricing queued) or returns it with feedback
  that reopens a final edit carrying the manuscript. Feedback themes are extracted for marketing.
  **Skip-level escalations** are one line, `ESCALATE: up|down :: message`, routed one level above the
  superior or below a subordinate and answered in the next cycle. The **treasury** is the persisted
  daily counters minus caps; each company's Settings slider is its share and the academy and leisure
  split the rest; allowances go to free academy agents out of the academy's own budget; a registered
  local model is a separate pool for producer-tier seats and academy tasks. Open seats prefer an
  **exploring agent whose interest matches the roles**, woken into exploitation; failed academy tasks
  come back with the grader's teaching note; the Head of Production and Art Director carry reachable
  KPIs, the reviewer seat is never fired, and a CEO HOLD buys a seat a full week.
- **Self-hosted VM (batch F, hardened in G)**: the Deploy Kit's `oracle-vm` target (also committed at
  the repo root: `Dockerfile`, `docker-compose.yml`, `Caddyfile`, `Caddyfile.open`, `scripts/vm-*.sh`)
  runs the app, a 24/7 worker container (`python -m orchestrator.jobs --worker`) that serves every
  background job and restores the society tick after a restart, an Ollama local model registered as
  the `local` Cortex endpoint (`CHAT_JOHNSON_LOCAL_ENDPOINT`) that takes the academy's cheap labour
  first, and Caddy. Provider keys in the VM's `.env` reach the **worker container only**; the app
  container never loads them. With `DOMAIN` set, Caddy serves automatic TLS with basic auth
  (`CADDY_USER`/`CADDY_HASH`); without one the stack listens on `127.0.0.1:8080` for an SSH tunnel
  and never on plain HTTP to the internet. A session's GitHub token or paid key reaches the worker as
  a Fernet blob keyed by `CHAT_JOHNSON_JOB_KEY` (generated by `scripts/vm-bootstrap.sh`), deleted on
  claim. Workers stamp only the job rows their threads hold, a restarted container fails the rows its
  predecessor left running, and the vault sits on a persistent volume.
- **Chat surface (batch C)**: code blocks are held whole while an answer streams (prose stays live, no
  half-open fences flicker), currency dollars are escaped so KaTeX math (`$…$`, `$$…$$`) renders only
  where meant, the More menu has a **navigator** (search this chat, jump to any turn, show the window
  around it, back to latest), and **skills** in `skills/*.md` (front matter with keywords) are loaded
  into the prompt only when their keywords appear in the message; the answer caption names the skills
  applied. Shipped: deploy-kit, repository-patching, mission-writing, company-reporting, spatial-layout,
  mission-nodes.
- **Spatial layout and web QA (batch D)**: a spatial mission (room, floor plan, arrangement…) asks the
  model for one fenced `scene` JSON block (room, objects with size, mass, anchor), then a
  deterministic numpy solver resolves it (floor snap, wall clamp, pairwise push-out along the
  least-penetration axis with the lighter box moving more, everything clamped inside the room) and
  a self-contained canvas preview shows the result (drag to rotate, wheel to zoom; no external
  script, same strict CSP as the sanitized canvas). The scene is locked as a JSON artifact. Mission
  steps can now carry an `executor` (`solver`, `webqa`) that runs without a model call. Web QA:
  `webcheck` missions and Repository Work → Deploy Kit → "Check a deployed URL" run an HTTP check
  (status, latency, expected text, health JSON) and a browser check where Chromium exists (the VM
  worker), reported as unavailable elsewhere.
- **Run-mode preview and automatic fixes (batch T)**: the Live preview canvas has two modes. Preview only
  (buttons off) strips every script. Run the page executes the generated page inside a sealed frame: an
  opaque-origin iframe with scripts on and nothing else (no network requests, no storage, no dialogs,
  no navigation, nothing reaches the app; WebRTC, which browsers offer no policy for, is shadowed by
  the shim as a best effort), with Three.js available as `import 'three'` from a vendored copy. The
  shim is the first thing the page parses and reports script errors with their real line numbers,
  blocked resources and the sandbox's otherwise silent failures (a form submit, an alert, a download)
  back to the app, one report per run. In Heavy Mode a page that errors, renders blank, never loads,
  tries to navigate away, or depends on an external script or stylesheet is sent back to the model
  with that report for up to two automatic fix rounds per page (a round counts only once the fix
  lands; the same error twice stops the loop; a real send always goes first), which is the
  "Self-Correcting Execution Sandbox" for chat output. Blocked images or fonts alone are noted, not
  fixed. Run mode adds PREVIEW
  RULES to every send so the model writes pages that run there (inline everything, simulate data,
  buttons instead of forms, pointer-event drag, no dialogs). The canvas gains Render, Download
  preview (.html) and Clear preview buttons, and a `data:text/html` link in an answer opens as its
  markup. Every text field outside a form has its own Clear button bound to that one field. The
  persona now answers the newest message first and then continues an unfinished earlier request, and
  the Heavy critique checks for both.
- **Mission nodes, connectors, MCP, Heavy streaming (batch E)**: every Task Finder step is a node
  with an `executor` (`model`, `solver`, `webqa`, `connector`, `sub_mission`), a `config`, `inputs`
  (earlier steps pasted in verbatim), an `output` target (`chat`, `artifact` locked under
  `missions/node-<chat>-<n>.md`, or `both`) and an `on_failure` policy (`stop` default, `skip`,
  `retry_once`). Connector nodes run built-in actions without a model call: `deploy_kit.generate`,
  `github.fetch`, `github.push` and `github.revert` (session token only; Launch is blocked while the
  push slot is disarmed), `repository.run`, `vault.export_thread`, `vault.save_artifact`,
  `webqa.check`, and `mcp.call` against servers declared in `mcp_servers.yaml` (a hand-rolled
  JSON-RPC 2.0 stdio client in `orchestrator/mcp_client.py`; servers run inside the VM worker). A
  `sub_mission` node runs a nested plan in the same chat with `[Sub n.m]` titles and returns its
  deliverable. The plan panel has an executor column and a Configure expander per step, validates
  offline before Launch (unknown connector, missing arguments, disarmed push, bad inputs), stores the
  node graph (`mission_nodes`) so a chat can **Re-run this mission**, and **Refine in chat** posts the
  nodes into Normal Chat as a fenced ```` ```mission ```` block. Any answer that ends with such a
  block shows **Send to Task Finder**, which prefills the panel; nothing runs until Launch. Heavy
  Mode's synthesis pass now streams like a Normal Chat answer (draft and review still block).
- **Private scope per visitor**: chats, artifacts, jobs, and the routing log are keyed by a scope id
  minted for each browser session and kept on the URL (`?scope=`); bookmark it to come back. Nothing
  is shared between visitors of the same deployment.
- **Repository Work**: the hub connector (list, pick, connect, run, push through the session-only token), the
  sandboxed patch pipeline, and a discussion thread that sees the connected repository.
- **Chat Bot**: long-form developer chat with file uploads injected as context.
- **Normal Chat**: single-pass chat.

## Implemented vs roadmap

| Bible feature | Status |
|---|---|
| BYOK key panel, session-scoped, Test keys for all 7 providers | Implemented |
| Tri-Processor Cortex (1/f probe, MILP selection, streaming) with self-healing model ids | Implemented (routing signal only) |
| Heavy Mode (draft → review → synthesis) with optional per-session paid review slot | Implemented |
| SQLite vault: per-workspace threads, 200-message windows, texturize-then-archive, artifacts | Implemented |
| Long chats summarised into a new chat (the summary is background only, never a rule) | Implemented |
| Repository sandbox pipeline with AST + pytest repair loop | Implemented (local) |
| GitHub identity | The session-only push token's login, shown in the sidebar; no OAuth app |
| 10-cloud connector fabric | Roadmap · local SQLite is the only store; stubs listed in the sidebar |
| Background Git-Streamer | Roadmap · not implemented |
| Live SDK Document Scraper | Roadmap · not implemented |
| Self-Correcting Execution Sandbox for chat output | Implemented for chat pages · Run the page preview with up to 2 automatic Heavy Mode fix rounds; the capability card still says partial because the repository pipeline keeps its own pytest repair loop |
| Cross-thread semantic search | Implemented as long-distance memory: FTS5/BM25 over the project's other chats (summaries, digests, missions, artifact summaries) with recency decay and superseding, in every prompt and every vision digest; no embedding index |
| Controlled chaos: the validated 1/f signal applied across routing, Heavy Mode, memory recall, and migration | Implemented · bounded nudges only, per-project gain and frequency profiles, gain 0 is deterministic |
| Cortex 2 as an empirical learner: measured speed, Bayesian quality from verdicts, pink-wave exploration, constraint-law dynamics | Implemented · bounded terms, hard limits untouched, exploration rate verified in the outcome log |
| Supply-chain planning of the token treasury: activities as nodes, vendors as capped resources, a verified MILP with diagnosis and tail risk; feasibility verdicts for release waves and missions | Implemented (Batches Q and R, engine on Python 3.12) · the tick runs each activity at the planned size; per-request routing and pacing unchanged |
| Society: per-project product briefs, per-company release waves, every seat's title/roles/KPIs and every persona editable | Implemented |
| Capability card in every prompt (what the app can and cannot do) and memory as real chat turns | Implemented |
| Repository Work hub connector: fetch a GitHub repository through the API (public, or private with the armed token) into a temporary sandbox, run the pipeline, download the patch, push the change as a branch plus pull request; four tabs (Work, Deploy Kit, GitHub, Directions) | Implemented · tests of the fetched repository run only when ticked |
| Deploy Kit (Repository Work): CI/CD workflow, Dockerfile, Helm chart, Terraform skeleton, serverless template, observability, rollback script, runbook; offline validation; zip download; lock as artifacts | Implemented · generation only, nothing is pushed or applied |
| Post-deploy checks for this app: `?health=1` JSON view, `scripts/smoke_drive.py`, `post-deploy-smoke` workflow, `docs/RUNBOOK.md` | Implemented · set the `DEPLOY_URL` repository variable to arm the workflow |
| Mission nodes: executors, connectors, sub-missions, offline validation, stored node graph, chat handoff | Implemented (batch E) |
| Session-only GitHub push: token and repo armed per session, one commit on a new branch plus an opened pull request, revert PR for any push from the session | Implemented · never the default branch |
| App observability: every send persisted to `route_log` with the runner-up endpoint, the pink-wave state, and the operator's verdict (thumbs, locked artifact); Ops view, chaos on vs off comparison, CSV export | Implemented |

Controls: **Heavy Mode** toggle (multi-pass, more tokens, longer wait), **Artifact Lock** beside every
code block (versioned save to SQLite), output token budget slider, per-project scope.

## Threads and long chats

Every workspace holds any number of **chats** (threads). The row at the top of each workspace lets you
switch, start a new one, **Clear chat** (messages move to the archive and leave the context; keys are
untouched), or **Delete chat** (the chat, its archive, and its summaries are removed after a
confirmation; locked artifacts stay). Rename, Summarise into a new chat now, and **Download this chat** (Markdown or JSON,
with the archive, summaries, the inherited digest, and the route facts behind every answer) live under More. Commit a download to a `transcripts/`
folder in the repository to hand a full conversation to the assistant for an audit without pasting it.

Every send is traced in the **Routing log** expander at the bottom of the page (workspace, task type, provider/model,
latency, solver reason) and stored with its task type on the message, so routing can be judged
against the project's vision over a long session.
Each thread has its own 200-message window and its own texturized summaries.

Before every send, a zero-quota **health sweep** measures the active thread: message count, estimated
tokens in the live window, stacked summaries, repeated prompts, and error loops. When a threshold
trips (or you press *Summarise into a new chat now*), the app:

1. compresses the whole thread, archive included, into a **vision digest**: how it started, decisions
   and constraints, key facts (files, numbers), open items, locked artifacts, and the summaries;
2. asks the cheapest available free model to refine that digest when a key is present (the
   deterministic base is kept underneath for audit);
3. locks the digest as an immutable artifact, opens a successor thread that injects the digest into
   every prompt, and marks the old thread *migrated*. Nothing raw is deleted.

The result is a fresh thread that carries the original vision in a re-optimized, token-light form.
Switch **Summarise long chats automatically** off in the sidebar to keep it manual.

**Long-distance memory.** Every prompt also recalls lines from the project's *other* chats: their
texturized summaries, vision digests, pinned missions, this chat's own older summaries, and artifact
summaries, ranked by keyword overlap with the request and bounded to a share of the context budget
(a tenth, up to a quarter when the controlled-chaos gain lifts it). A vision digest carries the same
recall into the successor thread. Recall never crosses a project scope, and it costs no provider quota.
Under the hood it is an SQLite FTS5 index (`recall_index`, BM25 ranking, identifiers kept whole) fed
by every summary, mission, digest, and artifact summary as it is written; a line loses half its weight
every 30 days, and lines from a migrated chat or an older digest are halved again (superseded). Without
FTS5 the same recall falls back to keyword overlap.

**Outcome log.** Every send's `route_log` row also keeps the solver's runner-up endpoint and the
pink-wave state (gain, profile, jitter), and is linked to the answer. Thumbs under an answer and a
locked artifact attach a verdict to that decision. The routing expander's *Chaos on vs off* table
compares the two settings on failures, truncations, latency, verdicts, and runner-up disagreements,
so the math is judged on results.

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
- **Controlled chaos** (`orchestrator/pinkwave.py`): the same 1/f generator, walked one step per
  use at a chosen frequency profile (white α 0.5, pink α 1.0, brown α 1.5), adds a jitter of at most
  0.15 to the entropy penalty (0.045 utility), so near-ties between endpoints break differently over
  time while the capacity rows stay untouched; Heavy Mode gets a temperature schedule (warmer draft,
  cold critique, base synthesis); the long-distance recall share and the digest's cross-chat section
  grow with the wave. Gain and profiles are per project (sidebar → Controlled chaos) and shared with
  the worker; gain 0 is fully deterministic. A bounded signal, never an optimization claim.
- **Cortex learner** (`orchestrator/learner.py`, `orchestrator/dynamics.py`): Cortex 2's hand-tuned
  speed constant is replaced by the recorded p50 latency per endpoint (blended toward the table while
  observations are few), and a Beta prior per endpoint and task type, updated by thumbs and locked
  artifacts, shifts utility by at most ±0.15. Constraint-law analogies (dissipation, friction,
  momentum, coupling) add a bounded ±0.10 from load, telemetry, and the pairwise statistics of the
  endpoints' latency series (Pearson, lagged cross-correlation, transfer entropy; `pyspi` when
  installed, built-in estimators otherwise). Exploration is the pink wave's job: at gain g a share
  `explore_max × g` of sends goes to the feasible runner-up whose quality is least certain, inside a
  regret bound and never for context-heavy requests; the outcome log records every exploration so
  the observed rate is checked against the configured one. Every capacity row, key, timeout, and
  daily cap is evaluated before any of this and is never moved by it.
- **Monte Carlo proctor** (`orchestrator/proctor.py`): statistics a single run cannot give, never a
  decision by itself. *Routing fragility* re-runs Cortex 2 over 2,048 pink-wave realizations at 1×, 2×, 4×,
  and 8× chaos (the selector's own capacity rows, evaluated once, then an exact argmax per path) and
  reports each endpoint's win rate, the decision entropy in bits, the deterministic winner's
  fragility, and the outliers that only win under amplification; a cached per-minute fragility rides every send into the
  outcome log. *Budget forecast* simulates the rest of the UTC day as 1/f-bursty demand around today's
  rate per keyed vendor (probability of capping, median and early-tail cap hour); the society tick
  defers its cycles when every keyed vendor is out of headroom or likely to cap within the hour. Both
  are CPU only, on demand under the routing expander. `docs/MONTE_CARLO_PROCTOR.md` ranks the rest.
- **Legacy providers** (NVIDIA NIM, OpenRouter, Cerebras, Mistral) are a fallback only: they serve a
  request when no Cortex key (Gemini, Groq, Hugging Face) is set or when every Cortex endpoint fails
  it. Their retries, sibling-model attempts, and rediscovery calls are metered like everything else.
- **Prompt context sizing**: the project-memory block is sized so the request fits every keyed
  endpoint's TPM ceiling at the current output budget (4 chars per token, 500-token reserve, never
  below 8k or above 24k characters), so a long thread does not silently lock out the fastest
  endpoint. The live window fills newest-first and before memory, so a follow-up always sees the
  latest results whole; an inherited digest is capped at an eighth and labelled background, never a rule.
- **Finished answers (batch U1)**: every message remembers the vendor's finish reason. An answer cut
  inside its code is continued by the app (verbatim from its tail, stitched on overlap, up to three
  rounds) before it is stored or shown; a still-cut answer carries a plain note into the next prompt
  so the model never mistakes a cut page for a design. A page or app request is raised automatically
  to the largest answer a keyed provider can write (up to 16,384; sidebar checkbox, default on), routes
  only to endpoints that can write it, gets the whole budget for the Heavy draft, a critique told a cut
  is a cut, and a synthesis that must not shorten. A fragment or deliberation-only answer is asked for
  once more. The model receives an APP STATE block (canvas mode, answer limit, whether the last answer
  was cut, the last sandbox report) and CANVAS RULES (no CDN, no network) in both preview modes, and
  the page it is asked to change travels whole on the request turn. Only the operator's own sentences
  can become digest decisions; recall needs whole real words and is off for page requests; a chat
  migrates far later and never counts automatic turns as load. The canvas keeps one whole page per
  scope (the last complete page fence wins over snippets and stubs), survives a reload with its mode,
  and an incomplete page is continued, never sent to a repair round.
- **Free-tier pacing in Task Finder**: before each workstream the ledger is consulted; if every keyed
  vendor is inside its RPM/TPM window the step waits (up to 65 s) instead of failing. Missions are
  pinned to the chat, so they survive window eviction and thread migration.
- **Job runner**: `orchestrator/jobs.py` claims rows from the vault's `jobs` table on daemon worker
  threads (`CHAT_JOHNSON_JOB_WORKERS`, default 2; 0 disables) and runs registered handlers
  (missions, company and academy cycles, the society tick). Session keys reach the job encrypted
  (`CHAT_JOHNSON_JOB_KEY`) or through process memory that ages out after an hour, and are bound in a
  fresh context per job; the table never holds them. A restart fails every unfinished job with a
  note, because its keys are gone. The runner also sweeps stale repository sandboxes on start.
- **Quota ledger**: one bucket per vendor credential (keyed by `vendor:sha256(key)`), shared by every
  ledger that uses that credential, so adding a second vendor's key never resets the first vendor's
  counters and visitors with different keys never throttle each other. Every HTTP attempt (retries,
  rediscovery, probes) counts toward RPM; tokens are charged on the raw text a stream produced, hidden
  reasoning included, and charged even when the stream failed part-way. **Daily caps** (`DAILY_CAPS`
  in `orchestrator/config.py`, override with `CHAT_JOHNSON_DAILY_<VENDOR>`) are rows in the MILP
  selection like RPM and TPM: a capped vendor is not selected, and when every keyed vendor is capped
  the send fails with one sentence naming the reset. Daily counts persist in the vault
  (`quota_usage`, UTC days) so the app and the worker count one day together and a restart keeps it.
- **Chat pacing and plain errors**: chat sends wait for a free-tier window like missions do, and every
  failure is explained in one plain sentence (key rejected, window full and when it resets, model
  retired, server error) with the raw detail folded away.
- **Keys made easy**: the API keys panel links each vendor's key page with three steps, states where a
  key goes (only to its vendor, session memory only), and offers per-vendor model overrides for the session.
- **Heavy Mode payload**: the review and synthesis passes receive the operator's request and the draft,
  not the whole system prompt and history, so a Heavy send costs about a third of what it did.
- **Repository safety**: on a shared deployment a repository's own tests are never executed and the
  "Local path" source does not exist; both appear only with `CHAT_JOHNSON_SELF_HOSTED=1`, and local
  paths must sit under `CHAT_JOHNSON_REPO_ROOTS`. Secret files (`.env`, `secrets.toml`, private keys)
  never enter the prompt. A model may not write under `.git/` or `.orchestrator/`, git runs with hooks
  and fsmonitor disabled, and a worktree sandbox's branch and registration are removed with it. A red
  repair loop leaves the sandbox uncommitted; fenced blocks that look like excerpts ("… rest unchanged",
  one function out of many) are refused instead of overwriting a file. Web QA checks public http(s)
  hosts only and re-checks every redirect hop.

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
- **Quota ledger counts RPM, TPM, and tokens per day.** Cortex 2 only selects endpoints with headroom;
  429s fall through the ranked list.
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
- **The vault survives redeploys** when the app secrets carry `CHAT_JOHNSON_SUPABASE_URL` and
  `CHAT_JOHNSON_SUPABASE_KEY` (a service key for a private Storage bucket, default
  `chat-johnson-vault`): a fresh container restores the last gzip snapshot before its first query,
  and a daemon uploads a consistent SQLite backup every ten minutes when the vault changed, plus
  *Snapshot now* and a confirmed *Restore from bucket* in the sidebar. Chats, artifacts, the recall
  index, the outcome log, and the learner's priors carry over. Set `CHAT_JOHNSON_DEMO=1` to show the
  banner that says this copy is the front door, not the 24/7 studio.
- **No keepalive ping**: a `curl` of `?health=1` only fetches Streamlit's shell page and never runs the
  script, so the old five-minute keepalive did nothing and was removed. The `post-deploy-smoke` workflow
  drives the live app with a real browser every six hours instead (that does run the script), when the
  `DEPLOY_URL` repository variable is set; schedules only run from the default branch.
- **The operator is told the truth (batch U2)**: a page with scripts switches the canvas to Run the page on
  its own; a status line under the canvas says the mode, the page size against the answer limit, repairs used
  and the last run; a cut answer gets a box with *Continue the answer*, *Raise the limit and continue* and
  *Ask for a smaller page*; a failed send is a row in the thread; every answer has *Why this answer came out
  like this* (endpoint, reason, runner-up, latency, how it ended); a restart and keys from an earlier session
  are announced; browser errors are said plainly ("the page's code stops mid-way"); `?health=1` reports the
  process start, workers, jobs, ledger and snapshot age.
- **Routing and quota that see reality (batch U3)**: the ledger reserves a request's estimated tokens while it
  is in flight and settles them with the vendor's own count from the last stream chunk (hidden reasoning
  included); a 429 with Retry-After or a rate-limit reset header blocks that vendor for exactly that long, so
  the send moves to another keyed vendor at once and a long wait is never slept through under the lock;
  Gemini's requests per day are counted (`CHAT_JOHNSON_RPD_GEMINI` overrides 200); the route row records the
  model that actually served and the pass latency net of window pauses and lock waits, so learned speed is the
  endpoint's; a cut answer, a failed send and a page that threw in the sandbox move the endpoint's quality
  prior like a thumbs-down would; momentum follows the sending workspace's last endpoint, not another's;
  background cycles run in normal mode and wait five minutes after a chat send (ten after a page request); the
  chat waits one full window (65 s) for a background job to release the provider.
- **Missions that fail out loud and see the page (batch U4)**: a failed step posts a system row in the mission
  chat (and one more when the mission stops there); a full free-tier window is a pause with one retry, not a failed
  step; a mission that refers to the canvas carries the page with it whole and every model step sees it under the
  canvas rules; a `preview` mission kind ("fix the buttons on this preview") runs `preview.validate` (static
  check: structure, closed fences and tags, script balance, external resources the canvas would block) and
  `preview.repair` (a bounded regenerate loop against that check); the page the mission produced comes back as an
  artifact with *Put this page on the canvas*; Task Finder says plainly that missions cannot run the browser and
  offers *Send to Chat Bot instead*. In the chats, a page over 6,000 characters is edited in **patch mode**: the
  model returns SEARCH/REPLACE blocks, the app applies them in Python (exact match, then whitespace-tolerant),
  reports any block that matched nothing, and puts the merged page on the canvas; say "rewrite the page" for a
  fresh one.
- **The controls stay on screen (batch U5)**: the chat bar pins the page to its bottom, so anything above a long
  conversation scrolled off the top and could not be tapped at all on a tablet. Each chat's own controls (chat picker,
  New chat, Clear chat, Delete, More) now render directly above the chat bar, and the preview canvas opens itself only
  for a page produced in this session, never for one restored on load: a *Show the page on the preview canvas* button
  opens it. Together that takes about a thousand pixels off the page and keeps every control in view.

## Development

```
ruff check app.py cli.py orchestrator research tests
pytest -q
```

CI runs the same lint, compile, and test steps on every push and pull request.

## License

Proprietary. Copyright (c) 2026 Cole Gleason, all rights reserved. Using, running, copying, modifying,
distributing, or training on this repository or any part of it requires the copyright holder's prior
written permission; no license is granted by publishing it. See [LICENSE](LICENSE) for the terms and
how to ask. Third-party packages keep their own licenses.
