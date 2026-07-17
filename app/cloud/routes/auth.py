from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.cloud.dependencies import current_user, verify_csrf
from app.cloud.repository import CloudRepository, User, normalize_username
from app.cloud.schemas import (
    AuthResponse,
    ChangePasswordRequest,
    LoginRequest,
    MeResponse,
    MessageResponse,
    PublicUser,
    RegisterRequest,
)


SESSION_LIFETIME = timedelta(days=7)
LOGIN_WINDOW = timedelta(minutes=15)
LOGIN_FAILURE_LIMIT = 5
REGISTRATION_WINDOW = timedelta(hours=1)
REGISTRATION_LIMIT = 10
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024
GENERIC_LOGIN_ERROR = "Invalid username or password"


router = APIRouter(prefix="/api/auth", tags=["authentication"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _remote_addr(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


def _repository(request: Request) -> CloudRepository:
    return request.app.state.repository


def _validate_new_password(password: str) -> None:
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Password does not meet requirements",
        )


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key="session",
        value=token,
        max_age=int(SESSION_LIFETIME.total_seconds()),
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key="session",
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def _start_session(request: Request, response: Response, user: User) -> AuthResponse:
    token_service = request.app.state.token_service
    session_token = token_service.generate_session_token()
    csrf_token = token_service.generate_csrf_token()
    now = _now()
    _repository(request).create_session(
        user.id,
        token=session_token,
        csrf_token=csrf_token,
        expires_at=now + SESSION_LIFETIME,
        now=now,
    )
    _set_session_cookie(response, session_token)
    return AuthResponse(user=PublicUser.from_user(user), csrf_token=csrf_token)


@router.post(
    "/register",
    response_model=AuthResponse,
    status_code=status.HTTP_201_CREATED,
)
def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
) -> AuthResponse:
    repository = _repository(request)
    now = _now()
    remote_addr = _remote_addr(request)
    if repository.count_registration_attempts(
        since=now - REGISTRATION_WINDOW, remote_addr=remote_addr
    ) >= REGISTRATION_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many registration attempts",
        )

    succeeded = False
    try:
        normalized_username = normalize_username(payload.username)
        _validate_new_password(payload.password)
        password_hash = request.app.state.password_service.hash(payload.password)
        user = repository.register_user_with_invitation(
            username=normalized_username,
            password_hash=password_hash,
            invitation_code=payload.invitation_code,
            now=now,
        )
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Registration could not be completed",
            )
        succeeded = True
        return _start_session(request, response, user)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Registration details are invalid",
        ) from None
    except sqlite3.IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username is unavailable",
        ) from None
    finally:
        repository.record_auth_attempt(
            username=None,
            remote_addr=remote_addr,
            successful=succeeded,
            now=now,
        )


@router.post("/login", response_model=AuthResponse)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
) -> AuthResponse:
    repository = _repository(request)
    now = _now()
    remote_addr = _remote_addr(request)
    since = now - LOGIN_WINDOW
    if (
        repository.count_failed_auth_attempts(
            since=since, username=payload.username
        )
        >= LOGIN_FAILURE_LIMIT
        or repository.count_failed_auth_attempts(
            since=since, remote_addr=remote_addr
        )
        >= LOGIN_FAILURE_LIMIT
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts",
        )

    try:
        user = repository.get_user_by_username(payload.username)
    except ValueError:
        user = None
    password_hash = (
        user.password_hash if user is not None else request.app.state.dummy_password_hash
    )
    verified = request.app.state.password_service.verify(
        password_hash, payload.password
    )
    accepted = bool(verified and user is not None and user.status == "active")
    repository.record_auth_attempt(
        username=payload.username,
        remote_addr=remote_addr,
        successful=accepted,
        now=now,
    )
    if not accepted or user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=GENERIC_LOGIN_ERROR,
        )

    user = repository.record_login(user.id, now=now)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=GENERIC_LOGIN_ERROR,
        )
    return _start_session(request, response, user)


@router.get("/me", response_model=MeResponse)
def me(user: User = Depends(current_user)) -> MeResponse:
    return MeResponse(user=PublicUser.from_user(user))


@router.post(
    "/logout",
    response_model=MessageResponse,
    dependencies=[Depends(verify_csrf)],
)
def logout(
    request: Request,
    response: Response,
    user: User = Depends(current_user),
) -> MessageResponse:
    session = request.state.cloud_session
    _repository(request).revoke_session(user.id, session.id)
    _clear_session_cookie(response)
    return MessageResponse(message="Logged out")


@router.post(
    "/change-password",
    response_model=AuthResponse,
    dependencies=[Depends(verify_csrf)],
)
def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    response: Response,
    user: User = Depends(current_user),
) -> AuthResponse:
    if not request.app.state.password_service.verify(
        user.password_hash, payload.current_password
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password could not be changed",
        )
    _validate_new_password(payload.new_password)
    password_hash = request.app.state.password_service.hash(payload.new_password)
    updated = _repository(request).update_password_and_revoke_sessions(
        user.id, password_hash
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    return _start_session(request, response, updated)
