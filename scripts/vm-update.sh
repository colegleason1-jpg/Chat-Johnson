#!/usr/bin/env bash
# Deploy the current branch: pull, rebuild, restart, and make sure the local model is present.
set -euo pipefail
cd "$(dirname "$0")/.."
git pull --ff-only
docker compose up -d --build
docker compose exec -T ollama ollama pull "llama3.1:8b" || echo "ollama pull failed; the router falls back to cloud endpoints"
docker compose ps
echo "Health: $(docker compose exec -T app python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8501/?health=1', timeout=10).read()[:200].decode())" 2>/dev/null || echo 'not answering yet')"
DOMAIN_VALUE="$(grep -E '^DOMAIN=' .env | cut -d= -f2- || true)"
if [ -n "${DOMAIN_VALUE}" ]; then echo "Open https://${DOMAIN_VALUE}/ (basic auth)"; else echo "No domain: ssh -L 8080:127.0.0.1:8080 <vm> then open http://127.0.0.1:8080/"; fi
