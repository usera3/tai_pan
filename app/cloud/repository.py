from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from app.cloud.db import Database
from app.cloud.security import KeyCipher, hash_secret


DEFAULT_CUSTOM_DOMAIN = "pan.cloudcode.xyz"
USERNAME_PATTERN = re.compile(r"^[a-z0-9_-]{3,32}$")


@dataclass(frozen=True)
class User:
    id: str
    username: str
    password_hash: str = field(repr=False)
    role: str
    status: str
    must_change_password: bool
    created_at: datetime
    updated_at: datetime
    last_login_at: datetime | None


@dataclass(frozen=True)
class Invitation:
    id: str
    code_hash: str = field(repr=False)
    created_by: str
    created_at: datetime
    expires_at: datetime | None
    used_by: str | None
    used_at: datetime | None


@dataclass(frozen=True)
class Session:
    id: str
    user_id: str
    token_hash: str = field(repr=False)
    csrf_hash: str = field(repr=False)
    created_at: datetime
    expires_at: datetime
    last_seen_at: datetime
    revoked_at: datetime | None


@dataclass(frozen=True)
class UserSettings:
    user_id: str
    custom_domain: str
    updated_at: datetime
    key_configured: bool


@dataclass(frozen=True)
class CloudFile:
    id: str
    user_id: str
    original_name: str
    content_type: str
    size_bytes: int
    storage_path: str
    sha256: str
    created_at: datetime


@dataclass(frozen=True)
class AutomaticDownloadLink:
    id: str
    user_id: str
    ukey: str = field(repr=False)
    dkey: str = field(repr=False)
    link: str = field(repr=False)
    expires_at: datetime | None


@dataclass(frozen=True)
class AuditEvent:
    id: str
    user_id: str | None
    event_type: str
    target_type: str
    target_id: str
    created_at: datetime


@dataclass(frozen=True)
class AuthAttempt:
    id: str
    username: str | None
    remote_addr: str | None = field(repr=False)
    successful: bool
    created_at: datetime


def normalize_username(username: str) -> str:
    normalized = username.strip().lower()
    if not USERNAME_PATTERN.fullmatch(normalized):
        raise ValueError(
            "username must be 3-32 characters using letters, numbers, '_' or '-'"
        )
    return normalized


def _normalize_auth_identifier(username: str) -> str:
    return username.strip().lower()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _serialize_datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime values must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _deserialize_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def _user_from_row(row: sqlite3.Row) -> User:
    return User(
        id=row["id"],
        username=row["username"],
        password_hash=row["password_hash"],
        role=row["role"],
        status=row["status"],
        must_change_password=bool(row["must_change_password"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        last_login_at=_deserialize_datetime(row["last_login_at"]),
    )


def _invitation_from_row(row: sqlite3.Row) -> Invitation:
    return Invitation(
        id=row["id"],
        code_hash=row["code_hash"],
        created_by=row["created_by"],
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=_deserialize_datetime(row["expires_at"]),
        used_by=row["used_by"],
        used_at=_deserialize_datetime(row["used_at"]),
    )


def _session_from_row(row: sqlite3.Row) -> Session:
    return Session(
        id=row["id"],
        user_id=row["user_id"],
        token_hash=row["token_hash"],
        csrf_hash=row["csrf_hash"],
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        last_seen_at=datetime.fromisoformat(row["last_seen_at"]),
        revoked_at=_deserialize_datetime(row["revoked_at"]),
    )


def _settings_from_row(row: sqlite3.Row) -> UserSettings:
    return UserSettings(
        user_id=row["user_id"],
        custom_domain=row["custom_domain"],
        updated_at=datetime.fromisoformat(row["updated_at"]),
        key_configured=bool(row["encrypted_tmp_key"]),
    )


def _cloud_file_from_row(row: sqlite3.Row) -> CloudFile:
    return CloudFile(
        id=row["id"],
        user_id=row["user_id"],
        original_name=row["original_name"],
        content_type=row["content_type"],
        size_bytes=row["size_bytes"],
        storage_path=row["storage_path"],
        sha256=row["sha256"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _automatic_link_from_row(row: sqlite3.Row) -> AutomaticDownloadLink:
    return AutomaticDownloadLink(
        id=row["id"],
        user_id=row["user_id"],
        ukey=row["ukey"],
        dkey=row["dkey"],
        link=row["link"],
        expires_at=_deserialize_datetime(row["expires_at"]),
    )


def _audit_event_from_row(row: sqlite3.Row) -> AuditEvent:
    return AuditEvent(
        id=row["id"],
        user_id=row["user_id"],
        event_type=row["event_type"],
        target_type=row["target_type"],
        target_id=row["target_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _auth_attempt_from_row(row: sqlite3.Row) -> AuthAttempt:
    return AuthAttempt(
        id=row["id"],
        username=row["username"],
        remote_addr=row["remote_addr"],
        successful=bool(row["successful"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _insert_session(
    connection: sqlite3.Connection,
    user_id: str,
    *,
    token: str,
    csrf_token: str,
    expires_at: datetime,
    timestamp: datetime,
) -> sqlite3.Row:
    session_id = str(uuid4())
    connection.execute(
        """
        INSERT INTO sessions (
            id, user_id, token_hash, csrf_hash, created_at,
            expires_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            user_id,
            hash_secret(token),
            hash_secret(csrf_token),
            _serialize_datetime(timestamp),
            _serialize_datetime(expires_at),
            _serialize_datetime(timestamp),
        ),
    )
    row = connection.execute(
        "SELECT * FROM sessions WHERE id = ? AND user_id = ?",
        (session_id, user_id),
    ).fetchone()
    if row is None:
        raise RuntimeError("session insert could not be read back")
    return row


class CloudRepository:
    def __init__(self, database: Database, key_cipher: KeyCipher) -> None:
        self._database = database
        self._key_cipher = key_cipher

    def create_user(
        self,
        username: str,
        password_hash: str,
        *,
        role: str = "user",
        status: str = "active",
        must_change_password: bool = False,
        now: datetime | None = None,
    ) -> User:
        timestamp = now or _now()
        values = (
            str(uuid4()),
            normalize_username(username),
            password_hash,
            role,
            status,
            int(must_change_password),
            _serialize_datetime(timestamp),
            _serialize_datetime(timestamp),
        )
        with closing(self._database.connection()) as connection, connection:
            connection.execute(
                """
                INSERT INTO users (
                    id, username, password_hash, role, status,
                    must_change_password, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            row = connection.execute(
                "SELECT * FROM users WHERE id = ?", (values[0],)
            ).fetchone()
        return _user_from_row(row)

    def get_user(self, user_id: str) -> User | None:
        with closing(self._database.connection()) as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return _user_from_row(row) if row is not None else None

    def get_user_by_username(self, username: str) -> User | None:
        normalized = normalize_username(username)
        with closing(self._database.connection()) as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username = ?", (normalized,)
            ).fetchone()
        return _user_from_row(row) if row is not None else None

    def list_users(self) -> list[User]:
        with closing(self._database.connection()) as connection:
            rows = connection.execute(
                "SELECT * FROM users ORDER BY created_at, id"
            ).fetchall()
        return [_user_from_row(row) for row in rows]

    def create_invitation(
        self,
        *,
        created_by: str,
        code: str,
        expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> Invitation:
        timestamp = now or _now()
        invitation_id = str(uuid4())
        with closing(self._database.connection()) as connection, connection:
            connection.execute(
                """
                INSERT INTO invitations (
                    id, code_hash, created_by, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    invitation_id,
                    hash_secret(code),
                    created_by,
                    _serialize_datetime(timestamp),
                    _serialize_datetime(expires_at) if expires_at else None,
                ),
            )
            row = connection.execute(
                "SELECT * FROM invitations WHERE id = ?", (invitation_id,)
            ).fetchone()
        return _invitation_from_row(row)

    def consume_invitation(
        self, code: str, *, used_by: str, now: datetime | None = None
    ) -> Invitation | None:
        timestamp = now or _now()
        serialized_now = _serialize_datetime(timestamp)
        with closing(self._database.connection()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """
                    UPDATE invitations
                    SET used_by = ?, used_at = ?
                    WHERE code_hash = ?
                      AND used_by IS NULL
                      AND (expires_at IS NULL OR expires_at > ?)
                    """,
                    (used_by, serialized_now, hash_secret(code), serialized_now),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    return None
                row = connection.execute(
                    "SELECT * FROM invitations WHERE code_hash = ?",
                    (hash_secret(code),),
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return _invitation_from_row(row)

    def register_user_with_invitation(
        self,
        *,
        username: str,
        password_hash: str,
        invitation_code: str,
        session_token: str,
        csrf_token: str,
        session_expires_at: datetime,
        now: datetime | None = None,
    ) -> User | None:
        timestamp = now or _now()
        serialized_now = _serialize_datetime(timestamp)
        user_id = str(uuid4())
        normalized_username = normalize_username(username)
        with closing(self._database.connection()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                invitation = connection.execute(
                    """
                    SELECT id FROM invitations
                    WHERE code_hash = ?
                      AND used_by IS NULL
                      AND (expires_at IS NULL OR expires_at > ?)
                    """,
                    (hash_secret(invitation_code), serialized_now),
                ).fetchone()
                if invitation is None:
                    connection.rollback()
                    return None

                connection.execute(
                    """
                    INSERT INTO users (
                        id, username, password_hash, role, status,
                        must_change_password, created_at, updated_at
                    ) VALUES (?, ?, ?, 'user', 'active', 0, ?, ?)
                    """,
                    (
                        user_id,
                        normalized_username,
                        password_hash,
                        serialized_now,
                        serialized_now,
                    ),
                )
                cursor = connection.execute(
                    """
                    UPDATE invitations SET used_by = ?, used_at = ?
                    WHERE id = ? AND used_by IS NULL
                    """,
                    (user_id, serialized_now, invitation["id"]),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("invitation could not be consumed")
                _insert_session(
                    connection,
                    user_id,
                    token=session_token,
                    csrf_token=csrf_token,
                    expires_at=session_expires_at,
                    timestamp=timestamp,
                )
                row = connection.execute(
                    "SELECT * FROM users WHERE id = ?", (user_id,)
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return _user_from_row(row)

    def update_password_and_revoke_sessions(
        self,
        user_id: str,
        password_hash: str,
        *,
        expected_password_hash: str,
        token: str,
        csrf_token: str,
        expires_at: datetime,
        now: datetime | None = None,
    ) -> User | None:
        timestamp = now or _now()
        serialized_now = _serialize_datetime(timestamp)
        with closing(self._database.connection()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """
                    UPDATE users
                    SET password_hash = ?, must_change_password = 0, updated_at = ?
                    WHERE id = ? AND status = 'active' AND password_hash = ?
                    """,
                    (
                        password_hash,
                        serialized_now,
                        user_id,
                        expected_password_hash,
                    ),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    return None
                connection.execute(
                    """
                    UPDATE sessions SET revoked_at = ?
                    WHERE user_id = ? AND revoked_at IS NULL
                    """,
                    (serialized_now, user_id),
                )
                _insert_session(
                    connection,
                    user_id,
                    token=token,
                    csrf_token=csrf_token,
                    expires_at=expires_at,
                    timestamp=timestamp,
                )
                row = connection.execute(
                    "SELECT * FROM users WHERE id = ?", (user_id,)
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return _user_from_row(row)

    def create_session_for_verified_user(
        self,
        user_id: str,
        *,
        expected_password_hash: str,
        token: str,
        csrf_token: str,
        expires_at: datetime,
        now: datetime | None = None,
    ) -> User | None:
        timestamp = now or _now()
        serialized_now = _serialize_datetime(timestamp)
        with closing(self._database.connection()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """
                    UPDATE users SET last_login_at = ?
                    WHERE id = ? AND status = 'active' AND password_hash = ?
                    """,
                    (serialized_now, user_id, expected_password_hash),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    return None
                _insert_session(
                    connection,
                    user_id,
                    token=token,
                    csrf_token=csrf_token,
                    expires_at=expires_at,
                    timestamp=timestamp,
                )
                row = connection.execute(
                    "SELECT * FROM users WHERE id = ?", (user_id,)
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return _user_from_row(row)

    def record_login(self, user_id: str, *, now: datetime | None = None) -> User | None:
        with closing(self._database.connection()) as connection, connection:
            cursor = connection.execute(
                "UPDATE users SET last_login_at = ? WHERE id = ?",
                (_serialize_datetime(now or _now()), user_id),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return _user_from_row(row)

    def create_session(
        self,
        user_id: str,
        *,
        token: str,
        csrf_token: str,
        expires_at: datetime,
        now: datetime | None = None,
    ) -> Session:
        timestamp = now or _now()
        with closing(self._database.connection()) as connection, connection:
            row = _insert_session(
                connection,
                user_id,
                token=token,
                csrf_token=csrf_token,
                expires_at=expires_at,
                timestamp=timestamp,
            )
        return _session_from_row(row)

    def get_active_session_by_token(
        self, token: str, *, now: datetime | None = None
    ) -> Session | None:
        timestamp = _serialize_datetime(now or _now())
        with closing(self._database.connection()) as connection:
            row = connection.execute(
                """
                SELECT * FROM sessions
                WHERE token_hash = ? AND revoked_at IS NULL AND expires_at > ?
                """,
                (hash_secret(token), timestamp),
            ).fetchone()
        return _session_from_row(row) if row is not None else None

    def get_session(self, user_id: str, session_id: str) -> Session | None:
        with closing(self._database.connection()) as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE id = ? AND user_id = ?",
                (session_id, user_id),
            ).fetchone()
        return _session_from_row(row) if row is not None else None

    def revoke_session(
        self, user_id: str, session_id: str, *, now: datetime | None = None
    ) -> bool:
        with closing(self._database.connection()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE sessions SET revoked_at = ?
                WHERE id = ? AND user_id = ? AND revoked_at IS NULL
                """,
                (_serialize_datetime(now or _now()), session_id, user_id),
            )
        return cursor.rowcount == 1

    def revoke_all_sessions(
        self, user_id: str, *, now: datetime | None = None
    ) -> int:
        with closing(self._database.connection()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE sessions SET revoked_at = ?
                WHERE user_id = ? AND revoked_at IS NULL
                """,
                (_serialize_datetime(now or _now()), user_id),
            )
        return cursor.rowcount

    def save_user_settings(
        self,
        user_id: str,
        *,
        tmp_key: str | None,
        custom_domain: str = DEFAULT_CUSTOM_DOMAIN,
        now: datetime | None = None,
    ) -> UserSettings:
        timestamp = _serialize_datetime(now or _now())
        with closing(self._database.connection()) as connection, connection:
            current = connection.execute(
                "SELECT encrypted_tmp_key FROM user_settings WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if tmp_key is None:
                encrypted_tmp_key = current[0] if current is not None else ""
            elif tmp_key:
                encrypted_tmp_key = self._key_cipher.encrypt(tmp_key)
            else:
                encrypted_tmp_key = ""
            connection.execute(
                """
                INSERT INTO user_settings (
                    user_id, encrypted_tmp_key, custom_domain, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    encrypted_tmp_key = excluded.encrypted_tmp_key,
                    custom_domain = excluded.custom_domain,
                    updated_at = excluded.updated_at
                """,
                (user_id, encrypted_tmp_key, custom_domain, timestamp),
            )
            row = connection.execute(
                "SELECT * FROM user_settings WHERE user_id = ?", (user_id,)
            ).fetchone()
        return _settings_from_row(row)

    def get_user_settings(self, user_id: str) -> UserSettings | None:
        with closing(self._database.connection()) as connection:
            row = connection.execute(
                "SELECT * FROM user_settings WHERE user_id = ?", (user_id,)
            ).fetchone()
        return _settings_from_row(row) if row is not None else None

    def get_tmp_key(self, user_id: str) -> str | None:
        with closing(self._database.connection()) as connection:
            row = connection.execute(
                "SELECT encrypted_tmp_key FROM user_settings WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None or not row[0]:
            return None
        return self._key_cipher.decrypt(row[0])

    def clear_tmp_key(
        self, user_id: str, *, now: datetime | None = None
    ) -> UserSettings | None:
        with closing(self._database.connection()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE user_settings SET encrypted_tmp_key = '', updated_at = ?
                WHERE user_id = ?
                """,
                (_serialize_datetime(now or _now()), user_id),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT * FROM user_settings WHERE user_id = ?", (user_id,)
            ).fetchone()
        return _settings_from_row(row)

    def create_cloud_file(
        self,
        user_id: str,
        *,
        original_name: str,
        content_type: str,
        size_bytes: int,
        storage_path: str,
        sha256: str,
        now: datetime | None = None,
    ) -> CloudFile:
        file_id = str(uuid4())
        with closing(self._database.connection()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cloud_files (
                    id, user_id, original_name, content_type, size_bytes,
                    storage_path, sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    file_id,
                    user_id,
                    original_name,
                    content_type,
                    size_bytes,
                    storage_path,
                    sha256,
                    _serialize_datetime(now or _now()),
                ),
            )
            row = connection.execute(
                "SELECT * FROM cloud_files WHERE id = ? AND user_id = ?",
                (file_id, user_id),
            ).fetchone()
        return _cloud_file_from_row(row)

    def get_cloud_file(self, user_id: str, file_id: str) -> CloudFile | None:
        with closing(self._database.connection()) as connection:
            row = connection.execute(
                "SELECT * FROM cloud_files WHERE id = ? AND user_id = ?",
                (file_id, user_id),
            ).fetchone()
        return _cloud_file_from_row(row) if row is not None else None

    def list_cloud_files(self, user_id: str) -> list[CloudFile]:
        with closing(self._database.connection()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM cloud_files
                WHERE user_id = ? ORDER BY created_at DESC, id
                """,
                (user_id,),
            ).fetchall()
        return [_cloud_file_from_row(row) for row in rows]

    def delete_cloud_file(self, user_id: str, file_id: str) -> bool:
        with closing(self._database.connection()) as connection, connection:
            cursor = connection.execute(
                "DELETE FROM cloud_files WHERE id = ? AND user_id = ?",
                (file_id, user_id),
            )
        return cursor.rowcount == 1

    def user_storage_bytes(self, user_id: str) -> int:
        with closing(self._database.connection()) as connection:
            row = connection.execute(
                "SELECT COALESCE(SUM(size_bytes), 0) FROM cloud_files WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return int(row[0])

    def global_storage_bytes(self) -> int:
        with closing(self._database.connection()) as connection:
            row = connection.execute(
                "SELECT COALESCE(SUM(size_bytes), 0) FROM cloud_files"
            ).fetchone()
        return int(row[0])

    def save_automatic_download_link(
        self,
        user_id: str,
        *,
        ukey: str,
        dkey: str,
        link: str,
        expires_at: datetime | None,
    ) -> AutomaticDownloadLink:
        link_id = str(uuid4())
        with closing(self._database.connection()) as connection, connection:
            connection.execute(
                """
                INSERT INTO automatic_download_links (
                    id, user_id, ukey, dkey, link, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, dkey) DO UPDATE SET
                    ukey = excluded.ukey,
                    link = excluded.link,
                    expires_at = excluded.expires_at
                """,
                (
                    link_id,
                    user_id,
                    ukey,
                    dkey,
                    link,
                    _serialize_datetime(expires_at) if expires_at else None,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM automatic_download_links
                WHERE user_id = ? AND dkey = ?
                """,
                (user_id, dkey),
            ).fetchone()
        return _automatic_link_from_row(row)

    def get_automatic_download_link(
        self, user_id: str, dkey: str
    ) -> AutomaticDownloadLink | None:
        with closing(self._database.connection()) as connection:
            row = connection.execute(
                """
                SELECT * FROM automatic_download_links
                WHERE user_id = ? AND dkey = ?
                """,
                (user_id, dkey),
            ).fetchone()
        return _automatic_link_from_row(row) if row is not None else None

    def list_automatic_download_links(
        self,
        user_id: str,
        *,
        ukey: str | None = None,
        active_at: datetime | None = None,
    ) -> list[AutomaticDownloadLink]:
        clauses = ["user_id = ?"]
        parameters: list[str] = [user_id]
        if ukey is not None:
            clauses.append("ukey = ?")
            parameters.append(ukey)
        if active_at is not None:
            clauses.append("(expires_at IS NULL OR expires_at > ?)")
            parameters.append(_serialize_datetime(active_at))
        query = (
            "SELECT * FROM automatic_download_links WHERE "
            + " AND ".join(clauses)
            + " ORDER BY id"
        )
        with closing(self._database.connection()) as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [_automatic_link_from_row(row) for row in rows]

    def delete_automatic_download_link(self, user_id: str, dkey: str) -> bool:
        with closing(self._database.connection()) as connection, connection:
            cursor = connection.execute(
                """
                DELETE FROM automatic_download_links
                WHERE user_id = ? AND dkey = ?
                """,
                (user_id, dkey),
            )
        return cursor.rowcount == 1

    def create_audit_event(
        self,
        user_id: str | None,
        *,
        event_type: str,
        target_type: str,
        target_id: str,
        now: datetime | None = None,
    ) -> AuditEvent:
        event_id = str(uuid4())
        with closing(self._database.connection()) as connection, connection:
            connection.execute(
                """
                INSERT INTO audit_events (
                    id, user_id, event_type, target_type, target_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    user_id,
                    event_type,
                    target_type,
                    target_id,
                    _serialize_datetime(now or _now()),
                ),
            )
            if user_id is None:
                row = connection.execute(
                    "SELECT * FROM audit_events WHERE id = ? AND user_id IS NULL",
                    (event_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM audit_events WHERE id = ? AND user_id = ?",
                    (event_id, user_id),
                ).fetchone()
        return _audit_event_from_row(row)

    def list_audit_events(self, user_id: str) -> list[AuditEvent]:
        with closing(self._database.connection()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM audit_events
                WHERE user_id = ? ORDER BY created_at DESC, id
                """,
                (user_id,),
            ).fetchall()
        return [_audit_event_from_row(row) for row in rows]

    def record_auth_attempt(
        self,
        *,
        username: str | None,
        remote_addr: str | None,
        successful: bool,
        now: datetime | None = None,
    ) -> AuthAttempt:
        attempt_id = str(uuid4())
        normalized_username = (
            _normalize_auth_identifier(username) if username is not None else None
        )
        with closing(self._database.connection()) as connection, connection:
            connection.execute(
                """
                INSERT INTO auth_attempts (
                    id, username, remote_addr, successful, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    normalized_username,
                    remote_addr,
                    int(successful),
                    _serialize_datetime(now or _now()),
                ),
            )
            row = connection.execute(
                "SELECT * FROM auth_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
        return _auth_attempt_from_row(row)

    def claim_failed_login_attempt(
        self,
        *,
        username: str,
        remote_addr: str,
        since: datetime,
        limit: int,
        now: datetime | None = None,
    ) -> bool:
        if limit < 1:
            raise ValueError("limit must be positive")
        normalized_username = _normalize_auth_identifier(username)
        serialized_since = _serialize_datetime(since)
        timestamp = now or _now()
        with closing(self._database.connection()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                account_count = connection.execute(
                    """
                    SELECT COUNT(*) FROM auth_attempts
                    WHERE successful = 0 AND username = ? AND created_at >= ?
                    """,
                    (normalized_username, serialized_since),
                ).fetchone()[0]
                ip_count = connection.execute(
                    """
                    SELECT COUNT(*) FROM auth_attempts
                    WHERE successful = 0 AND username IS NOT NULL
                      AND remote_addr = ? AND created_at >= ?
                    """,
                    (remote_addr, serialized_since),
                ).fetchone()[0]
                if account_count >= limit or ip_count >= limit:
                    connection.rollback()
                    return False
                connection.execute(
                    """
                    INSERT INTO auth_attempts (
                        id, username, remote_addr, successful, created_at
                    ) VALUES (?, ?, ?, 0, ?)
                    """,
                    (
                        str(uuid4()),
                        normalized_username,
                        remote_addr,
                        _serialize_datetime(timestamp),
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return True

    def claim_registration_submission(
        self,
        *,
        remote_addr: str,
        since: datetime,
        limit: int,
        now: datetime | None = None,
    ) -> bool:
        if limit < 1:
            raise ValueError("limit must be positive")
        timestamp = now or _now()
        with closing(self._database.connection()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                count = connection.execute(
                    """
                    SELECT COUNT(*) FROM auth_attempts
                    WHERE username IS NULL AND remote_addr = ? AND created_at >= ?
                    """,
                    (remote_addr, _serialize_datetime(since)),
                ).fetchone()[0]
                if count >= limit:
                    connection.rollback()
                    return False
                connection.execute(
                    """
                    INSERT INTO auth_attempts (
                        id, username, remote_addr, successful, created_at
                    ) VALUES (?, NULL, ?, 0, ?)
                    """,
                    (str(uuid4()), remote_addr, _serialize_datetime(timestamp)),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return True

    def count_failed_auth_attempts(
        self,
        *,
        since: datetime,
        username: str | None = None,
        remote_addr: str | None = None,
    ) -> int:
        if username is None and remote_addr is None:
            raise ValueError("username or remote_addr is required")
        clauses = ["successful = 0", "created_at >= ?"]
        parameters = [_serialize_datetime(since)]
        if username is not None:
            clauses.append("username = ?")
            parameters.append(_normalize_auth_identifier(username))
        if remote_addr is not None:
            clauses.append("username IS NOT NULL")
            clauses.append("remote_addr = ?")
            parameters.append(remote_addr)
        query = "SELECT COUNT(*) FROM auth_attempts WHERE " + " AND ".join(clauses)
        with closing(self._database.connection()) as connection:
            row = connection.execute(query, parameters).fetchone()
        return int(row[0])

    def count_registration_attempts(
        self, *, since: datetime, remote_addr: str
    ) -> int:
        with closing(self._database.connection()) as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) FROM auth_attempts
                WHERE username IS NULL AND remote_addr = ? AND created_at >= ?
                """,
                (remote_addr, _serialize_datetime(since)),
            ).fetchone()
        return int(row[0])
