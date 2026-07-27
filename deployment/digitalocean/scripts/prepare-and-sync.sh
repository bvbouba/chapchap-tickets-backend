#!/usr/bin/env bash
# Prepare production secrets + .env on your laptop, then rsync to the droplet.
#
# Prerequisites:
#   - SSH access: ssh vaflaly@162.243.162.104
#   - DNS A record: server.chapchapticket.com → 162.243.162.104
#   - Prefer resizing the droplet to 4 GB before go-live
#
# Usage:
#   ./scripts/prepare-and-sync.sh
#   REMOTE=vaflaly@162.243.162.104 ./scripts/prepare-and-sync.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SALEOR_ROOT="$(cd "${DEPLOY_DIR}/../.." && pwd)"
REMOTE="${REMOTE:-vaflaly@162.243.162.104}"
# Home path — no root needed for rsync (sudo only for Docker/Nginx install).
REMOTE_APP="${REMOTE_APP:-chapchap/saleor}"

mkdir -p "${DEPLOY_DIR}/secrets" "${DEPLOY_DIR}/backups"

# RSA keys: prefer saleor.key / saleor.pub, else copy from local .env is manual
if [[ ! -f "${DEPLOY_DIR}/secrets/rsa_private.pem" ]]; then
  if [[ -f "${SALEOR_ROOT}/saleor.key" ]]; then
    cp "${SALEOR_ROOT}/saleor.key" "${DEPLOY_DIR}/secrets/rsa_private.pem"
  elif [[ -f "${SALEOR_ROOT}/.jwt_key.pem" ]]; then
    cp "${SALEOR_ROOT}/.jwt_key.pem" "${DEPLOY_DIR}/secrets/rsa_private.pem"
  else
    echo "Place RSA private key at ${DEPLOY_DIR}/secrets/rsa_private.pem" >&2
    exit 1
  fi
fi
if [[ ! -f "${DEPLOY_DIR}/secrets/rsa_public.pem" ]]; then
  if [[ -f "${SALEOR_ROOT}/saleor.pub" ]]; then
    cp "${SALEOR_ROOT}/saleor.pub" "${DEPLOY_DIR}/secrets/rsa_public.pem"
  else
    openssl rsa -in "${DEPLOY_DIR}/secrets/rsa_private.pem" -pubout -out "${DEPLOY_DIR}/secrets/rsa_public.pem"
  fi
fi
chmod 600 "${DEPLOY_DIR}/secrets/"*.pem

if [[ ! -f "${DEPLOY_DIR}/.env" ]]; then
  cp "${DEPLOY_DIR}/.env.example" "${DEPLOY_DIR}/.env"
  # Pull reusable secrets from local .env (non-PEM)
  if [[ -f "${SALEOR_ROOT}/.env" ]]; then
    for key in SECRET_KEY TWILIO_ACCOUNT_SID TWILIO_AUTH_TOKEN TWILIO_FROM_NUMBER VERIFY_SERVICE_SID; do
      val="$(grep -E "^${key}=" "${SALEOR_ROOT}/.env" | head -1 | cut -d= -f2- || true)"
      if [[ -n "${val}" ]]; then
        if grep -qE "^${key}=" "${DEPLOY_DIR}/.env"; then
          # portable-ish in-place replace
          awk -v k="${key}" -v v="${val}" 'BEGIN{FS=OFS="="} $1==k{$0=k"="v} {print}' \
            "${DEPLOY_DIR}/.env" > "${DEPLOY_DIR}/.env.tmp" && mv "${DEPLOY_DIR}/.env.tmp" "${DEPLOY_DIR}/.env"
        fi
      fi
    done
  fi
  # Strong DB password if still placeholder
  if grep -q 'change-me-strong-db-password' "${DEPLOY_DIR}/.env"; then
    DBPASS="$(openssl rand -base64 24 | tr -d '/+=' | head -c 32)"
    sed -i.bak "s/change-me-strong-db-password/${DBPASS}/g" "${DEPLOY_DIR}/.env"
    rm -f "${DEPLOY_DIR}/.env.bak"
  fi
  if grep -q 'change-me-to-a-long-random-string' "${DEPLOY_DIR}/.env"; then
    SK="$(openssl rand -base64 48 | tr -d '/+=' | head -c 64)"
    sed -i.bak "s/change-me-to-a-long-random-string/${SK}/g" "${DEPLOY_DIR}/.env"
    rm -f "${DEPLOY_DIR}/.env.bak"
  fi
  echo "Created ${DEPLOY_DIR}/.env — review POSTGRES_PASSWORD / EMAIL_URL before production."
fi

echo "==> Rsync saleor repo → ${REMOTE}:${REMOTE_APP}"
echo "    (excludes .venv, media dump optional)"
rsync -az --delete \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude 'node_modules' \
  --exclude '__pycache__' \
  --exclude '.pytest_cache' \
  --exclude 'deployment/digitalocean/data/media' \
  --exclude 'deployment/digitalocean/data/static' \
  --exclude 'deployment/digitalocean/backups' \
  "${SALEOR_ROOT}/" "${REMOTE}:${REMOTE_APP}/"

echo "==> Sync media (ticket images)"
rsync -az "${SALEOR_ROOT}/media/" "${REMOTE}:${REMOTE_APP}/deployment/digitalocean/data/media/" || true

echo "Synced. On the server run:"
echo "  ssh -t ${REMOTE}"
echo "  bash ~/chapchap/saleor/deployment/digitalocean/scripts/bootstrap-server.sh   # first time (sudo)"
echo "  newgrp docker   # if just added to docker group"
echo "  bash ~/chapchap/saleor/deployment/digitalocean/scripts/deploy.sh"
echo "  # then Nginx + certbot (see README)"
