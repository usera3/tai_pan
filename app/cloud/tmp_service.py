from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Depends, HTTPException, Request, status

from app.client import TmpLinkError
from app.cloud.dependencies import active_user, verify_csrf
from app.cloud.repository import User
from app.models import ServiceResult


TMP_REQUEST_FAILED = "TMP.link request failed"
TMP_KEY_NOT_CONFIGURED = "TMP.link API key is not configured"


def active_user_with_csrf(
    user: User = Depends(active_user),
    _csrf_user: User = Depends(verify_csrf),
) -> User:
    return user


def _tmp_client(request: Request, user: User) -> Any:
    try:
        api_key = request.app.state.repository.get_tmp_key(user.id)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=TMP_REQUEST_FAILED,
        ) from None
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=TMP_KEY_NOT_CONFIGURED,
        )
    try:
        return request.app.state.tmp_client_factory(api_key)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=TMP_REQUEST_FAILED,
        ) from None


async def call_tmp(
    request: Request,
    user: User,
    operation: Callable[[Any], Awaitable[ServiceResult]],
) -> ServiceResult:
    client = _tmp_client(request, user)
    try:
        result = await operation(client)
    except HTTPException:
        raise
    except TmpLinkError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=TMP_REQUEST_FAILED,
        ) from None
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=TMP_REQUEST_FAILED,
        ) from None
    if not isinstance(result, ServiceResult):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=TMP_REQUEST_FAILED,
        )
    return result


def result_envelope(
    result: ServiceResult,
    default_message: str = "",
    *,
    data: Any | None = None,
) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "data": result.data if data is None else data,
        "message": result.message or default_message,
    }


def with_tmp_source(data: Any) -> Any:
    if isinstance(data, dict):
        return {**data, "source": "tmp"}
    if isinstance(data, list):
        return [with_tmp_source(item) for item in data]
    return data


__all__ = [
    "active_user_with_csrf",
    "call_tmp",
    "result_envelope",
    "with_tmp_source",
]
