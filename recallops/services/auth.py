"""Operator authentication: password hashing, signed sessions, route guard.

Design notes
------------
*No new dependencies.* Password digests use :mod:`hashlib` (PBKDF2-HMAC-SHA256)
and session cookies are signed with :mod:`hmac`. Nothing here needs bcrypt,
passlib or python-jose, so the deployed image stays small and the attack surface
does not grow.

*Why a signed cookie rather than a server-side session table.* The console is a
read-mostly SRE surface; the only state we need to keep server-side is "this
operator exists and is active". A stateless, signed, expiring cookie carries
that decision, so there is no session table to grow and nothing to clean up
after a crash.

*Why SameSite is configurable.* A Vercel frontend talking to a Render backend is
cross-**site**, which needs ``SameSite=None; Secure``. A local
``localhost:4321 -> localhost:8765`` pair is same-**site**, where ``Lax`` is both
sufficient and safer. One setting covers both.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
from typing import Any

from fastapi import HTTPException, Request, Response, status

from recallops.config import Settings, get_settings
from recallops.persistence import models as orm
from recallops.persistence.db import get_sessionmaker

logger = logging.getLogger("recallops.auth")

COOKIE_NAME = "recallops_session"
PBKDF2_ITERATIONS = 240_000
_LOCKOUT_AFTER = 5
_LOCKOUT_SECONDS = 60.0


# --------------------------------------------------------------------------- secrets


def signing_key(settings: Settings | None = None) -> bytes:
    """HMAC key for session cookies.

    If ``AUTH_SECRET`` is not configured, a per-process random key is generated.
    That is deliberately safe-by-default (nobody can forge a token) but means
    every restart logs operators out, so production must set the variable.
    """
    s = settings or get_settings()
    if s.auth_secret:
        return s.auth_secret.encode("utf-8")
    global _EPHEMERAL_KEY
    if _EPHEMERAL_KEY is None:
        _EPHEMERAL_KEY = secrets.token_bytes(32)
        logger.warning("AUTH_SECRET is not set - generated an ephemeral signing key. Sessions will be invalidated on restart. Set AUTH_SECRET for any shared deployment.")
    return _EPHEMERAL_KEY


_EPHEMERAL_KEY: bytes | None = None


# --------------------------------------------------------------------------- passwords


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _epoch(value: Any) -> float:
    """Seconds since the epoch for a stored datetime.

    SQLite does not persist a timezone, so ``DateTime(timezone=True)`` values
    come back *naive* even though they were written as UTC. Calling
    ``.timestamp()`` on those silently reinterprets UTC as local time, which on
    a machine ahead of UTC makes an active lockout look expired. Normalise first.
    """
    if value is None:
        return 0.0
    from datetime import timezone as _tz

    if getattr(value, "tzinfo", None) is None:
        value = value.replace(tzinfo=_tz.utc)
    return value.timestamp()


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    """Return ``pbkdf2_sha256$<iterations>$<salt>$<hash>``."""
    if not password:
        raise ValueError("password must not be empty")
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${_b64e(salt)}${_b64e(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time verification. Never raises on malformed input."""
    try:
        algorithm, iterations, salt_b64, hash_b64 = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        expected = _b64d(hash_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), _b64d(salt_b64), int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(expected, actual)


def generate_password(length: int = 20) -> str:
    """A readable-but-strong password for the first-run bootstrap."""
    alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


# --------------------------------------------------------------------------- sessions


def issue_token(username: str, ttl_s: int, settings: Settings | None = None) -> str:
    """``<payload_b64>.<hmac_b64>`` where payload is ``user|expiry|nonce``."""
    s = settings or get_settings()
    payload = f"{username}|{int(time.time()) + ttl_s}|{secrets.token_hex(8)}"
    encoded = _b64e(payload.encode("utf-8"))
    signature = hmac.new(signing_key(s), encoded.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded}.{_b64e(signature)}"


def read_token(token: str, settings: Settings | None = None) -> str | None:
    """Return the username if the token is authentic and unexpired, else None."""
    if not token or "." not in token:
        return None
    encoded, _, signature_b64 = token.rpartition(".")
    expected = hmac.new(signing_key(settings), encoded.encode("ascii"), hashlib.sha256).digest()
    try:
        supplied = _b64d(signature_b64)
    except Exception:  # noqa: BLE001
        return None
    if not hmac.compare_digest(expected, supplied):
        return None
    try:
        username, expiry, _nonce = _b64d(encoded).decode("utf-8").split("|")
    except (ValueError, UnicodeDecodeError):
        return None
    if int(expiry) < time.time():
        return None
    return username


def set_session_cookie(response: Response, token: str, settings: Settings | None = None) -> None:
    s = settings or get_settings()
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=s.auth_session_ttl_s,
        httponly=True,  # not readable from JavaScript -> XSS cannot steal the session
        secure=s.auth_cookie_secure,
        samesite=s.auth_cookie_samesite,
        path="/",
    )


def clear_session_cookie(response: Response, settings: Settings | None = None) -> None:
    s = settings or get_settings()
    response.delete_cookie(key=COOKIE_NAME, path="/", secure=s.auth_cookie_secure, samesite=s.auth_cookie_samesite)


# --------------------------------------------------------------------------- store


def find_user(username: str) -> orm.OperatorUser | None:
    session = get_sessionmaker()()
    try:
        return session.query(orm.OperatorUser).filter(orm.OperatorUser.username == username).one_or_none()
    finally:
        session.close()


def count_users() -> int:
    session = get_sessionmaker()()
    try:
        return session.query(orm.OperatorUser).count()
    finally:
        session.close()


class UsernameTaken(ValueError):
    """Raised when a sign-up collides with an existing operator."""


class EmailTaken(ValueError):
    """Raised when a sign-up reuses an email already on file."""


_EMAIL_RE = __import__("re").compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")


def normalise_email(email: str | None) -> str | None:
    """Lower-case and trim; treat blank as absent."""
    if email is None:
        return None
    value = email.strip().lower()
    return value or None


def find_user_by_identifier(identifier: str) -> orm.OperatorUser | None:
    """Look an operator up by email *or* username.

    Sign-in accepts either so an account created before email existed, or one
    provisioned by the bootstrap operator, can still get in.
    """
    ident = (identifier or "").strip()
    if not ident:
        return None
    session = get_sessionmaker()()
    try:
        row = session.query(orm.OperatorUser).filter(orm.OperatorUser.username == ident).one_or_none()
        if row is not None:
            return row
        return session.query(orm.OperatorUser).filter(orm.OperatorUser.email == ident.lower()).one_or_none()
    finally:
        session.close()


def create_user(
    username: str,
    password: str,
    display_name: str = "",
    email: str | None = None,
) -> str:
    """Create an operator.

    Raises :class:`UsernameTaken` or :class:`EmailTaken` on a collision so the
    route can answer 409 with the right message.
    """
    username = (username or "").strip()
    if not username:
        raise ValueError("username must not be empty")
    if find_user(username) is not None:
        raise UsernameTaken(username)

    email_value = normalise_email(email)
    if email_value is not None and find_user_by_identifier(email_value) is not None:
        raise EmailTaken(email_value)

    session = get_sessionmaker()()
    try:
        row = orm.OperatorUser(
            id=f"usr_{secrets.token_hex(8)}",
            username=username,
            display_name=(display_name or "").strip() or username,
            email=email_value,
            password_hash=hash_password(password),
        )
        session.add(row)
        session.commit()
        return row.id
    except Exception as exc:  # noqa: BLE001 - unique-constraint races
        session.rollback()
        message = str(exc).lower()
        if "unique" in message and "email" in message:
            raise EmailTaken(email_value or "") from exc
        if "unique" in message:
            raise UsernameTaken(username) from exc
        raise
    finally:
        session.close()


def validate_registration(
    username: str,
    password: str,
    display_name: str = "",
    email: str | None = None,
) -> tuple[bool, str]:
    """Field-level checks for self-service sign-up.

    Deliberately does not reject a taken username or email: the caller turns
    those into a 409 (a conflict with existing state), which is a different
    outcome from a 400 (the request itself was malformed). Keeping the two apart
    lets a client highlight the right field.
    """
    settings = get_settings()
    if not settings.auth_registration_enabled:
        return False, "Sign-up is disabled on this deployment. Ask an existing operator for an account."

    name = (username or "").strip()
    if len(name) < 3:
        return False, "Username must be at least 3 characters."
    if len(name) > 64:
        return False, "Username must be 64 characters or fewer."
    if not all(ch.isalnum() or ch in "._-@+" for ch in name):
        return False, "Username may only contain letters, numbers and . _ - @ +"

    email_value = (email or "").strip()
    if email_value:
        if len(email_value) > 255:
            return False, "Email must be 255 characters or fewer."
        if not _EMAIL_RE.match(email_value):
            return False, "Enter a valid email address."

    if len(password or "") < settings.auth_min_password_length:
        return False, f"Password must be at least {settings.auth_min_password_length} characters."
    if password == name:
        return False, "Password must not be the same as the username."
    if len(display_name or "") > 120:
        return False, "Full name must be 120 characters or fewer."
    return True, "ok"


def bootstrap_operator() -> str | None:
    """Create the first operator if the table is empty.

    Credentials come from ``AUTH_USERNAME`` / ``AUTH_PASSWORD``. If no password is
    configured a strong random one is generated and logged once - a deployable
    default that cannot be guessed, rather than ``admin/admin``.
    """
    s = get_settings()
    if count_users() > 0:
        return None

    username = (s.auth_username or "").strip() or "operator"
    password = s.auth_password or ""
    generated = False
    if not password:
        password = generate_password()
        generated = True

    create_user(username, password)
    if generated:
        logger.warning("=" * 72)
        logger.warning("RecallOps operator account created")
        logger.warning("  username: %s", username)
        logger.warning("  password: %s", password)
        logger.warning("This password is shown ONCE. Set AUTH_USERNAME and AUTH_PASSWORD")
        logger.warning("in the environment to choose your own, then change it.")
        logger.warning("=" * 72)
    else:
        logger.info("RecallOps operator account created for '%s' (from AUTH_USERNAME/AUTH_PASSWORD).", username)
    return username


def authenticate(identifier: str, password: str) -> tuple[bool, str]:
    """Return ``(ok, reason)``; ``identifier`` is an email *or* a username."""
    user = find_user_by_identifier(identifier)
    if user is None or not user.is_active:
        # Same message either way: do not reveal which identifiers exist.
        return False, "Invalid email or password."

    now = time.time()
    if _epoch(user.locked_until) > now:
        wait = int(_epoch(user.locked_until) - now)
        return False, f"Too many failed attempts. Try again in {wait}s."

    if not verify_password(password, user.password_hash):
        _register_failure(user)
        return False, "Invalid email or password."

    session = get_sessionmaker()()
    try:
        from datetime import datetime, timezone

        row = session.query(orm.OperatorUser).filter(orm.OperatorUser.id == user.id).one()
        row.last_login_at = datetime.now(timezone.utc)
        row.failed_attempts = 0
        row.locked_until = None
        session.commit()
    finally:
        session.close()
    return True, "ok"


def _register_failure(user: orm.OperatorUser) -> None:
    session = get_sessionmaker()()
    try:
        from datetime import datetime, timedelta, timezone

        row = session.query(orm.OperatorUser).filter(orm.OperatorUser.id == user.id).one()
        row.failed_attempts = (row.failed_attempts or 0) + 1
        if row.failed_attempts >= _LOCKOUT_AFTER:
            row.locked_until = datetime.now(timezone.utc) + timedelta(seconds=_LOCKOUT_SECONDS)
            logger.warning("Locked out operator '%s' after %d failed logins.", row.username, row.failed_attempts)
        session.commit()
    finally:
        session.close()


# --------------------------------------------------------------------------- guard


#: Endpoints that must stay reachable without a session.
PUBLIC_PATHS: tuple[str, ...] = (
    "/health",
    "/health/live",
    "/health/ready",
    "/health/memory",
    "/health/hindsight",
    "/health/llm",
    "/health/database",
    "/health/network",
    "/health/errors",
    "/api/auth/login",
    "/api/auth/register",
    "/api/auth/session",
    "/docs",
    "/redoc",
    "/openapi.json",
)


def is_public_path(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    return path.startswith("/docs") or path.startswith("/redoc")


def current_user(request: Request) -> str | None:
    """The authenticated username, or None. Never raises.

    A valid session cookie always wins, even when auth is not required. That way
    ``/api/auth/me`` reports the operator who actually signed in during local
    development, instead of a synthetic identity.
    """
    settings = get_settings()
    token = request.cookies.get(COOKIE_NAME, "")
    username = read_token(token, settings)
    if username is not None:
        user = find_user(username)
        if user is not None and user.is_active:
            return username
    if not settings.auth_required:
        # Auth disabled: report the configured operator so the UI still has an
        # identity to render, but do not demand a credential.
        return settings.auth_username or "local-operator"
    return None


def require_user(request: Request) -> str:
    """FastAPI dependency: 401 unless a valid session is present."""
    settings = get_settings()
    if not settings.auth_required:
        return settings.auth_username or "local-operator"
    username = current_user(request)
    if username is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sign in to use the RecallOps console.",
            headers={"WWW-Authenticate": "Session"},
        )
    return username


def auth_status() -> dict[str, Any]:
    settings = get_settings()
    return {
        "required": settings.auth_required,
        "configured": count_users() > 0,
        "registration_enabled": settings.auth_registration_enabled,
        "min_password_length": settings.auth_min_password_length,
        "cookie_secure": settings.auth_cookie_secure,
        "session_ttl_s": settings.auth_session_ttl_s,
    }


__all__ = [
    "COOKIE_NAME",
    "PUBLIC_PATHS",
    "UsernameTaken",
    "auth_status",
    "authenticate",
    "bootstrap_operator",
    "clear_session_cookie",
    "count_users",
    "create_user",
    "current_user",
    "find_user",
    "generate_password",
    "hash_password",
    "is_public_path",
    "issue_token",
    "read_token",
    "require_user",
    "set_session_cookie",
    "validate_registration",
    "verify_password",
]
