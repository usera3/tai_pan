from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.cloud.repository import User


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterRequest(StrictRequest):
    username: str
    password: str = Field(repr=False)
    invitation_code: str = Field(repr=False)


class LoginRequest(StrictRequest):
    username: str
    password: str = Field(repr=False)


class ChangePasswordRequest(StrictRequest):
    current_password: str = Field(repr=False)
    new_password: str = Field(repr=False)


class PublicUser(BaseModel):
    id: str
    username: str
    role: str
    must_change_password: bool

    @classmethod
    def from_user(cls, user: User) -> PublicUser:
        return cls(
            id=user.id,
            username=user.username,
            role=user.role,
            must_change_password=user.must_change_password,
        )


class AuthResponse(BaseModel):
    user: PublicUser
    csrf_token: str = Field(repr=False)


class MeResponse(BaseModel):
    user: PublicUser
    csrf_token: str = Field(repr=False)


class MessageResponse(BaseModel):
    message: str
