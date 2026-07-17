from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from app.cloud.dependencies import admin_user, verify_csrf
from app.cloud.repository import CloudRepository, Invitation, User


router = APIRouter(prefix="/api/admin", tags=["administration"])


class AdminUserResponse(BaseModel):
    id: str
    username: str
    role: str
    status: str
    must_change_password: bool
    created_at: datetime
    updated_at: datetime
    last_login_at: datetime | None
    storage_bytes: int


class AdminInvitationResponse(BaseModel):
    id: str
    code_hash: str
    created_by: str
    created_at: datetime
    expires_at: datetime | None
    used_by: str | None
    used_at: datetime | None
    status: Literal["available", "expired", "used"]


class CreateInvitationRequest(BaseModel):
    expires_at: datetime | None = None


class CreateInvitationResponse(BaseModel):
    invitation: AdminInvitationResponse
    code: str


class UpdateUserStatusRequest(BaseModel):
    status: Literal["active", "disabled"]


class ResetPasswordResponse(BaseModel):
    user: AdminUserResponse
    temporary_password: str


class MessageResponse(BaseModel):
    message: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _repository(request: Request) -> CloudRepository:
    return request.app.state.repository


def _admin_with_csrf(
    actor: User = Depends(admin_user),
    csrf_user: User = Depends(verify_csrf),
) -> User:
    return actor


def _user_response(repository: CloudRepository, user: User) -> AdminUserResponse:
    return AdminUserResponse(
        id=user.id,
        username=user.username,
        role=user.role,
        status=user.status,
        must_change_password=user.must_change_password,
        created_at=user.created_at,
        updated_at=user.updated_at,
        last_login_at=user.last_login_at,
        storage_bytes=repository.user_storage_bytes(user.id),
    )


def _invitation_response(
    invitation: Invitation, *, now: datetime
) -> AdminInvitationResponse:
    if invitation.used_by is not None:
        invitation_status = "used"
    elif invitation.expires_at is not None and invitation.expires_at <= now:
        invitation_status = "expired"
    else:
        invitation_status = "available"
    return AdminInvitationResponse(
        id=invitation.id,
        code_hash=invitation.code_hash,
        created_by=invitation.created_by,
        created_at=invitation.created_at,
        expires_at=invitation.expires_at,
        used_by=invitation.used_by,
        used_at=invitation.used_at,
        status=invitation_status,
    )


def _ordinary_user_or_error(repository: CloudRepository, user_id: str) -> User:
    target = repository.get_user(user_id)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    if target.role != "user":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Administrator accounts cannot be modified",
        )
    return target


@router.get("/users", response_model=list[AdminUserResponse])
def list_users(
    request: Request,
    actor: User = Depends(admin_user),
) -> list[AdminUserResponse]:
    repository = _repository(request)
    return [_user_response(repository, user) for user in repository.list_users()]


@router.patch("/users/{user_id}", response_model=AdminUserResponse)
def update_user_status(
    user_id: str,
    payload: UpdateUserStatusRequest,
    request: Request,
    actor: User = Depends(_admin_with_csrf),
) -> AdminUserResponse:
    repository = _repository(request)
    _ordinary_user_or_error(repository, user_id)
    updated = repository.set_user_status_and_revoke_sessions(user_id, payload.status)
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    return _user_response(repository, updated)


@router.post(
    "/users/{user_id}/reset-password",
    response_model=ResetPasswordResponse,
)
def reset_user_password(
    user_id: str,
    request: Request,
    actor: User = Depends(_admin_with_csrf),
) -> ResetPasswordResponse:
    repository = _repository(request)
    _ordinary_user_or_error(repository, user_id)
    temporary_password = secrets.token_urlsafe(32)
    password_hash = request.app.state.password_service.hash(temporary_password)
    updated = repository.reset_user_password_and_revoke_sessions(
        user_id,
        password_hash,
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    return ResetPasswordResponse(
        user=_user_response(repository, updated),
        temporary_password=temporary_password,
    )


@router.get("/invitations", response_model=list[AdminInvitationResponse])
def list_invitations(
    request: Request,
    actor: User = Depends(admin_user),
) -> list[AdminInvitationResponse]:
    now = _now()
    return [
        _invitation_response(invitation, now=now)
        for invitation in _repository(request).list_invitations()
    ]


@router.post(
    "/invitations",
    response_model=CreateInvitationResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_invitation(
    payload: CreateInvitationRequest,
    request: Request,
    actor: User = Depends(_admin_with_csrf),
) -> CreateInvitationResponse:
    now = _now()
    if payload.expires_at is not None:
        if payload.expires_at.tzinfo is None or payload.expires_at <= now:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Invitation details are invalid",
            )
    code = secrets.token_urlsafe(32)
    invitation = _repository(request).create_invitation(
        created_by=actor.id,
        code=code,
        expires_at=payload.expires_at,
        now=now,
    )
    return CreateInvitationResponse(
        invitation=_invitation_response(invitation, now=now),
        code=code,
    )


@router.delete("/invitations/{invitation_id}", response_model=MessageResponse)
def revoke_invitation(
    invitation_id: str,
    request: Request,
    actor: User = Depends(_admin_with_csrf),
) -> MessageResponse:
    result = _repository(request).revoke_unused_invitation(invitation_id)
    if result == "not_found":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invitation not found",
        )
    if result == "used":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Invitation cannot be revoked",
        )
    return MessageResponse(message="Invitation revoked")
