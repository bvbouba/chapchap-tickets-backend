#!/usr/bin/env bash
# Export the local Saleor Postgres DB for migration to the droplet.
# Usage (on your Mac, from saleor/):
#   ./deployment/digitalocean/scripts/export-local-db.sh
#
# Reads DATABASE_URL from saleor/.env (or pass DATABASE_URL=...).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SALEOR_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
OUT_DIR="${OUT_DIR:-${SCRIPT_DIR}/../backups}"
mkdir -p "${OUT_DIR}"

if [[ -z "${DATABASE_URL:-}" && -f "${SALEOR_ROOT}/.env" ]]; then
  # shellcheck disable=SC1091
  set -a
  # Only pull DATABASE_URL without executing the rest of multiline PEM chaos:
  DATABASE_URL="$(grep -E '^DATABASE_URL=' "${SALEOR_ROOT}/.env" | head -1 | cut -d= -f2-)"
  set +a
fi

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "DATABASE_URL not set. Export it or put it in ${SALEOR_ROOT}/.env" >&2
  exit 1
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="${OUT_DIR}/saleor-${STAMP}.sql.gz"

echo "Dumping ${DATABASE_URL%%@*}@… → ${OUT}"
pg_dump --no-owner --no-acl "${DATABASE_URL}" | gzip -c > "${OUT}"
ls -lh "${OUT}"
echo "Copy to server:"
echo "  scp ${OUT} vaflaly@162.243.162.104:chapchap/saleor/deployment/digitalocean/backups/"
