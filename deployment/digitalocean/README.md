# ChapChap Saleor on DigitalOcean

Target: **`https://server.chapchapticket.com/graphql/`** on droplet `ubuntu-01` (`162.243.162.104`).

SSH user: **`vaflaly`** (not root). App lives in **`~/chapchap/saleor`**.

This folder is a ready-to-run production stack: Postgres + Redis + Saleor API + Celery worker + Celery beat, with host Nginx for TLS.

## Before you start

1. **Resize the droplet to 4 GB RAM** (strongly recommended). The box is currently ~2 GB with swap already in use.
2. **DNS**: create an `A` record  
   `server.chapchapticket.com` → `162.243.162.104`  
   Wait until it resolves (`dig +short server.chapchapticket.com`).
3. **SSH**:
   ```bash
   ssh vaflaly@162.243.162.104
   ```
4. Docker install + Nginx vhost need **sudo** once (password prompt on the server).

## One-shot path

On your Mac, from the `saleor` repo:

```bash
chmod +x deployment/digitalocean/scripts/*.sh

# 1) Build local .env + RSA secrets, rsync code + media to the droplet
./deployment/digitalocean/scripts/prepare-and-sync.sh

# 2) Optional: dump local DB and copy it
./deployment/digitalocean/scripts/export-local-db.sh
scp deployment/digitalocean/backups/saleor-*.sql.gz \
  vaflaly@162.243.162.104:chapchap/saleor/deployment/digitalocean/backups/
```

On the droplet (will ask for your sudo password):

```bash
ssh -t vaflaly@162.243.162.104

bash ~/chapchap/saleor/deployment/digitalocean/scripts/bootstrap-server.sh
bash ~/chapchap/saleor/deployment/digitalocean/scripts/deploy.sh

# If you uploaded a DB dump:
# bash ~/chapchap/saleor/deployment/digitalocean/scripts/import-db.sh \
#   ~/chapchap/saleor/deployment/digitalocean/backups/saleor-XXXX.sql.gz
# bash ~/chapchap/saleor/deployment/digitalocean/scripts/deploy.sh

# Nginx vhost (keeps existing Django sites untouched)
sudo cp ~/chapchap/saleor/deployment/digitalocean/nginx/server.chapchapticket.com.conf \
  /etc/nginx/sites-available/server.chapchapticket.com.conf
sudo ln -sf /etc/nginx/sites-available/server.chapchapticket.com.conf \
  /etc/nginx/sites-enabled/server.chapchapticket.com.conf
sudo nginx -t && sudo systemctl reload nginx

# TLS
sudo certbot --nginx -d server.chapchapticket.com
```

After Certbot works, edit `~/chapchap/saleor/deployment/digitalocean/.env`:

```env
PUBLIC_URL=https://server.chapchapticket.com/
ENABLE_SSL=True
```

Then:

```bash
cd ~/chapchap/saleor/deployment/digitalocean
docker compose up -d api worker beat
```

## Point the apps

Staff mobile (and customer apps):

```env
EXPO_PUBLIC_SALEOR_API_URL=https://server.chapchapticket.com/graphql/
```

Restart Expo / rebuild so the URL is baked in.

## Smoke tests

```bash
curl -sS https://server.chapchapticket.com/health/
curl -sS -X POST https://server.chapchapticket.com/graphql/ \
  -H 'Content-Type: application/json' \
  -d '{"query":"{ shop { name } }"}'
```

## Layout

| Path | Role |
|------|------|
| `docker-compose.yml` | db, redis, api, worker, beat |
| `.env.example` | template → copy to `.env` on server |
| `secrets/rsa_*.pem` | JWT signing keys (not committed) |
| `data/media` | uploads (Nginx `/media/`) |
| `data/static` | collectstatic copy (Nginx `/static/`) |
| `nginx/server.chapchapticket.com.conf` | host Nginx vhost |
| `scripts/prepare-and-sync.sh` | laptop → droplet rsync |
| `scripts/deploy.sh` | build + migrate + start |
| `scripts/export-local-db.sh` / `import-db.sh` | DB migration |

## Notes

- API is bound to **`127.0.0.1:8000` only** so it does not clash with your other Django app; Nginx is the public entry.
- Celery **worker** is required for staff push / async tasks; **beat** for scheduled jobs.
- Keep the same RSA keys as local if you restore the same database (existing JWTs / app tokens).
- Payment apps (Paystack, etc.) need their own public URLs and webhook re-registration after cutover.
