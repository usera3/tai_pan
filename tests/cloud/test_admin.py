from __future__ import annotations

import sqlite3
import stat
import subprocess
import sys
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
    assert invitation["code_hash"] == hash_secret(invitation_code)
    assert invitation_code not in str(invitation)

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
    assert body["user"]["id"] == target.id
    assert body["user"]["must_change_password"] is True
    assert plaintext_tmp_key not in reset.text
    assert encrypted_tmp_key not in reset.text
    assert "password_hash" not in reset.text

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
    cloud_app, database: Database, admin_client
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
    temporary_password = credentials_file.read_text(encoding="utf-8").strip()
    assert len(temporary_password) >= 32
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
    with original_connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'admin'"
        ).fetchone()[0] == 0
