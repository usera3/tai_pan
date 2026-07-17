from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.cloud.db import Database
from app.cloud.repository import normalize_username
from app.cloud.security import PasswordService


class BootstrapError(RuntimeError):
    pass


def _pending_name(final_name: str) -> str:
    return f".{final_name}.pending"


def _open_credentials_directory(path: Path) -> int:
    parent = path.parent
    if parent.is_symlink():
        raise BootstrapError("Credentials directory is not safe")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(parent, flags)
    except OSError:
        raise BootstrapError("Credentials directory is not safe") from None
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise BootstrapError("Credentials directory is not safe")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _entry_exists(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _read_pending_credentials(directory_fd: int, name: str) -> dict[str, str]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except OSError:
        raise BootstrapError("Pending credentials are not safe") from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise BootstrapError("Pending credentials are not safe")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            payload = json.load(handle)
    except BootstrapError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise BootstrapError("Pending credentials are not valid") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
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


def _write_pending_credentials(
    directory_fd: int,
    name: str,
    *,
    username: str,
    temporary_password: str,
) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
        os.fchmod(descriptor, 0o600)
        payload = json.dumps(
            {
                "username": username,
                "temporary_password": temporary_password,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            handle.write(payload)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise


def _remove_pending_credentials(directory_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        return
    os.fsync(directory_fd)


def _rename_pending_credentials(
    directory_fd: int,
    pending_name: str,
    final_name: str,
) -> None:
    if _entry_exists(directory_fd, final_name):
        raise BootstrapError("Credentials file already exists")
    os.rename(
        pending_name,
        final_name,
        src_dir_fd=directory_fd,
        dst_dir_fd=directory_fd,
    )
    os.fsync(directory_fd)


def _find_admin(database: Database):
    with closing(database.connection()) as connection:
        return connection.execute(
            "SELECT * FROM users WHERE role = 'admin' ORDER BY created_at LIMIT 1"
        ).fetchone()


def _matching_admin(
    database: Database,
    *,
    username: str,
    temporary_password: str,
):
    row = _find_admin(database)
    if row is None or row["username"] != username:
        return None
    try:
        matches = PasswordService().verify(row["password_hash"], temporary_password)
    except Exception:
        return None
    return row if matches else None


def _finalize_recoverable_pending(
    *,
    database: Database,
    directory_fd: int,
    pending_name: str,
    final_name: str,
    credentials: dict[str, str],
) -> str | None:
    row = _matching_admin(
        database,
        username=credentials["username"],
        temporary_password=credentials["temporary_password"],
    )
    if row is None:
        return None
    _rename_pending_credentials(directory_fd, pending_name, final_name)
    return str(row["id"])


def bootstrap_initial_admin(
    *,
    database: Database,
    username: str,
    credentials_file: Path | str,
) -> str:
    credential_path = Path(credentials_file)
    normalized_username = normalize_username(username)
    database.initialize()
    directory_fd = _open_credentials_directory(credential_path)
    final_name = credential_path.name
    pending_name = _pending_name(final_name)
    try:
        if _entry_exists(directory_fd, final_name):
            raise BootstrapError("Initial administrator could not be created")

        if _entry_exists(directory_fd, pending_name):
            pending = _read_pending_credentials(directory_fd, pending_name)
            recovered_id = _finalize_recoverable_pending(
                database=database,
                directory_fd=directory_fd,
                pending_name=pending_name,
                final_name=final_name,
                credentials=pending,
            )
            if recovered_id is not None:
                return recovered_id
            if _find_admin(database) is not None:
                raise BootstrapError("Initial administrator already exists")
            _remove_pending_credentials(directory_fd, pending_name)

        if _find_admin(database) is not None:
            raise BootstrapError("Initial administrator already exists")

        temporary_password = secrets.token_urlsafe(32)
        password_service = PasswordService()
        password_hash = password_service.hash(temporary_password)
        _write_pending_credentials(
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
            existing_admin = connection.execute(
                "SELECT 1 FROM users WHERE role = 'admin' LIMIT 1"
            ).fetchone()
            if existing_admin is not None:
                raise BootstrapError("Initial administrator already exists")
            connection.execute(
                """
                INSERT INTO users (
                    id, username, password_hash, role, status,
                    must_change_password, created_at, updated_at
                ) VALUES (?, ?, ?, 'admin', 'active', 1, ?, ?)
                """,
                (
                    user_id,
                    normalized_username,
                    password_hash,
                    timestamp,
                    timestamp,
                ),
            )
            connection.commit()
        except BaseException as error:
            try:
                connection.rollback()
            except BaseException:
                pass
            connection.close()
            recovered_id = _finalize_recoverable_pending(
                database=database,
                directory_fd=directory_fd,
                pending_name=pending_name,
                final_name=final_name,
                credentials={
                    "username": normalized_username,
                    "temporary_password": temporary_password,
                },
            )
            if recovered_id is not None:
                return recovered_id
            _remove_pending_credentials(directory_fd, pending_name)
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(error, BootstrapError):
                raise
            raise BootstrapError("Initial administrator could not be created") from None
        else:
            connection.close()

        _rename_pending_credentials(directory_fd, pending_name, final_name)
        return user_id
    except BootstrapError:
        raise
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        raise BootstrapError("Initial administrator could not be created") from None
    finally:
        os.close(directory_fd)


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
