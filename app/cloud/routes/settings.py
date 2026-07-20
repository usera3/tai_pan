from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from app.cloud.dependencies import active_user
from app.cloud.repository import (
    DEFAULT_CUSTOM_DOMAIN,
    CloudRepository,
    User,
    UserSettings,
)
from app.cloud.tmp_service import active_user_with_csrf, call_tmp, result_envelope
from app.config import validate_domain


router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: str = ""
    custom_domain: str


def _repository(request: Request) -> CloudRepository:
    return request.app.state.repository


def _public_settings(settings: UserSettings | None) -> dict[str, bool | str]:
    if settings is None:
        return {
            "key_configured": False,
            "custom_domain": DEFAULT_CUSTOM_DOMAIN,
        }
    return {
        "key_configured": settings.key_configured,
        "custom_domain": settings.custom_domain,
    }


@router.get("")
def get_settings(
    request: Request,
    user: User = Depends(active_user),
) -> dict[str, bool | str]:
    return _public_settings(_repository(request).get_user_settings(user.id))


@router.put("")
def put_settings(
    payload: SettingsUpdate,
    request: Request,
    user: User = Depends(active_user_with_csrf),
) -> dict[str, bool | str]:
    try:
        custom_domain = validate_domain(payload.custom_domain)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Request validation failed",
        ) from None
    normalized_key = payload.api_key.strip()
    settings = _repository(request).save_user_settings(
        user.id,
        tmp_key=normalized_key or None,
        custom_domain=custom_domain,
    )
    return _public_settings(settings)


@router.delete("/key")
def clear_settings_key(
    request: Request,
    user: User = Depends(active_user_with_csrf),
) -> dict[str, bool | str]:
    repository = _repository(request)
    settings = repository.clear_tmp_key(user.id)
    return _public_settings(settings or repository.get_user_settings(user.id))


@router.post("/test")
async def test_settings(
    request: Request,
    user: User = Depends(active_user_with_csrf),
) -> dict:
    result = await call_tmp(request, user, lambda client: client.quota())
    return result_envelope(result, "Connection successful")
