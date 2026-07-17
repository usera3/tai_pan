from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.cloud.dependencies import active_user
from app.cloud.repository import CloudRepository, User
from app.cloud.routes.tmp_files import IDENTIFIER_PATTERN
from app.cloud.tmp_service import (
    active_user_with_csrf,
    call_tmp,
    result_envelope,
    with_tmp_source,
)


IdentifierPath = Annotated[
    str,
    Path(min_length=1, max_length=128, pattern=IDENTIFIER_PATTERN),
]


router = APIRouter(prefix="/api/links", tags=["TMP.link links"])


class LinkCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ukey: str = Field(min_length=1, max_length=128, pattern=IDENTIFIER_PATTERN)
    valid_time: int | None = Field(default=None, ge=1)
    download_limit: int | None = Field(default=None, ge=1)


def _repository(request: Request) -> CloudRepository:
    return request.app.state.repository


def _link_identity(item: Any) -> str | None:
    if not isinstance(item, dict):
        return None
    for key in ("dkey", "direct_key"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _has_link_url(item: dict[str, Any]) -> bool:
    return any(
        isinstance(item.get(key), str) and item[key].strip()
        for key in ("link", "url")
    )


def _normalize_links(data: Any, hidden_dkeys: set[str]) -> Any:
    wrapper_key = None
    items = data
    if isinstance(data, dict):
        for key in ("data", "list"):
            if isinstance(data.get(key), list):
                wrapper_key = key
                items = data[key]
                break

    if not isinstance(items, list):
        return data

    normalized = []
    for item in items:
        dkey = _link_identity(item)
        if dkey is None or not isinstance(item, dict) or not _has_link_url(item):
            continue
        if dkey not in hidden_dkeys:
            normalized.append(with_tmp_source(item))
    if wrapper_key is None:
        return normalized
    return {**data, wrapper_key: normalized}


@router.get("")
async def links(
    request: Request,
    page: int = Query(default=1, ge=1),
    user: User = Depends(active_user),
) -> dict[str, Any]:
    result = await call_tmp(
        request,
        user,
        lambda client: client.list_links(page),
    )
    hidden_dkeys = {
        link.dkey
        for link in _repository(request).list_automatic_download_links(user.id)
    }
    data = _normalize_links(result.data, hidden_dkeys)
    return result_envelope(result, data=data)


@router.post("")
async def create_link(
    payload: LinkCreate,
    request: Request,
    user: User = Depends(active_user_with_csrf),
) -> dict[str, Any]:
    result = await call_tmp(
        request,
        user,
        lambda client: client.create_link(
            payload.ukey,
            valid_time=payload.valid_time,
            download_limit=payload.download_limit,
        ),
    )
    return result_envelope(
        result,
        "Direct link created",
        data=with_tmp_source(result.data),
    )


@router.delete("/{dkey}")
async def delete_link(
    dkey: IdentifierPath,
    request: Request,
    delete_file: bool = Query(default=False),
    user: User = Depends(active_user_with_csrf),
) -> dict[str, Any]:
    result = await call_tmp(
        request,
        user,
        lambda client: client.delete_link(dkey, delete_file=delete_file),
    )
    if result.ok:
        _repository(request).delete_automatic_download_link(user.id, dkey)
    return result_envelope(result, "Direct link deleted")
