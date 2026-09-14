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
[ -f .env ] || cp .env.example .env
echo "Edit .env with your keys, then run: bash scripts/vm-update.sh"
