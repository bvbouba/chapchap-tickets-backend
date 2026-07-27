#!/usr/bin/env bash
# Restore a gzipped pg_dump into the compose Postgres (ON THE DROPLET).
# Usage:
#   ./scripts/import-db.sh backups/saleor-YYYYMMDD-HHMMSS.sql.gz
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${DEPLOY_DIR}"

DUMP="${1:-}"
if [[ -z "${DUMP}" || ! -f "${DUMP}" ]]; then
  echo "Usage: $0 backups/saleor-….sql.gz" >&2
  exit 1
fi

# shellcheck disable=SC1091
set -a
source .env
set +a

echo "==> Ensuring db is up"
docker compose up -d db
sleep 3

echo "==> Dropping & recreating database ${POSTGRES_DB} (DESTRUCTIVE)"
docker compose exec -T db psql -U "${POSTGRES_USER}" -d postgres -v ON_ERROR_STOP=1 <<SQL
SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${POSTGRES_DB}' AND pid <> pg_backend_pid();
DROP DATABASE IF EXISTS ${POSTGRES_DB};
CREATE DATABASE ${POSTGRES_DB} OWNER ${POSTGRES_USER};
SQL

echo "==> Restoring ${DUMP}"
gunzip -c "${DUMP}" | docker compose exec -T db psql -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -v ON_ERROR_STOP=1

echo "==> Migrations (safe no-op if dump is current)"
docker compose run --rm api python manage.py migrate --noinput

echo "Restore complete."
