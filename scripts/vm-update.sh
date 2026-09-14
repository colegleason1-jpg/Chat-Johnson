#!/usr/bin/env bash
# Deploy the current branch: pull, rebuild, restart, and make sure the local model is present.
set -euo pipefail
cd "$(dirname "$0")/.."
git pull --ff-only
docker compose up -d --build
docker compose exec -T ollama ollama pull "llama3.1:8b" || echo "ollama pull failed; the router falls back to cloud endpoints"
docker compose ps
echo "Health: $(curl -fsS http://127.0.0.1/?health=1 2>/dev/null | head -c 200 || echo 'not answering yet')"
