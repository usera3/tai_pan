from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request, Response, UploadFile, status

from app.cloud.repository import CloudFile, User
from app.cloud.storage import (
    EmptyUpload,
    FileTooLarge,
    InsufficientStorage,
    StorageForbidden,
    StorageQuotaExceeded,
)


def cloud_file_data(cloud_file: CloudFile) -> dict[str, Any]:
    return {
        "id": cloud_file.id,
        "name": cloud_file.original_name,
        "content_type": cloud_file.content_type,
        "size": cloud_file.size_bytes,
        "sha256": cloud_file.sha256,
        "source": "cloud",
    }


def list_cloud_file_data(request: Request, user: User) -> list[dict[str, Any]]:
    return [
        cloud_file_data(cloud_file)
        for cloud_file in request.app.state.repository.list_cloud_files(user.id)
    ]


async def upload_cloud_file(
    request: Request, user: User, upload: UploadFile
) -> dict[str, Any]:
    try:
        cloud_file = await request.app.state.cloud_storage.store(user.id, upload)
    except EmptyUpload:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Request validation failed",
        ) from None
    except FileTooLarge:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Upload is too large",
        ) from None
    except (StorageQuotaExceeded, InsufficientStorage):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cloud storage quota is unavailable",
        ) from None
    return {
        "ok": True,
        "data": cloud_file_data(cloud_file),
        "message": "File uploaded",
    }


def download_cloud_file(request: Request, user: User, file_id: str) -> Response:
    try:
        download = request.app.state.cloud_storage.resolve_download(user.id, file_id)
    except StorageForbidden:
        raise _forbidden() from None
    return Response(
        content=b"",
        status_code=status.HTTP_200_OK,
        media_type=download.file.content_type,
        headers={
            "X-Accel-Redirect": download.accel_redirect,
            "Content-Disposition": download.content_disposition,
        },
    )


def delete_cloud_file(request: Request, user: User, file_id: str) -> dict[str, Any]:
    try:
        deleted = request.app.state.cloud_storage.delete(user.id, file_id)
    except StorageForbidden:
        raise _forbidden() from None
    return {
        "ok": True,
        "data": {"id": deleted.id, "source": "cloud"},
        "message": "File deleted",
    }


def _forbidden() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="File is not available",
    )


__all__ = [
    "cloud_file_data",
    "delete_cloud_file",
    "download_cloud_file",
    "list_cloud_file_data",
    "upload_cloud_file",
]
