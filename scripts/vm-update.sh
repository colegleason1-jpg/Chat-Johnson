#!/usr/bin/env bash
# Deploy the current branch: pull, rebuild, restart, and make sure the local model is present.
set -euo pipefail
cd "$(dirname "$0")/.."
# Until this user's next login the docker group is not in effect, so reach the daemon through sudo instead.
DOCKER=(docker)
if ! docker info >/dev/null 2>&1 </dev/null; then DOCKER=(sudo docker); fi
value_of() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- | sed 's/[[:space:]]*#.*$//' | tr -d '[:space:]'; }
# The default Caddyfile gates every request on basic auth, and Caddy refuses to start with an empty hash.
# Say so here rather than leaving the proxy in a restart loop with nothing serving the app.
if [ "$(value_of CADDYFILE)" != "Caddyfile.open" ] && [ -z "$(value_of CADDY_HASH)" ]; then
  echo "CADDY_HASH is empty in .env, so the proxy cannot start. Run: bash scripts/vm-bootstrap.sh <repo url> <branch>" >&2
  echo "(it fills only the values that are still missing), or set CADDYFILE=Caddyfile.open for tunnel-only use." >&2
  exit 1
fi
git pull --ff-only
"${DOCKER[@]}" compose up -d --build
"${DOCKER[@]}" compose exec -T ollama ollama pull "llama3.1:8b" || echo "ollama pull failed; the router falls back to cloud endpoints"
"${DOCKER[@]}" compose ps
echo "Health: $("${DOCKER[@]}" compose exec -T app python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8501/?health=1', timeout=10).read()[:200].decode())" 2>/dev/null || echo 'not answering yet')"
DOMAIN_VALUE="$(grep -E '^DOMAIN=' .env | cut -d= -f2- || true)"
if [ -n "${DOMAIN_VALUE}" ]; then echo "Open https://${DOMAIN_VALUE}/ (basic auth)"; else echo "No domain: ssh -L 8080:127.0.0.1:8080 <vm> then open http://127.0.0.1:8080/"; fi
