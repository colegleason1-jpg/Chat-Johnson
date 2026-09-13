# Deploy automation: plan for the features Chat Johnson claimed it had

Two live chats (13 Sep 2026, "what about post deployment" and "how much of this can Chat
Johnson automate") described capabilities the app did not have. The operator asked for every
one of them to be planned, introduced, and integrated. Decisions: GitHub writes are a
**session-only opt-in** (toggle plus a repo-scoped token pasted each session, never stored,
push only on a button press); the automation lives **inside Repository Work**; the
**foundation ships first**.

## Feature map (from the two transcripts)

| Claimed capability | Reality today | Plan |
|---|---|---|
| Knows what it can automate | Guessed from its name; no self-knowledge in the prompt | Phase 0: capability card in every prompt (`orchestrator/capabilities.py`) |
| Health-check endpoint, version endpoint, smoke tests, golden-signal gates, canary/blue-green | None for the app itself; Streamlit Cloud deploys main directly | Phase 2: `?health=1` JSON view (build marker, vault check, keyed vendors), smoke drive in `scripts/`, post-deploy CI job against `DEPLOY_URL`, release runbook with rollback = revert on main |
| Dockerfile, `.dockerignore`, lint/test scripts, security-scan config, artefact publishing steps | Not generated | Phase 1: Deploy Kit generator (deterministic templates + optional model refinement), offline validation, locked artifacts, zip download |
| Terraform / Pulumi / Bicep modules, Helm chart, serverless templates, observability values, rollback scripts, runbooks | Not generated; cannot be run from the app (no credentials, no binaries) | Phase 1 generates and validates syntax offline; CI runs them once the operator adds secrets; the app never applies infrastructure itself |
| "CI runs the artefacts automatically" | The app has no CI; GitHub Actions in the target repo does | Phase 3: session-only push of the kit as a branch plus an opened PR; the target repo's Actions run it |
| Rollback automation | Not implemented | Phase 3: "Open revert PR" for the last kit push; Phase 2 runbook covers the app's own rollback |

## Phases

- **Phase 0 · Foundation (shipped)**: capability card in the system prompt; live window passed
  as real user/assistant turns (digest, summaries, notes in the system prompt); vendor finish
  reason surfaced ("stopped at the output budget", "filtered") in chat and mission summaries.
- **Phase 1 · Deploy Kit generator (shipped)** (Repository Work): target form (Streamlit Cloud,
  Docker + Kubernetes, serverless), language, registry, cloud; deterministic templates for
  Dockerfile, `.dockerignore`, GitHub Actions ci-cd.yml (lint, test, build, scan, push,
  deploy hooks), Helm chart skeleton, Terraform skeleton, serverless.yml, OpenTelemetry and
  Prometheus values, rollback script, runbook, post-deploy smoke test; optional Heavy Mode
  refinement per file; offline validation (YAML, JSON, shell `-n`, Dockerfile rules); every
  file locked as an artifact; "Download kit (.zip)". Zero provider calls unless refinement is on.
- **Phase 2 · Post-deploy checks for this app (shipped)**: health view, smoke drive, post-deploy CI job,
  runbook (`docs/RUNBOOK.md`: deploy, verify, rollback, key rotation).
- **Phase 3 · GitHub push, session-only (shipped)**: toggle + token, branch + PR via the GitHub REST API
  with `requests`, revert PR; boundary text in README, sidebar, and the capability card updated
  the day it ships.
- **Phase 4 · Observability for the app**: routing telemetry persisted to the vault, an Ops
  panel (error rate, latency per endpoint), transcript and telemetry export.

Out of scope on the free tier: running `terraform apply`, `helm install`, or `docker build`
from the app. Those run in the target repository's CI after the operator adds secrets.
