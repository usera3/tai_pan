from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from stat import S_IMODE
from uuid import UUID

import pytest
from cryptography.fernet import Fernet

from app.cloud.db import Database
from app.cloud.repository import CloudRepository
from app.cloud.security import KeyCipher
from app.cloud.storage import (
    CloudStorage,
    EmptyUpload,
    FileTooLarge,
    InsufficientStorage,
    StorageForbidden,
    StorageQuotaExceeded,
    safe_content_type,
)


@dataclass(frozen=True)
class DiskUsage:
    total: int = 1_000
    used: int = 0
    free: int = 1_000


class AsyncUpload:
    def __init__(
        self,
        content: bytes,
        *,
        filename: str = "upload.bin",
        content_type: str = "application/octet-stream",
        declared_size: int | None = None,
    ) -> None:
        self._content = content
        self._offset = 0
        self.filename = filename
        self.content_type = content_type
        self.size = declared_size
        self.read_sizes: list[int] = []

    async def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        chunk = self._content[self._offset : self._offset + size]
        self._offset += len(chunk)
        await asyncio.sleep(0)
        return chunk


class InterruptedUpload(AsyncUpload):
    def __init__(self, error: BaseException) -> None:
        super().__init__(b"abc")
        self._error = error
        self._reads = 0

    async def read(self, size: int) -> bytes:
        self._reads += 1
        if self._reads == 1:
            return b"a"
        raise self._error


class BarrierUpload(AsyncUpload):
    def __init__(self, content: bytes, barrier: threading.Barrier) -> None:
        super().__init__(content)
        self._barrier = barrier
        self._waited = False

    async def read(self, size: int) -> bytes:
        if not self._waited:
            self._waited = True
            self._barrier.wait(timeout=5)
        return await super().read(size)


@pytest.fixture
def database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "cloud.db")
    database.initialize()
    return database


@pytest.fixture
def repository(database: Database) -> CloudRepository:
    return CloudRepository(database, KeyCipher(Fernet.generate_key().decode("ascii")))


@pytest.fixture
def owner(repository: CloudRepository):
    return repository.create_user("storage-owner", "password-hash")


def make_storage(
    root: Path,
    repository: CloudRepository,
    *,
    max_file_bytes: int = 6,
    user_quota_bytes: int = 20,
    global_quota_bytes: int = 40,
    min_free_bytes: int = 5,
    disk_usage=lambda _path: DiskUsage(),
) -> CloudStorage:
    return CloudStorage(
        root,
        repository,
        max_file_bytes=max_file_bytes,
        user_quota_bytes=user_quota_bytes,
        global_quota_bytes=global_quota_bytes,
        min_free_bytes=min_free_bytes,
        chunk_bytes=3,
        disk_usage=disk_usage,
    )


def regular_files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file()]


def physical_path(root: Path, cloud_file) -> Path:
    return root.joinpath(*PurePosixPath(cloud_file.storage_path).parts)


def trash_path(root: Path, cloud_file) -> Path:
    return root / ".trash" / cloud_file.user_id / cloud_file.id


def install_delete_failure(database: Database) -> None:
    with database.connection() as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_cloud_delete_audit
            BEFORE INSERT ON audit_events
            WHEN NEW.event_type = 'cloud_file_deleted'
            BEGIN
                SELECT RAISE(ABORT, 'audit write failed');
            END
            """
        )


def remove_delete_failure(database: Database) -> None:
    with database.connection() as connection:
        connection.execute("DROP TRIGGER fail_cloud_delete_audit")


@pytest.mark.parametrize(
    "control",
    [*(chr(value) for value in range(32)), *(chr(value) for value in range(127, 160))],
)
def test_safe_content_type_rejects_every_control_character(control: str):
    assert (
        safe_content_type(f"text/plain{control};charset=utf-8")
        == "application/octet-stream"
    )


def test_store_streams_fixed_chunks_accepts_exact_limit_and_uses_controlled_paths(
    tmp_path: Path, repository: CloudRepository, owner
):
    storage_root = tmp_path / "files"
    storage = make_storage(storage_root, repository)
    upload = AsyncUpload(
        b"abcdef",
        filename="../../private/safe-\u6d4b\u8bd5.txt",
        content_type="text/plain",
        declared_size=6,
    )

    stored = asyncio.run(storage.store(owner.id, upload))

    UUID(stored.id)
    relative = PurePosixPath(stored.storage_path)
    assert relative.parts == ("users", owner.id, stored.id)
    assert stored.original_name == "safe-\u6d4b\u8bd5.txt"
    assert stored.size_bytes == 6
    assert stored.sha256 == hashlib.sha256(b"abcdef").hexdigest()
    assert set(upload.read_sizes) == {3}
    physical = storage_root.joinpath(*relative.parts)
    assert physical.read_bytes() == b"abcdef"
    assert S_IMODE(physical.stat().st_mode) == 0o600
    assert S_IMODE(physical.parent.stat().st_mode) == 0o700
    assert S_IMODE(storage_root.stat().st_mode) == 0o700
    assert {
        S_IMODE(path.stat().st_mode)
        for path in storage_root.rglob("*")
        if path.is_dir()
    } == {0o700}


@pytest.mark.parametrize(
    ("content", "error"),
    [(b"", EmptyUpload), (b"1234567", FileTooLarge)],
)
def test_invalid_streams_are_rejected_and_leave_no_files(
    content: bytes,
    error: type[Exception],
    tmp_path: Path,
    repository: CloudRepository,
    owner,
):
    storage_root = tmp_path / "files"
    storage = make_storage(storage_root, repository)

    with pytest.raises(error):
        asyncio.run(storage.store(owner.id, AsyncUpload(content)))

    assert repository.list_cloud_files(owner.id) == []
    assert regular_files(storage_root) == []


def test_precheck_enforces_user_global_and_disk_limits_without_reading(
    tmp_path: Path, repository: CloudRepository, owner
):
    other = repository.create_user("storage-other", "password-hash")
    repository.create_cloud_file(
        owner.id,
        original_name="existing.bin",
        content_type="application/octet-stream",
        size_bytes=4,
        storage_path=f"users/{owner.id}/existing",
        sha256="a" * 64,
    )
    repository.create_cloud_file(
        other.id,
        original_name="other.bin",
        content_type="application/octet-stream",
        size_bytes=4,
        storage_path=f"users/{other.id}/existing",
        sha256="b" * 64,
    )

    user_upload = AsyncUpload(b"12", declared_size=2)
    with pytest.raises(StorageQuotaExceeded):
        asyncio.run(
            make_storage(
                tmp_path / "user",
                repository,
                user_quota_bytes=5,
            ).store(owner.id, user_upload)
        )
    assert user_upload.read_sizes == []

    global_upload = AsyncUpload(b"12", declared_size=2)
    with pytest.raises(StorageQuotaExceeded):
        asyncio.run(
            make_storage(
                tmp_path / "global",
                repository,
                global_quota_bytes=9,
            ).store(owner.id, global_upload)
        )
    assert global_upload.read_sizes == []

    disk_upload = AsyncUpload(b"1234", declared_size=4)
    with pytest.raises(InsufficientStorage):
        asyncio.run(
            make_storage(
                tmp_path / "disk",
                repository,
                min_free_bytes=7,
                disk_usage=lambda _path: DiskUsage(free=10),
            ).store(owner.id, disk_upload)
        )
    assert disk_upload.read_sizes == []


def test_final_disk_check_and_upload_failures_cleanup_staging(
    tmp_path: Path, repository: CloudRepository, owner
):
    storage_root = tmp_path / "files"
    free_values = iter((100, 4))
    storage = make_storage(
        storage_root,
        repository,
        min_free_bytes=5,
        disk_usage=lambda _path: DiskUsage(free=next(free_values)),
    )

    with pytest.raises(InsufficientStorage):
        asyncio.run(storage.store(owner.id, AsyncUpload(b"abc")))
    assert regular_files(storage_root) == []

    with pytest.raises(OSError, match="interrupted"):
        asyncio.run(
            make_storage(storage_root, repository).store(
                owner.id, InterruptedUpload(OSError("interrupted"))
            )
        )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            make_storage(storage_root, repository).store(
                owner.id, InterruptedUpload(asyncio.CancelledError())
            )
        )
    assert repository.list_cloud_files(owner.id) == []
    assert regular_files(storage_root) == []


def test_sqlite_insert_failure_rolls_back_and_removes_the_finalized_file(
    tmp_path: Path,
    database: Database,
    repository: CloudRepository,
    owner,
):
    storage_root = tmp_path / "files"
    storage = make_storage(storage_root, repository)
    with database.connection() as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_cloud_file_insert
            BEFORE INSERT ON cloud_files
            BEGIN
                SELECT RAISE(ABORT, 'cloud file insert failed');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="cloud file insert failed"):
        asyncio.run(storage.store(owner.id, AsyncUpload(b"abc")))

    assert repository.list_cloud_files(owner.id) == []
    assert regular_files(storage_root) == []


def test_user_quota_accepts_exact_equality(
    tmp_path: Path, repository: CloudRepository, owner
):
    repository.create_cloud_file(
        owner.id,
        original_name="existing.bin",
        content_type="application/octet-stream",
        size_bytes=2,
        storage_path=f"users/{owner.id}/existing",
        sha256="a" * 64,
    )
    storage = make_storage(
        tmp_path / "files",
        repository,
        user_quota_bytes=5,
    )

    stored = asyncio.run(
        storage.store(owner.id, AsyncUpload(b"abc", declared_size=3))
    )

    assert stored.size_bytes == 3
    assert repository.user_storage_bytes(owner.id) == 5


def test_global_quota_accepts_exact_equality(
    tmp_path: Path, repository: CloudRepository, owner
):
    other = repository.create_user("global-boundary-other", "password-hash")
    repository.create_cloud_file(
        other.id,
        original_name="existing.bin",
        content_type="application/octet-stream",
        size_bytes=5,
        storage_path=f"users/{other.id}/existing",
        sha256="a" * 64,
    )
    storage = make_storage(
        tmp_path / "files",
        repository,
        global_quota_bytes=8,
    )

    stored = asyncio.run(
        storage.store(owner.id, AsyncUpload(b"abc", declared_size=3))
    )

    assert stored.size_bytes == 3
    assert repository.global_storage_bytes() == 8


def test_transactional_final_quota_check_prevents_concurrent_overcommit(
    tmp_path: Path, repository: CloudRepository, owner
):
    storage = make_storage(
        tmp_path / "files",
        repository,
        user_quota_bytes=5,
    )
    barrier = threading.Barrier(2)

    def upload() -> object:
        try:
            return asyncio.run(storage.store(owner.id, BarrierUpload(b"abc", barrier)))
        except Exception as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: upload(), range(2)))

    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, StorageQuotaExceeded) for result in results) == 1
    assert repository.user_storage_bytes(owner.id) == 3
    assert len(regular_files(tmp_path / "files")) == 1


def test_transactional_global_quota_prevents_concurrent_overcommit(
    tmp_path: Path, repository: CloudRepository, owner
):
    other = repository.create_user("global-concurrent-other", "password-hash")
    storage = make_storage(
        tmp_path / "files",
        repository,
        global_quota_bytes=5,
    )
    barrier = threading.Barrier(2)

    def upload(user_id: str) -> object:
        try:
            return asyncio.run(storage.store(user_id, BarrierUpload(b"abc", barrier)))
        except Exception as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(upload, (owner.id, other.id)))

    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, StorageQuotaExceeded) for result in results) == 1
    assert repository.global_storage_bytes() == 3
    assert len(regular_files(tmp_path / "files")) == 1


def test_delete_rollback_immediately_restores_the_deterministic_tombstone(
    tmp_path: Path,
    database: Database,
    repository: CloudRepository,
    owner,
):
    storage_root = tmp_path / "files"
    storage = make_storage(storage_root, repository)
    stored = asyncio.run(storage.store(owner.id, AsyncUpload(b"abc")))
    install_delete_failure(database)

    with pytest.raises(sqlite3.IntegrityError, match="audit write failed"):
        storage.delete(owner.id, stored.id)

    assert repository.get_cloud_file(owner.id, stored.id) == stored
    assert physical_path(storage_root, stored).read_bytes() == b"abc"
    assert not trash_path(storage_root, stored).exists()


def test_startup_restores_tombstone_after_rollback_restore_failure(
    tmp_path: Path,
    database: Database,
    repository: CloudRepository,
    owner,
    monkeypatch: pytest.MonkeyPatch,
):
    storage_root = tmp_path / "files"
    storage = make_storage(storage_root, repository)
    stored = asyncio.run(storage.store(owner.id, AsyncUpload(b"abc")))
    final_path = physical_path(storage_root, stored)
    recoverable_path = trash_path(storage_root, stored)
    install_delete_failure(database)
    real_replace = os.replace

    def fail_restore(source, destination):
        if Path(source) == recoverable_path and Path(destination) == final_path:
            raise OSError("restore unavailable")
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_restore)

    with pytest.raises(sqlite3.IntegrityError, match="audit write failed"):
        storage.delete(owner.id, stored.id)

    assert repository.get_cloud_file(owner.id, stored.id) == stored
    assert not final_path.exists()
    assert recoverable_path.read_bytes() == b"abc"

    remove_delete_failure(database)
    monkeypatch.setattr(os, "replace", real_replace)
    make_storage(storage_root, repository)

    assert final_path.read_bytes() == b"abc"
    assert not recoverable_path.exists()


def test_startup_reconciliation_restores_or_purges_each_crash_window(
    tmp_path: Path, repository: CloudRepository, owner
):
    storage_root = tmp_path / "files"
    storage = make_storage(storage_root, repository, max_file_bytes=8)
    restore_file = asyncio.run(storage.store(owner.id, AsyncUpload(b"restore")))
    purge_file = asyncio.run(storage.store(owner.id, AsyncUpload(b"purge")))
    restore_final = physical_path(storage_root, restore_file)
    purge_final = physical_path(storage_root, purge_file)
    restore_trash = trash_path(storage_root, restore_file)
    purge_trash = trash_path(storage_root, purge_file)
    restore_trash.parent.mkdir(parents=True, exist_ok=True)
    os.replace(restore_final, restore_trash)
    os.replace(purge_final, purge_trash)
    assert repository.delete_cloud_file(owner.id, purge_file.id)

    make_storage(storage_root, repository)

    assert restore_final.read_bytes() == b"restore"
    assert not restore_trash.exists()
    assert not purge_final.exists()
    assert not purge_trash.exists()


def test_successful_delete_ignores_purge_failure_and_startup_finishes_it(
    tmp_path: Path,
    repository: CloudRepository,
    owner,
    monkeypatch: pytest.MonkeyPatch,
):
    storage_root = tmp_path / "files"
    storage = make_storage(storage_root, repository)
    stored = asyncio.run(storage.store(owner.id, AsyncUpload(b"abc")))
    recoverable_path = trash_path(storage_root, stored)
    real_unlink = Path.unlink

    def fail_trash_purge(path: Path, *args, **kwargs):
        if ".trash" in path.parts:
            raise OSError("purge unavailable")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_trash_purge)

    deleted = storage.delete(owner.id, stored.id)

    assert deleted == stored
    assert repository.get_cloud_file(owner.id, stored.id) is None
    assert recoverable_path.read_bytes() == b"abc"

    monkeypatch.setattr(Path, "unlink", real_unlink)
    make_storage(storage_root, repository)

    assert not recoverable_path.exists()


def test_startup_reconciliation_uses_only_uuid_paths_and_never_follows_symlinks(
    tmp_path: Path, repository: CloudRepository, owner
):
    storage_root = tmp_path / "files"
    storage = make_storage(storage_root, repository)
    stored = asyncio.run(storage.store(owner.id, AsyncUpload(b"abc")))
    final_path = physical_path(storage_root, stored)
    recoverable_path = trash_path(storage_root, stored)
    recoverable_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(final_path, recoverable_path)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    final_path.symlink_to(outside)
    with repository._database.connection() as connection:
        connection.execute(
            "UPDATE cloud_files SET storage_path = ? WHERE id = ?",
            ("../../outside.txt", stored.id),
        )

    make_storage(storage_root, repository)

    assert outside.read_bytes() == b"outside"
    assert not final_path.is_symlink()
    assert final_path.read_bytes() == b"abc"
    assert not recoverable_path.exists()


def test_delete_never_follows_a_symlinked_trash_user_directory(
    tmp_path: Path, repository: CloudRepository, owner
):
    storage_root = tmp_path / "files"
    storage = make_storage(storage_root, repository)
    stored = asyncio.run(storage.store(owner.id, AsyncUpload(b"abc")))
    physical_path(storage_root, stored).unlink()
    outside_directory = tmp_path / "outside-trash"
    outside_directory.mkdir()
    outside_file = outside_directory / stored.id
    outside_file.write_bytes(b"outside")
    trash_user_directory = storage_root / ".trash" / owner.id
    trash_user_directory.symlink_to(outside_directory, target_is_directory=True)

    with pytest.raises(StorageForbidden):
        storage.delete(owner.id, stored.id)

    assert outside_file.read_bytes() == b"outside"
    assert repository.get_cloud_file(owner.id, stored.id) == stored


def test_delete_is_owner_scoped_cleans_missing_disk_and_audits_only_safe_metadata(
    tmp_path: Path, repository: CloudRepository, owner
):
    storage_root = tmp_path / "files"
    storage = make_storage(storage_root, repository)
    stored = asyncio.run(storage.store(owner.id, AsyncUpload(b"abc")))
    other = repository.create_user("delete-other", "password-hash")

    with pytest.raises(StorageForbidden):
        storage.delete(other.id, stored.id)

    storage_root.joinpath(*PurePosixPath(stored.storage_path).parts).unlink()
    deleted = storage.delete(owner.id, stored.id)

    assert deleted.id == stored.id
    assert repository.get_cloud_file(owner.id, stored.id) is None
    events = repository.list_audit_events(owner.id)
    assert [(event.event_type, event.target_type, event.target_id) for event in events] == [
        ("cloud_file_deleted", "cloud_file", stored.id)
    ]
    assert str(storage_root) not in repr(events)
    with pytest.raises(StorageForbidden):
        storage.delete(owner.id, stored.id)


def test_concurrent_delete_has_one_success_and_one_forbidden_result(
    tmp_path: Path, repository: CloudRepository, owner
):
    storage = make_storage(tmp_path / "files", repository)
    stored = asyncio.run(storage.store(owner.id, AsyncUpload(b"abc")))

    def delete() -> object:
        try:
            return storage.delete(owner.id, stored.id)
        except Exception as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: delete(), range(2)))

    assert sum(getattr(result, "id", None) == stored.id for result in results) == 1
    assert sum(isinstance(result, StorageForbidden) for result in results) == 1
    assert repository.get_cloud_file(owner.id, stored.id) is None
    assert regular_files(tmp_path / "files") == []
    assert [event.target_id for event in repository.list_audit_events(owner.id)] == [
        stored.id
    ]
