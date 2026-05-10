import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.middleware import get_trusted_client_ip
from app.core.permission_catalog import get_permission_catalog
from app.core.permissions import (
    get_effective_permissions,
    require_admin,
    serialize_permissions,
)
from app.core.security import (
    create_access_token,
    get_current_user,
    hash_password,
    password_needs_rehash,
    try_refresh_access_token,
    verify_password,
)
from app.database import get_async_session
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.schemas.user import UserCreate, UserOut, UserLogin
from app.core.rate_limit import limiter
from app.core.templates import templates

router = APIRouter(tags=["Auth"])


def _redis_client():
    import redis.asyncio as aioredis

    return aioredis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=settings.REDIS_SOCKET_CONNECT_TIMEOUT,
        socket_timeout=settings.REDIS_SOCKET_TIMEOUT,
        retry_on_timeout=False,
    )


@router.get("/", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "landing.html", {})


@router.post("/auth/login")
@limiter.limit(settings.LOGIN_RATE_LIMIT)
async def login(
    data: UserLogin,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_async_session),
):
    from app.core.log import record

    # Brute-force protection: track failed attempts per IP in Redis
    import logging
    _brute_logger = logging.getLogger("erp")
    _client_ip = get_trusted_client_ip(request)
    _fail_key = f"login_fail:{_client_ip}"
    try:
        _redis = _redis_client()
        _fails = await _redis.get(_fail_key)
        if _fails and int(_fails) >= 5:
            await _redis.aclose()
            raise HTTPException(
                status_code=429,
                detail="Too many failed attempts. Try again in 15 minutes.",
            )
        await _redis.aclose()
    except HTTPException:
        raise
    except Exception:
        _brute_logger.warning("Redis unavailable for brute-force check — allowing login attempt")

    result = await db.execute(select(User).where(User.email == data.email))
    user = result.scalar_one_or_none()
    if not user or not verify_password(data.password, user.password):
        # Log failed attempt (no user object, store email)
        record(db, "Auth", "login_failed",
               f"Failed login attempt for email: {data.email}")
        await db.commit()
        try:
            _redis = _redis_client()
            await _redis.incr(_fail_key)
            await _redis.expire(_fail_key, 900)  # 15 minutes TTL
            await _redis.aclose()
        except Exception:
            pass
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is disabled")
    if password_needs_rehash(user.password):
        user.password = hash_password(data.password)
    permissions = serialize_permissions(
        get_effective_permissions(user.role, user.permissions)
    )
    token = create_access_token(
        {"sub": user.id, "role": user.role, "permissions": permissions}
    )
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        samesite="lax",
        secure=settings.COOKIE_SECURE,
        path="/",
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    response.set_cookie(
        key="logged_in",
        value="true",
        httponly=False,
        samesite="lax",
        secure=settings.COOKIE_SECURE,
        path="/",
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    # Issue refresh token
    raw_rt = secrets.token_urlsafe(48)
    rt_hash = hashlib.sha256(raw_rt.encode()).hexdigest()
    rt_expires = datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    db.add(RefreshToken(user_id=user.id, token_hash=rt_hash, expires_at=rt_expires))
    response.set_cookie(
        key="refresh_token",
        value=raw_rt,
        httponly=True,
        samesite="lax",
        secure=settings.COOKIE_SECURE,
        path="/",
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400,
    )

    # Reset brute-force counter on successful login
    try:
        _redis = _redis_client()
        await _redis.delete(_fail_key)
        await _redis.aclose()
    except Exception:
        pass
    record(db, "Auth", "login",
           f"User logged in: {user.name} ({user.role})",
           user=user, ref_type="user", ref_id=user.id)
    await db.commit()
    # access_token is in the httpOnly cookie — not returned in body to prevent XSS
    return {
        "role": user.role,
        "name": user.name,
        "permissions": permissions,
    }


@router.get("/auth/me")
async def me(current_user: User = Depends(get_current_user)):
    permissions = serialize_permissions(
        get_effective_permissions(current_user.role, current_user.permissions)
    )
    return {
        "id": current_user.id,
        "name": current_user.name,
        "email": current_user.email,
        "role": current_user.role,
        "is_active": current_user.is_active,
        "permissions": permissions,
    }


@router.get("/auth/permissions/catalog")
async def permissions_catalog(current_user: User = Depends(get_current_user)):
    return {
        "catalog": get_permission_catalog(),
        "role": current_user.role,
        "permissions": sorted(get_effective_permissions(current_user.role, current_user.permissions)),
    }


@router.post("/auth/register", response_model=UserOut, status_code=201)
async def register(
    data: UserCreate,
    db: AsyncSession = Depends(get_async_session),
    _: User = Depends(require_admin),
):
    result = await db.execute(select(User).where(User.email == data.email))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already registered")
    user = User(
        name=data.name,
        email=data.email,
        password=hash_password(data.password),
        role=data.role,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/auth/logout")
async def logout(
    response: Response,
    db: AsyncSession = Depends(get_async_session),
    refresh_token: str | None = Cookie(None, alias="refresh_token"),
):
    """Clear the auth cookie and invalidate the refresh token."""
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("refresh_token", path="/")
    response.delete_cookie("logged_in", path="/")
    if refresh_token:
        token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
        _r = await db.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
        rt = _r.scalar_one_or_none()
        if rt:
            await db.delete(rt)
            await db.commit()
    return {"ok": True}


@router.post("/auth/refresh")
@limiter.limit(settings.REFRESH_RATE_LIMIT)
async def refresh(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_async_session),
    refresh_token: str | None = Cookie(None, alias="refresh_token"),
):
    """Issue a new access token if a valid refresh token cookie is present."""
    if not refresh_token:
        raise HTTPException(status_code=401, detail="No refresh token")

    refreshed = await try_refresh_access_token(db, refresh_token)
    if not refreshed:
        raise HTTPException(status_code=401, detail="Refresh token expired or invalid")
    new_token, new_raw_rt = refreshed

    response.set_cookie(
        key="access_token",
        value=new_token,
        httponly=True,
        samesite="lax",
        secure=settings.COOKIE_SECURE,
        path="/",
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    response.set_cookie(
        key="logged_in",
        value="true",
        httponly=False,
        samesite="lax",
        secure=settings.COOKIE_SECURE,
        path="/",
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    response.set_cookie(
        key="refresh_token",
        value=new_raw_rt,
        httponly=True,
        samesite="lax",
        secure=settings.COOKIE_SECURE,
        path="/",
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400,
    )
    return {"ok": True}
