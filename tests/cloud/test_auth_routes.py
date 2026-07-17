from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Event, Lock

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


def test_registration_counts_invalid_json_and_fields_before_body_parsing(
    cloud_app, client: TestClient
):
    responses = []
    for index in range(10):
        headers = {
            "Content-Type": "application/json",
            "X-Forwarded-For": f"198.51.100.{index + 1}",
        }
        if index % 2:
            responses.append(client.post("/api/auth/register", json={}, headers=headers))
        else:
            responses.append(
                client.post(
                    "/api/auth/register",
                    content=b'{"password":"must-not-be-echoed"',
                    headers=headers,
                )
            )

    blocked = client.post(
        "/api/auth/register",
        content=b'{"password":"still-must-not-be-echoed"',
        headers={
            "Content-Type": "application/json",
            "X-Forwarded-For": "203.0.113.250",
        },
    )

    assert [response.status_code for response in responses] == [422] * 10
    assert {response.text for response in responses} == {
        '{"detail":"Request validation failed"}'
    }
    assert blocked.status_code == 429
    assert "still-must-not-be-echoed" not in blocked.text
    assert cloud_app.state.repository.count_registration_attempts(
        since=datetime.now(timezone.utc) - timedelta(hours=1),
        remote_addr="testclient",
    ) == 10


def test_registration_rate_limit_claim_fails_closed(
    cloud_app, monkeypatch: pytest.MonkeyPatch
):
    secret = "registration-secret-that-must-not-leak"

    def unavailable(**_kwargs):
        raise sqlite3.OperationalError("rate limit database unavailable")

    monkeypatch.setattr(
        cloud_app.state.repository,
        "claim_registration_submission",
        unavailable,
        raising=False,
    )

    with TestClient(
        cloud_app,
        base_url=PUBLIC_ORIGIN,
        raise_server_exceptions=False,
    ) as fail_closed_client:
        response = register(
            fail_closed_client,
            username="fail-closed-registration",
            invitation_code=secret,
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "Registration is temporarily unavailable"}
    assert secret not in response.text


def test_eleven_concurrent_registration_submissions_claim_only_ten_slots(
    cloud_app, monkeypatch: pytest.MonkeyPatch
):
    repository = cloud_app.state.repository
    original_count = repository.count_registration_attempts
    counted = Barrier(11)
    start = Barrier(11)

    def synchronized_count(**kwargs):
        value = original_count(**kwargs)
        counted.wait(timeout=10)
        return value

    monkeypatch.setattr(repository, "count_registration_attempts", synchronized_count)
    monkeypatch.setattr(
        cloud_app.state.password_service,
        "hash",
        lambda _password: "test-password-hash",
    )
    clients = [
        TestClient(
            cloud_app,
            base_url=PUBLIC_ORIGIN,
            client=("198.51.100.40", 50_000 + index),
        )
        for index in range(11)
    ]

    def submit(index: int):
        start.wait(timeout=10)
        return register(
            clients[index],
            username=f"concurrent-candidate-{index}",
            invitation_code=f"missing-invitation-{index}",
        )

    try:
        with ThreadPoolExecutor(max_workers=11) as executor:
            responses = list(executor.map(submit, range(11)))
    finally:
        for concurrent_client in clients:
            concurrent_client.close()

    assert sorted(response.status_code for response in responses) == [400] * 10 + [429]
    assert original_count(
        since=datetime.now(timezone.utc) - timedelta(hours=1),
        remote_addr="198.51.100.40",
    ) == 10


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


@pytest.mark.parametrize("dimension", ["account", "ip"])
def test_login_rate_limit_covers_account_and_ip_independently(
    cloud_app, dimension: str, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        cloud_app.state.password_service,
        "verify",
        lambda _password_hash, _password: False,
    )
    shared_username = "independent-account-limit"
    shared_remote_addr = "198.51.100.80"
    responses = []

    for index in range(6):
        remote_addr = (
            f"198.51.100.{100 + index}"
            if dimension == "account"
            else shared_remote_addr
        )
        username = (
            shared_username if dimension == "account" else f"independent-user-{index}"
        )
        with TestClient(
            cloud_app,
            base_url=PUBLIC_ORIGIN,
            client=(remote_addr, 50_000 + index),
        ) as dimension_client:
            responses.append(
                login(
                    dimension_client,
                    username=username,
                    password=f"wrong-password-{index}",
                )
            )

    assert [response.status_code for response in responses] == [401] * 5 + [429]
    since = datetime.now(timezone.utc) - timedelta(minutes=15)
    if dimension == "account":
        assert cloud_app.state.repository.count_failed_auth_attempts(
            since=since, username=shared_username
        ) == 5
    else:
        assert cloud_app.state.repository.count_failed_auth_attempts(
            since=since, remote_addr=shared_remote_addr
        ) == 5


@pytest.mark.parametrize("dimension", ["account", "ip"])
def test_six_concurrent_login_failures_claim_only_five_slots(
    cloud_app, dimension: str, monkeypatch: pytest.MonkeyPatch
):
    verified = Barrier(6)
    start = Barrier(6)
    shared_username = "concurrent-account-limit"
    shared_remote_addr = "203.0.113.60"

    def synchronized_failure(_password_hash: str, _password: str) -> bool:
        verified.wait(timeout=10)
        return False

    monkeypatch.setattr(
        cloud_app.state.password_service, "verify", synchronized_failure
    )
    clients = []
    for index in range(6):
        remote_addr = (
            f"203.0.113.{100 + index}"
            if dimension == "account"
            else shared_remote_addr
        )
        clients.append(
            TestClient(
                cloud_app,
                base_url=PUBLIC_ORIGIN,
                client=(remote_addr, 51_000 + index),
            )
        )

    def fail_login(index: int):
        username = (
            shared_username if dimension == "account" else f"concurrent-user-{index}"
        )
        start.wait(timeout=10)
        return login(
            clients[index],
            username=username,
            password=f"concurrent-wrong-password-{index}",
        )

    try:
        with ThreadPoolExecutor(max_workers=6) as executor:
            responses = list(executor.map(fail_login, range(6)))
    finally:
        for concurrent_client in clients:
            concurrent_client.close()

    assert sorted(response.status_code for response in responses) == [401] * 5 + [429]
    since = datetime.now(timezone.utc) - timedelta(minutes=15)
    if dimension == "account":
        assert cloud_app.state.repository.count_failed_auth_attempts(
            since=since, username=shared_username
        ) == 5
    else:
        assert cloud_app.state.repository.count_failed_auth_attempts(
            since=since, remote_addr=shared_remote_addr
        ) == 5


def test_successful_login_response_survives_audit_failure_without_secrets(
    cloud_app, monkeypatch: pytest.MonkeyPatch
):
    password = "audit-safe-login-password"
    create_user(cloud_app, "audit-safe-user", password=password)
    captured = []

    def unavailable_audit(**kwargs):
        captured.append(kwargs)
        raise sqlite3.OperationalError("audit database unavailable")

    monkeypatch.setattr(
        cloud_app.state.repository, "record_auth_attempt", unavailable_audit
    )

    with TestClient(
        cloud_app,
        base_url=PUBLIC_ORIGIN,
        raise_server_exceptions=False,
    ) as audit_client:
        response = login(
            audit_client,
            username="audit-safe-user",
            password=password,
        )

    assert response.status_code == 200
    assert captured
    rendered = repr(captured)
    assert password not in rendered
    assert response.json()["csrf_token"] not in rendered


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


def test_expired_session_is_rejected_by_authenticated_route(
    cloud_app, client: TestClient
):
    user = create_user(cloud_app, "expired-session-user")
    token = "expired-session-token"
    cloud_app.state.repository.create_session(
        user.id,
        token=token,
        csrf_token="expired-session-csrf-token",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    client.cookies.set("session", token, domain="cloud.example.com", path="/")

    response = client.get("/api/auth/me")

    assert response.status_code == 401


def test_ordinary_user_is_rejected_by_admin_dependency(
    cloud_app, client: TestClient
):
    create_user(cloud_app, "ordinary-user")

    @cloud_app.get("/_test/admin-forbidden")
    async def admin_only(user=Depends(admin_user)):
        return {"username": user.username}

    assert login(client, username="ordinary-user").status_code == 200

    response = client.get("/_test/admin-forbidden")

    assert response.status_code == 403
    assert response.json() == {"detail": "Administrator access required"}


def test_password_change_revokes_other_sessions_and_keeps_only_new_session(
    cloud_app, client: TestClient
):
    user = create_user(cloud_app, "multi-session-user")
    first = login(client, username=user.username)
    first_token = client.cookies["session"]

    with TestClient(
        cloud_app,
        base_url=PUBLIC_ORIGIN,
        client=("198.51.100.121", 50_001),
    ) as other_client:
        other = login(other_client, username=user.username)
        other_token = other_client.cookies["session"]

        changed = client.post(
            "/api/auth/change-password",
            headers=csrf_headers(first.json()["csrf_token"]),
            json={
                "current_password": VALID_PASSWORD,
                "new_password": "replacement multi session password",
            },
        )

        assert changed.status_code == 200
        assert other.status_code == 200
        assert client.cookies["session"] not in {first_token, other_token}
        assert cloud_app.state.repository.get_active_session_by_token(
            first_token
        ) is None
        assert cloud_app.state.repository.get_active_session_by_token(
            other_token
        ) is None
        assert other_client.get("/api/auth/me").status_code == 401
        assert client.get("/api/auth/me").status_code == 200


def test_login_with_verified_old_password_loses_race_to_password_change(
    cloud_app,
    client: TestClient,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
):
    old_password = "old password for synchronized race"
    new_password = "new password after synchronized race"
    user = create_user(cloud_app, "password-race-user", password=old_password)
    authenticated = login(
        client,
        username=user.username,
        password=old_password,
    )
    original_verify = cloud_app.state.password_service.verify
    login_verified = Event()
    release_login = Event()
    pause_lock = Lock()
    should_pause = True

    def coordinated_verify(password_hash: str, password: str) -> bool:
        nonlocal should_pause
        result = original_verify(password_hash, password)
        pause_this_call = False
        with pause_lock:
            if result and password == old_password and should_pause:
                should_pause = False
                pause_this_call = True
        if pause_this_call:
            login_verified.set()
            if not release_login.wait(timeout=10):
                raise TimeoutError("password race login was not released")
        return result

    monkeypatch.setattr(
        cloud_app.state.password_service, "verify", coordinated_verify
    )

    with TestClient(
        cloud_app,
        base_url=PUBLIC_ORIGIN,
        client=("198.51.100.122", 50_002),
    ) as stale_login_client:
        with ThreadPoolExecutor(max_workers=1) as executor:
            stale_login_future = executor.submit(
                login,
                stale_login_client,
                username=user.username,
                password=old_password,
            )
            assert login_verified.wait(timeout=10)
            try:
                changed = client.post(
                    "/api/auth/change-password",
                    headers=csrf_headers(authenticated.json()["csrf_token"]),
                    json={
                        "current_password": old_password,
                        "new_password": new_password,
                    },
                )
            finally:
                release_login.set()
            stale_login = stale_login_future.result(timeout=10)

    assert changed.status_code == 200
    assert stale_login.status_code == 401
    assert stale_login.json() == {"detail": "Invalid username or password"}
    assert old_password not in stale_login.text
    with database.connection() as connection:
        active_sessions = connection.execute(
            """
            SELECT COUNT(*) FROM sessions
            WHERE user_id = ? AND revoked_at IS NULL
            """,
            (user.id,),
        ).fetchone()[0]
    assert active_sessions == 1


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
