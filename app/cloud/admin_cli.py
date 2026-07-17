from __future__ import annotations

import argparse
import os
import secrets
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


def _write_credentials(path: Path, temporary_password: str) -> None:
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        created = True
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as credential_file:
            descriptor = None
            credential_file.write(f"{temporary_password}\n")
            credential_file.flush()
            os.fsync(credential_file.fileno())
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            path.unlink(missing_ok=True)
        raise


def bootstrap_initial_admin(
    *,
    database: Database,
    username: str,
    credentials_file: Path | str,
) -> str:
    credential_path = Path(credentials_file)
    credentials_created = False
    try:
        normalized_username = normalize_username(username)
        database.initialize()
        temporary_password = secrets.token_urlsafe(32)
        password_hash = PasswordService().hash(temporary_password)
        timestamp = datetime.now(timezone.utc).isoformat()
        user_id = str(uuid4())
        with closing(database.connection()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
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
                _write_credentials(credential_path, temporary_password)
                credentials_created = True
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return user_id
    except BootstrapError:
        if credentials_created:
            credential_path.unlink(missing_ok=True)
        raise
    except Exception:
        if credentials_created:
            credential_path.unlink(missing_ok=True)
        raise BootstrapError("Initial administrator could not be created") from None


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
