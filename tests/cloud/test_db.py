from __future__ import annotations

import sqlite3
from pathlib import Path

from app.cloud.db import Database


EXPECTED_TABLES = {
    "users",
    "invitations",
    "sessions",
    "user_settings",
    "cloud_files",
    "automatic_download_links",
    "audit_events",
    "auth_attempts",
}


def test_initialize_configures_sqlite_and_creates_cloud_schema(tmp_path: Path):
    database = Database(tmp_path / "cloud.db")

    database.initialize()

    with database.connection() as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert journal_mode == "wal"
    assert foreign_keys == 1
    assert schema_version == 1
    assert EXPECTED_TABLES <= tables


def test_backup_copies_initialized_database(tmp_path: Path):
    database = Database(tmp_path / "cloud.db")
    database.initialize()
    destination = tmp_path / "backups" / "cloud.db"

    database.backup(destination)

    with sqlite3.connect(destination) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert EXPECTED_TABLES <= tables
