#!/bin/sh
# Container entrypoint. Kept as a separate file so line-ending issues
# (CRLF vs LF) in the Dockerfile CMD don't crash the container before
# any code runs.
set -e

echo "[entrypoint] Running alembic upgrade head..."
python -m alembic upgrade head || {
    echo "[entrypoint] WARNING: alembic upgrade head failed — starting gunicorn anyway"
}

echo "[entrypoint] Starting gunicorn..."
exec gunicorn -c gunicorn.conf.py app.main:app