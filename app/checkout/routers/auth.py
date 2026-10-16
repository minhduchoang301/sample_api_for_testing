from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.checkout.db import User, get_db
from app.checkout.errors import ApiError
from app.checkout.schemas import TokenRequest, TokenResponse
from app.checkout.security import issue_token, verify_password

router = APIRouter(tags=["auth"])


@router.post("/auth/token", response_model=TokenResponse)
async def login(body: TokenRequest, db: Annotated[Session, Depends(get_db)]) -> TokenResponse:
    user = db.scalars(select(User).where(User.username == body.username)).one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        raise ApiError(401, "invalid_credentials", "Invalid username or password")
    return TokenResponse(access_token=issue_token(user.username, user.role), role=user.role)
