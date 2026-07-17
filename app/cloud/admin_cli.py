from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import json
import os
import secrets
import stat
import sys
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.cloud.db import Database
from app.cloud.repository import normalize_username
from app.cloud.security import PasswordService


class BootstrapError(RuntimeError):
    pass


@dataclass
class _PendingCredentials:
    name: str
    username: str
    temporary_password: str = field(repr=False)
    descriptor: int = field(repr=False)
    device: int
    inode: int

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1


@dataclass
class _BootstrapLock:
    descriptor: int = field(repr=False)

    def close(self) -> None:
        if self.descriptor >= 0:
            try:
                fcntl.flock(self.descriptor, fcntl.LOCK_UN)
            finally:
                os.close(self.descriptor)
                self.descriptor = -1


def _pending_name(final_name: str) -> str:
    return f".{final_name}.pending"


def _lock_name(final_name: str) -> str:
    return f".{final_name}.lock"


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _open_credentials_directory(path: Path) -> int:
    absolute_parent = path.absolute().parent
    descriptor: int | None = None
    try:
        descriptor = os.open(os.path.sep, _directory_flags())
        for component in absolute_parent.parts[1:]:
            next_descriptor = os.open(component, _directory_flags(), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        raise BootstrapError("Credentials directory is not safe") from None

    metadata = os.fstat(descriptor)
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or mode & 0o022
    ):
        os.close(descriptor)
        raise BootstrapError("Credentials directory is not safe")
    return descriptor


def _entry_metadata(directory_fd: int, name: str):
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _entry_exists(directory_fd: int, name: str) -> bool:
    return _entry_metadata(directory_fd, name) is not None


def _acquire_bootstrap_lock(directory_fd: int, name: str) -> _BootstrapLock:
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
    except OSError:
        raise BootstrapError("Bootstrap lock is not safe") from None
    try:
        os.fchmod(descriptor, 0o600)
        metadata = os.fstat(descriptor)
        named_metadata = _entry_metadata(directory_fd, name)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid()
            or named_metadata is None
            or named_metadata.st_dev != metadata.st_dev
            or named_metadata.st_ino != metadata.st_ino
        ):
            raise BootstrapError("Bootstrap lock is not safe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        os.fsync(directory_fd)
        return _BootstrapLock(descriptor)
    except BaseException:
        os.close(descriptor)
        raise


def _validate_pending_metadata(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.geteuid()
    ):
        raise BootstrapError("Pending credentials are not safe")


def _parse_pending_payload(raw_payload: bytes) -> dict[str, str]:
    try:
        payload = json.loads(raw_payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise BootstrapError("Pending credentials are not valid") from None
    if not isinstance(payload, dict) or set(payload) != {
        "username",
        "temporary_password",
    }:
        raise BootstrapError("Pending credentials are not valid")
    username = payload.get("username")
    temporary_password = payload.get("temporary_password")
    if not isinstance(username, str) or not isinstance(temporary_password, str):
        raise BootstrapError("Pending credentials are not valid")
    if not username or not temporary_password:
        raise BootstrapError("Pending credentials are not valid")
    return {"username": username, "temporary_password": temporary_password}


def _read_pending_credentials(directory_fd: int, name: str) -> _PendingCredentials:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except OSError:
        raise BootstrapError("Pending credentials are not safe") from None
    try:
        metadata = os.fstat(descriptor)
        _validate_pending_metadata(metadata)
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, 4096)
            if not chunk:
                break
            size += len(chunk)
            if size > 16 * 1024:
                raise BootstrapError("Pending credentials are not valid")
            chunks.append(chunk)
        payload = _parse_pending_payload(b"".join(chunks))
        return _PendingCredentials(
            name=name,
            username=payload["username"],
            temporary_password=payload["temporary_password"],
            descriptor=descriptor,
            device=metadata.st_dev,
            inode=metadata.st_ino,
        )
    except BaseException:
        os.close(descriptor)
        raise


def _same_directory_entry(directory_fd: int, pending: _PendingCredentials) -> bool:
    metadata = _entry_metadata(directory_fd, pending.name)
    return bool(
        metadata is not None
        and metadata.st_dev == pending.device
        and metadata.st_ino == pending.inode
        and stat.S_ISREG(metadata.st_mode)
    )


def _unlink_pending_if_same(directory_fd: int, pending: _PendingCredentials) -> bool:
    if not _same_directory_entry(directory_fd, pending):
        return False
    os.unlink(pending.name, dir_fd=directory_fd)
    os.fsync(directory_fd)
    return True


def _cleanup_created_pending(
    directory_fd: int, name: str, descriptor: int | None
) -> None:
    if descriptor is None:
        return
    try:
        metadata = os.fstat(descriptor)
        named_metadata = _entry_metadata(directory_fd, name)
        if (
            named_metadata is not None
            and named_metadata.st_dev == metadata.st_dev
            and named_metadata.st_ino == metadata.st_ino
        ):
            os.unlink(name, dir_fd=directory_fd)
            os.fsync(directory_fd)
    except BaseException:
        pass


def _write_pending_credentials(
    directory_fd: int,
    name: str,
    *,
    username: str,
    temporary_password: str,
) -> _PendingCredentials:
    raw_payload = (
        json.dumps(
            {"username": username, "temporary_password": temporary_password},
            ensure_ascii=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
        os.fchmod(descriptor, 0o600)
        metadata = os.fstat(descriptor)
        _validate_pending_metadata(metadata)
        pending = _PendingCredentials(
            name=name,
            username=username,
            temporary_password=temporary_password,
            descriptor=descriptor,
            device=metadata.st_dev,
            inode=metadata.st_ino,
        )
        offset = 0
        while offset < len(raw_payload):
            written = os.write(descriptor, raw_payload[offset:])
            if written <= 0:
                raise OSError("credential write did not make progress")
            offset += written
        os.fsync(descriptor)
        os.fsync(directory_fd)
        return pending
    except BaseException:
        _cleanup_created_pending(directory_fd, name, descriptor)
        if descriptor is not None:
            os.close(descriptor)
        raise


_RENAME_NOREPLACE = 1
_LIBC = ctypes.CDLL(None, use_errno=True)
_RENAMEAT2 = getattr(_LIBC, "renameat2", None)


def _require_secure_rename() -> None:
    if _RENAMEAT2 is None:
        raise BootstrapError("Secure credential publication is unavailable")


def _rename_pending_credentials(
    directory_fd: int, pending: _PendingCredentials, final_name: str
) -> None:
    if not _same_directory_entry(directory_fd, pending):
        raise BootstrapError("Pending credentials changed during publication")
    _require_secure_rename()
    result = _RENAMEAT2(
        directory_fd,
        ctypes.c_char_p(os.fsencode(pending.name)),
        directory_fd,
        ctypes.c_char_p(os.fsencode(final_name)),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        if error_number == errno.EEXIST:
            raise BootstrapError("Credentials file already exists")
        raise BootstrapError("Credentials file could not be published")
    final_metadata = _entry_metadata(directory_fd, final_name)
    if (
        final_metadata is None
        or final_metadata.st_dev != pending.device
        or final_metadata.st_ino != pending.inode
        or not stat.S_ISREG(final_metadata.st_mode)
    ):
        raise BootstrapError("Published credentials could not be verified")
    os.fsync(directory_fd)


def _find_admin(database: Database):
    with closing(database.connection()) as connection:
        return connection.execute(
            "SELECT * FROM users WHERE role = 'admin' ORDER BY created_at LIMIT 1"
        ).fetchone()


def _matching_admin(
    database: Database, *, username: str, temporary_password: str
):
    row = _find_admin(database)
    if row is None or row["username"] != username:
        return None
    return (
        row
        if PasswordService().verify(row["password_hash"], temporary_password)
        else None
    )


def _finalize_recoverable_pending(
    *,
    database: Database,
    directory_fd: int,
    pending: _PendingCredentials,
    final_name: str,
) -> str | None:
    row = _matching_admin(
        database,
        username=pending.username,
        temporary_password=pending.temporary_password,
    )
    if row is None:
        return None
    _rename_pending_credentials(directory_fd, pending, final_name)
    return str(row["id"])


def _close_quietly(resource) -> None:
    try:
        resource.close()
    except BaseException:
        pass


def bootstrap_initial_admin(
    *, database: Database, username: str, credentials_file: Path | str
) -> str:
    try:
        normalized_username = normalize_username(username)
    except ValueError:
        raise BootstrapError("Initial administrator could not be created") from None
    credential_path = Path(credentials_file)
    if credential_path.name in {"", ".", ".."}:
        raise BootstrapError("Initial administrator could not be created")
    directory_fd = _open_credentials_directory(credential_path)
    bootstrap_lock: _BootstrapLock | None = None
    active_pending: _PendingCredentials | None = None
    try:
        _require_secure_rename()
        bootstrap_lock = _acquire_bootstrap_lock(
            directory_fd, _lock_name(credential_path.name)
        )
        database.initialize()
        final_name = credential_path.name
        pending_name = _pending_name(final_name)

        if _entry_exists(directory_fd, final_name):
            raise BootstrapError("Initial administrator could not be created")

        if _entry_exists(directory_fd, pending_name):
            active_pending = _read_pending_credentials(directory_fd, pending_name)
            recovered_id = _finalize_recoverable_pending(
                database=database,
                directory_fd=directory_fd,
                pending=active_pending,
                final_name=final_name,
            )
            if recovered_id is not None:
                return recovered_id
            if _find_admin(database) is not None:
                raise BootstrapError("Initial administrator already exists")
            if not _unlink_pending_if_same(directory_fd, active_pending):
                raise BootstrapError("Pending credentials changed during recovery")
            active_pending.close()
            active_pending = None

        if _find_admin(database) is not None:
            raise BootstrapError("Initial administrator already exists")

        temporary_password = secrets.token_urlsafe(32)
        password_hash = PasswordService().hash(temporary_password)
        active_pending = _write_pending_credentials(
            directory_fd,
            pending_name,
            username=normalized_username,
            temporary_password=temporary_password,
        )

        timestamp = datetime.now(timezone.utc).isoformat()
        user_id = str(uuid4())
        connection = database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM users WHERE role = 'admin' LIMIT 1"
            ).fetchone() is not None:
                raise BootstrapError("Initial administrator already exists")
            connection.execute(
                """
                INSERT INTO users (
                    id, username, password_hash, role, status,
                    must_change_password, created_at, updated_at
                ) VALUES (?, ?, ?, 'admin', 'active', 1, ?, ?)
                """,
                (user_id, normalized_username, password_hash, timestamp, timestamp),
            )
            connection.commit()
        except BaseException as error:
            try:
                connection.rollback()
            except BaseException:
                pass
            _close_quietly(connection)
            try:
                recovered_id = _finalize_recoverable_pending(
                    database=database,
                    directory_fd=directory_fd,
                    pending=active_pending,
                    final_name=final_name,
                )
            except BaseException:
                raise BootstrapError(
                    "Initial administrator state requires recovery"
                ) from None
            if recovered_id is not None:
                return recovered_id
            if not _unlink_pending_if_same(directory_fd, active_pending):
                raise BootstrapError("Pending credentials changed during recovery")
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(error, BootstrapError):
                raise
            raise BootstrapError("Initial administrator could not be created") from None
        else:
            _close_quietly(connection)

        _rename_pending_credentials(directory_fd, active_pending, final_name)
        return user_id
    except BootstrapError:
        raise
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        raise BootstrapError("Initial administrator could not be created") from None
    finally:
        if active_pending is not None:
            _close_quietly(active_pending)
        if bootstrap_lock is not None:
            _close_quietly(bootstrap_lock)
        try:
            os.close(directory_fd)
        except OSError:
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create the initial cloud administrator")
    parser.add_argument("--database-path", type=Path, required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--credentials-file", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        bootstrap_initial_admin(
            database=Database(arguments.database_path),
            username=arguments.username,
            credentials_file=arguments.credentials_file,
        )
    except BootstrapError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("Initial administrator created.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
