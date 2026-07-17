from __future__ import annotations

import argparse
import fcntl
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from app.cloud.db import Database


BACKUP_RETENTION = 7
DAILY_INTERVAL_SECONDS = 24 * 60 * 60


@contextmanager
def _backup_lock(backup_dir: Path) -> Iterator[None]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        backup_dir / ".backup.lock",
        os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("backup timestamp must include a timezone")
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _unused_destination(backup_dir: Path, timestamp: str) -> Path:
    counter = 0
    while True:
        candidate = backup_dir / f"app-{timestamp}-{counter:06d}.sqlite3"
        if not candidate.exists():
            return candidate
        counter += 1


def _prune_old_backups(backup_dir: Path) -> None:
    backups = sorted(backup_dir.glob("app-*.sqlite3"))
    for obsolete in backups[:-BACKUP_RETENTION]:
        obsolete.unlink()


def create_backup(
    database: Database,
    backup_dir: Path | str,
    *,
    now: datetime | None = None,
) -> Path:
    """Publish a consistent SQLite snapshot and retain the latest seven."""
    destination_dir = Path(backup_dir)
    timestamp = _timestamp(now or datetime.now(timezone.utc))

    with _backup_lock(destination_dir):
        destination = _unused_destination(destination_dir, timestamp)
        staging = destination_dir / f".{destination.name}.pending-{uuid4().hex}"
        try:
            database.backup(staging)
            descriptor = os.open(staging, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(staging, destination)
            _fsync_directory(destination_dir)
            _prune_old_backups(destination_dir)
            _fsync_directory(destination_dir)
        except BaseException:
            staging.unlink(missing_ok=True)
            raise
        return destination


def run_daily_backups(database: Database, backup_dir: Path | str) -> None:
    while True:
        published = create_backup(database, backup_dir)
        print(f"Published SQLite backup: {published.name}", flush=True)
        time.sleep(DAILY_INTERVAL_SECONDS)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Cloud SQLite backup maintenance")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("backup", "daily"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--database-path", type=Path, required=True)
        subparser.add_argument("--backup-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    database = Database(arguments.database_path)
    if arguments.command == "backup":
        published = create_backup(database, arguments.backup_dir)
        print(f"Published SQLite backup: {published.name}")
        return 0
    run_daily_backups(database, arguments.backup_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
