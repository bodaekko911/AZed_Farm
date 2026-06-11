from contextlib import asynccontextmanager
from pathlib import Path

from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette_csrf import CSRFMiddleware

from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.api.routes import ROUTERS
from app.core.config import settings
from app.core.log import configure_logging, logger
from app.core.middleware import RequestLoggingMiddleware, SecurityHeadersMiddleware
from app.core.migrations import verify_migration_status
from app.core.monitoring import configure_monitoring
from app.core.rate_limit import limiter
from app.database import get_async_session


async def ensure_payroll_columns() -> None:
    """Self-healing guard: ensure recently-added payroll columns exist even if
    the alembic migration hasn't applied yet. Idempotent and safe to run on
    every startup. Without these columns, every query against the payroll table
    fails (e.g. the Payroll tab hangs on 'Loading...')."""
    from sqlalchemy import text
    from app.db.session import AsyncSessionLocal

    statements = [
        "ALTER TABLE payroll ADD COLUMN IF NOT EXISTS paid_amount NUMERIC(12,2)",
        "ALTER TABLE payroll ADD COLUMN IF NOT EXISTS days_off_credited NUMERIC(8,2) NOT NULL DEFAULT 0",
    ]
    try:
        async with AsyncSessionLocal() as db:
            for stmt in statements:
                await db.execute(text(stmt))
            await db.commit()
            logger.info("ensure_payroll_columns: payroll columns ready")
    except Exception:
        logger.exception("ensure_payroll_columns: failed")


async def ensure_carbon_methodology() -> None:
    """Self-healing guard for the carbon module's methodology upgrade.

    1. Adds GHG Protocol provenance columns to carbon_emission_factors
       (scope, methodology_source, source_year, region) if missing.
    2. Seeds a set of documented default emission factors (inserted only
       when the source_key does not already exist — never overwrites
       user-edited values).
    3. Backfills methodology fields on known seeded keys where they are
       still NULL, so pre-existing installs gain citations too.

    Idempotent and safe to run on every startup. Default factor values are
    indicative figures from widely used public datasets (DEFRA GHG
    Conversion Factors; IFI Harmonised Grid Emission Factors for the Egypt
    grid) and are fully editable in the Factors admin page.
    """
    from sqlalchemy import text
    from app.db.session import AsyncSessionLocal

    column_statements = [
        "ALTER TABLE carbon_emission_factors ADD COLUMN IF NOT EXISTS scope INTEGER",
        "ALTER TABLE carbon_emission_factors ADD COLUMN IF NOT EXISTS methodology_source VARCHAR(200)",
        "ALTER TABLE carbon_emission_factors ADD COLUMN IF NOT EXISTS source_year INTEGER",
        "ALTER TABLE carbon_emission_factors ADD COLUMN IF NOT EXISTS region VARCHAR(80)",
    ]

    # (source_type, source_key, label, factor, unit, scope, methodology_source, source_year, region)
    DEFAULT_FACTORS = [
        ("energy", "diesel_liter", "Diesel fuel (per litre)", 2.68, "litre", 1,
         "DEFRA GHG Conversion Factors 2024 — diesel (average biofuel blend)", 2024, "Global default"),
        ("energy", "petrol_liter", "Petrol fuel (per litre)", 2.31, "litre", 1,
         "DEFRA GHG Conversion Factors 2024 — petrol (average biofuel blend)", 2024, "Global default"),
        ("energy", "lpg_liter", "LPG (per litre)", 1.56, "litre", 1,
         "DEFRA GHG Conversion Factors 2024 — LPG", 2024, "Global default"),
        ("energy", "electricity_kwh", "Grid electricity (per kWh)", 0.46, "kWh", 2,
         "IFI Harmonised Grid Emission Factors — Egypt national grid (indicative; verify against latest dataset)", 2023, "Egypt"),
        ("transport", "van_km", "Van / pickup transport (per km)", 0.23, "km", 1,
         "DEFRA GHG Conversion Factors 2024 — average van, diesel", 2024, "Global default"),
        ("transport", "truck_km", "Rigid truck transport (per km)", 0.81, "km", 1,
         "DEFRA GHG Conversion Factors 2024 — rigid HGV, average laden", 2024, "Global default"),
        ("waste", "organic_waste_kg", "Organic waste to landfill (per kg)", 0.58, "kg", 3,
         "DEFRA GHG Conversion Factors 2024 — organic/food waste to landfill (indicative)", 2024, "Global default"),
        ("waste", "compost_kg", "Organic waste composted (per kg)", 0.01, "kg", 3,
         "DEFRA GHG Conversion Factors 2024 — open-loop composting", 2024, "Global default"),
        ("production", "processing_kwh", "Processing energy (per kWh)", 0.46, "kWh", 2,
         "Grid electricity factor — Egypt (IFI Harmonised Grid Emission Factors)", 2023, "Egypt"),
    ]

    try:
        async with AsyncSessionLocal() as db:
            for stmt in column_statements:
                await db.execute(text(stmt))
            await db.commit()

            for (stype, skey, label, factor, unit, scope, msrc, syear, region) in DEFAULT_FACTORS:
                await db.execute(text("""
                    INSERT INTO carbon_emission_factors
                        (source_type, source_key, label, factor_kg_co2e_per_unit, unit,
                         scope, methodology_source, source_year, region, is_active)
                    SELECT :stype, :skey, :label, :factor, :unit,
                           :scope, :msrc, :syear, :region, TRUE
                    WHERE NOT EXISTS (
                        SELECT 1 FROM carbon_emission_factors WHERE source_key = :skey
                    )
                """), {"stype": stype, "skey": skey, "label": label, "factor": factor,
                       "unit": unit, "scope": scope, "msrc": msrc, "syear": syear, "region": region})

                # Backfill methodology on pre-existing rows with the same key
                # (never touches factor values or labels the user may have edited).
                await db.execute(text("""
                    UPDATE carbon_emission_factors
                       SET scope = COALESCE(scope, :scope),
                           methodology_source = COALESCE(methodology_source, :msrc),
                           source_year = COALESCE(source_year, :syear),
                           region = COALESCE(region, :region)
                     WHERE source_key = :skey
                """), {"skey": skey, "scope": scope, "msrc": msrc, "syear": syear, "region": region})

            await db.commit()
            logger.info("ensure_carbon_methodology: carbon methodology columns and default factors ready")
    except Exception:
        logger.exception("ensure_carbon_methodology: failed")


async def seed_chart_of_accounts() -> None:
    """Ensure core accounting accounts exist. Safe to run on every startup."""
    from decimal import Decimal
    from sqlalchemy import select
    from app.db.session import AsyncSessionLocal
    from app.models.accounting import Account

    CORE_ACCOUNTS = [
        ("1000", "Cash",              "asset"),
        ("1100", "Accounts Receivable", "asset"),
        ("2000", "Accounts Payable",  "liability"),
        ("2200", "Deferred Revenue",  "liability"),
        ("4000", "Revenue",           "revenue"),
        ("5000", "Cost of Goods Sold","expense"),
        ("6000", "Expenses",          "expense"),
    ]
    try:
        async with AsyncSessionLocal() as db:
            for code, name, atype in CORE_ACCOUNTS:
                r = await db.execute(select(Account).where(Account.code == code))
                if r.scalar_one_or_none() is None:
                    db.add(Account(code=code, name=name, type=atype, balance=Decimal("0")))
            await db.commit()
            logger.info("seed_chart_of_accounts: core accounts ready")
    except Exception:
        logger.exception("seed_chart_of_accounts: failed — journals will not post until accounts exist")

STATIC_DIR = Path(__file__).resolve().parent / "static"

# ── HTML shown when an unhandled 500 occurs during an HTML page navigation ─
_ERROR_HTML_PAGE = """<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<title>Something went wrong</title>
<style>
  :root{--card:rgba(15,20,36,0.88);--border:rgba(255,255,255,0.08);--text:#fff;--sub:#8899bb}
  *{margin:0;padding:0;box-sizing:border-box}
  body{font-family:'Segoe UI',sans-serif;min-height:100vh;display:flex;align-items:center;
       justify-content:center;color:var(--text);padding:24px;
       background:linear-gradient(rgba(6,8,16,.68),rgba(6,8,16,.68)),
                 url('/static/home1.jpg.jpeg') center/cover no-repeat}
  .box{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:40px;
       width:360px;backdrop-filter:blur(8px);box-shadow:0 24px 60px rgba(0,0,0,.35);text-align:center}
  h2{color:#ff4d6d;font-size:22px;margin-bottom:12px}
  p{color:var(--sub);font-size:14px;margin-bottom:28px}
  a{display:inline-block;padding:12px 28px;background:linear-gradient(135deg,#00ff9d,#00d4ff);
    border-radius:10px;color:#021a10;font-weight:800;font-size:14px;text-decoration:none}
  a:hover{filter:brightness(1.1)}
</style>
</head><body>
<div class="box">
  <h2>Something went wrong</h2>
  <p>An unexpected error occurred. Please try again or go back.</p>
  <a href="javascript:history.back()">Go back</a>
</div>
</body></html>"""


async def _try_silent_refresh(refresh_token_value: str) -> tuple[str, str] | None:
    """
    Open a fresh DB session and attempt to rotate the given raw refresh token.
    Returns ``(access_token, refresh_token)`` on success, or None on any
    failure, so callers can safely fall through to a login redirect without
    raising.

    The refresh token is rotated server-side on success, so callers MUST write
    both returned cookies back to the browser. Defined at module level (not
    inside create_app) so tests can monkeypatch
    ``app.app_factory._try_silent_refresh`` without a real DB connection.
    """
    from app.db.session import AsyncSessionLocal
    from app.core import security
    try:
        async with AsyncSessionLocal() as db:
            return await security.try_refresh_access_token(db, refresh_token_value)
    except Exception:
        return None


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging()
    configure_monitoring()
    await verify_migration_status()
    await ensure_payroll_columns()
    await ensure_carbon_methodology()
    await seed_chart_of_accounts()
    from app.core.cache import init_redis_pool, close_redis_pool
    await init_redis_pool()
    yield
    await close_redis_pool()


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.APP_NAME,
        debug=settings.DEBUG,
        lifespan=lifespan,
    )

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    (STATIC_DIR / "uploads").mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ALLOW_ORIGINS,
        allow_credentials=settings.CORS_ALLOW_CREDENTIALS,
        allow_methods=settings.CORS_ALLOW_METHODS,
        allow_headers=settings.CORS_ALLOW_HEADERS,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.ALLOWED_HOSTS)
    app.add_middleware(SecurityHeadersMiddleware)
    # CSRF protection: only triggers on requests that carry the access_token cookie.
    # Auth endpoints (/auth/*) are exempt — they use credentials as proof, not a session.
    # Pure JSON API calls (/*/api/*) are also exempt — protected by CORS same-origin policy.
    import re
    app.add_middleware(
        CSRFMiddleware,
        secret=settings.SECRET_KEY,
        sensitive_cookies={"access_token"},
        exempt_urls=[
            re.compile(r"^/auth/.*"),
            re.compile(r".*/api/.*"),
            re.compile(r"^/hr/clear-data$"),
            re.compile(r"^/import/.*"),
            re.compile(r"^/invoice.*"),
            re.compile(r"^/health.*"),
        ],
    )
    app.add_middleware(RequestLoggingMiddleware)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logger.exception(
            "Unhandled application error",
            exc_info=exc,
            extra={
                "method": request.method,
                "path": request.url.path,
                "query": str(request.url.query) or None,
            },
        )
        # Return a styled HTML page for browser navigations so users never see
        # raw JSON from an unhandled 500.  API callers (JSON Accept) still get
        # the machine-readable JSON body.
        if (
            request.method == "GET"
            and "text/html" in request.headers.get("accept", "")
        ):
            return HTMLResponse(content=_ERROR_HTML_PAGE, status_code=500)
        return JSONResponse(
            status_code=500,
            content={"detail": "An internal server error occurred."},
        )

    # ── Session-expiry middleware ────────────────────────────────────────────
    # Intercepts 401 responses on HTML GET navigations (e.g. the browser
    # requests /dashboard after the access token has expired):
    #
    #  • If a refresh_token cookie is present → attempt a silent refresh and
    #    307-redirect back to the same URL with the new access_token cookie so
    #    the browser retries with a fresh token.
    #  • Otherwise → 307-redirect to /?next=<path>&reason=expired so the login
    #    page can show a friendly "session expired" message and bounce the user
    #    back after sign-in.
    #
    # Only HTML GETs are rewritten.  JSON/API callers and POST/PUT/DELETE
    # requests keep receiving the plain 401 JSON so auth-guard.js keeps
    # working and API clients are unaffected.
    # /auth/* and /health* are explicitly excluded.
    @app.middleware("http")
    async def _session_expiry(request: Request, call_next):
        response = await call_next(request)

        if (
            response.status_code == 401
            and request.method == "GET"
            and "text/html" in request.headers.get("accept", "")
            and not request.url.path.startswith("/auth/")
            and not request.url.path.startswith("/health")
        ):
            # Reconstruct the original path + query so ?next= roundtrips.
            path = request.url.path
            if request.url.query:
                path += "?" + request.url.query

            refresh_token_value = request.cookies.get("refresh_token")
            if refresh_token_value:
                refreshed = await _try_silent_refresh(refresh_token_value)
                if refreshed:
                    new_access_token, new_refresh_token = refreshed
                    # Redirect to the same URL; the new cookie rides along so
                    # the retry succeeds without another round-trip.
                    redirect = RedirectResponse(url=path, status_code=307)
                    redirect.set_cookie(
                        key="access_token",
                        value=new_access_token,
                        httponly=True,
                        samesite="lax",
                        secure=settings.COOKIE_SECURE,
                        path="/",
                        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
                    )
                    redirect.set_cookie(
                        key="logged_in",
                        value="true",
                        httponly=False,
                        samesite="lax",
                        secure=settings.COOKIE_SECURE,
                        path="/",
                        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
                    )
                    redirect.set_cookie(
                        key="refresh_token",
                        value=new_refresh_token,
                        httponly=True,
                        samesite="lax",
                        secure=settings.COOKIE_SECURE,
                        path="/",
                        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400,
                    )
                    return redirect

            # No valid refresh token — send to login with context.
            login_url = "/?next=" + quote(path, safe="") + "&reason=expired"
            return RedirectResponse(url=login_url, status_code=307)

        return response

    for router in ROUTERS:
        app.include_router(router)

    @app.get("/health/live")
    async def liveness():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def readiness(db: AsyncSession = Depends(get_async_session)):
        try:
            await db.execute(text("SELECT 1"))
            return {"status": "ok", "db": "ok"}
        except Exception:
            return JSONResponse(
                status_code=503,
                content={"status": "error", "db": "unreachable"},
            )

    # Backward-compat alias
    @app.get("/health")
    async def health():
        return {"status": "ok", "app": settings.APP_NAME, "environment": settings.APP_ENV}

    logger.info("Application configured")
    return app