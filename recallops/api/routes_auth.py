"""Sign-up, sign-in, sign-out and session introspection.

Kept in one file so the credential surface is auditable: what it accepts, what it
returns, and what it deliberately never reveals. Every failure path returns a
short, user-facing sentence - never a stack trace, a SQL string or a hash.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from recallops.config import get_settings
from recallops.services import auth

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    # Named `email` because that is what the form shows, but it accepts either an
    # email address or a username so accounts predating email still work.
    email: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=256)


class RegisterRequest(BaseModel):
    full_name: str = Field(default="", max_length=120)
    email: str = Field(min_length=3, max_length=255)
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


def _public_user(username: str) -> dict[str, Any]:
    """The only shape of a user this API ever returns. No hash, no id."""
    user = auth.find_user(username)
    return {
        "username": user.username if user else username,
        "email": user.email if user else None,
        "full_name": (user.display_name if user else "") or username,
    }


@router.post("/register", status_code=201)
def register(payload: RegisterRequest, response: Response) -> dict[str, Any]:
    """Create an operator account and sign the new operator straight in.

    Gated by ``AUTH_REGISTRATION_ENABLED``. With sign-up on, whoever can reach
    this endpoint can operate the console, so a private deployment should turn it
    off once its real accounts exist.
    """
    ok, reason = auth.validate_registration(
        username=payload.username,
        password=payload.password,
        display_name=payload.full_name,
        email=payload.email,
    )
    if not ok:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=reason)

    try:
        auth.create_user(
            username=payload.username,
            password=payload.password,
            display_name=payload.full_name,
            email=payload.email,
        )
    except auth.EmailTaken:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="An account with that email already exists.") from None
    except auth.UsernameTaken:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="That username is already taken.") from None

    settings = get_settings()
    token = auth.issue_token(payload.username, settings.auth_session_ttl_s, settings)
    auth.set_session_cookie(response, token, settings)
    return {
        "authenticated": True,
        "user": _public_user(payload.username),
        "expires_in_s": settings.auth_session_ttl_s,
    }


@router.post("/login")
def login(payload: LoginRequest, response: Response) -> dict[str, Any]:
    """Exchange credentials for an HttpOnly session cookie."""
    ok, reason = auth.authenticate(payload.email, payload.password)
    if not ok:
        # 401 for wrong credentials, 429 while a lockout is in force.
        code = status.HTTP_429_TOO_MANY_REQUESTS if "Too many" in reason else status.HTTP_401_UNAUTHORIZED
        raise HTTPException(status_code=code, detail=reason)

    settings = get_settings()
    user = auth.find_user_by_identifier(payload.email)
    username = user.username if user else payload.email
    token = auth.issue_token(username, settings.auth_session_ttl_s, settings)
    auth.set_session_cookie(response, token, settings)
    return {
        "authenticated": True,
        "user": _public_user(username),
        "expires_in_s": settings.auth_session_ttl_s,
    }


@router.post("/logout")
def logout(response: Response) -> dict[str, Any]:
    auth.clear_session_cookie(response, get_settings())
    return {"authenticated": False}


@router.get("/me")
def me(request: Request) -> dict[str, Any]:
    """The signed-in operator, or 401. Never returns a hash or an internal id."""
    username = auth.current_user(request)
    if username is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not signed in.")
    return {"authenticated": True, "user": _public_user(username)}


@router.get("/session")
def session(request: Request) -> dict[str, Any]:
    """Who am I, and is a session required at all?

    Public on purpose: the login page needs to know whether to render itself, and
    the answer contains no secrets.
    """
    settings = get_settings()
    username = auth.current_user(request)
    return {
        "auth_required": settings.auth_required,
        "authenticated": username is not None,
        "username": username,
        "user": _public_user(username) if username else None,
        "operators_configured": auth.count_users() > 0,
        "registration_enabled": settings.auth_registration_enabled,
        "min_password_length": settings.auth_min_password_length,
    }


__all__ = ["router"]
