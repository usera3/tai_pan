from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Event, get_ident

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


class FailingSessionInsertConnection:
    def __init__(self, connection: sqlite3.Connection):
        self._connection = connection

    def close(self) -> None:
        self._connection.close()

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def execute(self, query: str, parameters=()):
        if "INSERT INTO sessions" in query:
            raise sqlite3.OperationalError("injected session insert failure")
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


def registration_session(username: str) -> dict[str, object]:
    return {
        "session_token": f"session-token-for-{username}",
        "csrf_token": f"csrf-token-for-{username}",
        "session_expires_at": NOW + timedelta(hours=1),
    }


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


def test_invited_user_creation_rolls_back_invitation_on_duplicate_username(
    repository: CloudRepository,
):
    admin = create_user(repository, "registration-admin")
    create_user(repository, "existing-registration-user")
    code = "registration-remains-usable"
    repository.create_invitation(created_by=admin.id, code=code, now=NOW)

    with pytest.raises(sqlite3.IntegrityError):
        repository.register_user_with_invitation(
            username="existing-registration-user",
            password_hash="first-password-hash",
            invitation_code=code,
            now=NOW,
            **registration_session("existing-registration-user"),
        )

    registered = repository.register_user_with_invitation(
        username="new-registration-user",
        password_hash="second-password-hash",
        invitation_code=code,
        now=NOW,
        **registration_session("new-registration-user"),
    )

    assert registered is not None
    assert registered.username == "new-registration-user"
    assert repository.get_user_by_username("new-registration-user") == registered
    assert repository.get_active_session_by_token(
        "session-token-for-new-registration-user", now=NOW
    ).user_id == registered.id


def test_invited_user_creation_is_single_use_under_concurrency(
    repository: CloudRepository,
):
    admin = create_user(repository, "concurrent-registration-admin")
    code = "concurrent-registration-code"
    repository.create_invitation(created_by=admin.id, code=code, now=NOW)
    barrier = Barrier(2)

    def register(username: str):
        barrier.wait()
        return repository.register_user_with_invitation(
            username=username,
            password_hash=f"hash-for-{username}",
            invitation_code=code,
            now=NOW,
            **registration_session(username),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(register, ["concurrent-one", "concurrent-two"]))

    registered = [result for result in results if result is not None]
    assert len(registered) == 1
    assert registered[0].username in {"concurrent-one", "concurrent-two"}
    created = [
        repository.get_user_by_username(username)
        for username in ("concurrent-one", "concurrent-two")
    ]
    assert len([user for user in created if user is not None]) == 1


def test_registration_rolls_back_user_and_invitation_when_session_insert_fails(
    repository: CloudRepository,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
):
    admin = create_user(repository, "rollback-registration-admin")
    code = "rollback-registration-code"
    repository.create_invitation(created_by=admin.id, code=code, now=NOW)
    original_connection = database.connection

    def failing_connection() -> FailingSessionInsertConnection:
        return FailingSessionInsertConnection(original_connection())

    monkeypatch.setattr(database, "connection", failing_connection)

    with pytest.raises(sqlite3.OperationalError, match="session insert failure"):
        repository.register_user_with_invitation(
            username="rolled-back-registration-user",
            password_hash="registration-password-hash",
            invitation_code=code,
            now=NOW,
            **registration_session("rolled-back-registration-user"),
        )

    with original_connection() as connection:
        user = connection.execute(
            "SELECT id FROM users WHERE username = ?",
            ("rolled-back-registration-user",),
        ).fetchone()
        invitation = connection.execute(
            "SELECT used_by, used_at FROM invitations WHERE code_hash = ?",
            (hash_secret(code),),
        ).fetchone()
        sessions = connection.execute(
            "SELECT COUNT(*) FROM sessions WHERE token_hash = ?",
            (hash_secret("session-token-for-rolled-back-registration-user"),),
        ).fetchone()[0]

    assert user is None
    assert tuple(invitation) == (None, None)
    assert sessions == 0


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


def test_password_change_rolls_back_password_and_revocations_if_new_session_fails(
    repository: CloudRepository,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
):
    user = create_user(repository, "password-rollback-user")
    old_token = "password-rollback-old-session"
    old_session = repository.create_session(
        user.id,
        token=old_token,
        csrf_token="password-rollback-old-csrf",
        expires_at=NOW + timedelta(hours=1),
        now=NOW,
    )
    original_connection = database.connection

    def failing_connection() -> FailingSessionInsertConnection:
        return FailingSessionInsertConnection(original_connection())

    monkeypatch.setattr(database, "connection", failing_connection)

    with pytest.raises(sqlite3.OperationalError, match="session insert failure"):
        repository.update_password_and_revoke_sessions(
            user.id,
            "replacement-password-hash",
            expected_password_hash=user.password_hash,
            token="password-rollback-new-session",
            csrf_token="password-rollback-new-csrf",
            expires_at=NOW + timedelta(hours=1),
            now=NOW,
        )

    assert repository.get_user(user.id).password_hash == user.password_hash
    assert repository.get_session(user.id, old_session.id).revoked_at is None
    assert repository.get_active_session_by_token(old_token, now=NOW) is not None
    assert (
        repository.get_active_session_by_token(
            "password-rollback-new-session", now=NOW
        )
        is None
    )


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


def test_empty_key_save_does_not_select_and_rewrite_the_old_ciphertext(
    repository: CloudRepository,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
):
    user = create_user(repository, "atomic-settings-query")
    repository.save_user_settings(user.id, tmp_key="old-key", now=NOW)
    queries: list[tuple[str, object]] = []
    original_connection = database.connection
    monkeypatch.setattr(
        database,
        "connection",
        lambda: RecordingConnection(original_connection(), queries),
    )

    repository.save_user_settings(
        user.id,
        tmp_key=None,
        custom_domain="files.example.com",
        now=NOW + timedelta(seconds=1),
    )

    assert not any(
        "SELECT encrypted_tmp_key FROM user_settings" in query
        for query, _ in queries
    )
    assert repository.get_tmp_key(user.id) == "old-key"


def test_empty_key_save_cannot_restore_a_key_cleared_concurrently(
    repository: CloudRepository,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
):
    user = create_user(repository, "atomic-settings-race")
    repository.save_user_settings(user.id, tmp_key="must-stay-cleared", now=NOW)
    ready = Event()
    resume = Event()
    worker_ids: list[int] = []
    original_connection = database.connection

    class ControlledSettingsConnection:
        def __init__(self, connection: sqlite3.Connection):
            self._connection = connection
            self._paused = False

        def __enter__(self):
            self._connection.__enter__()
            return self

        def __exit__(self, *args):
            return self._connection.__exit__(*args)

        def close(self) -> None:
            self._connection.close()

        def execute(self, query: str, parameters=()):
            is_worker = bool(worker_ids) and get_ident() == worker_ids[0]
            selects_old_key = "SELECT encrypted_tmp_key FROM user_settings" in query
            atomic_upsert = (
                "INSERT INTO user_settings" in query
                and "ON CONFLICT(user_id) DO UPDATE" in query
            )
            if is_worker and selects_old_key:
                cursor = self._connection.execute(query, parameters)
                self._paused = True
                ready.set()
                assert resume.wait(timeout=2)
                return cursor
            if is_worker and atomic_upsert and not self._paused:
                self._paused = True
                ready.set()
                assert resume.wait(timeout=2)
            return self._connection.execute(query, parameters)

    monkeypatch.setattr(
        database,
        "connection",
        lambda: ControlledSettingsConnection(original_connection()),
    )

    def preserve_key():
        worker_ids.append(get_ident())
        return repository.save_user_settings(
            user.id,
            tmp_key=None,
            custom_domain="cdn.example.com",
            now=NOW + timedelta(seconds=1),
        )

    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(preserve_key)
        assert ready.wait(timeout=2)
        repository.clear_tmp_key(user.id, now=NOW + timedelta(seconds=2))
        resume.set()
        saved = pending.result(timeout=2)

    assert saved.key_configured is False
    assert repository.get_tmp_key(user.id) is None


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


def test_user_storage_listing_is_typed_and_uses_one_left_join_query(
    repository: CloudRepository,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
):
    first = create_user(repository, "storage-list-first")
    second = create_user(repository, "storage-list-second")
    repository.create_cloud_file(
        first.id,
        original_name="first.bin",
        content_type="application/octet-stream",
        size_bytes=321,
        storage_path=f"{first.id}/first.bin",
        sha256="1" * 64,
        now=NOW,
    )
    queries: list[tuple[str, object]] = []
    original_connection = database.connection
    monkeypatch.setattr(
        database,
        "connection",
        lambda: RecordingConnection(original_connection(), queries),
    )

    listed = repository.list_users_with_storage()

    assert all(is_dataclass(item) for item in listed)
    assert {item.user.id: item.storage_bytes for item in listed} == {
        first.id: 321,
        second.id: 0,
    }
    assert [item.user.id for item in listed] == sorted((first.id, second.id))
    selects = [
        query
        for query, _ in queries
        if query.lstrip().upper().startswith("SELECT")
    ]
    assert len(selects) == 1
    assert "LEFT JOIN" in selects[0].upper()
    assert "GROUP BY" in selects[0].upper()


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


def test_automatic_download_claims_are_atomic_expirable_and_releasable(
    repository: CloudRepository,
    database: Database,
):
    user = create_user(repository, "claim-owner")
    competing_repository = CloudRepository(
        database,
        KeyCipher(Fernet.generate_key()),
    )
    first_expiry = NOW + timedelta(minutes=1)

    assert repository.try_claim_automatic_download(
        user.id,
        ukey="SAME-UKEY",
        claim_token="first-token",
        expires_at=first_expiry,
        now=NOW,
    )
    assert not competing_repository.try_claim_automatic_download(
        user.id,
        ukey="SAME-UKEY",
        claim_token="second-token",
        expires_at=first_expiry + timedelta(minutes=1),
        now=first_expiry - timedelta(microseconds=1),
    )
    assert competing_repository.try_claim_automatic_download(
        user.id,
        ukey="SAME-UKEY",
        claim_token="second-token",
        expires_at=first_expiry + timedelta(minutes=1),
        now=first_expiry,
    )
    assert not repository.release_automatic_download_claim(
        user.id,
        ukey="SAME-UKEY",
        claim_token="first-token",
    )
    assert competing_repository.release_automatic_download_claim(
        user.id,
        ukey="SAME-UKEY",
        claim_token="second-token",
    )


def test_automatic_download_claim_renewal_extends_only_its_own_token(
    repository: CloudRepository,
    database: Database,
):
    user = create_user(repository, "claim-renewal-owner")
    competing_repository = CloudRepository(
        database,
        KeyCipher(Fernet.generate_key()),
    )
    initial_expiry = NOW + timedelta(minutes=1)
    renewed_expiry = NOW + timedelta(minutes=2)

    assert repository.try_claim_automatic_download(
        user.id,
        ukey="SAME-UKEY",
        claim_token="winner-token",
        expires_at=initial_expiry,
        now=NOW,
    )
    assert not competing_repository.renew_automatic_download_claim(
        user.id,
        ukey="SAME-UKEY",
        claim_token="loser-token",
        expires_at=renewed_expiry,
        now=NOW + timedelta(seconds=30),
    )
    assert repository.renew_automatic_download_claim(
        user.id,
        ukey="SAME-UKEY",
        claim_token="winner-token",
        expires_at=renewed_expiry,
        now=NOW + timedelta(seconds=30),
    )
    assert not competing_repository.try_claim_automatic_download(
        user.id,
        ukey="SAME-UKEY",
        claim_token="loser-token",
        expires_at=renewed_expiry + timedelta(minutes=1),
        now=initial_expiry,
    )
    assert repository.try_claim_automatic_download(
        user.id,
        ukey="EXPIRED-UKEY",
        claim_token="expired-token",
        expires_at=initial_expiry,
        now=NOW,
    )
    assert not repository.renew_automatic_download_claim(
        user.id,
        ukey="EXPIRED-UKEY",
        claim_token="expired-token",
        expires_at=renewed_expiry,
        now=initial_expiry,
    )
    assert competing_repository.try_claim_automatic_download(
        user.id,
        ukey="EXPIRED-UKEY",
        claim_token="replacement-token",
        expires_at=renewed_expiry,
        now=initial_expiry,
    )


def test_completing_a_claim_atomically_replaces_it_with_one_real_link(
    repository: CloudRepository,
    database: Database,
):
    user = create_user(repository, "claim-completion")
    assert repository.try_claim_automatic_download(
        user.id,
        ukey="CLAIMED-UKEY",
        claim_token="winner-token",
        expires_at=NOW + timedelta(minutes=1),
        now=NOW,
    )

    link = repository.complete_automatic_download_claim(
        user.id,
        ukey="CLAIMED-UKEY",
        claim_token="winner-token",
        dkey="REAL-DKEY",
        link="https://files.example/real",
        expires_at=NOW + timedelta(hours=24),
    )

    assert link is not None
    assert link.dkey == "REAL-DKEY"
    with database.connection() as connection:
        claim_count = connection.execute(
            "SELECT COUNT(*) FROM automatic_download_claims WHERE user_id = ?",
            (user.id,),
        ).fetchone()[0]
        link_count = connection.execute(
            "SELECT COUNT(*) FROM automatic_download_links WHERE user_id = ? AND ukey = ?",
            (user.id, "CLAIMED-UKEY"),
        ).fetchone()[0]
    assert claim_count == 0
    assert link_count == 1


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


def test_login_ip_rate_limit_ignores_registration_attempts(
    repository: CloudRepository,
):
    remote_addr = "203.0.113.78"
    since = NOW - timedelta(minutes=15)

    for index in range(5):
        assert repository.claim_registration_submission(
            remote_addr=remote_addr,
            since=since,
            limit=10,
            now=NOW + timedelta(seconds=index),
        )

    assert repository.count_registration_attempts(
        since=since, remote_addr=remote_addr
    ) == 5
    assert repository.count_failed_auth_attempts(
        since=since, remote_addr=remote_addr
    ) == 0
    assert repository.claim_failed_login_attempt(
        username="login-user",
        remote_addr=remote_addr,
        since=since,
        limit=5,
        now=NOW + timedelta(seconds=5),
    )

    for index in range(4):
        assert repository.claim_failed_login_attempt(
            username=f"login-user-{index}",
            remote_addr=remote_addr,
            since=since,
            limit=5,
            now=NOW + timedelta(seconds=index + 6),
        )

    assert not repository.claim_failed_login_attempt(
        username="login-user-final",
        remote_addr=remote_addr,
        since=since,
        limit=5,
        now=NOW + timedelta(seconds=10),
    )
    assert repository.count_failed_auth_attempts(
        since=since, remote_addr=remote_addr
    ) == 5
