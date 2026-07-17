from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Path as ApiPath,
    Query,
    Request,
    UploadFile,
    status,
)

from app.cloud.dependencies import active_user
from app.cloud.repository import CloudRepository, User
from app.cloud.tmp_service import (
    TMP_REQUEST_FAILED,
    active_user_with_csrf,
    call_tmp,
    result_envelope,
    with_tmp_source,
)
from app.models import ServiceResult


IDENTIFIER_PATTERN = r"^[A-Za-z0-9_-]+$"
IdentifierPath = Annotated[
    str,
    ApiPath(min_length=1, max_length=128, pattern=IDENTIFIER_PATTERN),
]
UPLOAD_CHUNK_BYTES = 1024 * 1024
TMP_STORAGE_MODELS = {0, 1, 2}


router = APIRouter(prefix="/api", tags=["TMP.link files"])


def _repository(request: Request) -> CloudRepository:
    return request.app.state.repository


def _download_data(data: Any) -> tuple[str, str] | None:
    candidate = data[0] if isinstance(data, list) and data else data
    if not isinstance(candidate, dict):
        return None
    dkey = candidate.get("dkey") or candidate.get("direct_key")
    link = candidate.get("link") or candidate.get("url")
    if not isinstance(dkey, str) or not isinstance(link, str):
        return None
    if not dkey.strip() or not link.strip():
        return None
    return dkey.strip(), link.strip()


def _invalid_upload() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail="Request validation failed",
    )


@router.get("/quota")
async def quota(
    request: Request,
    user: User = Depends(active_user),
) -> dict[str, Any]:
    return result_envelope(
        await call_tmp(request, user, lambda client: client.quota())
    )


@router.get("/files")
async def files(
    request: Request,
    page: int = Query(default=1, ge=1),
    user: User = Depends(active_user),
) -> dict[str, Any]:
    result = await call_tmp(
        request,
        user,
        lambda client: client.list_files(page),
    )
    return result_envelope(result, data=with_tmp_source(result.data))


@router.post("/uploads")
async def upload(
    request: Request,
    file: UploadFile = File(...),
    model: int = Form(default=2),
    user: User = Depends(active_user_with_csrf),
) -> dict[str, Any]:
    if model not in TMP_STORAGE_MODELS:
        raise _invalid_upload()

    storage_path = request.app.state.config.storage_path
    assert storage_path is not None
    staging_path = storage_path / ".tmp-link-staging"
    staging_path.mkdir(parents=True, exist_ok=True)
    staged = tempfile.NamedTemporaryFile(
        mode="w+b",
        prefix="upload-",
        dir=staging_path,
        delete=False,
    )
    temporary_path = Path(staged.name)
    size = 0
    try:
        while chunk := await file.read(UPLOAD_CHUNK_BYTES):
            staged.write(chunk)
            size += len(chunk)
        if size == 0:
            raise _invalid_upload()
        staged.flush()
        staged.seek(0)
        result = await call_tmp(
            request,
            user,
            lambda client: client.upload_file(
                file.filename or "upload.bin",
                staged,
                model,
                file.content_type or "application/octet-stream",
            ),
        )
        return result_envelope(result, "File uploaded")
    finally:
        staged.close()
        temporary_path.unlink(missing_ok=True)


@router.post("/files/{ukey}/download")
async def download_file(
    ukey: IdentifierPath,
    request: Request,
    user: User = Depends(active_user_with_csrf),
) -> dict[str, Any]:
    repository = _repository(request)
    now = datetime.now(timezone.utc)
    reusable_after = now + timedelta(hours=1)
    cached = next(
        (
            link
            for link in repository.list_automatic_download_links(
                user.id,
                ukey=ukey,
                active_at=reusable_after,
            )
            if link.expires_at is not None and link.expires_at >= reusable_after
        ),
        None,
    )
    if cached is not None:
        return result_envelope(
            ServiceResult(
                ok=True,
                data={
                    "dkey": cached.dkey,
                    "link": cached.link,
                    "source": "tmp",
                },
            )
        )

    result = await call_tmp(
        request,
        user,
        lambda client: client.create_download_link(ukey),
    )
    extracted = _download_data(result.data)
    if extracted is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=TMP_REQUEST_FAILED,
        )
    dkey, link = extracted
    repository.save_automatic_download_link(
        user.id,
        ukey=ukey,
        dkey=dkey,
        link=link,
        expires_at=now + timedelta(hours=24),
    )
    data = {"dkey": dkey, "link": link, "source": "tmp"}
    return result_envelope(result, data=data)


@router.delete("/files/{ukey}")
async def delete_file(
    ukey: IdentifierPath,
    request: Request,
    user: User = Depends(active_user_with_csrf),
) -> dict[str, Any]:
    result = await call_tmp(
        request,
        user,
        lambda client: client.delete_file(ukey),
    )
    if result.ok:
        _repository(request).delete_automatic_download_links(user.id, ukey=ukey)
    return result_envelope(result, "File deleted")
