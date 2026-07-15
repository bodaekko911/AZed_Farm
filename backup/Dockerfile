# Backup cron image — postgres client + aws cli, nothing else.
# This folder is deployed as its own Railway service with Root Directory set
# to "backup", so this Dockerfile and the railway.json beside it are the only
# config that applies — the app's Dockerfile at the repo root cannot
# interfere. postgres:18 client tools can dump any server version <= 18 —
# keep this major version >= the Railway Postgres server major version.
FROM postgres:18-alpine

RUN apk add --no-cache aws-cli

WORKDIR /app
COPY backup.sh restore.sh ./
RUN chmod +x backup.sh restore.sh

# Railway cron services run this on schedule; exiting 0 = success.
CMD ["./backup.sh"]
