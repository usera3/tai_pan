from __future__ import annotations

import asyncio
import tempfile
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import uuid4

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
from app.cloud.routes.cloud_files import (
    delete_cloud_file,
    download_cloud_file,
    list_cloud_file_data,
    upload_cloud_file,
)
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
DOWNLOAD_CLAIM_LIFETIME = timedelta(seconds=45)
DOWNLOAD_CLAIM_RENEW_SECONDS = 15.0
DOWNLOAD_CLAIM_WAIT_SECONDS = 50.0
DOWNLOAD_CLAIM_POLL_SECONDS = 0.05


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


def _upload_basename(filename: str | None) -> str:
    basename = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    basename = "".join(
        "_" if ord(character) < 32 or ord(character) == 127 else character
        for character in basename
    )
    return basename if basename not in {"", ".", ".."} else "upload.bin"


async def _renew_download_claim(
    repository: CloudRepository,
    user_id: str,
    ukey: str,
    claim_token: str,
) -> None:
    while True:
        await asyncio.sleep(DOWNLOAD_CLAIM_RENEW_SECONDS)
        now = datetime.now(timezone.utc)
        if not repository.renew_automatic_download_claim(
            user_id,
            ukey=ukey,
            claim_token=claim_token,
            expires_at=now + DOWNLOAD_CLAIM_LIFETIME,
            now=now,
        ):
            return


def _invalid_upload() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail="Request validation failed",
    )


def _upload_too_large() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        detail="Upload is too large",
    )


def _reusable_download_link(
    repository: CloudRepository,
    user_id: str,
    ukey: str,
    now: datetime,
):
    reusable_after = now + timedelta(hours=1)
    return next(
        (
            link
            for link in repository.list_automatic_download_links(
                user_id,
                ukey=ukey,
                active_at=reusable_after,
            )
            if link.expires_at is not None and link.expires_at >= reusable_after
        ),
        None,
    )


def _download_response(link) -> dict[str, Any]:
    return result_envelope(
        ServiceResult(
            ok=True,
            data={
                "dkey": link.dkey,
                "link": link.link,
                "source": "tmp",
            },
        )
    )


def _flat_tmp_file_data(data: Any) -> list[dict[str, Any]]:
    items = data
    if isinstance(data, dict):
        items = data.get("data", data.get("list", []))
    if not isinstance(items, list):
        return []
    return [
        with_tmp_source(item)
        for item in items
        if isinstance(item, dict)
    ]


@router.get("/quota")
async def quota(
    request: Request,
    user: User = Depends(active_user),
) -> dict[str, Any]:
    return result_envelope(
        await call_tmp(request, user, lambda client: client.quota())
    )


@router.get("/cloud/quota")
async def cloud_quota(
    request: Request,
    user: User = Depends(active_user),
) -> dict[str, Any]:
    return {
        "ok": True,
        "data": {
            "used": _repository(request).user_storage_bytes(user.id),
            "total": request.app.state.config.user_quota_bytes,
        },
        "message": "",
    }


@router.get("/files")
async def files(
    request: Request,
    page: int = Query(default=1, ge=1),
    source: Literal["tmp", "cloud", "all"] = Query(default="tmp"),
    user: User = Depends(active_user),
) -> dict[str, Any]:
    if source == "cloud":
        return {
            "ok": True,
            "data": list_cloud_file_data(request, user),
            "message": "",
        }
    if source == "all":
        cloud_data = list_cloud_file_data(request, user)
        if _repository(request).get_tmp_key(user.id) is None:
            return {"ok": True, "data": cloud_data, "message": ""}
    result = await call_tmp(
        request,
        user,
        lambda client: client.list_files(page),
    )
    if source == "all":
        return result_envelope(
            result,
            data=[*_flat_tmp_file_data(result.data), *cloud_data],
        )
    return result_envelope(result, data=with_tmp_source(result.data))


@router.post("/uploads")
async def upload(
    request: Request,
    file: UploadFile = File(...),
    model: int = Form(default=1),
    storage: Literal["tmp", "cloud"] = Form(default="tmp"),
    user: User = Depends(active_user_with_csrf),
) -> dict[str, Any]:
    if storage == "cloud":
        return await upload_cloud_file(request, user, file)
    if model not in TMP_STORAGE_MODELS:
        raise _invalid_upload()

    storage_path = request.app.state.config.storage_path
    assert storage_path is not None
    staging_path = storage_path / ".tmp-link-staging"
    staging_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging_path.chmod(0o700)
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
            size += len(chunk)
            if size > request.app.state.config.max_file_bytes:
                raise _upload_too_large()
            staged.write(chunk)
        if size == 0:
            raise _invalid_upload()
        staged.flush()
        staged.seek(0)
        result = await call_tmp(
            request,
            user,
            lambda client: client.upload_file(
                _upload_basename(file.filename),
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
    source: Literal["tmp", "cloud"] = Query(default="tmp"),
    user: User = Depends(active_user_with_csrf),
) -> Any:
    if source == "cloud":
        return download_cloud_file(request, user, ukey)
    repository = _repository(request)
    claim_token = str(uuid4())
    wait_deadline = asyncio.get_running_loop().time() + DOWNLOAD_CLAIM_WAIT_SECONDS

    while True:
        now = datetime.now(timezone.utc)
        cached = _reusable_download_link(repository, user.id, ukey, now)
        if cached is not None:
            return _download_response(cached)

        claimed = repository.try_claim_automatic_download(
            user.id,
            ukey=ukey,
            claim_token=claim_token,
            expires_at=now + DOWNLOAD_CLAIM_LIFETIME,
            now=now,
        )
        if claimed:
            heartbeat = asyncio.create_task(
                _renew_download_claim(repository, user.id, ukey, claim_token)
            )
            try:
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
                dkey, direct_link = extracted
                saved = repository.complete_automatic_download_claim(
                    user.id,
                    ukey=ukey,
                    claim_token=claim_token,
                    dkey=dkey,
                    link=direct_link,
                    expires_at=now + timedelta(hours=24),
                )
                if saved is not None:
                    return result_envelope(
                        result,
                        data={
                            "dkey": saved.dkey,
                            "link": saved.link,
                            "source": "tmp",
                        },
                    )
            except BaseException:
                repository.release_automatic_download_claim(
                    user.id,
                    ukey=ukey,
                    claim_token=claim_token,
                )
                raise
            finally:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat

        if asyncio.get_running_loop().time() >= wait_deadline:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Download link is temporarily unavailable",
            )
        await asyncio.sleep(DOWNLOAD_CLAIM_POLL_SECONDS)


@router.get("/files/{ukey}/download")
async def download_permanent_file(
    ukey: IdentifierPath,
    request: Request,
    source: Literal["cloud"] = Query(default="cloud"),
    user: User = Depends(active_user),
) -> Any:
    return download_cloud_file(request, user, ukey)


@router.head("/files/{ukey}/download")
async def preflight_permanent_download(
    ukey: IdentifierPath,
    request: Request,
    source: Literal["cloud"] = Query(default="cloud"),
    user: User = Depends(active_user),
) -> Any:
    return download_cloud_file(request, user, ukey)


@router.delete("/files/{ukey}")
async def delete_file(
    ukey: IdentifierPath,
    request: Request,
    source: Literal["tmp", "cloud"] = Query(default="tmp"),
    user: User = Depends(active_user_with_csrf),
) -> dict[str, Any]:
    if source == "cloud":
        return delete_cloud_file(request, user, ukey)
    result = await call_tmp(
        request,
        user,
        lambda client: client.delete_file(ukey),
    )
    if result.ok:
        _repository(request).delete_automatic_download_links(user.id, ukey=ukey)
    return result_envelope(result, "File deleted")
