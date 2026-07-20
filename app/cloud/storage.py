from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote
from uuid import UUID, uuid4

from app.cloud.repository import CloudFile, CloudRepository


DEFAULT_CHUNK_BYTES = 1024 * 1024
SAFE_CONTENT_TYPE = re.compile(
    r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+(?: *; *[A-Za-z0-9!#$&^_.+-]+=[A-Za-z0-9!#$&^_.+\-]+)*$"
)


class CloudStorageError(Exception):
    pass


class EmptyUpload(CloudStorageError):
    pass


class FileTooLarge(CloudStorageError):
    pass


class StorageQuotaExceeded(CloudStorageError):
    pass


class InsufficientStorage(CloudStorageError):
    pass


class StorageForbidden(CloudStorageError):
    pass


@dataclass(frozen=True)
class CloudDownload:
    file: CloudFile
    accel_redirect: str
    content_disposition: str


def safe_display_name(filename: str | None) -> str:
    basename = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    basename = "".join(
        "_" if ord(character) < 32 or ord(character) == 127 else character
        for character in basename
    )
    if basename in {"", ".", ".."}:
        return "upload.bin"
    return basename[:255]


def safe_content_type(content_type: str | None) -> str:
    raw_content_type = content_type or ""
    if any(
        ord(character) < 32 or 127 <= ord(character) <= 159
        for character in raw_content_type
    ):
        return "application/octet-stream"
    candidate = raw_content_type.strip()
    if not candidate or not SAFE_CONTENT_TYPE.fullmatch(candidate):
        return "application/octet-stream"
    return candidate


def attachment_disposition(filename: str) -> str:
    safe_name = safe_display_name(filename)
    fallback = safe_name.encode("ascii", "replace").decode("ascii")
    fallback = fallback.replace("?", "_").replace('"', "_").replace("\\", "_")
    return (
        f'attachment; filename="{fallback}"; '
        f"filename*=UTF-8''{quote(safe_name, safe='')}"
    )


class CloudStorage:
    def __init__(
        self,
        root: Path | str,
        repository: CloudRepository,
        *,
        max_file_bytes: int,
        user_quota_bytes: int,
        global_quota_bytes: int,
        min_free_bytes: int,
        chunk_bytes: int = DEFAULT_CHUNK_BYTES,
        disk_usage: Callable[[Path], Any] = shutil.disk_usage,
    ) -> None:
        self.root = Path(root)
        self.repository = repository
        self.max_file_bytes = max_file_bytes
        self.user_quota_bytes = user_quota_bytes
        self.global_quota_bytes = global_quota_bytes
        self.min_free_bytes = min_free_bytes
        self.chunk_bytes = chunk_bytes
        self._disk_usage = disk_usage
        if min(
            max_file_bytes,
            user_quota_bytes,
            global_quota_bytes,
            chunk_bytes,
        ) <= 0 or min_free_bytes < 0:
            raise ValueError("storage limits must be positive")
        self._secure_directory(self.root)
        self._reconcile_trash()

    async def store(self, user_id: str, upload: Any) -> CloudFile:
        self._validated_uuid(user_id)
        declared_size = getattr(upload, "size", None)
        if isinstance(declared_size, int):
            if declared_size == 0:
                raise EmptyUpload("upload is empty")
            if declared_size < 0 or declared_size > self.max_file_bytes:
                raise FileTooLarge("upload is too large")
        else:
            declared_size = None
        self._precheck(user_id, declared_size)

        staging_directory = self.root / ".staging" / user_id
        final_directory = self.root / "users" / user_id
        self._secure_directory(staging_directory)
        self._secure_directory(final_directory)
        staged_path = staging_directory / str(uuid4())
        file_id = str(uuid4())
        relative_path = PurePosixPath("users", user_id, file_id)
        final_path = self.root.joinpath(*relative_path.parts)
        original_name = safe_display_name(getattr(upload, "filename", None))
        content_type = safe_content_type(getattr(upload, "content_type", None))
        digest = hashlib.sha256()
        size = 0
        registered = False

        try:
            descriptor = os.open(staged_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as staged:
                os.chmod(staged_path, 0o600)
                while True:
                    chunk = await upload.read(self.chunk_bytes)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self.max_file_bytes:
                        raise FileTooLarge("upload is too large")
                    staged.write(chunk)
                    digest.update(chunk)
                if size == 0:
                    raise EmptyUpload("upload is empty")
                staged.flush()
                os.fsync(staged.fileno())

            def validate_quota(user_total: int, global_total: int) -> None:
                if user_total + size > self.user_quota_bytes:
                    raise StorageQuotaExceeded("user quota exceeded")
                if global_total + size > self.global_quota_bytes:
                    raise StorageQuotaExceeded("global quota exceeded")
                if self._disk_usage(self.root).free < self.min_free_bytes:
                    raise InsufficientStorage("disk reserve would be violated")

            def finalize() -> None:
                os.replace(staged_path, final_path)
                os.chmod(final_path, 0o600)

            cloud_file = self.repository.finalize_cloud_file(
                user_id,
                file_id=file_id,
                original_name=original_name,
                content_type=content_type,
                size_bytes=size,
                storage_path=relative_path.as_posix(),
                sha256=digest.hexdigest(),
                validate_quota=validate_quota,
                finalize=finalize,
            )
            registered = True
            return cloud_file
        finally:
            staged_path.unlink(missing_ok=True)
            if not registered:
                final_path.unlink(missing_ok=True)

    def resolve_download(self, user_id: str, file_id: str) -> CloudDownload:
        cloud_file = self.repository.get_cloud_file(user_id, file_id)
        if cloud_file is None:
            raise StorageForbidden("file is not available")
        physical_path = self._physical_path(cloud_file)
        if physical_path.is_symlink() or not physical_path.is_file():
            raise StorageForbidden("file is not available")
        return CloudDownload(
            file=cloud_file,
            accel_redirect=f"/_protected_files/{cloud_file.storage_path}",
            content_disposition=attachment_disposition(cloud_file.original_name),
        )

    def delete(self, user_id: str, file_id: str) -> CloudFile:
        self._validated_uuid(user_id)
        self._validated_uuid(file_id)
        moved_from: Path | None = None
        moved_to: Path | None = None

        def stage_delete(cloud_file: CloudFile) -> None:
            nonlocal moved_from, moved_to
            physical_path = self._physical_path(cloud_file)
            trash_path = self._trash_path(cloud_file.user_id, cloud_file.id)
            self._secure_directory(physical_path.parent)
            self._secure_directory(trash_path.parent)
            physical_kind = self._path_kind(physical_path)
            trash_kind = self._path_kind(trash_path)
            if physical_kind is None:
                if trash_kind == "file":
                    moved_from = physical_path
                    moved_to = trash_path
                return
            if physical_kind != "file":
                raise StorageForbidden("file is not available")
            if trash_kind == "file" or trash_kind == "symlink":
                trash_path.unlink()
            elif trash_kind is not None:
                raise StorageForbidden("file is not available")
            os.replace(physical_path, trash_path)
            moved_from = physical_path
            moved_to = trash_path

        try:
            deleted = self.repository.delete_cloud_file_with_audit(
                user_id,
                file_id,
                stage_delete=stage_delete,
            )
        except BaseException:
            if moved_from is not None and moved_to is not None:
                try:
                    self._secure_directory(moved_from.parent)
                    self._secure_directory(moved_to.parent)
                    if self._path_kind(moved_to) == "file":
                        os.replace(moved_to, moved_from)
                except (OSError, StorageForbidden):
                    pass
            raise
        if deleted is None:
            raise StorageForbidden("file is not available")
        if moved_to is not None:
            try:
                self._secure_directory(moved_to.parent)
                moved_to.unlink(missing_ok=True)
                moved_to.parent.rmdir()
            except (OSError, StorageForbidden):
                pass
        return deleted

    def _precheck(self, user_id: str, declared_size: int | None) -> None:
        user_total = self.repository.user_storage_bytes(user_id)
        global_total = self.repository.global_storage_bytes()
        requested = declared_size or 1
        if user_total + requested > self.user_quota_bytes:
            raise StorageQuotaExceeded("user quota exceeded")
        if global_total + requested > self.global_quota_bytes:
            raise StorageQuotaExceeded("global quota exceeded")
        free = self._disk_usage(self.root).free
        if free - (declared_size or 0) < self.min_free_bytes:
            raise InsufficientStorage("disk reserve would be violated")

    def _physical_path(self, cloud_file: CloudFile) -> Path:
        self._validated_uuid(cloud_file.user_id)
        self._validated_uuid(cloud_file.id)
        expected = PurePosixPath("users", cloud_file.user_id, cloud_file.id)
        if cloud_file.storage_path != expected.as_posix():
            raise StorageForbidden("file is not available")
        return self._final_path(cloud_file.user_id, cloud_file.id)

    def _final_path(self, user_id: str, file_id: str) -> Path:
        self._validated_uuid(user_id)
        self._validated_uuid(file_id)
        return self.root / "users" / user_id / file_id

    def _trash_path(self, user_id: str, file_id: str) -> Path:
        self._validated_uuid(user_id)
        self._validated_uuid(file_id)
        return self.root / ".trash" / user_id / file_id

    def _reconcile_trash(self) -> None:
        trash_root = self.root / ".trash"
        trash_kind = self._path_kind(trash_root)
        if trash_kind == "symlink" or trash_kind == "file":
            try:
                trash_root.unlink()
            except OSError:
                return
        elif trash_kind is not None and trash_kind != "directory":
            return
        self._secure_directory(trash_root)

        try:
            user_entries = list(os.scandir(trash_root))
        except OSError:
            return
        for user_entry in user_entries:
            user_path = Path(user_entry.path)
            if user_entry.is_symlink():
                self._unlink_best_effort(user_path)
                continue
            if not user_entry.is_dir(follow_symlinks=False):
                self._unlink_best_effort(user_path)
                continue
            try:
                self._validated_uuid(user_entry.name)
                self._secure_directory(user_path)
                file_entries = list(os.scandir(user_path))
            except (OSError, StorageForbidden):
                continue

            for file_entry in file_entries:
                tombstone = Path(file_entry.path)
                if file_entry.is_symlink():
                    self._unlink_best_effort(tombstone)
                    continue
                if not file_entry.is_file(follow_symlinks=False):
                    continue
                try:
                    self._validated_uuid(file_entry.name)
                except StorageForbidden:
                    self._unlink_best_effort(tombstone)
                    continue
                self._reconcile_tombstone(
                    user_entry.name,
                    file_entry.name,
                    tombstone,
                )
            try:
                user_path.rmdir()
            except OSError:
                pass

    def _reconcile_tombstone(
        self, user_id: str, file_id: str, tombstone: Path
    ) -> None:
        cloud_file = self.repository.get_cloud_file(user_id, file_id)
        if cloud_file is None:
            self._unlink_best_effort(tombstone)
            return
        final_path = self._final_path(user_id, file_id)
        try:
            self._secure_directory(final_path.parent)
            final_kind = self._path_kind(final_path)
            if final_kind == "symlink":
                final_path.unlink()
                final_kind = None
            if final_kind is None:
                os.replace(tombstone, final_path)
                os.chmod(final_path, 0o600)
            elif final_kind == "file":
                tombstone.unlink()
        except (OSError, StorageForbidden):
            pass

    @staticmethod
    def _unlink_best_effort(path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass

    @staticmethod
    def _path_kind(path: Path) -> str | None:
        try:
            mode = os.lstat(path).st_mode
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(mode):
            return "symlink"
        if stat.S_ISREG(mode):
            return "file"
        if stat.S_ISDIR(mode):
            return "directory"
        return "other"

    @staticmethod
    def _validated_uuid(value: str) -> UUID:
        try:
            parsed = UUID(value)
        except (ValueError, TypeError, AttributeError):
            raise StorageForbidden("file is not available") from None
        if str(parsed) != value:
            raise StorageForbidden("file is not available")
        return parsed

    def _secure_directory(self, path: Path) -> None:
        try:
            relative = path.relative_to(self.root)
        except ValueError:
            raise StorageForbidden("file is not available")
        if any(part in {"", ".", ".."} for part in relative.parts):
            raise StorageForbidden("file is not available")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        current = self.root
        self._chmod_directory_no_follow(current)
        for part in relative.parts:
            current /= part
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            self._chmod_directory_no_follow(current)

    @staticmethod
    def _chmod_directory_no_follow(path: Path) -> None:
        flags = os.O_RDONLY
        flags |= getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError:
            raise StorageForbidden("file is not available") from None
        try:
            os.fchmod(descriptor, 0o700)
        finally:
            os.close(descriptor)


__all__ = [
    "CloudDownload",
    "CloudStorage",
    "CloudStorageError",
    "EmptyUpload",
    "FileTooLarge",
    "InsufficientStorage",
    "StorageForbidden",
    "StorageQuotaExceeded",
    "attachment_disposition",
    "safe_content_type",
    "safe_display_name",
]
