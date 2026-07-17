from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import pytest
from cryptography.fernet import Fernet

from app.cloud.db import Database
from app.cloud.repository import CloudRepository
from app.cloud.security import KeyCipher, hash_secret


NOW = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)


class RecordingConnection:
    def __init__(self, connection: sqlite3.Connection, queries: list[tuple[str, object]]):
        self._connection = connection
        self._queries = queries

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, *args):
        return self._connection.__exit__(*args)

    def close(self) -> None:
        self._connection.close()

    def execute(self, query: str, parameters=()):
        self._queries.append((query, parameters))
        return self._connection.execute(query, parameters)


@pytest.fixture
def database(tmp_path: Path) -> Database:
    value = Database(tmp_path / "cloud.db")
    value.initialize()
    return value


@pytest.fixture
def repository(database: Database) -> CloudRepository:
    return CloudRepository(database, KeyCipher(Fernet.generate_key()))


def create_user(repository: CloudRepository, username: str):
    return repository.create_user(username, "argon2-password-hash", now=NOW)


def test_users_are_normalized_unique_and_return_typed_records(
    repository: CloudRepository,
):
    user = create_user(repository, "  Alice_User  ")

    assert is_dataclass(user)
    assert user.username == "alice_user"
    assert repository.get_user_by_username(" ALICE_USER ") == user
    assert "argon2-password-hash" not in repr(user)

    with pytest.raises(sqlite3.IntegrityError):
        create_user(repository, "alice_user")


@pytest.mark.parametrize("username", ["ab", "has spaces", "invalid!", "x" * 33])
def test_users_reject_invalid_normalized_usernames(
    repository: CloudRepository, username: str
):
    with pytest.raises(ValueError, match="username"):
        create_user(repository, username)


def test_invitation_consumption_is_single_use_under_concurrency(
    repository: CloudRepository,
):
    admin = create_user(repository, "admin")
    first = create_user(repository, "first-user")
    second = create_user(repository, "second-user")
    code = "one-time-invitation"
    repository.create_invitation(
        created_by=admin.id,
        code=code,
        expires_at=NOW + timedelta(hours=1),
        now=NOW,
    )
    barrier = Barrier(2)

    def consume(user_id: str):
        barrier.wait()
        return repository.consume_invitation(code, used_by=user_id, now=NOW)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(consume, [first.id, second.id]))

    consumed = [result for result in results if result is not None]
    assert len(consumed) == 1
    assert consumed[0].used_by in {first.id, second.id}
    assert is_dataclass(consumed[0])
    assert code not in repr(consumed[0])


def test_expired_invitation_cannot_be_consumed(repository: CloudRepository):
    admin = create_user(repository, "admin")
    user = create_user(repository, "invited-user")
    code = "expired-invitation"
    repository.create_invitation(
        created_by=admin.id,
        code=code,
        expires_at=NOW - timedelta(seconds=1),
        now=NOW - timedelta(hours=1),
    )

    assert repository.consume_invitation(code, used_by=user.id, now=NOW) is None


def test_sessions_store_only_hashes_support_opaque_lookup_and_scoped_revocation(
    repository: CloudRepository, database: Database
):
    user = create_user(repository, "session-user")
    other = create_user(repository, "other-user")
    token = "plain-session-token"
    csrf_token = "plain-csrf-token"
    session = repository.create_session(
        user.id,
        token=token,
        csrf_token=csrf_token,
        expires_at=NOW + timedelta(hours=1),
        now=NOW,
    )

    with database.connection() as connection:
        row = connection.execute(
            "SELECT token_hash, csrf_hash FROM sessions WHERE id = ?", (session.id,)
        ).fetchone()
    assert tuple(row) == (hash_secret(token), hash_secret(csrf_token))
    assert repository.get_active_session_by_token(token, now=NOW) == session
    assert token not in repr(session)
    assert csrf_token not in repr(session)

    assert repository.revoke_session(other.id, session.id, now=NOW) is False
    assert repository.revoke_session(user.id, session.id, now=NOW) is True
    assert repository.get_active_session_by_token(token, now=NOW) is None
    assert repository.get_session(user.id, session.id).revoked_at == NOW


def test_tenant_owned_writes_read_back_by_id_and_user_id(
    repository: CloudRepository, database: Database, monkeypatch: pytest.MonkeyPatch
):
    user = create_user(repository, "write-owner")
    queries: list[tuple[str, object]] = []
    original_connection = database.connection

    def recording_connection() -> RecordingConnection:
        return RecordingConnection(original_connection(), queries)

    monkeypatch.setattr(database, "connection", recording_connection)

    session = repository.create_session(
        user.id,
        token="session-token",
        csrf_token="csrf-token",
        expires_at=NOW + timedelta(hours=1),
        now=NOW,
    )
    cloud_file = repository.create_cloud_file(
        user.id,
        original_name="report.pdf",
        content_type="application/pdf",
        size_bytes=123,
        storage_path=f"{user.id}/stored-file",
        sha256="a" * 64,
        now=NOW,
    )
    event = repository.create_audit_event(
        user.id,
        event_type="file.created",
        target_type="cloud_file",
        target_id=cloud_file.id,
        now=NOW,
    )

    reads = [
        (query, parameters)
        for query, parameters in queries
        if "SELECT * FROM sessions" in query
        or "SELECT * FROM cloud_files" in query
        or "SELECT * FROM audit_events" in query
    ]

    assert reads == [
        ("SELECT * FROM sessions WHERE id = ? AND user_id = ?", (session.id, user.id)),
        (
            "SELECT * FROM cloud_files WHERE id = ? AND user_id = ?",
            (cloud_file.id, user.id),
        ),
        (
            "SELECT * FROM audit_events WHERE id = ? AND user_id = ?",
            (event.id, user.id),
        ),
    ]


def test_settings_encrypt_tmp_key_and_expose_only_configuration_status(
    repository: CloudRepository, database: Database
):
    user = create_user(repository, "settings-user")
    tmp_key = "plain-tmp-key"

    settings = repository.save_user_settings(
        user.id,
        tmp_key=tmp_key,
        custom_domain="files.example.com",
        now=NOW,
    )

    with database.connection() as connection:
        encrypted = connection.execute(
            "SELECT encrypted_tmp_key FROM user_settings WHERE user_id = ?", (user.id,)
        ).fetchone()[0]
    assert encrypted != tmp_key
    assert tmp_key not in encrypted
    assert settings.key_configured is True
    assert settings.custom_domain == "files.example.com"
    assert repository.get_user_settings(user.id) == settings
    assert repository.get_tmp_key(user.id) == tmp_key
    assert tmp_key not in repr(settings)
    assert encrypted not in repr(settings)


def test_file_queries_and_mutations_are_user_scoped(repository: CloudRepository):
    owner = create_user(repository, "file-owner")
    other = create_user(repository, "file-other")
    cloud_file = repository.create_cloud_file(
        owner.id,
        original_name="report.pdf",
        content_type="application/pdf",
        size_bytes=123,
        storage_path=f"{owner.id}/stored-file",
        sha256="a" * 64,
        now=NOW,
    )

    assert repository.get_cloud_file(owner.id, cloud_file.id) == cloud_file
    assert repository.get_cloud_file(other.id, cloud_file.id) is None
    assert repository.list_cloud_files(owner.id) == [cloud_file]
    assert repository.list_cloud_files(other.id) == []
    assert repository.delete_cloud_file(other.id, cloud_file.id) is False
    assert repository.delete_cloud_file(owner.id, cloud_file.id) is True


def test_file_quota_sums_are_aggregated_per_user_and_globally(
    repository: CloudRepository,
):
    first = create_user(repository, "quota-first")
    second = create_user(repository, "quota-second")
    for index, size in enumerate((100, 250)):
        repository.create_cloud_file(
            first.id,
            original_name=f"first-{index}",
            content_type="application/octet-stream",
            size_bytes=size,
            storage_path=f"{first.id}/{index}",
            sha256=str(index) * 64,
            now=NOW,
        )
    repository.create_cloud_file(
        second.id,
        original_name="second",
        content_type="application/octet-stream",
        size_bytes=400,
        storage_path=f"{second.id}/0",
        sha256="f" * 64,
        now=NOW,
    )

    assert repository.user_storage_bytes(first.id) == 350
    assert repository.user_storage_bytes(second.id) == 400
    assert repository.global_storage_bytes() == 750


def test_automatic_links_are_user_scoped_and_filter_by_source_and_expiry(
    repository: CloudRepository,
):
    owner = create_user(repository, "link-owner")
    other = create_user(repository, "link-other")
    active = repository.save_automatic_download_link(
        owner.id,
        ukey="source-a",
        dkey="active-dkey",
        link="https://files.example/active",
        expires_at=NOW + timedelta(hours=1),
    )
    repository.save_automatic_download_link(
        owner.id,
        ukey="source-a",
        dkey="expired-dkey",
        link="https://files.example/expired",
        expires_at=NOW - timedelta(seconds=1),
    )
    repository.save_automatic_download_link(
        owner.id,
        ukey="source-b",
        dkey="other-source-dkey",
        link="https://files.example/other-source",
        expires_at=None,
    )

    assert repository.get_automatic_download_link(owner.id, active.dkey) == active
    assert repository.get_automatic_download_link(other.id, active.dkey) is None
    assert repository.list_automatic_download_links(
        owner.id, ukey="source-a", active_at=NOW
    ) == [active]
    assert repository.list_automatic_download_links(other.id, active_at=NOW) == []


def test_audit_events_and_rate_limit_attempts_return_typed_records(
    repository: CloudRepository,
):
    user = create_user(repository, "audit-user")
    event = repository.create_audit_event(
        user.id,
        event_type="file.created",
        target_type="cloud_file",
        target_id="file-id",
        now=NOW,
    )
    attempt = repository.record_auth_attempt(
        username="  AUDIT-USER ",
        remote_addr="203.0.113.10",
        successful=False,
        now=NOW,
    )

    assert is_dataclass(event)
    assert repository.list_audit_events(user.id) == [event]
    assert is_dataclass(attempt)
    assert attempt.username == "audit-user"
    assert repository.count_failed_auth_attempts(
        since=NOW - timedelta(minutes=15), username="AUDIT-USER"
    ) == 1
    assert repository.count_failed_auth_attempts(
        since=NOW - timedelta(minutes=15), remote_addr="203.0.113.10"
    ) == 1


def test_auth_attempts_record_invalid_login_identifiers_for_ip_rate_limiting(
    repository: CloudRepository,
):
    login_identifier = "  invalid login name!  "
    remote_addr = "203.0.113.77"

    attempt = repository.record_auth_attempt(
        username=login_identifier,
        remote_addr=remote_addr,
        successful=False,
        now=NOW,
    )

    assert attempt.username == "invalid login name!"
    assert repository.count_failed_auth_attempts(
        since=NOW - timedelta(minutes=15), username=login_identifier
    ) == 1
    assert repository.count_failed_auth_attempts(
        since=NOW - timedelta(minutes=15), remote_addr=remote_addr
    ) == 1
