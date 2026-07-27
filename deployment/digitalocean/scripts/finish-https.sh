#!/usr/bin/env bash
# Finish Nginx vhost + Let's Encrypt for server.chapchapticket.com
# Saleor API must already be healthy on 127.0.0.1:8000.
#
#   ssh -t vaflaly@162.243.162.104 'bash ~/chapchap/saleor/deployment/digitalocean/scripts/finish-https.sh'
set -euo pipefail

DEPLOY_DIR="${DEPLOY_DIR:-$HOME/chapchap/saleor/deployment/digitalocean}"
DOMAIN="server.chapchapticket.com"
NGINX_SRC="${DEPLOY_DIR}/nginx/server.chapchapticket.com.conf"
NGINX_AVAIL="/etc/nginx/sites-available/${DOMAIN}.conf"
NGINX_ENABLED="/etc/nginx/sites-enabled/${DOMAIN}.conf"

echo "==> Checking local Saleor"
curl -fsS "http://127.0.0.1:8000/health/" >/dev/null
echo "API healthy"

echo "==> Installing Nginx vhost"
sudo cp "${NGINX_SRC}" "${NGINX_AVAIL}"
sudo ln -sf "${NGINX_AVAIL}" "${NGINX_ENABLED}"
sudo nginx -t
sudo systemctl reload nginx

echo "==> Certbot TLS"
sudo certbot --nginx -d "${DOMAIN}" --non-interactive --agree-tos \
  --register-unsafely-without-email --redirect || \
  sudo certbot --nginx -d "${DOMAIN}" --redirect

echo "==> Point Saleor PUBLIC_URL at https"
cd "${DEPLOY_DIR}"
sed -i.bak 's|^PUBLIC_URL=.*|PUBLIC_URL=https://server.chapchapticket.com/|' .env
sed -i.bak 's|^ENABLE_SSL=.*|ENABLE_SSL=True|' .env
rm -f .env.bak
docker compose up -d api worker beat

sleep 5
echo "==> Smoke"
curl -sS -o /dev/null -w "https health: %{http_code}\n" "https://${DOMAIN}/health/" || true
curl -sS -X POST "https://${DOMAIN}/graphql/" \
  -H 'Content-Type: application/json' \
  -d '{"query":"{ shop { name } }"}' || true
echo
echo "Done → https://${DOMAIN}/graphql/"
