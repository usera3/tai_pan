from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.cloud.app import create_cloud_app
from app.cloud.config import CloudConfig
from app.cloud.db import Database
from app.cloud.security import PasswordService, hash_secret


PUBLIC_ORIGIN = "https://cloud.example.com"
ADMIN_PASSWORD = "permanent administrator password"
USER_PASSWORD = "correct horse battery staple"


@pytest.fixture
def config(tmp_path: Path) -> CloudConfig:
    return CloudConfig(
        mode="cloud",
        session_secret="test-session-secret",
        key_encryption_key=Fernet.generate_key().decode("ascii"),
        database_path=tmp_path / "cloud.db",
        storage_path=tmp_path / "files",
        public_origin=PUBLIC_ORIGIN,
    )


@pytest.fixture
def database(config: CloudConfig) -> Database:
    assert config.database_path is not None
    return Database(config.database_path)


@pytest.fixture
def cloud_app(config: CloudConfig, database: Database):
    return create_cloud_app(config, database)


def create_user(cloud_app, username: str, password: str = USER_PASSWORD, **overrides):
    return cloud_app.state.repository.create_user(
        username,
        cloud_app.state.password_service.hash(password),
        **overrides,
    )


def login(client: TestClient, username: str, password: str) -> str:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200
    return response.json()["csrf_token"]


def csrf_headers(csrf_token: str, origin: str = PUBLIC_ORIGIN) -> dict[str, str]:
    return {"Origin": origin, "X-CSRF-Token": csrf_token}


def pending_credentials_path(credentials_file: Path) -> Path:
    return credentials_file.with_name(f".{credentials_file.name}.pending")


def legacy_pin_path(credentials_file: Path) -> Path:
    return credentials_file.with_name(f".{credentials_file.name}.pending.pin")


def write_pending_credentials(
    credentials_file: Path,
    *,
    username: str,
    temporary_password: str,
) -> Path:
    pending_path = pending_credentials_path(credentials_file)
    pending_path.write_text(
        json.dumps(
            {
                "username": username,
                "temporary_password": temporary_password,
            }
        ),
        encoding="utf-8",
    )
    pending_path.chmod(0o600)
    return pending_path


@pytest.fixture
def admin(cloud_app):
    return create_user(
        cloud_app,
        "administrator",
        password=ADMIN_PASSWORD,
        role="admin",
    )


@pytest.fixture
def admin_client(cloud_app, admin):
    with TestClient(cloud_app, base_url=PUBLIC_ORIGIN) as client:
        csrf_token = login(client, admin.username, ADMIN_PASSWORD)
        yield client, csrf_token


def test_ordinary_users_are_forbidden_from_every_admin_endpoint(cloud_app):
    ordinary = create_user(cloud_app, "ordinary-user")
    target = create_user(cloud_app, "target-user")
    creator = create_user(cloud_app, "invitation-admin", role="admin")
    invitation = cloud_app.state.repository.create_invitation(
        created_by=creator.id,
        code="ordinary-user-must-not-see-this",
    )

    with TestClient(cloud_app, base_url=PUBLIC_ORIGIN) as client:
        csrf_token = login(client, ordinary.username, USER_PASSWORD)
        headers = csrf_headers(csrf_token)
        responses = [
            client.get("/api/admin/users"),
            client.get("/api/admin/invitations"),
            client.post("/api/admin/invitations", headers=headers, json={}),
            client.patch(
                f"/api/admin/users/{target.id}",
                headers=headers,
                json={"status": "disabled"},
            ),
            client.post(
                f"/api/admin/users/{target.id}/reset-password",
                headers=headers,
            ),
            client.delete(
                f"/api/admin/invitations/{invitation.id}", headers=headers
            ),
        ]

    assert {response.status_code for response in responses} == {403}
    assert all(
        response.json() == {"detail": "Administrator access required"}
        for response in responses
    )


def test_every_admin_mutation_requires_csrf_and_matching_origin(
    cloud_app, admin, admin_client
):
    client, csrf_token = admin_client
    target = create_user(cloud_app, "csrf-target")
    invitation = cloud_app.state.repository.create_invitation(
        created_by=admin.id,
        code="csrf-protected-invitation",
    )
    bad_headers = csrf_headers(csrf_token, origin="https://attacker.example")

    responses = [
        client.post("/api/admin/invitations", headers=bad_headers, json={}),
        client.patch(
            f"/api/admin/users/{target.id}",
            headers=bad_headers,
            json={"status": "disabled"},
        ),
        client.post(
            f"/api/admin/users/{target.id}/reset-password",
            headers=bad_headers,
        ),
        client.delete(
            f"/api/admin/invitations/{invitation.id}", headers=bad_headers
        ),
    ]

    assert {response.status_code for response in responses} == {403}
    assert all(
        response.json() == {"detail": "CSRF validation failed"}
        for response in responses
    )
    assert cloud_app.state.repository.get_user(target.id).status == "active"
    with cloud_app.state.database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM invitations WHERE id = ?", (invitation.id,)
        ).fetchone()[0] == 1


def test_every_admin_mutation_rejects_wrong_csrf_with_correct_origin(
    cloud_app, database: Database, admin, admin_client
):
    client, _ = admin_client
    target = create_user(cloud_app, "wrong-csrf-target")
    original_password_hash = target.password_hash
    invitation = cloud_app.state.repository.create_invitation(
        created_by=admin.id,
        code="wrong-csrf-protected-invitation",
    )
    bad_headers = csrf_headers("wrong-csrf-token")

    responses = [
        client.post("/api/admin/invitations", headers=bad_headers, json={}),
        client.patch(
            f"/api/admin/users/{target.id}",
            headers=bad_headers,
            json={"status": "disabled"},
        ),
        client.post(
            f"/api/admin/users/{target.id}/reset-password",
            headers=bad_headers,
        ),
        client.delete(
            f"/api/admin/invitations/{invitation.id}", headers=bad_headers
        ),
    ]

    assert {response.status_code for response in responses} == {403}
    assert all(
        response.json() == {"detail": "CSRF validation failed"}
        for response in responses
    )
    unchanged = cloud_app.state.repository.get_user(target.id)
    assert unchanged is not None
    assert unchanged.status == "active"
    assert unchanged.password_hash == original_password_hash
    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM invitations"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM invitations WHERE id = ?", (invitation.id,)
        ).fetchone()[0] == 1


def test_invitation_plaintext_is_returned_once_and_used_code_cannot_be_revoked_or_reused(
    cloud_app, database: Database, admin_client
):
    client, csrf_token = admin_client

    created = client.post(
        "/api/admin/invitations",
        headers=csrf_headers(csrf_token),
        json={"expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()},
    )

    assert created.status_code == 201
    body = created.json()
    invitation_code = body["code"]
    invitation = body["invitation"]
    assert len(invitation_code) >= 32
    assert invitation["status"] == "available"
    assert "code_hash" not in invitation
    assert invitation_code not in str(invitation)
    assert created.text.count(invitation_code) == 1
    assert hash_secret(invitation_code) not in created.text
    from app.cloud.routes.admin import CreateInvitationResponse

    response_model = CreateInvitationResponse.model_validate(body)
    assert invitation_code not in repr(response_model)
    assert invitation_code not in str(response_model)

    with database.connection() as connection:
        stored = connection.execute(
            "SELECT * FROM invitations WHERE id = ?", (invitation["id"],)
        ).fetchone()
    assert stored["code_hash"] == hash_secret(invitation_code)
    assert invitation_code not in tuple(value for value in stored if value is not None)

    listed = client.get("/api/admin/invitations")
    assert listed.status_code == 200
    assert listed.json() == [invitation]
    assert invitation_code not in listed.text
    assert hash_secret(invitation_code) not in listed.text

    with TestClient(cloud_app, base_url=PUBLIC_ORIGIN) as registration_client:
        registered = registration_client.post(
            "/api/auth/register",
            json={
                "username": "invited-user",
                "password": USER_PASSWORD,
                "invitation_code": invitation_code,
            },
        )
    assert registered.status_code == 201

    revoke = client.delete(
        f"/api/admin/invitations/{invitation['id']}",
        headers=csrf_headers(csrf_token),
    )
    assert revoke.status_code == 409
    assert revoke.json() == {"detail": "Invitation cannot be revoked"}
    assert invitation_code not in revoke.text

    with TestClient(cloud_app, base_url=PUBLIC_ORIGIN) as reuse_client:
        reused = reuse_client.post(
            "/api/auth/register",
            json={
                "username": "second-invited-user",
                "password": USER_PASSWORD,
                "invitation_code": invitation_code,
            },
        )
    assert reused.status_code == 400
    assert invitation_code not in reused.text

    used_list = client.get("/api/admin/invitations")
    assert used_list.status_code == 200
    assert used_list.json()[0]["status"] == "used"
    assert used_list.json()[0]["used_by"] == registered.json()["user"]["id"]
    assert invitation_code not in used_list.text


def test_unused_invitation_can_be_revoked_and_then_cannot_be_used(
    cloud_app, database: Database, admin_client
):
    client, csrf_token = admin_client
    created = client.post(
        "/api/admin/invitations", headers=csrf_headers(csrf_token), json={}
    )
    invitation_id = created.json()["invitation"]["id"]
    invitation_code = created.json()["code"]

    revoked = client.delete(
        f"/api/admin/invitations/{invitation_id}",
        headers=csrf_headers(csrf_token),
    )

    assert revoked.status_code == 200
    assert revoked.json() == {"message": "Invitation revoked"}
    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM invitations WHERE id = ?", (invitation_id,)
        ).fetchone()[0] == 0
    with TestClient(cloud_app, base_url=PUBLIC_ORIGIN) as registration_client:
        rejected = registration_client.post(
            "/api/auth/register",
            json={
                "username": "revoked-invitation-user",
                "password": USER_PASSWORD,
                "invitation_code": invitation_code,
            },
        )
    assert rejected.status_code == 400
    assert invitation_code not in rejected.text


def test_disabling_user_atomically_revokes_all_sessions_and_restore_creates_none(
    cloud_app, database: Database, admin_client
):
    client, csrf_token = admin_client
    target = create_user(cloud_app, "disable-target")
    tokens = ["first-disable-session", "second-disable-session"]
    for index, token in enumerate(tokens):
        cloud_app.state.repository.create_session(
            target.id,
            token=token,
            csrf_token=f"disable-csrf-{index}",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

    disabled = client.patch(
        f"/api/admin/users/{target.id}",
        headers=csrf_headers(csrf_token),
        json={"status": "disabled"},
    )

    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"
    assert all(
        cloud_app.state.repository.get_active_session_by_token(token) is None
        for token in tokens
    )
    with database.connection() as connection:
        row = connection.execute(
            "SELECT status, updated_at FROM users WHERE id = ?", (target.id,)
        ).fetchone()
        revocations = connection.execute(
            "SELECT revoked_at FROM sessions WHERE user_id = ?", (target.id,)
        ).fetchall()
    assert row["status"] == "disabled"
    assert {item["revoked_at"] for item in revocations} == {row["updated_at"]}

    restored = client.patch(
        f"/api/admin/users/{target.id}",
        headers=csrf_headers(csrf_token),
        json={"status": "active"},
    )

    assert restored.status_code == 200
    assert restored.json()["status"] == "active"
    with database.connection() as connection:
        assert connection.execute(
            """
            SELECT COUNT(*) FROM sessions
            WHERE user_id = ? AND revoked_at IS NULL
            """,
            (target.id,),
        ).fetchone()[0] == 0


def test_session_revocation_failure_rolls_back_user_disable(
    cloud_app, database: Database, monkeypatch: pytest.MonkeyPatch
):
    target = create_user(cloud_app, "atomic-disable-target")
    cloud_app.state.repository.create_session(
        target.id,
        token="atomic-disable-session",
        csrf_token="atomic-disable-csrf",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    original_connection = database.connection

    class FailingRevocationConnection:
        def __init__(self):
            self._connection = original_connection()

        def __enter__(self):
            self._connection.__enter__()
            return self

        def __exit__(self, *args):
            return self._connection.__exit__(*args)

        def close(self):
            self._connection.close()

        def commit(self):
            self._connection.commit()

        def rollback(self):
            self._connection.rollback()

        def execute(self, query: str, parameters=()):
            if "UPDATE sessions SET revoked_at" in query:
                raise sqlite3.OperationalError("injected revocation failure")
            return self._connection.execute(query, parameters)

    monkeypatch.setattr(database, "connection", FailingRevocationConnection)

    with pytest.raises(sqlite3.OperationalError, match="injected revocation failure"):
        cloud_app.state.repository.set_user_status_and_revoke_sessions(
            target.id, "disabled"
        )

    assert cloud_app.state.repository.get_user(target.id).status == "active"
    assert (
        cloud_app.state.repository.get_active_session_by_token(
            "atomic-disable-session"
        )
        is not None
    )


def test_admin_accounts_cannot_be_disabled_or_reset(cloud_app, admin, admin_client):
    client, csrf_token = admin_client
    other_admin = create_user(cloud_app, "other-administrator", role="admin")

    responses = [
        client.patch(
            f"/api/admin/users/{admin.id}",
            headers=csrf_headers(csrf_token),
            json={"status": "disabled"},
        ),
        client.patch(
            f"/api/admin/users/{other_admin.id}",
            headers=csrf_headers(csrf_token),
            json={"status": "disabled"},
        ),
        client.post(
            f"/api/admin/users/{admin.id}/reset-password",
            headers=csrf_headers(csrf_token),
        ),
    ]

    assert {response.status_code for response in responses} == {409}
    assert all(
        response.json() == {"detail": "Administrator accounts cannot be modified"}
        for response in responses
    )
    assert cloud_app.state.repository.get_user(admin.id).status == "active"
    assert cloud_app.state.repository.get_user(other_admin.id).status == "active"


def test_reset_password_returns_plaintext_once_stores_only_argon2_hash_and_revokes_sessions(
    cloud_app, database: Database, admin_client
):
    client, csrf_token = admin_client
    target = create_user(cloud_app, "password-reset-target")
    plaintext_tmp_key = "tmp-key-admin-must-never-read"
    cloud_app.state.repository.save_user_settings(
        target.id,
        tmp_key=plaintext_tmp_key,
    )
    session_tokens = ["reset-session-one", "reset-session-two"]
    for index, token in enumerate(session_tokens):
        cloud_app.state.repository.create_session(
            target.id,
            token=token,
            csrf_token=f"reset-csrf-{index}",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
    with database.connection() as connection:
        encrypted_tmp_key = connection.execute(
            "SELECT encrypted_tmp_key FROM user_settings WHERE user_id = ?",
            (target.id,),
        ).fetchone()[0]

    reset = client.post(
        f"/api/admin/users/{target.id}/reset-password",
        headers=csrf_headers(csrf_token),
    )

    assert reset.status_code == 200
    body = reset.json()
    temporary_password = body["temporary_password"]
    assert len(temporary_password) >= 32
    assert reset.text.count(temporary_password) == 1
    assert body["user"]["id"] == target.id
    assert body["user"]["must_change_password"] is True
    assert plaintext_tmp_key not in reset.text
    assert encrypted_tmp_key not in reset.text
    assert "password_hash" not in reset.text
    from app.cloud.routes.admin import ResetPasswordResponse

    response_model = ResetPasswordResponse.model_validate(body)
    assert temporary_password not in repr(response_model)
    assert temporary_password not in str(response_model)

    with database.connection() as connection:
        stored_user = connection.execute(
            "SELECT password_hash, must_change_password FROM users WHERE id = ?",
            (target.id,),
        ).fetchone()
        active_sessions = connection.execute(
            """
            SELECT COUNT(*) FROM sessions
            WHERE user_id = ? AND revoked_at IS NULL
            """,
            (target.id,),
        ).fetchone()[0]
    assert stored_user["password_hash"].startswith("$argon2")
    assert cloud_app.state.password_service.verify(
        stored_user["password_hash"], temporary_password
    )
    assert temporary_password not in tuple(stored_user)
    assert stored_user["must_change_password"] == 1
    assert active_sessions == 0

    users = client.get("/api/admin/users")
    assert users.status_code == 200
    assert plaintext_tmp_key not in users.text
    assert encrypted_tmp_key not in users.text
    assert temporary_password not in users.text
    assert "password_hash" not in users.text

    with TestClient(cloud_app, base_url=PUBLIC_ORIGIN) as target_client:
        old_login = target_client.post(
            "/api/auth/login",
            json={"username": target.username, "password": USER_PASSWORD},
        )
        temporary_login = target_client.post(
            "/api/auth/login",
            json={"username": target.username, "password": temporary_password},
        )
        assert old_login.status_code == 401
        assert temporary_login.status_code == 200
        assert temporary_login.json()["user"]["must_change_password"] is True
        assert target_client.get("/api/admin/users").status_code == 403


def test_user_listing_contains_metadata_and_storage_usage_but_no_tmp_credentials(
    cloud_app,
    database: Database,
    admin_client,
    monkeypatch: pytest.MonkeyPatch,
):
    client, _ = admin_client
    target = create_user(cloud_app, "listed-user")
    plaintext_tmp_key = "listed-users-plaintext-tmp-key"
    cloud_app.state.repository.save_user_settings(target.id, tmp_key=plaintext_tmp_key)
    cloud_app.state.repository.create_cloud_file(
        target.id,
        original_name="usage.bin",
        content_type="application/octet-stream",
        size_bytes=1234,
        storage_path=f"{target.id}/usage.bin",
        sha256="a" * 64,
    )
    with database.connection() as connection:
        encrypted_tmp_key = connection.execute(
            "SELECT encrypted_tmp_key FROM user_settings WHERE user_id = ?",
            (target.id,),
        ).fetchone()[0]

    def fail_per_user_storage_query(user_id: str) -> int:
        raise AssertionError("admin user listing must use one aggregate query")

    monkeypatch.setattr(
        cloud_app.state.repository,
        "user_storage_bytes",
        fail_per_user_storage_query,
    )

    response = client.get("/api/admin/users")

    assert response.status_code == 200
    listed = next(item for item in response.json() if item["id"] == target.id)
    created_at = datetime.fromisoformat(listed.pop("created_at").replace("Z", "+00:00"))
    updated_at = datetime.fromisoformat(listed.pop("updated_at").replace("Z", "+00:00"))
    assert created_at == target.created_at
    assert updated_at == target.updated_at
    assert listed == {
        "id": target.id,
        "username": target.username,
        "role": "user",
        "status": "active",
        "must_change_password": False,
        "last_login_at": None,
        "storage_bytes": 1234,
    }
    assert plaintext_tmp_key not in response.text
    assert encrypted_tmp_key not in response.text
    assert "tmp_key" not in response.text


def run_admin_cli(
    database_path: Path, credentials_file: Path, username: str = "bootstrap-admin"
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "app.cloud.admin_cli",
            "--database-path",
            str(database_path),
            "--username",
            username,
            "--credentials-file",
            str(credentials_file),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_initial_admin_cli_writes_password_once_with_0600_and_requires_first_change(
    tmp_path: Path,
):
    database_path = tmp_path / "bootstrap.db"
    credentials_file = tmp_path / "initial-admin-password"

    result = run_admin_cli(database_path, credentials_file)

    assert result.returncode == 0
    credentials = json.loads(credentials_file.read_text(encoding="utf-8"))
    assert set(credentials) == {"username", "temporary_password"}
    assert credentials["username"] == "bootstrap-admin"
    temporary_password = credentials["temporary_password"]
    assert len(temporary_password) >= 32
    assert credentials_file.read_text(encoding="utf-8").count(temporary_password) == 1
    assert temporary_password not in result.stdout
    assert temporary_password not in result.stderr
    assert stat.S_IMODE(credentials_file.stat().st_mode) == 0o600

    database = Database(database_path)
    with database.connection() as connection:
        row = connection.execute(
            "SELECT * FROM users WHERE username = 'bootstrap-admin'"
        ).fetchone()
    assert row["role"] == "admin"
    assert row["status"] == "active"
    assert row["must_change_password"] == 1
    assert PasswordService().verify(row["password_hash"], temporary_password)
    assert temporary_password not in tuple(value for value in row if value is not None)

    config = CloudConfig(
        mode="cloud",
        session_secret="bootstrap-session-secret",
        key_encryption_key=Fernet.generate_key().decode("ascii"),
        database_path=database_path,
        storage_path=tmp_path / "bootstrap-files",
        public_origin=PUBLIC_ORIGIN,
    )
    app = create_cloud_app(config, database)
    with TestClient(app, base_url=PUBLIC_ORIGIN) as client:
        csrf_token = login(client, "bootstrap-admin", temporary_password)
        assert client.get("/api/admin/users").status_code == 403
        changed = client.post(
            "/api/auth/change-password",
            headers=csrf_headers(csrf_token),
            json={
                "current_password": temporary_password,
                "new_password": ADMIN_PASSWORD,
            },
        )
        assert changed.status_code == 200
        assert client.get("/api/admin/users").status_code == 200


def test_initial_admin_bootstrap_never_overwrites_existing_admin_or_credentials(
    tmp_path: Path,
):
    database_path = tmp_path / "bootstrap.db"
    credentials_file = tmp_path / "initial-admin-password"
    first = run_admin_cli(database_path, credentials_file)
    assert first.returncode == 0
    original_credentials = credentials_file.read_bytes()
    alternate_credentials = tmp_path / "alternate-password"

    same_path = run_admin_cli(database_path, credentials_file, "replacement-admin")
    alternate_path = run_admin_cli(
        database_path, alternate_credentials, "replacement-admin"
    )

    assert same_path.returncode != 0
    assert alternate_path.returncode != 0
    assert credentials_file.read_bytes() == original_credentials
    assert not alternate_credentials.exists()
    with Database(database_path).connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'admin'"
        ).fetchone()[0] == 1


def test_initial_admin_bootstrap_rolls_back_when_credential_write_fails(
    tmp_path: Path,
):
    from app.cloud.admin_cli import BootstrapError, bootstrap_initial_admin

    database_path = tmp_path / "bootstrap.db"
    database = Database(database_path)
    credentials_file = tmp_path / "occupied-credentials"
    credentials_file.write_text("do-not-overwrite", encoding="utf-8")

    with pytest.raises(BootstrapError, match="Initial administrator could not be created"):
        bootstrap_initial_admin(
            database=database,
            username="bootstrap-admin",
            credentials_file=credentials_file,
        )

    assert credentials_file.read_text(encoding="utf-8") == "do-not-overwrite"
    database.initialize()
    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'admin'"
        ).fetchone()[0] == 0


def test_initial_admin_bootstrap_removes_credentials_when_user_insert_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from app.cloud.admin_cli import BootstrapError, bootstrap_initial_admin

    database = Database(tmp_path / "bootstrap.db")
    database.initialize()
    credentials_file = tmp_path / "rolled-back-credentials"
    original_connection = database.connection

    class FailingUserInsertConnection:
        def __init__(self):
            self._connection = original_connection()

        def __enter__(self):
            self._connection.__enter__()
            return self

        def __exit__(self, *args):
            return self._connection.__exit__(*args)

        def close(self):
            self._connection.close()

        def commit(self):
            self._connection.commit()

        def rollback(self):
            self._connection.rollback()

        def execute(self, query: str, parameters=()):
            if "INSERT INTO users" in query:
                raise sqlite3.OperationalError("injected user insert failure")
            return self._connection.execute(query, parameters)

    monkeypatch.setattr(database, "connection", FailingUserInsertConnection)

    with pytest.raises(BootstrapError, match="Initial administrator could not be created"):
        bootstrap_initial_admin(
            database=database,
            username="bootstrap-admin",
            credentials_file=credentials_file,
        )

    assert not credentials_file.exists()
    assert not pending_credentials_path(credentials_file).exists()
    with original_connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'admin'"
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(
            sqlite3.OperationalError("injected post-commit failure"),
            id="exception",
        ),
        pytest.param(KeyboardInterrupt(), id="keyboard-interrupt"),
    ],
)
def test_initial_admin_bootstrap_recovers_when_commit_completed_before_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
):
    from app.cloud.admin_cli import bootstrap_initial_admin

    database = Database(tmp_path / "bootstrap.db")
    database.initialize()
    credentials_file = tmp_path / "committed-credentials.json"
    original_connection = database.connection
    failure_raised = False

    class CommitThenFailConnection:
        def __init__(self):
            self._connection = original_connection()

        def __enter__(self):
            self._connection.__enter__()
            return self

        def __exit__(self, *args):
            return self._connection.__exit__(*args)

        def close(self):
            self._connection.close()

        def commit(self):
            nonlocal failure_raised
            self._connection.commit()
            if not failure_raised:
                failure_raised = True
                raise failure

        def rollback(self):
            self._connection.rollback()

        def execute(self, query: str, parameters=()):
            return self._connection.execute(query, parameters)

    monkeypatch.setattr(database, "connection", CommitThenFailConnection)

    try:
        user_id = bootstrap_initial_admin(
            database=database,
            username="bootstrap-admin",
            credentials_file=credentials_file,
        )
    except BaseException as exc:
        pytest.fail(
            f"bootstrap did not recover after {type(failure).__name__}: "
            f"{type(exc).__name__}"
        )

    credentials = json.loads(credentials_file.read_text(encoding="utf-8"))
    assert credentials["username"] == "bootstrap-admin"
    assert not pending_credentials_path(credentials_file).exists()
    with original_connection() as connection:
        row = connection.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    assert row is not None
    assert PasswordService().verify(
        row["password_hash"], credentials["temporary_password"]
    )


def test_initial_admin_bootstrap_cleans_pending_and_rolls_back_on_keyboard_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from app.cloud.admin_cli import bootstrap_initial_admin

    database = Database(tmp_path / "bootstrap.db")
    database.initialize()
    credentials_file = tmp_path / "interrupted-credentials.json"
    original_connection = database.connection

    class InterruptedCommitConnection:
        def __init__(self):
            self._connection = original_connection()

        def __enter__(self):
            self._connection.__enter__()
            return self

        def __exit__(self, *args):
            return self._connection.__exit__(*args)

        def close(self):
            self._connection.close()

        def commit(self):
            raise KeyboardInterrupt

        def rollback(self):
            self._connection.rollback()

        def execute(self, query: str, parameters=()):
            return self._connection.execute(query, parameters)

    monkeypatch.setattr(database, "connection", InterruptedCommitConnection)

    with pytest.raises(KeyboardInterrupt):
        bootstrap_initial_admin(
            database=database,
            username="bootstrap-admin",
            credentials_file=credentials_file,
        )

    assert not credentials_file.exists()
    assert not pending_credentials_path(credentials_file).exists()
    with original_connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'admin'"
        ).fetchone()[0] == 0


def test_initial_admin_bootstrap_restarts_after_pending_only_crash(tmp_path: Path):
    from app.cloud.admin_cli import bootstrap_initial_admin

    database = Database(tmp_path / "bootstrap.db")
    database.initialize()
    credentials_file = tmp_path / "pending-only-credentials.json"
    abandoned_password = "abandoned-pending-password"
    write_pending_credentials(
        credentials_file,
        username="bootstrap-admin",
        temporary_password=abandoned_password,
    )

    user_id = bootstrap_initial_admin(
        database=database,
        username="bootstrap-admin",
        credentials_file=credentials_file,
    )

    credentials = json.loads(credentials_file.read_text(encoding="utf-8"))
    assert credentials["temporary_password"] != abandoned_password
    assert not pending_credentials_path(credentials_file).exists()
    with database.connection() as connection:
        row = connection.execute(
            "SELECT password_hash FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    assert PasswordService().verify(
        row["password_hash"], credentials["temporary_password"]
    )


def test_initial_admin_bootstrap_finalizes_matching_committed_pending(tmp_path: Path):
    from app.cloud.admin_cli import bootstrap_initial_admin

    database = Database(tmp_path / "bootstrap.db")
    database.initialize()
    credentials_file = tmp_path / "committed-pending-credentials.json"
    temporary_password = "committed-pending-password"
    pending_path = write_pending_credentials(
        credentials_file,
        username="bootstrap-admin",
        temporary_password=temporary_password,
    )
    user_id = "committed-admin-id"
    timestamp = datetime.now(timezone.utc).isoformat()
    with database.connection() as connection:
        connection.execute(
            """
            INSERT INTO users (
                id, username, password_hash, role, status,
                must_change_password, created_at, updated_at
            ) VALUES (?, 'bootstrap-admin', ?, 'admin', 'active', 1, ?, ?)
            """,
            (
                user_id,
                PasswordService().hash(temporary_password),
                timestamp,
                timestamp,
            ),
        )

    recovered_id = bootstrap_initial_admin(
        database=database,
        username="bootstrap-admin",
        credentials_file=credentials_file,
    )

    assert recovered_id == user_id
    assert not pending_path.exists()
    assert json.loads(credentials_file.read_text(encoding="utf-8")) == {
        "username": "bootstrap-admin",
        "temporary_password": temporary_password,
    }


@pytest.mark.parametrize("entry_type", ["file", "symlink"])
def test_initial_admin_bootstrap_refuses_existing_final_entry(
    tmp_path: Path, entry_type: str
):
    from app.cloud.admin_cli import BootstrapError, bootstrap_initial_admin

    database = Database(tmp_path / "bootstrap.db")
    credentials_file = tmp_path / "existing-final-credentials.json"
    if entry_type == "file":
        credentials_file.write_text("do-not-overwrite", encoding="utf-8")
    else:
        target = tmp_path / "final-symlink-target"
        target.write_text("do-not-overwrite", encoding="utf-8")
        credentials_file.symlink_to(target)

    with pytest.raises(BootstrapError):
        bootstrap_initial_admin(
            database=database,
            username="bootstrap-admin",
            credentials_file=credentials_file,
        )

    if entry_type == "file":
        assert credentials_file.read_text(encoding="utf-8") == "do-not-overwrite"
    else:
        assert credentials_file.is_symlink()
        assert target.read_text(encoding="utf-8") == "do-not-overwrite"
    database.initialize()
    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'admin'"
        ).fetchone()[0] == 0


@pytest.mark.parametrize("entry_type", ["file", "symlink"])
def test_initial_admin_bootstrap_refuses_unsafe_pending_entry(
    tmp_path: Path, entry_type: str
):
    from app.cloud.admin_cli import BootstrapError, bootstrap_initial_admin

    database = Database(tmp_path / "bootstrap.db")
    credentials_file = tmp_path / "unsafe-pending-credentials.json"
    pending_path = pending_credentials_path(credentials_file)
    if entry_type == "file":
        pending_path.write_text("not valid credential JSON", encoding="utf-8")
    else:
        target = tmp_path / "pending-symlink-target"
        target.write_text("do-not-overwrite", encoding="utf-8")
        pending_path.symlink_to(target)

    with pytest.raises(BootstrapError):
        bootstrap_initial_admin(
            database=database,
            username="bootstrap-admin",
            credentials_file=credentials_file,
        )

    assert not credentials_file.exists()
    if entry_type == "file":
        assert pending_path.read_text(encoding="utf-8") == "not valid credential JSON"
    else:
        assert pending_path.is_symlink()
        assert target.read_text(encoding="utf-8") == "do-not-overwrite"
    database.initialize()
    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'admin'"
        ).fetchone()[0] == 0


def test_initial_admin_bootstrap_refuses_symlink_parent(tmp_path: Path):
    from app.cloud.admin_cli import BootstrapError, bootstrap_initial_admin

    database = Database(tmp_path / "bootstrap.db")
    real_parent = tmp_path / "real-credentials-parent"
    real_parent.mkdir()
    symlink_parent = tmp_path / "credentials-parent-link"
    symlink_parent.symlink_to(real_parent, target_is_directory=True)
    credentials_file = symlink_parent / "initial-admin.json"

    with pytest.raises(BootstrapError):
        bootstrap_initial_admin(
            database=database,
            username="bootstrap-admin",
            credentials_file=credentials_file,
        )

    assert not (real_parent / credentials_file.name).exists()
    assert not (real_parent / f".{credentials_file.name}.pending").exists()
    database.initialize()
    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'admin'"
        ).fetchone()[0] == 0


def test_initial_admin_bootstrap_refuses_symlink_ancestor(tmp_path: Path):
    from app.cloud.admin_cli import BootstrapError, bootstrap_initial_admin

    database = Database(tmp_path / "bootstrap.db")
    real_parent = tmp_path / "real-parent"
    nested_parent = real_parent / "nested"
    nested_parent.mkdir(parents=True)
    symlink_ancestor = tmp_path / "ancestor-link"
    symlink_ancestor.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(BootstrapError):
        bootstrap_initial_admin(
            database=database,
            username="bootstrap-admin",
            credentials_file=symlink_ancestor / "nested" / "credentials.json",
        )

    assert not (nested_parent / "credentials.json").exists()
    assert not (nested_parent / ".credentials.json.pending").exists()


def test_initial_admin_bootstrap_refuses_group_writable_credentials_directory(
    tmp_path: Path,
):
    from app.cloud.admin_cli import BootstrapError, bootstrap_initial_admin

    database = Database(tmp_path / "bootstrap.db")
    unsafe_parent = tmp_path / "unsafe-parent"
    unsafe_parent.mkdir(mode=0o770)
    unsafe_parent.chmod(0o770)

    with pytest.raises(BootstrapError):
        bootstrap_initial_admin(
            database=database,
            username="bootstrap-admin",
            credentials_file=unsafe_parent / "credentials.json",
        )

    assert list(unsafe_parent.iterdir()) == []


def test_initial_admin_bootstrap_refuses_pending_that_mismatches_existing_admin(
    tmp_path: Path,
):
    from app.cloud.admin_cli import BootstrapError, bootstrap_initial_admin

    database = Database(tmp_path / "bootstrap.db")
    database.initialize()
    credentials_file = tmp_path / "mismatched-pending-credentials.json"
    temporary_password = "pending-password-for-different-admin"
    pending_path = write_pending_credentials(
        credentials_file,
        username="bootstrap-admin",
        temporary_password=temporary_password,
    )
    config = CloudConfig(
        mode="cloud",
        session_secret="unused-session-secret",
        key_encryption_key=Fernet.generate_key().decode("ascii"),
        database_path=database.path,
        storage_path=tmp_path / "unused-files",
        public_origin=PUBLIC_ORIGIN,
    )
    create_cloud_app(config, database).state.repository.create_user(
        "other-admin",
        PasswordService().hash("different-password"),
        role="admin",
    )

    with pytest.raises(BootstrapError) as error:
        bootstrap_initial_admin(
            database=database,
            username="bootstrap-admin",
            credentials_file=credentials_file,
        )

    assert temporary_password not in str(error.value)
    assert temporary_password not in repr(error.value)
    assert pending_path.exists()
    assert not credentials_file.exists()


def test_initial_admin_bootstrap_never_replaces_racing_final_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import app.cloud.admin_cli as admin_cli

    database = Database(tmp_path / "bootstrap.db")
    credentials_file = tmp_path / "racing-final.json"
    original_rename = admin_cli._rename_pending_credentials
    sentinel = b"do-not-overwrite"

    def create_final_then_rename(directory_fd, pending, final_name):
        descriptor = os.open(
            final_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=directory_fd,
        )
        try:
            os.write(descriptor, sentinel)
        finally:
            os.close(descriptor)
        return original_rename(directory_fd, pending, final_name)

    monkeypatch.setattr(
        admin_cli,
        "_rename_pending_credentials",
        create_final_then_rename,
    )

    with pytest.raises(admin_cli.BootstrapError):
        admin_cli.bootstrap_initial_admin(
            database=database,
            username="bootstrap-admin",
            credentials_file=credentials_file,
        )

    assert credentials_file.read_bytes() == sentinel
    assert pending_credentials_path(credentials_file).exists()


def test_initial_admin_bootstrap_serializes_concurrent_invocations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import app.cloud.admin_cli as admin_cli

    database = Database(tmp_path / "bootstrap.db")
    credentials_file = tmp_path / "concurrent-bootstrap.json"
    barrier = threading.Barrier(2)
    original_acquire = admin_cli._acquire_bootstrap_lock

    def synchronized_acquire(directory_fd, name):
        barrier.wait(timeout=5)
        return original_acquire(directory_fd, name)

    monkeypatch.setattr(admin_cli, "_acquire_bootstrap_lock", synchronized_acquire)

    def invoke():
        try:
            return (
                "created",
                admin_cli.bootstrap_initial_admin(
                    database=database,
                    username="bootstrap-admin",
                    credentials_file=credentials_file,
                ),
            )
        except admin_cli.BootstrapError:
            return ("rejected", None)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: invoke(), range(2)))

    assert [result[0] for result in results].count("created") == 1
    assert [result[0] for result in results].count("rejected") == 1
    credentials = json.loads(credentials_file.read_text(encoding="utf-8"))
    assert credentials["username"] == "bootstrap-admin"
    with database.connection() as connection:
        rows = connection.execute(
            "SELECT id, password_hash FROM users WHERE role = 'admin'"
        ).fetchall()
    assert len(rows) == 1
    assert PasswordService().verify(
        rows[0]["password_hash"], credentials["temporary_password"]
    )


def test_initial_admin_bootstrap_serializes_independent_processes(tmp_path: Path):
    database_path = tmp_path / "process-bootstrap.db"
    credentials_file = tmp_path / "process-bootstrap.json"
    command = [
        sys.executable,
        "-m",
        "app.cloud.admin_cli",
        "--database-path",
        str(database_path),
        "--username",
        "bootstrap-admin",
        "--credentials-file",
        str(credentials_file),
    ]

    processes = [
        subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(2)
    ]
    results = [process.communicate(timeout=30) + (process.returncode,) for process in processes]

    assert [result[2] for result in results].count(0) == 1
    assert [result[2] for result in results].count(1) == 1
    credentials = json.loads(credentials_file.read_text(encoding="utf-8"))
    assert all(credentials["temporary_password"] not in result[0] for result in results)
    assert all(credentials["temporary_password"] not in result[1] for result in results)
    with Database(database_path).connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'admin'"
        ).fetchone()[0] == 1


@pytest.mark.parametrize("legacy_state", ["anchor-only", "pending-and-anchor"])
def test_initial_admin_bootstrap_refuses_legacy_anchor_state(
    tmp_path: Path, legacy_state: str
):
    from app.cloud.admin_cli import BootstrapError, bootstrap_initial_admin

    database = Database(tmp_path / "legacy-anchor.db")
    credentials_file = tmp_path / "legacy-anchor.json"
    pending_path = pending_credentials_path(credentials_file)
    pin_path = legacy_pin_path(credentials_file)
    if legacy_state == "anchor-only":
        pin_path.write_text("legacy secret state", encoding="utf-8")
        pin_path.chmod(0o600)
    else:
        write_pending_credentials(
            credentials_file,
            username="bootstrap-admin",
            temporary_password="legacy-anchor-password",
        )
        os.link(pending_path, pin_path)

    with pytest.raises(BootstrapError):
        bootstrap_initial_admin(
            database=database,
            username="bootstrap-admin",
            credentials_file=credentials_file,
        )

    assert not credentials_file.exists()
    assert pin_path.exists()


@pytest.mark.parametrize("entry_type", ["regular", "symlink"])
def test_initial_admin_bootstrap_validates_existing_lock_entry(
    tmp_path: Path, entry_type: str
):
    from app.cloud.admin_cli import BootstrapError, bootstrap_initial_admin

    database = Database(tmp_path / "lock-entry.db")
    credentials_file = tmp_path / "lock-entry.json"
    lock_path = credentials_file.with_name(f".{credentials_file.name}.lock")
    if entry_type == "regular":
        lock_path.touch(mode=0o600)
        lock_path.chmod(0o600)
        user_id = bootstrap_initial_admin(
            database=database,
            username="bootstrap-admin",
            credentials_file=credentials_file,
        )
        assert user_id
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
    else:
        target = tmp_path / "lock-target"
        target.touch(mode=0o600)
        lock_path.symlink_to(target)
        with pytest.raises(BootstrapError):
            bootstrap_initial_admin(
                database=database,
                username="bootstrap-admin",
                credentials_file=credentials_file,
            )
        assert not credentials_file.exists()


def test_initial_admin_bootstrap_rejects_lock_inode_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import app.cloud.admin_cli as admin_cli

    database = Database(tmp_path / "lock-replacement.db")
    credentials_file = tmp_path / "lock-replacement.json"
    lock_path = credentials_file.with_name(f".{credentials_file.name}.lock")
    original_flock = admin_cli.fcntl.flock
    replaced = False

    def replace_after_lock(descriptor: int, operation: int):
        nonlocal replaced
        result = original_flock(descriptor, operation)
        if operation == admin_cli.fcntl.LOCK_EX and not replaced:
            replaced = True
            lock_path.unlink()
            lock_path.touch(mode=0o600)
            lock_path.chmod(0o600)
        return result

    monkeypatch.setattr(admin_cli.fcntl, "flock", replace_after_lock)

    with pytest.raises(admin_cli.BootstrapError):
        admin_cli.bootstrap_initial_admin(
            database=database,
            username="bootstrap-admin",
            credentials_file=credentials_file,
        )

    assert not credentials_file.exists()
    database.initialize()
    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'admin'"
        ).fetchone()[0] == 0


def test_initial_admin_bootstrap_cleans_interrupted_pending_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import app.cloud.admin_cli as admin_cli

    database = Database(tmp_path / "bootstrap.db")
    credentials_file = tmp_path / "interrupted-write.json"
    original_fsync = os.fsync
    interrupted = False

    def interrupt_file_fsync(descriptor: int):
        nonlocal interrupted
        if not interrupted and stat.S_ISREG(os.fstat(descriptor).st_mode):
            interrupted = True
            raise KeyboardInterrupt
        return original_fsync(descriptor)

    monkeypatch.setattr(admin_cli.os, "fsync", interrupt_file_fsync)

    with pytest.raises(KeyboardInterrupt):
        admin_cli.bootstrap_initial_admin(
            database=database,
            username="bootstrap-admin",
            credentials_file=credentials_file,
        )

    assert not credentials_file.exists()
    assert not pending_credentials_path(credentials_file).exists()


def test_initial_admin_bootstrap_cleans_interrupted_os_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import app.cloud.admin_cli as admin_cli

    database = Database(tmp_path / "bootstrap.db")
    credentials_file = tmp_path / "interrupted-os-write.json"
    monkeypatch.setattr(
        admin_cli.os,
        "write",
        lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        admin_cli.bootstrap_initial_admin(
            database=database,
            username="bootstrap-admin",
            credentials_file=credentials_file,
        )

    assert not credentials_file.exists()
    assert not pending_credentials_path(credentials_file).exists()


@pytest.mark.parametrize("failure_point", ["fchmod", "metadata"])
def test_initial_admin_bootstrap_cleans_pre_metadata_pending_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
):
    import app.cloud.admin_cli as admin_cli

    database = Database(tmp_path / "bootstrap.db")
    credentials_file = tmp_path / f"interrupted-{failure_point}.json"
    if failure_point == "fchmod":
        original_fchmod = os.fchmod
        calls = 0

        def interrupt_pending_fchmod(descriptor: int, mode: int):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt
            return original_fchmod(descriptor, mode)

        monkeypatch.setattr(admin_cli.os, "fchmod", interrupt_pending_fchmod)
    else:
        monkeypatch.setattr(
            admin_cli,
            "_validate_pending_metadata",
            lambda metadata: (_ for _ in ()).throw(KeyboardInterrupt()),
        )

    with pytest.raises(KeyboardInterrupt):
        admin_cli.bootstrap_initial_admin(
            database=database,
            username="bootstrap-admin",
            credentials_file=credentials_file,
        )

    assert not credentials_file.exists()
    assert not pending_credentials_path(credentials_file).exists()


def test_pending_credentials_repr_redacts_temporary_password():
    import app.cloud.admin_cli as admin_cli

    secret = "temporary-password-must-not-appear"
    pending = admin_cli._PendingCredentials(
        name=".credentials.pending",
        username="bootstrap-admin",
        temporary_password=secret,
        descriptor=-1,
        device=1,
        inode=2,
    )

    assert secret not in repr(pending)
    assert secret not in str(pending)


def test_initial_admin_bootstrap_survives_resource_close_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import app.cloud.admin_cli as admin_cli

    database = Database(tmp_path / "close-errors.db")
    database.initialize()
    monkeypatch.setattr(database, "initialize", lambda: None)
    credentials_file = tmp_path / "close-errors.json"
    original_connection = database.connection
    original_pending_close = admin_cli._PendingCredentials.close

    class CloseFailConnection:
        def __init__(self):
            self._connection = original_connection()

        def close(self):
            self._connection.close()
            raise OSError("injected connection close failure")

        def commit(self):
            self._connection.commit()

        def rollback(self):
            self._connection.rollback()

        def execute(self, query: str, parameters=()):
            return self._connection.execute(query, parameters)

    def close_pending_then_fail(pending):
        original_pending_close(pending)
        raise OSError("injected pending close failure")

    monkeypatch.setattr(database, "connection", CloseFailConnection)
    monkeypatch.setattr(
        admin_cli._PendingCredentials,
        "close",
        close_pending_then_fail,
    )

    user_id = admin_cli.bootstrap_initial_admin(
        database=database,
        username="bootstrap-admin",
        credentials_file=credentials_file,
    )

    assert user_id
    assert credentials_file.exists()
    assert json.loads(credentials_file.read_text(encoding="utf-8"))["username"] == (
        "bootstrap-admin"
    )


def test_initial_admin_bootstrap_keeps_pending_when_commit_verification_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import app.cloud.admin_cli as admin_cli

    database = Database(tmp_path / "bootstrap.db")
    database.initialize()
    credentials_file = tmp_path / "verification-error.json"
    original_connection = database.connection
    failed = False

    class CommitThenFailConnection:
        def __init__(self):
            self._connection = original_connection()

        def __enter__(self):
            self._connection.__enter__()
            return self

        def __exit__(self, *args):
            return self._connection.__exit__(*args)

        def close(self):
            self._connection.close()

        def commit(self):
            nonlocal failed
            self._connection.commit()
            if not failed:
                failed = True
                raise sqlite3.OperationalError("post-commit failure")

        def rollback(self):
            self._connection.rollback()

        def execute(self, query: str, parameters=()):
            return self._connection.execute(query, parameters)

    monkeypatch.setattr(database, "connection", CommitThenFailConnection)
    monkeypatch.setattr(
        admin_cli.PasswordService,
        "verify",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("verification unavailable")),
    )

    with pytest.raises(admin_cli.BootstrapError):
        admin_cli.bootstrap_initial_admin(
            database=database,
            username="bootstrap-admin",
            credentials_file=credentials_file,
        )

    assert not credentials_file.exists()
    assert pending_credentials_path(credentials_file).exists()
    with original_connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'admin'"
        ).fetchone()[0] == 1


def test_initial_admin_bootstrap_fsyncs_pending_then_commits_renames_and_fsyncs_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import app.cloud.admin_cli as admin_cli

    database = Database(tmp_path / "bootstrap.db")
    database.initialize()
    credentials_file = tmp_path / "durable-credentials.json"
    original_connection = database.connection
    original_fsync = os.fsync
    events: list[str] = []

    class RecordingCommitConnection:
        def __init__(self):
            self._connection = original_connection()

        def __enter__(self):
            self._connection.__enter__()
            return self

        def __exit__(self, *args):
            return self._connection.__exit__(*args)

        def close(self):
            self._connection.close()

        def commit(self):
            events.append("commit")
            self._connection.commit()

        def rollback(self):
            self._connection.rollback()

        def execute(self, query: str, parameters=()):
            return self._connection.execute(query, parameters)

    def recording_fsync(descriptor: int):
        mode = os.fstat(descriptor).st_mode
        events.append("fsync-parent" if stat.S_ISDIR(mode) else "fsync-pending")
        original_fsync(descriptor)

    original_rename = admin_cli._rename_pending_credentials

    def recording_rename(*args, **kwargs):
        events.append("rename")
        return original_rename(*args, **kwargs)

    monkeypatch.setattr(database, "connection", RecordingCommitConnection)
    monkeypatch.setattr(admin_cli.os, "fsync", recording_fsync)
    monkeypatch.setattr(admin_cli, "_rename_pending_credentials", recording_rename)

    admin_cli.bootstrap_initial_admin(
        database=database,
        username="bootstrap-admin",
        credentials_file=credentials_file,
    )

    pending_fsync_index = events.index("fsync-pending")
    assert events[pending_fsync_index : pending_fsync_index + 5] == [
        "fsync-pending",
        "fsync-parent",
        "commit",
        "rename",
        "fsync-parent",
    ]
    assert stat.S_IMODE(credentials_file.stat().st_mode) == 0o600
