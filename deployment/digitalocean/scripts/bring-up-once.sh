#!/usr/bin/env bash
# One-shot bring-up for ChapChap Saleor on this droplet.
# Run with an interactive TTY so sudo can ask for your password:
#
#   ssh -t vaflaly@162.243.162.104 'bash ~/chapchap/saleor/deployment/digitalocean/scripts/bring-up-once.sh'
#
set -euo pipefail

APP_ROOT="${APP_ROOT:-$HOME/chapchap/saleor}"
DEPLOY_DIR="${APP_ROOT}/deployment/digitalocean"
DOMAIN="server.chapchapticket.com"
NGINX_SRC="${DEPLOY_DIR}/nginx/server.chapchapticket.com.conf"
NGINX_AVAIL="/etc/nginx/sites-available/${DOMAIN}.conf"
NGINX_ENABLED="/etc/nginx/sites-enabled/${DOMAIN}.conf"

echo "==> 1/5 Bootstrap (Docker + certbot packages)"
bash "${DEPLOY_DIR}/scripts/bootstrap-server.sh"

# Docker group may have just been added — use sg so we don't need re-login
run_docker() {
  if docker info >/dev/null 2>&1; then
    "$@"
  elif sg docker -c true >/dev/null 2>&1; then
    sg docker -c "$*"
  else
    sudo "$@"
  fi
}

echo "==> 2/5 Deploy Saleor stack (build + migrate + start)"
cd "${DEPLOY_DIR}"
if docker info >/dev/null 2>&1; then
  bash "${DEPLOY_DIR}/scripts/deploy.sh"
elif sg docker -c true >/dev/null 2>&1; then
  sg docker -c "bash ${DEPLOY_DIR}/scripts/deploy.sh"
else
  echo "Docker not usable yet. Try: newgrp docker  then re-run this script." >&2
  exit 1
fi

echo "==> 3/5 Install Nginx vhost for ${DOMAIN}"
sudo cp "${NGINX_SRC}" "${NGINX_AVAIL}"
sudo ln -sf "${NGINX_AVAIL}" "${NGINX_ENABLED}"
sudo nginx -t
sudo systemctl reload nginx

echo "==> 4/5 Issue / renew TLS with Certbot"
sudo certbot --nginx -d "${DOMAIN}" --non-interactive --agree-tos \
  --register-unsafely-without-email --redirect || \
sudo certbot --nginx -d "${DOMAIN}" --redirect

echo "==> 5/5 Flip Saleor PUBLIC_URL to https"
cd "${DEPLOY_DIR}"
if grep -q '^PUBLIC_URL=' .env; then
  sed -i.bak 's|^PUBLIC_URL=.*|PUBLIC_URL=https://server.chapchapticket.com/|' .env
  sed -i.bak 's|^ENABLE_SSL=.*|ENABLE_SSL=True|' .env
  rm -f .env.bak
fi

if docker info >/dev/null 2>&1; then
  docker compose up -d api worker beat
elif sg docker -c true >/dev/null 2>&1; then
  sg docker -c "cd ${DEPLOY_DIR} && docker compose up -d api worker beat"
fi

echo
echo "==== Smoke tests ===="
curl -sS -o /dev/null -w "local health: %{http_code}\n" http://127.0.0.1:8000/health/ || true
curl -sS -o /dev/null -w "https health: %{http_code}\n" "https://${DOMAIN}/health/" || true
curl -sS -o /dev/null -w "https graphql: %{http_code}\n" \
  -X POST "https://${DOMAIN}/graphql/" \
  -H 'Content-Type: application/json' \
  -d '{"query":"{ shop { name } }"}' || true

echo
echo "Done. GraphQL: https://${DOMAIN}/graphql/"
echo "Staff app: EXPO_PUBLIC_SALEOR_API_URL=https://${DOMAIN}/graphql/"
