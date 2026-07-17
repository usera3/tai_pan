from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi import Depends
from fastapi.testclient import TestClient

from app.cloud.app import create_cloud_app
from app.cloud.config import CloudConfig
from app.cloud.db import Database
from app.cloud.dependencies import active_user, admin_user
from app.cloud.schemas import LoginRequest, RegisterRequest
from app.cloud.security import hash_secret


PUBLIC_ORIGIN = "https://cloud.example.com"
VALID_PASSWORD = "correct horse battery staple"


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


@pytest.fixture
def client(cloud_app) -> TestClient:
    with TestClient(cloud_app, base_url=PUBLIC_ORIGIN) as value:
        yield value


def create_user(
    cloud_app,
    username: str,
    password: str = VALID_PASSWORD,
    **overrides,
):
    password_hash = cloud_app.state.password_service.hash(password)
    return cloud_app.state.repository.create_user(
        username, password_hash, **overrides
    )


def create_invitation(cloud_app, code: str = "single-use-invitation") -> str:
    admin = create_user(cloud_app, "admin", role="admin")
    cloud_app.state.repository.create_invitation(
        created_by=admin.id,
        code=code,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    return code


def register(
    client: TestClient,
    *,
    username: str,
    invitation_code: str,
    password: str = VALID_PASSWORD,
):
    return client.post(
        "/api/auth/register",
        json={
            "username": username,
            "password": password,
            "invitation_code": invitation_code,
        },
    )


def login(
    client: TestClient,
    *,
    username: str,
    password: str = VALID_PASSWORD,
):
    return client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )


def csrf_headers(csrf_token: str, origin: str = PUBLIC_ORIGIN) -> dict[str, str]:
    return {"Origin": origin, "X-CSRF-Token": csrf_token}


def assert_secure_session_cookie(response) -> None:
    cookie = response.headers["set-cookie"].lower()
    assert "session=" in cookie
    assert "httponly" in cookie
    assert "secure" in cookie
    assert "samesite=lax" in cookie
    assert "path=/" in cookie


def test_registration_consumes_invitation_once_and_stores_only_secret_hashes(
    cloud_app, client: TestClient, database: Database
):
    invitation_code = create_invitation(cloud_app)

    response = register(
        client, username="  Invited_User  ", invitation_code=invitation_code
    )

    assert response.status_code == 201
    body = response.json()
    assert body["user"] == {
        "id": body["user"]["id"],
        "username": "invited_user",
        "role": "user",
        "must_change_password": False,
    }
    assert body["csrf_token"]
    assert_secure_session_cookie(response)

    session_token = client.cookies["session"]
    with database.connection() as connection:
        invitation = connection.execute(
            "SELECT code_hash, used_by FROM invitations WHERE code_hash = ?",
            (hash_secret(invitation_code),),
        ).fetchone()
        session = connection.execute(
            "SELECT token_hash, csrf_hash FROM sessions WHERE user_id = ?",
            (body["user"]["id"],),
        ).fetchone()

    assert invitation["used_by"] == body["user"]["id"]
    assert invitation["code_hash"] == hash_secret(invitation_code)
    assert session["token_hash"] == hash_secret(session_token)
    assert session["csrf_hash"] == hash_secret(body["csrf_token"])
    assert invitation_code not in tuple(invitation)
    assert session_token not in tuple(session)
    assert body["csrf_token"] not in tuple(session)

    reused = register(
        client, username="another-user", invitation_code=invitation_code
    )
    assert reused.status_code == 400


def test_duplicate_username_does_not_waste_invitation(cloud_app, client: TestClient):
    create_user(cloud_app, "existing-user")
    invitation_code = create_invitation(cloud_app, "still-usable-after-conflict")

    duplicate = register(
        client, username="EXISTING-USER", invitation_code=invitation_code
    )
    accepted = register(
        client, username="different-user", invitation_code=invitation_code
    )

    assert duplicate.status_code == 409
    assert accepted.status_code == 201
    assert accepted.json()["user"]["username"] == "different-user"


def test_registration_is_limited_to_ten_submissions_per_ip(
    cloud_app, client: TestClient
):
    create_user(cloud_app, "admin", role="admin")

    responses = [
        register(
            client,
            username=f"candidate-{index}",
            invitation_code=f"invalid-invitation-{index}",
        )
        for index in range(11)
    ]

    assert [response.status_code for response in responses[:10]] == [400] * 10
    assert responses[10].status_code == 429


def test_login_returns_authenticated_json_and_secure_cookie(cloud_app, client):
    user = create_user(cloud_app, "login-user")

    response = login(client, username=" LOGIN-USER ")

    assert response.status_code == 200
    assert response.json()["user"]["id"] == user.id
    assert response.json()["csrf_token"]
    assert_secure_session_cookie(response)
    assert client.get("/api/auth/me").json() == {"user": response.json()["user"]}


def test_login_failures_are_generic_for_unknown_wrong_and_disabled_users(
    cloud_app, client: TestClient
):
    create_user(cloud_app, "known-user")
    create_user(cloud_app, "disabled-user", status="disabled")

    responses = [
        login(client, username="missing-user", password="wrong password"),
        login(client, username="known-user", password="wrong password"),
        login(client, username="disabled-user", password=VALID_PASSWORD),
    ]

    assert {response.status_code for response in responses} == {401}
    assert len({response.text for response in responses}) == 1
    assert "wrong password" not in responses[0].text
    assert VALID_PASSWORD not in responses[2].text


def test_login_is_throttled_after_five_failures_for_account_or_ip(
    cloud_app, client: TestClient
):
    create_user(cloud_app, "throttled-user")

    failures = [
        login(client, username="throttled-user", password=f"wrong-{index}")
        for index in range(5)
    ]
    blocked = login(client, username="throttled-user")

    assert [response.status_code for response in failures] == [401] * 5
    assert blocked.status_code == 429
    assert cloud_app.state.repository.count_failed_auth_attempts(
        since=datetime.now(timezone.utc) - timedelta(minutes=15),
        username="throttled-user",
    ) == 5


@pytest.mark.parametrize(
    ("headers", "payload"),
    [
        ({}, None),
        ({"Origin": PUBLIC_ORIGIN}, None),
        ({"Origin": "https://attacker.example", "X-CSRF-Token": "unused"}, None),
        ({"Origin": PUBLIC_ORIGIN, "X-CSRF-Token": "wrong-token"}, None),
    ],
)
def test_logout_rejects_missing_or_mismatched_origin_and_csrf(
    cloud_app, client: TestClient, headers, payload
):
    create_user(cloud_app, "csrf-user")
    authenticated = login(client, username="csrf-user")

    response = client.post("/api/auth/logout", headers=headers, json=payload)

    assert response.status_code == 403
    assert client.get("/api/auth/me").status_code == 200
    assert authenticated.json()["csrf_token"] not in response.text


def test_logout_revokes_session_and_expires_cookie(cloud_app, client: TestClient):
    create_user(cloud_app, "logout-user")
    authenticated = login(client, username="logout-user")
    session_token = client.cookies["session"]

    response = client.post(
        "/api/auth/logout",
        headers=csrf_headers(authenticated.json()["csrf_token"]),
    )

    assert response.status_code == 200
    assert cloud_app.state.repository.get_active_session_by_token(session_token) is None
    assert "max-age=0" in response.headers["set-cookie"].lower()
    assert "secure" in response.headers["set-cookie"].lower()
    assert client.get("/api/auth/me").status_code == 401


def test_disabled_user_is_rejected_even_with_an_existing_session(
    cloud_app, client: TestClient
):
    user = create_user(cloud_app, "disabled-session-user", status="disabled")
    token = "disabled-user-session-token"
    cloud_app.state.repository.create_session(
        user.id,
        token=token,
        csrf_token="disabled-user-csrf-token",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    client.cookies.set("session", token, domain="cloud.example.com", path="/")

    response = client.get("/api/auth/me")

    assert response.status_code == 401


def test_forced_password_change_allows_only_me_change_and_logout(
    cloud_app, client: TestClient
):
    create_user(
        cloud_app,
        "bootstrap-admin",
        password="temporary admin password",
        role="admin",
        must_change_password=True,
    )

    @cloud_app.get("/_test/active")
    async def active_only(user=Depends(active_user)):
        return {"username": user.username}

    @cloud_app.get("/_test/admin")
    async def admin_only(user=Depends(admin_user)):
        return {"username": user.username}

    authenticated = login(
        client,
        username="bootstrap-admin",
        password="temporary admin password",
    )
    csrf_token = authenticated.json()["csrf_token"]

    assert authenticated.status_code == 200
    assert authenticated.json()["user"]["must_change_password"] is True
    assert client.get("/api/auth/me").status_code == 200
    assert client.get("/_test/active").status_code == 403
    assert client.get("/_test/admin").status_code == 403

    rejected = client.post(
        "/api/auth/change-password",
        headers=csrf_headers(csrf_token, origin="https://attacker.example"),
        json={
            "current_password": "temporary admin password",
            "new_password": "new permanent admin password",
        },
    )
    changed = client.post(
        "/api/auth/change-password",
        headers=csrf_headers(csrf_token),
        json={
            "current_password": "temporary admin password",
            "new_password": "new permanent admin password",
        },
    )

    assert rejected.status_code == 403
    assert changed.status_code == 200
    assert changed.json()["user"]["must_change_password"] is False
    assert_secure_session_cookie(changed)
    assert client.get("/_test/active").status_code == 200
    assert client.get("/_test/admin").status_code == 200

    client.cookies.clear()
    assert login(
        client,
        username="bootstrap-admin",
        password="temporary admin password",
    ).status_code == 401
    assert login(
        client,
        username="bootstrap-admin",
        password="new permanent admin password",
    ).status_code == 200


def test_secret_fields_are_redacted_from_models_and_error_responses(
    cloud_app, client: TestClient
):
    password = "password-that-must-not-leak"
    invitation = "invitation-that-must-not-leak"
    login_payload = LoginRequest(username="missing-user", password=password)
    register_payload = RegisterRequest(
        username="bad username",
        password=password,
        invitation_code=invitation,
    )

    response = client.post(
        "/api/auth/register", json=register_payload.model_dump()
    )

    rendered = f"{login_payload!r} {register_payload!r} {response.text}"
    assert response.status_code == 422
    assert password not in rendered
    assert invitation not in rendered

