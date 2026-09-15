#!/usr/bin/env bash
# First-time setup on a fresh Ubuntu or Oracle Linux VM (ARM64 or x86_64). Run as a sudo-capable user.
set -euo pipefail
REPO_URL="${1:?usage: vm-bootstrap.sh <git repo url> [branch]}"
BRANCH="${2:-main}"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker "$USER"
fi
# Oracle images ship iptables rules that drop 80/443; open them (the VCN security list must allow them too).
if command -v iptables >/dev/null 2>&1; then
  sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT || true
  sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT || true
  sudo netfilter-persistent save 2>/dev/null || sudo sh -c 'iptables-save > /etc/iptables/rules.v4' 2>/dev/null || true
fi
if command -v firewall-cmd >/dev/null 2>&1; then
  sudo firewall-cmd --permanent --add-service=http --add-service=https && sudo firewall-cmd --reload || true
fi
if [ ! -d "chat-johnson" ]; then
  git clone --branch "$BRANCH" "$REPO_URL" "chat-johnson"
fi
cd "chat-johnson"
if [ ! -f .env ]; then
  cp .env.example .env
  # The job key lets the app hand a session's GitHub token or paid key to the worker encrypted (Fernet: 32 url-safe base64 bytes).
  JOB_KEY="$(openssl rand -base64 32 | tr '+/' '-_')"
  sed -i "s|^CHAT_JOHNSON_JOB_KEY=.*|CHAT_JOHNSON_JOB_KEY=${JOB_KEY}|" .env
  read -r -p "Domain for automatic TLS (blank = listen on 127.0.0.1:8080 only; reach it through an SSH tunnel): " DOMAIN
  if [ -n "${DOMAIN}" ]; then
    sed -i "s|^DOMAIN=.*|DOMAIN=${DOMAIN}|; s|^CADDY_BIND=.*|CADDY_BIND=0.0.0.0:80|; s|^CADDY_BIND_TLS=.*|CADDY_BIND_TLS=0.0.0.0:443|" .env
  fi
  read -r -p "Basic auth user [operator]: " CADDY_USER
  CADDY_USER="${CADDY_USER:-operator}"
  read -r -s -p "Basic auth password: " CADDY_PASS
  echo
  HASH="$(docker run --rm caddy:2 caddy hash-password --plaintext "${CADDY_PASS}")"
  HASH_ESCAPED="${HASH//\$/\$\$}"   # compose interpolation turns $$ back into $ before Caddy sees it
  sed -i "s|^CADDY_USER=.*|CADDY_USER=${CADDY_USER}|; s|^CADDY_HASH=.*|CADDY_HASH=${HASH_ESCAPED}|" .env
  chmod 600 .env
fi
echo "Add your provider keys to .env (they reach the worker container only), then run: bash scripts/vm-update.sh"
