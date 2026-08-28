"""Password hashing, persistent login throttling, and secure cookie sessions."""
import hashlib
import hmac
import os
import secrets
from datetime import timedelta
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request, Response
from sqlalchemy import delete
from sqlmodel import Session, select

from database import get_session
from models import AuthToken, LoginFailure, User
from time_utils import utc_now

_ITERATIONS = 600_000
TOKEN_TTL_DAYS = int(os.getenv("TOKEN_TTL_DAYS", "7"))
TOKEN_IDLE_MINUTES = int(os.getenv("TOKEN_IDLE_MINUTES", "480"))
LOGIN_MAX_FAILURES = int(os.getenv("LOGIN_MAX_FAILURES", "5"))
LOGIN_IP_MAX_FAILURES = int(os.getenv("LOGIN_IP_MAX_FAILURES", "20"))
LOGIN_WINDOW_SECONDS = int(os.getenv("LOGIN_WINDOW_SECONDS", str(15 * 60)))
AUTH_COOKIE_NAME = "pos_session"
CSRF_COOKIE_NAME = "pos_csrf"
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() in {"1", "true", "yes"}


def _client_ip(request: Request) -> str:
    if os.getenv("TRUST_PROXY_HEADERS", "false").lower() in {"1", "true", "yes"}:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",", 1)[0].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


def check_login_allowed(username: str, request: Request, session: Session) -> tuple[str, str]:
    username_key = username.strip().casefold()[:128]
    ip_address = _client_ip(request)
    cutoff = utc_now() - timedelta(seconds=LOGIN_WINDOW_SECONDS)
    username_failures = session.exec(
        select(LoginFailure.id).where(
            LoginFailure.username_key == username_key,
            LoginFailure.attempted_at >= cutoff,
        ).limit(LOGIN_MAX_FAILURES)
    ).all()
    ip_failures = session.exec(
        select(LoginFailure.id).where(
            LoginFailure.ip_address == ip_address,
            LoginFailure.attempted_at >= cutoff,
        ).limit(LOGIN_IP_MAX_FAILURES)
    ).all()
    if len(username_failures) >= LOGIN_MAX_FAILURES or len(ip_failures) >= LOGIN_IP_MAX_FAILURES:
        raise HTTPException(429, "登录失败次数过多，请 15 分钟后再试")
    return username_key, ip_address


def record_login_failure(key: tuple[str, str], session: Session) -> None:
    session.add(LoginFailure(username_key=key[0], ip_address=key[1]))
    session.commit()


def clear_login_failures(key: tuple[str, str], session: Session) -> None:
    session.execute(delete(LoginFailure).where(
        LoginFailure.username_key == key[0],
        LoginFailure.ip_address == key[1],
    ))


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), _ITERATIONS
    )
    return f"pbkdf2_sha256${_ITERATIONS}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, hexhash = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iters)
        )
        return hmac.compare_digest(dk.hex(), hexhash)
    except Exception:
        return False


def password_needs_rehash(stored: str) -> bool:
    try:
        return int(stored.split("$")[1]) < _ITERATIONS
    except Exception:
        return True


def hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def issue_token(session: Session, user: User) -> tuple[str, str, AuthToken]:
    raw_token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(24)
    now = utc_now()
    row = AuthToken(
        token=hash_token(raw_token),
        csrf_hash=hash_token(csrf_token),
        user_id=user.id,
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(days=TOKEN_TTL_DAYS),
    )
    session.add(row)
    return raw_token, csrf_token, row


def set_auth_cookies(response: Response, raw_token: str, csrf_token: str) -> None:
    max_age = TOKEN_TTL_DAYS * 24 * 60 * 60
    response.set_cookie(
        AUTH_COOKIE_NAME, raw_token, max_age=max_age, httponly=True,
        secure=COOKIE_SECURE, samesite="strict", path="/",
    )
    response.set_cookie(
        CSRF_COOKIE_NAME, csrf_token, max_age=max_age, httponly=False,
        secure=COOKIE_SECURE, samesite="strict", path="/",
    )


def clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(AUTH_COOKIE_NAME, path="/", secure=COOKIE_SECURE, samesite="strict")
    response.delete_cookie(CSRF_COOKIE_NAME, path="/", secure=COOKIE_SECURE, samesite="strict")


def revoke_user_tokens(session: Session, user_id: int) -> None:
    session.execute(delete(AuthToken).where(AuthToken.user_id == user_id))


def get_current_user(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_csrf_token: Optional[str] = Header(None, alias="X-CSRF-Token"),
    session: Session = Depends(get_session),
) -> User:
    cached = getattr(request.state, "current_user", None)
    if cached is not None:
        return cached

    via_cookie = True
    raw_token = request.cookies.get(AUTH_COOKIE_NAME)
    if authorization and authorization.lower().startswith("bearer "):
        raw_token = authorization.split(" ", 1)[1].strip()
        via_cookie = False
    if not raw_token:
        raise HTTPException(401, "未登录")

    row = session.exec(select(AuthToken).where(AuthToken.token == hash_token(raw_token))).first()
    now = utc_now()
    if not row or not row.last_seen_at:
        raise HTTPException(401, "登录已过期，请重新登录")
    if row.expires_at and row.expires_at < now:
        session.delete(row)
        session.commit()
        raise HTTPException(401, "登录已过期，请重新登录")
    if row.last_seen_at < now - timedelta(minutes=TOKEN_IDLE_MINUTES):
        session.delete(row)
        session.commit()
        raise HTTPException(401, "长时间未操作，请重新登录")

    if via_cookie and request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
        if not x_csrf_token or not row.csrf_hash or not hmac.compare_digest(
            hash_token(x_csrf_token), row.csrf_hash
        ):
            raise HTTPException(403, "安全校验失败，请刷新页面后重试")

    user = session.get(User, row.user_id)
    if not user or not user.active:
        raise HTTPException(401, "账号不可用")
    if row.last_seen_at < now - timedelta(minutes=5):
        row.last_seen_at = now
        session.add(row)
        session.commit()
    request.state.current_user = user
    request.state.auth_token = row
    return user


def require_manager(user: User = Depends(get_current_user)) -> User:
    if user.role != "manager":
        raise HTTPException(403, "需要店长权限")
    return user
