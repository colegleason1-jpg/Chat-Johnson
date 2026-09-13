# Runbook · Chat Johnson on Streamlit Community Cloud

## Deploy
1. Merge the pull request into `main`. Streamlit Community Cloud redeploys from `main` automatically.
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

## Data
- The SQLite vault lives on the container's disk. On Streamlit Community Cloud that disk is
  **ephemeral**: a reboot or redeploy starts with an empty vault. Download chats you care about
  (More → ⬇ Chat) before rebooting. Self-hosted deployments should point `CHAT_JOHNSON_DB_PATH`
  at persistent storage.
- Keys are never stored; every visitor pastes their own for the session.

## Repository sandboxes
- Fetched GitHub trees and pipeline sandboxes live under the temp directory (`chatjohnson-repos`,
  `chat_johnson_worktrees`) and are ephemeral. Running a fetched repository's tests executes its
  code in the app's container; the box is off by default for GitHub sources.

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
