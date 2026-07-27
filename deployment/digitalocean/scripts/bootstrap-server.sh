#!/usr/bin/env bash
# Run ONCE on a fresh-ish Ubuntu droplet (as root or with sudo).
# Does not remove or reconfigure your existing Django site — only installs deps
# and prepares ~/chapchap/saleor for the Saleor stack.
set -euo pipefail

APP_ROOT="${APP_ROOT:-$HOME/chapchap/saleor}"
DEPLOY_DIR="${APP_ROOT}/deployment/digitalocean"

if [[ "$(id -u)" -eq 0 ]]; then
  SUDO=""
else
  SUDO="sudo"
fi

echo "==> Installing Docker + Nginx + Certbot helpers (sudo)"
export DEBIAN_FRONTEND=noninteractive
$SUDO apt-get update -y
$SUDO apt-get install -y ca-certificates curl gnupg nginx certbot python3-certbot-nginx

if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | $SUDO sh
  $SUDO systemctl enable --now docker
fi

# Allow this user to run docker without root
if ! groups | tr ' ' '\n' | grep -qx docker; then
  $SUDO usermod -aG docker "$USER"
  echo "==> Added $USER to docker group. Log out/in (or: newgrp docker) before deploy.sh"
fi

# Docker Compose plugin
if ! docker compose version >/dev/null 2>&1; then
  $SUDO apt-get install -y docker-compose-plugin || true
fi

mkdir -p "${DEPLOY_DIR}/data/media" "${DEPLOY_DIR}/data/static" "${DEPLOY_DIR}/secrets" "${DEPLOY_DIR}/backups"
chmod 700 "${DEPLOY_DIR}/secrets"

echo "==> App root: ${APP_ROOT}"
echo "Next:"
echo "  1. Ensure code is synced to ${APP_ROOT}"
echo "  2. newgrp docker   # if just added to docker group"
echo "  3. Run: ${DEPLOY_DIR}/scripts/deploy.sh"
echo "  4. Install Nginx vhost + certbot (see README)"
