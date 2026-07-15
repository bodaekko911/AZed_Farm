#!/bin/sh
# ---------------------------------------------------------------------------
# Restore an AZed Farm ERP backup made by backup/backup.sh.
#
#   ./backup/restore.sh                     # list available backups
#   ./backup/restore.sh <key>               # restore that backup
#   ./backup/restore.sh <key> --i-am-sure   # skip the confirmation prompt
#
# <key> is the object name as shown by the listing, e.g.
#   azed-farm/azed-farm-20260715-020000.dump
#
# Restores into DATABASE_URL with --clean --if-exists: existing tables are
# dropped and recreated from the dump. Point DATABASE_URL at a FRESH database
# first when rehearsing (see BACKUPS.md — do a restore drill monthly; a backup
# that has never been restored is a hope, not a backup).
#
# Requires the same environment as backup.sh (DATABASE_URL, S3_BUCKET,
# S3_ENDPOINT_URL, AWS credentials).
# ---------------------------------------------------------------------------
set -eu

: "${S3_BUCKET:?S3_BUCKET is required}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-auto}"

ENDPOINT_ARG=""
if [ -n "${S3_ENDPOINT_URL:-}" ]; then
  ENDPOINT_ARG="--endpoint-url=${S3_ENDPOINT_URL}"
fi

if [ "$#" -lt 1 ]; then
  echo "Available backups in s3://${S3_BUCKET}:"
  aws $ENDPOINT_ARG s3 ls "s3://${S3_BUCKET}/" --recursive | sort
  echo ""
  echo "Usage: $0 <key> [--i-am-sure]"
  exit 0
fi

KEY="$1"
: "${DATABASE_URL:?DATABASE_URL is required}"
DB_URL=$(printf '%s' "$DATABASE_URL" | sed 's|postgresql+asyncpg://|postgresql://|; s|^postgres://|postgresql://|')

if [ "${2:-}" != "--i-am-sure" ]; then
  echo "About to restore  s3://${S3_BUCKET}/${KEY}"
  echo "            into  ${DB_URL%%\?*}"
  echo "This DROPS and recreates existing tables in that database."
  printf "Type the word RESTORE to continue: "
  read -r answer
  [ "$answer" = "RESTORE" ] || { echo "aborted."; exit 1; }
fi

FILE="/tmp/restore.dump"
echo "[restore] downloading…"
aws $ENDPOINT_ARG s3 cp "s3://${S3_BUCKET}/${KEY}" "$FILE" --only-show-errors

echo "[restore] restoring…"
pg_restore --clean --if-exists --no-owner --no-privileges \
           --dbname="$DB_URL" "$FILE"
rm -f "$FILE"

echo "[restore] done. Now verify:"
echo "  1. App boots and health check is ok"
echo "  2. Log in, open Sales, Inventory, B2B invoices — spot-check recent"
echo "     orders, stock levels and invoice payment statuses"
echo "  3. alembic current   (should be at head; the entrypoint runs"
echo "     'alembic upgrade head' on boot if the dump predates the app version)"
