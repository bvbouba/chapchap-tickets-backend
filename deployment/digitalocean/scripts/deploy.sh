#!/usr/bin/env bash
# Deploy / update Saleor on the droplet.
# Run from the droplet (or via SSH) inside deployment/digitalocean.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${DEPLOY_DIR}"

if [[ ! -f .env ]]; then
  echo "Missing ${DEPLOY_DIR}/.env — copy .env.example and fill values." >&2
  exit 1
fi

for f in secrets/rsa_private.pem secrets/rsa_public.pem; do
  if [[ ! -f "$f" ]]; then
    echo "Missing ${DEPLOY_DIR}/${f}" >&2
    exit 1
  fi
done

mkdir -p data/media data/static backups
chmod 700 secrets
chmod 600 secrets/*.pem .env || true

echo "==> Building image"
docker compose build api

echo "==> Starting db + redis"
docker compose up -d db redis
sleep 3

echo "==> Running migrations"
docker compose run --rm --no-deps api python manage.py migrate --noinput

echo "==> Starting api + worker + beat"
docker compose up -d api worker beat

echo "==> Syncing static files for Nginx"
mkdir -p "${DEPLOY_DIR}/data/static"
docker compose run --rm --no-deps --entrypoint "" \
  -v "${DEPLOY_DIR}/data/static:/out" \
  api sh -c 'cp -a /app/static/. /out/'

echo "==> Status"
docker compose ps
echo
echo "Health (local): curl -sS http://127.0.0.1:8000/health/"
curl -sS -o /dev/null -w "HTTP %{http_code}\n" http://127.0.0.1:8000/health/ || true
echo "Done."
