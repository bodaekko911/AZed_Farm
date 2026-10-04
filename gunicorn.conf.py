import os

from app.core.config import settings


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


# Railway injects PORT at runtime; local runs fall back to API_PORT.
_port = os.environ.get("PORT") or str(settings.API_PORT)
bind = f"{settings.API_HOST}:{_port}"

# Each worker is a full copy of the app in RAM, and Railway bills RAM by the
# minute. Set the WORKERS variable in Railway to change this (1 is enough for
# the farm's traffic and roughly halves the app's memory).
workers = settings.WORKERS
worker_class = "uvicorn.workers.UvicornWorker"
accesslog = "-"
errorlog = "-"
loglevel = settings.LOG_LEVEL.lower()
timeout = 120
graceful_timeout = 30

# --- Worker recycling ---------------------------------------------------------
#
# A long-lived Python process almost never returns memory to the OS: every
# Excel import, report or large query raises its resident size a little, and
# it never comes back down until the next deploy. That is the slow upward
# creep on the Railway memory graph, and Railway bills all of it.
#
# max_requests restarts a worker after it has served that many requests, which
# hands its memory back and starts it fresh. In-flight requests finish first,
# and the jitter staggers restarts so workers never recycle at the same time.
max_requests = _env_int("GUNICORN_MAX_REQUESTS", 2000)
max_requests_jitter = _env_int("GUNICORN_MAX_REQUESTS_JITTER", 200)