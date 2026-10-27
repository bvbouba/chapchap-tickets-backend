#!/usr/bin/env bash
# Redeploy ChapChap Saleor to the DigitalOcean droplet.
#
# Syncs local saleor code via rsync, then builds/migrates/restarts
# api + worker + beat on the server.
#
# Usage (from Mac, inside the saleor repo root OR this scripts folder):
#   ./deployment/digitalocean/scripts/redeploy.sh
#   REMOTE=vaflaly@162.243.162.104 ./deployment/digitalocean/scripts/redeploy.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REMOTE="${REMOTE:-vaflaly@162.243.162.104}"

echo "==> Sync code to ${REMOTE}"
bash "${SCRIPT_DIR}/prepare-and-sync.sh"

echo "==> Build + migrate + restart on server"
ssh "${REMOTE}" 'bash ~/chapchap/saleor/deployment/digitalocean/scripts/deploy.sh'

echo "==> Health check"
curl -sS -o /dev/null -w "health HTTP %{http_code}\n" \
  https://server.chapchapticket.com/health/ || true

echo "Done."
