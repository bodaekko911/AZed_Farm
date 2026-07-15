#!/bin/sh
# ---------------------------------------------------------------------------
# Offsite Postgres backup for AZed Farm ERP.
#
# Dumps the database (pg_dump custom format, compressed), uploads it to any
# S3-compatible bucket (Cloudflare R2 / Backblaze B2 / AWS S3), then prunes
# remote copies older than BACKUP_KEEP_DAYS. Designed to run as a Railway
# cron service (see backup/Dockerfile + BACKUPS.md), but runs anywhere the
# postgres client + aws cli exist.
#
# Required environment:
#   DATABASE_URL        postgres connection string (Railway injects this via
#                       a service reference: ${{Postgres.DATABASE_URL}})
#   S3_BUCKET           bucket name, e.g. azed-backups
#   S3_ENDPOINT_URL     e.g. https://<accountid>.r2.cloudflarestorage.com
#                       (leave empty for real AWS S3)
#   AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY   bucket credentials
#
# Optional:
#   BACKUP_PREFIX       folder inside the bucket, default "azed-farm" — use
#                       the customer/instance name, one prefix per instance
#   BACKUP_KEEP_DAYS    prune backups older than this, default 30
#   AWS_DEFAULT_REGION  default "auto" (correct for R2)
# ---------------------------------------------------------------------------
set -eu

: "${DATABASE_URL:?DATABASE_URL is required}"
: "${S3_BUCKET:?S3_BUCKET is required}"
PREFIX="${BACKUP_PREFIX:-azed-farm}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-30}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-auto}"

# pg_dump wants plain postgresql:// — strip the sqlalchemy driver suffix if the
# app's URL was reused verbatim.
DB_URL=$(printf '%s' "$DATABASE_URL" | sed 's|postgresql+asyncpg://|postgresql://|; s|^postgres://|postgresql://|')

STAMP=$(date -u +%Y%m%d-%H%M%S)
FILE="/tmp/${PREFIX}-${STAMP}.dump"
KEY="${PREFIX}/${PREFIX}-${STAMP}.dump"

ENDPOINT_ARG=""
if [ -n "${S3_ENDPOINT_URL:-}" ]; then
  ENDPOINT_ARG="--endpoint-url=${S3_ENDPOINT_URL}"
fi

echo "[backup] dumping database…"
pg_dump --format=custom --compress=6 --no-owner --no-privileges \
        --dbname="$DB_URL" --file="$FILE"

SIZE=$(du -h "$FILE" | cut -f1)
echo "[backup] dump complete (${SIZE}) — uploading s3://${S3_BUCKET}/${KEY}"
aws $ENDPOINT_ARG s3 cp "$FILE" "s3://${S3_BUCKET}/${KEY}" --only-show-errors
rm -f "$FILE"

# --- Prune: delete remote objects under our prefix older than KEEP_DAYS -----
echo "[backup] pruning copies older than ${KEEP_DAYS} days…"
# Pure shell arithmetic — works on GNU coreutils, BSD, and BusyBox (Alpine)
# alike. BusyBox date has no "-30 days"/"-v-30d" relative syntax.
CUTOFF=$(( $(date -u +%s) - KEEP_DAYS * 86400 ))
aws $ENDPOINT_ARG s3 ls "s3://${S3_BUCKET}/${PREFIX}/" | while read -r d t _ key; do
  [ -n "$key" ] || continue
  TS=$(date -u -d "$d $t" +%s 2>/dev/null || echo 0)
  if [ "$TS" -gt 0 ] && [ "$TS" -lt "$CUTOFF" ]; then
    echo "[backup]   deleting old backup: $key"
    aws $ENDPOINT_ARG s3 rm "s3://${S3_BUCKET}/${PREFIX}/${key}" --only-show-errors
  fi
done

echo "[backup] done: s3://${S3_BUCKET}/${KEY}"
