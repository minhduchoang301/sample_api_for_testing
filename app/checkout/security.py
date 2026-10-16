"""Password hashing, JWT issuing and the auth dependencies."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.checkout import clock
from app.checkout.config import settings
from app.checkout.errors import ApiError

_ITERATIONS = 20_000
_ALGORITHM = "HS256"

bearer_scheme = HTTPBearer(auto_error=False, description="Token from POST /auth/token")


def hash_password(password: str) -> str:
    salt = secrets.token_hex(8)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _ITERATIONS)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    salt, _, expected = stored.partition("$")
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _ITERATIONS)
    return hmac.compare_digest(digest.hex(), expected)


def issue_token(username: str, role: str) -> str:
    issued = clock.now()
    payload = {
        "sub": username,
        "role": role,
        "iat": int(issued.timestamp()),
        "exp": int((issued + timedelta(minutes=settings.jwt_ttl_minutes)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=_ALGORITHM)


@dataclass(frozen=True)
class Principal:
    username: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def _unauthorized(message: str) -> ApiError:
    return ApiError(401, "unauthorized", message, headers={"WWW-Authenticate": "Bearer"})


async def current_user(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> Principal:
    if creds is None or creds.scheme.lower() != "bearer" or not creds.credentials:
        raise _unauthorized("Missing bearer token")
    try:
        claims = jwt.decode(
            creds.credentials,
            settings.jwt_secret,
            algorithms=[_ALGORITHM],
            options={"verify_exp": False, "verify_iat": False, "require": ["sub", "role", "exp"]},
        )
    except jwt.PyJWTError as exc:
        raise _unauthorized("Invalid token") from exc
    if int(claims["exp"]) <= int(clock.now().timestamp()):
        raise _unauthorized("Token expired")
    role = str(claims["role"])
    if role not in {"customer", "admin"}:
        raise _unauthorized("Invalid token")
    return Principal(username=str(claims["sub"]), role=role)


async def require_admin(user: Annotated[Principal, Depends(current_user)]) -> Principal:
    if not user.is_admin:
        raise ApiError(403, "forbidden", "Admin role required")
    return user


CurrentUser = Annotated[Principal, Depends(current_user)]
AdminUser = Annotated[Principal, Depends(require_admin)]
