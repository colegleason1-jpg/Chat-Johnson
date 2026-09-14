---
name: deploy-kit
description: How to answer deployment questions with the Deploy Kit rather than promising pipelines the app cannot run.
keywords: deploy deployment pipeline docker kubernetes helm terraform serverless ci cd github actions rollback runbook vm compose
---
- The Deploy Kit (Repository Work → Deploy Kit) generates CI/CD, container, Helm, Terraform, serverless, observability, rollback, runbook, and self-hosted VM files from templates and validates them offline. It never builds, pushes, or applies anything.
- Point the operator at the target that fits: streamlit-cloud (CI + smoke + runbook), docker-kubernetes (full pipeline), serverless (SAM, Cloud Run, Container Apps), oracle-vm (compose stack with the 24/7 worker and a local model).
- Files can be downloaded as a zip, locked as artifacts, or pushed as a branch plus pull request through the session-only GitHub slot when it is armed.
- When asked "can you deploy this", say exactly which files the kit produces and that the target repository's CI runs them once the listed secrets exist.
