from __future__ import annotations

import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.cloud.db import Database
from app.cloud import maintenance
from app.cloud.maintenance import BACKUP_RETENTION, create_backup


def _seed_database(path: Path) -> Database:
    database = Database(path)
    database.initialize()
    with database.connection() as connection:
        connection.execute("CREATE TABLE backup_probe (value TEXT NOT NULL)")
        connection.execute("INSERT INTO backup_probe VALUES ('before-backup')")
    return database


def _probe_values(path: Path) -> list[str]:
    with sqlite3.connect(path) as connection:
        return [row[0] for row in connection.execute("SELECT value FROM backup_probe")]


def test_create_backup_uses_sqlite_snapshot_and_publishes_complete_file(
    tmp_path: Path,
):
    database = _seed_database(tmp_path / "data" / "app.db")
    backup_dir = tmp_path / "data" / "backups"

    published = create_backup(
        database,
        backup_dir,
        now=datetime(2026, 7, 17, 3, 4, 5, 678901, tzinfo=timezone.utc),
    )
    with database.connection() as connection:
        connection.execute("INSERT INTO backup_probe VALUES ('after-backup')")

    assert published.parent == backup_dir
    assert published.name == "app-20260717T030405678901Z-000000.sqlite3"
    assert published.is_file()
    assert stat.S_IMODE(published.stat().st_mode) == 0o600
    assert _probe_values(published) == ["before-backup"]
    assert list(backup_dir.glob(".*.pending-*")) == []


def test_create_backup_cleans_staging_file_when_sqlite_backup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    database = _seed_database(tmp_path / "app.db")
    backup_dir = tmp_path / "backups"

    def fail_after_partial_write(destination: Path | str) -> None:
        Path(destination).write_bytes(b"partial")
        raise RuntimeError("backup failed")

    monkeypatch.setattr(database, "backup", fail_after_partial_write)

    with pytest.raises(RuntimeError, match="backup failed"):
        create_backup(database, backup_dir)

    assert list(backup_dir.glob("*.sqlite3")) == []
    assert list(backup_dir.glob(".*.pending-*")) == []


def test_create_backup_retains_exactly_the_latest_seven_snapshots(tmp_path: Path):
    database = _seed_database(tmp_path / "app.db")
    backup_dir = tmp_path / "backups"
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)

    created = [
        create_backup(database, backup_dir, now=start + timedelta(days=offset))
        for offset in range(BACKUP_RETENTION + 3)
    ]

    remaining = sorted(backup_dir.glob("app-*.sqlite3"))
    assert BACKUP_RETENTION == 7
    assert remaining == created[-BACKUP_RETENTION:]
    assert all(_probe_values(path) == ["before-backup"] for path in remaining)


def test_create_backup_serializes_concurrent_publish_and_retention(tmp_path: Path):
    database = _seed_database(tmp_path / "app.db")
    backup_dir = tmp_path / "backups"
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = [
            executor.submit(
                create_backup,
                database,
                backup_dir,
                now=start + timedelta(microseconds=offset),
            )
            for offset in range(12)
        ]
        published = [future.result(timeout=10) for future in futures]

    remaining = sorted(backup_dir.glob("app-*.sqlite3"))
    assert len(set(published)) == 12
    assert len(remaining) == BACKUP_RETENTION
    assert remaining == sorted(published)[-BACKUP_RETENTION:]
    assert list(backup_dir.glob(".*.pending-*")) == []
    assert all(_probe_values(path) == ["before-backup"] for path in remaining)


def test_create_backup_orders_same_timestamp_invocations_by_publication(
    tmp_path: Path,
):
    database = _seed_database(tmp_path / "app.db")
    backup_dir = tmp_path / "backups"
    timestamp = datetime(2026, 7, 17, tzinfo=timezone.utc)

    published = [
        create_backup(database, backup_dir, now=timestamp)
        for _ in range(BACKUP_RETENTION + 1)
    ]

    assert sorted(backup_dir.glob("app-*.sqlite3")) == published[1:]


def test_daily_backup_keeps_one_restore_point_per_utc_day(tmp_path: Path):
    database = _seed_database(tmp_path / "app.db")
    backup_dir = tmp_path / "daily"
    start = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)

    first = maintenance.create_daily_backup(database, backup_dir, now=start)
    repeated = maintenance.create_daily_backup(
        database,
        backup_dir,
        now=start + timedelta(hours=11),
    )
    created = [first]
    for offset in range(1, BACKUP_RETENTION + 2):
        created.append(
            maintenance.create_daily_backup(
                database,
                backup_dir,
                now=start + timedelta(days=offset),
            )
        )

    remaining = sorted(backup_dir.glob("app-daily-*.sqlite3"))
    assert repeated == first
    assert remaining == created[-BACKUP_RETENTION:]
    assert len({path.name[10:18] for path in remaining}) == BACKUP_RETENTION
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in remaining)
