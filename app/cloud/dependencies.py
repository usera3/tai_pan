from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Request, status

from app.cloud.repository import CloudRepository, User
from app.cloud.security import hash_secret


AUTHENTICATION_REQUIRED = "Authentication required"


def _repository(request: Request) -> CloudRepository:
    return request.app.state.repository


def current_user(
    request: Request,
    repository: CloudRepository = Depends(_repository),
) -> User:
    token = request.cookies.get("session")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=AUTHENTICATION_REQUIRED,
        )
    session = repository.get_active_session_by_token(token)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=AUTHENTICATION_REQUIRED,
        )
    user = repository.get_user(session.user_id)
    if user is None or user.status != "active":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=AUTHENTICATION_REQUIRED,
        )
    request.state.cloud_session = session
    return user


def active_user(user: User = Depends(current_user)) -> User:
    if user.must_change_password:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Password change required",
        )
    return user


def admin_user(user: User = Depends(active_user)) -> User:
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator access required",
        )
    return user


def verify_csrf(
    request: Request,
    user: User = Depends(current_user),
) -> User:
    expected_origin = request.app.state.config.public_origin
    supplied_origin = request.headers.get("origin")
    supplied_token = request.headers.get("x-csrf-token")
    session = request.state.cloud_session
    valid_token = bool(supplied_token) and hmac.compare_digest(
        hash_secret(supplied_token), session.csrf_hash
    )
    if supplied_origin != expected_origin or not valid_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF validation failed",
        )
    return user
