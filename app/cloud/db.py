from __future__ import annotations

import sqlite3
from pathlib import Path


BUSY_TIMEOUT_MS = 5_000
SCHEMA_VERSION = 1


MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        """
        CREATE TABLE users (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('admin', 'user')),
            status TEXT NOT NULL CHECK (status IN ('active', 'disabled')),
            must_change_password INTEGER NOT NULL DEFAULT 0
                CHECK (must_change_password IN (0, 1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_login_at TEXT
        )
        """,
        """
        CREATE TABLE invitations (
            id TEXT PRIMARY KEY,
            code_hash TEXT NOT NULL UNIQUE,
            created_by TEXT NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL,
            expires_at TEXT,
            used_by TEXT REFERENCES users(id),
            used_at TEXT
        )
        """,
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id),
            token_hash TEXT NOT NULL UNIQUE,
            csrf_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            revoked_at TEXT
        )
        """,
        """
        CREATE TABLE user_settings (
            user_id TEXT PRIMARY KEY REFERENCES users(id),
            encrypted_tmp_key TEXT NOT NULL,
            custom_domain TEXT NOT NULL DEFAULT 'pan.cloudcode.xyz',
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE cloud_files (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id),
            original_name TEXT NOT NULL,
            content_type TEXT NOT NULL,
            size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
            storage_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (user_id, storage_path)
        )
        """,
        """
        CREATE TABLE automatic_download_links (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id),
            ukey TEXT NOT NULL,
            dkey TEXT NOT NULL,
            link TEXT NOT NULL,
            expires_at TEXT,
            UNIQUE (user_id, dkey)
        )
        """,
        """
        CREATE TABLE audit_events (
            id TEXT PRIMARY KEY,
            user_id TEXT REFERENCES users(id),
            event_type TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_id TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE auth_attempts (
            id TEXT PRIMARY KEY,
            username TEXT,
            remote_addr TEXT,
            successful INTEGER NOT NULL CHECK (successful IN (0, 1)),
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX sessions_user_id_idx ON sessions(user_id)",
        "CREATE INDEX cloud_files_user_id_idx ON cloud_files(user_id)",
        "CREATE INDEX audit_events_user_id_idx ON audit_events(user_id)",
        "CREATE INDEX auth_attempts_created_at_idx ON auth_attempts(created_at)",
    ),
)


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            current_version = connection.execute("PRAGMA user_version").fetchone()[0]
            if current_version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema version {current_version} is newer than {SCHEMA_VERSION}"
                )
            if current_version == SCHEMA_VERSION:
                return

            connection.execute("BEGIN IMMEDIATE")
            try:
                for version, migration in enumerate(MIGRATIONS, start=1):
                    if version <= current_version:
                        continue
                    for statement in migration:
                        connection.execute(statement)
                    connection.execute(f"PRAGMA user_version={version}")
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def backup(self, destination: Path | str) -> None:
        destination_path = Path(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as source, sqlite3.connect(destination_path) as target:
            source.backup(target)
