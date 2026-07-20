"""
auth.py – Authentication utilities and FastAPI request dependencies.

Strategy
--------
The app supports two token delivery mechanisms simultaneously:

  1. Authorization: Bearer <token>   — for direct API / curl / scripted access.
  2. HttpOnly cookie 'access_token'  — set by POST /login; used by the browser UI.

``get_current_user`` checks both; the Authorization header takes precedence.
Using ``auto_error=False`` on OAuth2PasswordBearer prevents FastAPI from
automatically rejecting requests with no Bearer header – we perform the
cookie fallback check ourselves and raise a clean 401 when both are absent.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt as _bcrypt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlmodel import Session, select

from app.config import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    ALGORITHM,
    MAX_PASSWORD_BYTES,
    MIN_PASSWORD_LENGTH,
    SECRET_KEY,
)
from app.database import get_session
from app.models import User

# ── Primitives ────────────────────────────────────────────────────────────────

# auto_error=False: do NOT raise 401 automatically when no Bearer header is
# present; we handle the cookie fallback before raising our own exception.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/token", auto_error=False)

# Reusable exception to avoid repeating header kwargs everywhere.
_CREDENTIALS_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


# ── Password helpers ──────────────────────────────────────────────────────────

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Return True if plain_password matches the stored bcrypt hash."""
    return _bcrypt.checkpw(
        plain_password.encode("utf-8"),
        hashed_password.encode("utf-8"),
    )


def get_password_hash(password: str) -> str:
    """Return a bcrypt hash of password (cost factor 12)."""
    return _bcrypt.hashpw(
        password.encode("utf-8"),
        _bcrypt.gensalt(rounds=12),
    ).decode("utf-8")


def validate_password_policy(password: str) -> Optional[str]:
    """Return a Czech error string if the password is unacceptable, else None.

    Single source of truth for the rules enforced at every place a password is
    set (bootstrap, admin-created users, self-service change).
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Heslo musí mít alespoň {MIN_PASSWORD_LENGTH} znaků."
    # bcrypt only considers the first 72 bytes; anything longer is silently
    # ignored, so reject it rather than pretend it strengthens the hash.
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        return f"Heslo je příliš dlouhé (max {MAX_PASSWORD_BYTES} bajtů)."
    return None


# ── JWT helpers ───────────────────────────────────────────────────────────────

def create_access_token_for_user(
    user: User,
    expires_delta: Optional[timedelta] = None,
) -> str:
    """Mint a JWT for ``user`` embedding both the 'sub' and 'ver' claims.

    The 'ver' claim pins the token to the user's current ``token_version``;
    bumping that value (on password change or disable) invalidates the token.
    """
    return create_access_token(
        data={"sub": user.username, "ver": user.token_version},
        expires_delta=expires_delta,
    )


def create_access_token(
    data: dict,
    expires_delta: Optional[timedelta] = None,
) -> str:
    """Return a signed JWT containing ``data`` plus an 'exp' claim."""
    payload = data.copy()
    expire = datetime.now(tz=timezone.utc) + (
        expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    payload["exp"] = expire
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    """Validate a JWT's signature/expiry and return its payload, or raise 401."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise _CREDENTIALS_EXCEPTION
    if not payload.get("sub"):
        raise _CREDENTIALS_EXCEPTION
    return payload


def resolve_user_from_token(session: Session, token: str) -> Optional[User]:
    """Return the active, current-version User for a token, or None if invalid.

    Centralises the checks shared by the API and UI auth dependencies:
    valid signature, user exists, account active, and token version current.
    Returns None (rather than raising) so each caller can choose its own
    rejection style (401 JSON vs 307 redirect).
    """
    try:
        payload = decode_token(token)
    except HTTPException:
        return None
    user: Optional[User] = session.exec(
        select(User).where(User.username == payload.get("sub"))
    ).first()
    if user is None or not user.is_active:
        return None
    if payload.get("ver") != user.token_version:
        return None
    return user


# ── FastAPI dependency ────────────────────────────────────────────────────────

def get_current_user(
    request: Request,
    token_from_header: Optional[str] = Depends(oauth2_scheme),
    session: Session = Depends(get_session),
) -> User:
    """Resolve the authenticated User from a Bearer token or HttpOnly cookie.

    Raises HTTP 401 when neither source provides a valid token.
    """
    token = token_from_header or request.cookies.get("access_token")
    if not token:
        raise _CREDENTIALS_EXCEPTION

    user = resolve_user_from_token(session, token)
    if user is None:
        raise _CREDENTIALS_EXCEPTION

    return user
