# Runbook · Chat Johnson on Streamlit Community Cloud

## Deploy
1. Push to the branch the Streamlit Community Cloud app is configured to deploy (today the working
   branch itself). Every push redeploys and restarts the container, which empties the vault: download
   any chat you want to keep before a push lands.
2. If the app still shows the old build marker after a few minutes, open *Manage app* and reboot.
3. Set `CHAT_JOHNSON_BUILD` in the app's secrets/environment to the deployed commit when you want
   the build marker verified; without it the marker reads `unknown` on platforms that strip `.git`.

## Verify (after every deploy)
1. Open `<app url>/?health=1`: a JSON block with `status`, `build`, `vault`, `keyed_vendors`.
2. Run the smoke drive locally or from Actions (*post-deploy-smoke*, needs the `DEPLOY_URL`
   repository variable): health view, build match, workspace switch, pinned chat bar.
3. Paste one key, send one message, and read the *Routing log* expander: the send must show a
   provider/model, a latency, and the solver's reason.

## Roll back
1. `git revert <bad merge commit>` on `main` and push; the platform redeploys the previous state.
2. Reboot from *Manage app* if the old build lingers. Verify as above.

## Self-hosted VM (Oracle Cloud Always Free or any Docker host)
- `docker-compose.yml` runs four services: `app` (Streamlit, no job workers, **no `.env`**), `worker`
  (`python -m orchestrator.jobs --worker`, Chromium included, loads `.env`, serves every job 24/7 and
  restores the society tick after a restart), `ollama` (the local model for Producer tasks and
  leisure notes), and `caddy`. The vault lives on the `data` volume at `/data/vault.db` and survives deploys.
- Edge: with `DOMAIN` in `.env`, Caddy binds 80/443 (`CADDY_BIND=0.0.0.0:80`, `CADDY_BIND_TLS=0.0.0.0:443`),
  serves automatic TLS, and requires basic auth (`CADDY_USER`, `CADDY_HASH` from `caddy hash-password`,
  every `$` doubled for compose). Without a domain the stack binds `127.0.0.1:8080` only: reach it with
  `ssh -L 8080:127.0.0.1:8080 <vm>`. `Caddyfile.open` (set `CADDYFILE=Caddyfile.open`) drops the auth
  for tunnel-only use; plain HTTP on the internet is never generated.
- First time: create the instance (Ubuntu or Oracle Linux, ARM64 is fine), allow 80/443 in the VCN
  security list only if you will set a domain, then `bash scripts/vm-bootstrap.sh <repo url> <branch>`:
  it writes `.env` with a generated `CHAT_JOHNSON_JOB_KEY`, asks for the domain and the basic-auth
  password, and sets `.env` to mode 600. Add the provider keys to `.env`; they reach the worker only.
- Session secrets: a GitHub token or paid key pasted in the browser reaches the worker as a Fernet blob
  in `job_secrets` (encrypted with `CHAT_JOHNSON_JOB_KEY`, deleted on claim); without the key the UI
  refuses nodes that need a token. Local paths in Repository Work must sit under `CHAT_JOHNSON_REPO_ROOTS`.
- Every deploy: `bash scripts/vm-update.sh` (pull, rebuild, restart, pull the local model). Roll
  back with `git checkout <previous>` and `docker compose up -d --build`. Back up with
  `bash scripts/vm-backup.sh` (keeps 14 copies under `backups/`).
- Heartbeats: a worker stamps only the rows its threads are executing, every 15 s, and fails rows
  silent for 3 minutes; on start it fails the rows an earlier process of the same container left
  running; the app never reaps rows because the worker owns them. A reap re-queues society ticks.

## MCP servers (VM worker only)
- Declare servers in `mcp_servers.yaml` (name, command, args, env; an env value `env:NAME` is read
  from the worker's environment at call time, so tokens stay in `.env`). A server starts from a
  minimal environment (`PATH`, `HOME`, `LANG`, `TMPDIR`, `PYTHONPATH`) plus exactly the `env:` entries
  it declares; the worker's provider keys never reach it. Its stderr tail is kept for error messages.
  `CHAT_JOHNSON_MCP_SERVERS` points at another file. The sidebar lists the servers; **List tools** probes one over stdio and is
  enabled only where `CHAT_JOHNSON_SELF_HOSTED=1`.
- A mission node with `executor: connector` and `config: {connector: mcp.call, server, tool,
  arguments}` starts the server for that call, runs `initialize` → `tools/call`, and records the
  text content in the chat. Errors are redacted before they are stored; a server that does not
  answer within 30 s fails the node under its failure policy.
- Mission nodes that push to GitHub take the session push token with the job (`github_token`,
  encrypted per job on the VM, process memory on a single-process host, never a plain row); Launch
  stays disabled while the slot is disarmed or while no worker can receive the token.

## Data
- The SQLite vault lives on the container's disk. On Streamlit Community Cloud that disk is
  **ephemeral**: a reboot or redeploy starts with an empty vault. Download chats you care about
  (More → ⬇ Chat) before rebooting. Self-hosted deployments should point `CHAT_JOHNSON_DB_PATH`
  at persistent storage.
- Keys are never stored; every visitor pastes their own for the session.
- Every browser session gets a private scope (`?scope=visitor-…` on the URL). A visitor who loses the
  URL loses the way back to those chats; the data itself stays in the vault until the next redeploy.
- Background jobs (Task Finder missions) live in the vault's `jobs` table and run on worker threads
  inside the app process (`CHAT_JOHNSON_JOB_WORKERS`, default 2). A reboot fails every unfinished
  job with a "restarted" note, since its session keys died with the process; launch it again.

## Repository sandboxes
- Fetched GitHub trees and pipeline sandboxes live under the temp directory (`chatjohnson-repos`,
  `chat_johnson_worktrees`). Trees older than six hours and sandboxes older than two are removed
  when a pipeline run or the job runner starts; a finished run keeps its sandbox until then so the
  push flow can read the changed files. Running a fetched repository's tests executes its code in
  the worker container on the VM (the box exists only when `CHAT_JOHNSON_SELF_HOSTED=1`; a shared
  deployment never executes a repository's code) and starts from a minimal environment without the
  worker's keys; the box is off by default for GitHub sources.

## Company cycles
- A cycle is a `company_cycle` job. Its budget is the smaller of the cycle's hard cap and the
  company's treasury share of what the keyed vendors can still serve today; it stops cleanly
  (`status = budget`) when either runs out and reports what it did; a treasury that cannot afford one
  call ends the cycle before any request. "Keep cycling" queues the next cycle with `run_after`; the
  chain lives only while the process and its session keys survive (the VM worker with env keys makes
  it 24/7). Pause cancels the queued successor. The tick backs off a failing cycle: each consecutive
  failure doubles the wait, up to sixteen intervals.

- Release waves are per company: *Works per release wave* (Settings) sets how many works of the
  current wave must be `final` before the wave goes to the board as one release (Company → Backlog &
  catalog → *Send wave N to the board for review*); the current wave is the lowest wave with an
  unpublished work, and each work's wave is editable in the catalog. Each work's manuscript is
  assembled from its finished items (story bible, chapters, final edit last) under
  `company/<key>/works/`; Approve publishes the wave, opens marketing and sales, and moves the next
  wave from the backlog into development (studio works get their starter items); Return records the
  feedback and opens a final-edit item per work that carries the manuscript. A work that is final
  outside the current wave can still be published on its own.
- Product briefs are per project: Settings → *Products and briefs* edits a work's title, logline, and
  brief for this scope only, and every open item of that work is rewritten with the new header.
- Roles: the Org chart is an editor. Seat titles, roles, KPIs, and importance save from the table,
  each seated agent's persona has its own editor, and *Add a seat* creates an open seat that the next
  free graduate fills. Changes reach a seat's next brief; nothing running is interrupted.

## Academy cycles and personnel
- `academy_cycle` jobs are bounded to 24 calls and the academy's treasury share (the remainder the
  companies' sliders leave, split 5:3 with leisure). Between 4 and 8 producers work per cycle as the
  budget allows; allowances are paid to free agents out of the same budget (never more than a fifth
  of it). Promotions are capped per cycle and every grade is paired with the deterministic check, so
  a model cannot promote by flattery alone; a failed task returns to the producer with the grader's
  note; a failed Philosopher exam sits out three days while other candidates go first. Firing needs
  two missed weeks (rolling seven-day KPIs) and the CEO's APPROVE; a HOLD resets the count to one
  week; the reviewer seat and the executive seats are never fired; released agents keep their
  history and sit out a week before graduation can seat them again. A cycle that raises is recorded
  as `failed` with the reason and its running items go back to `assigned`.

## Society tick and leisure
- Start the tick in the Academy workspace; it queues cycles on their intervals and runs a few
  leisure inquiries per tick. Leisure fetches go to public APIs (Wikipedia, gutendex, arXiv, Open
  Library, Hacker News); a blocked source is logged as an error on the inquiry and costs the agent
  its leisure fee only. Stop the tick before a redeploy if you want a clean cycle log.

## Controlled chaos (pink-wave signal)
- Sidebar → *Controlled chaos*: gain 0–100 % and a frequency profile per feature (routing, heavy,
  memory, migration). Settings are stored per project scope in the vault (`settings` table) and the
  worker reads them for background jobs; the step counters (`counters` table) advance once per use.
- What it moves, and the bounds: routing entropy penalty +≤ 0.15 × gain (near-ties only; RPM, TPM,
  daily caps, and keys are constraints and never move); Heavy draft temperature + ≤ 0.4 × gain, the
  critique at 0.0; long-distance recall 10 % of the context budget + ≤ 15 % × gain; the digest's
  cross-chat section 600 + ≤ 600 × gain characters. Gain 0 restores deterministic behaviour.
- Diagnosis: the routing log's reason string carries `chaos=<profile> jitter=<value>` and
  `runner_up=<endpoint>`, Heavy sends carry `temperatures=draft/critique/synthesis`.
- Judging it: give answers a thumbs up or down and lock the artifacts you keep; the routing expander's
  *Chaos on vs off* table (last 7 days) compares good rate, down rate, failures, truncations, latency,
  and runner-up disagreements between sends made at gain > 0 and at gain 0. Run a week at each
  setting before changing the bounds; `docs/MONTE_CARLO_PROCTOR.md` lists the next candidates.

## Long-distance memory index
- `recall_index` is an FTS5 table rebuilt automatically once for an older vault; `vault.rebuild_recall_index(scope)`
  rebuilds it by hand after a restore. Lines decay with a 30-day half-life; migration marks the old
  chat's summaries superseded, and a new digest supersedes the previous digest of the same chat.

## Quota caps
- Per-vendor daily token ceilings default to the `DAILY_CAPS` table; set `CHAT_JOHNSON_DAILY_<VENDOR>`
  (for example `CHAT_JOHNSON_DAILY_GEMINI=2000000`, `0` = uncapped) on the host to change them.
  The sidebar's BYOK channels show this minute's and today's usage and when a full window resets.

## Key rotation
1. Create the new key at the vendor.
2. Paste it in the sidebar (Apply keys) or update the environment secret for self-hosted runs.
3. Revoke the old key at the vendor. Keys shown in screenshots or logs count as exposed.

## Incident quick checks
- `Generation failed: HTTP 429` on every send: free-tier window exhausted; wait a minute or add a
  second vendor key. Task Finder waits up to 65 s on its own.
- `retired model` messages: discovery will replace the id on the next call; if it persists set
  `CORTEX_GEMINI_MODEL` / `CORTEX_GROQ_MODEL` explicitly.
- Blank streamed answers: the vendor filtered or truncated; the chat shows a notice with the finish reason.
