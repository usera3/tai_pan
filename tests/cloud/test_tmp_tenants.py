from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.client import TmpLinkBusinessError
from app.cloud.app import create_cloud_app
from app.cloud.config import CloudConfig
from app.cloud.db import Database
from app.models import ServiceResult


PUBLIC_ORIGIN = "https://cloud.example.com"
NOW = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)


class FakeTmpClient:
    def __init__(self, api_key: str, remote: RecordingRemote):
        self._api_key = api_key
        self._remote = remote

    def _call(self, action: str, *arguments):
        self._remote.calls.append((self._api_key, action, *arguments))
        error = self._remote.errors.get((self._api_key, action))
        if error is not None:
            raise error

    async def quota(self):
        self._call("quota")
        return ServiceResult(ok=True, data={"quota": 1024})

    async def list_files(self, page: int = 1):
        self._call("list_files", page)
        return ServiceResult(
            ok=True,
            data=self._remote.files.get(
                self._api_key,
                [{"ukey": "DEFAULT-UKEY", "name": "report.txt"}],
            ),
        )

    async def list_links(self, page: int = 1):
        self._call("list_links", page)
        return ServiceResult(
            ok=True,
            data=self._remote.links.get(self._api_key, []),
        )

    async def create_link(
        self,
        ukey: str,
        valid_time: int | None = None,
        download_limit: int | None = None,
    ):
        self._call("create_link", ukey, valid_time, download_limit)
        return ServiceResult(
            ok=True,
            data={"dkey": f"MANUAL-{ukey}", "link": f"https://tmp/MANUAL-{ukey}"},
        )

    async def create_download_link(self, ukey: str):
        self._call("create_download_link", ukey)
        data = self._remote.downloads.get(
            self._api_key,
            {"dkey": f"AUTO-{self._api_key}", "link": f"https://tmp/{self._api_key}"},
        )
        return ServiceResult(ok=True, data=data)

    async def delete_link(self, dkey: str, delete_file: bool = False):
        self._call("delete_link", dkey, delete_file)
        return ServiceResult(ok=True, data={"deleted": True})

    async def delete_file(self, ukey: str):
        self._call("delete_file", ukey)
        return ServiceResult(ok=True, data={"deleted": True})

    async def upload_file(
        self,
        file_name: str,
        file: BinaryIO,
        model: int,
        content_type: str = "application/octet-stream",
    ):
        self._call("upload_file", file_name, model, content_type, type(file))
        chunks: list[bytes] = []
        while chunk := file.read(2):
            chunks.append(chunk)
        self._remote.uploads.append(
            (self._api_key, file_name, model, content_type, b"".join(chunks))
        )
        return ServiceResult(ok=True, data="UPLOADED-UKEY")


class RecordingRemote:
    def __init__(self):
        self.factory_keys: list[str] = []
        self.calls: list[tuple] = []
        self.uploads: list[tuple[str, str, int, str, bytes]] = []
        self.files: dict[str, list[dict]] = {}
        self.links: dict[str, list[dict]] = {}
        self.downloads: dict[str, dict] = {}
        self.errors: dict[tuple[str, str], Exception] = {}

    def factory(self, api_key: str) -> FakeTmpClient:
        self.factory_keys.append(api_key)
        return FakeTmpClient(api_key, self)


@dataclass
class Tenant:
    id: str
    key: str
    csrf_token: str
    client: TestClient

    @property
    def csrf_headers(self) -> dict[str, str]:
        return {
            "Origin": PUBLIC_ORIGIN,
            "X-CSRF-Token": self.csrf_token,
        }


@pytest.fixture
def config(tmp_path: Path) -> CloudConfig:
    return CloudConfig(
        mode="cloud",
        session_secret="task-five-session-secret",
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
def remote() -> RecordingRemote:
    return RecordingRemote()


@pytest.fixture
def cloud_app(config: CloudConfig, database: Database, remote: RecordingRemote):
    application = create_cloud_app(config, database)
    application.state.tmp_client_factory = remote.factory
    return application


@pytest.fixture
def make_tenant(cloud_app):
    clients: list[TestClient] = []

    def create(username: str, key: str, *, must_change_password: bool = False) -> Tenant:
        repository = cloud_app.state.repository
        user = repository.create_user(
            username,
            cloud_app.state.password_service.hash("tenant password"),
            must_change_password=must_change_password,
            now=NOW,
        )
        repository.save_user_settings(user.id, tmp_key=key, now=NOW)
        session_token = f"session-{username}"
        csrf_token = f"csrf-{username}"
        repository.create_session(
            user.id,
            token=session_token,
            csrf_token=csrf_token,
            expires_at=NOW + timedelta(days=1),
            now=NOW,
        )
        client = TestClient(cloud_app, base_url=PUBLIC_ORIGIN)
        client.cookies.set(
            "session",
            session_token,
            domain="cloud.example.com",
            path="/",
        )
        clients.append(client)
        return Tenant(user.id, key, csrf_token, client)

    yield create

    for client in clients:
        client.close()


def test_settings_encrypt_key_preserve_empty_update_and_clear_explicitly(
    cloud_app, database: Database, make_tenant, remote: RecordingRemote
):
    tenant = make_tenant("settings-user", "initial-tenant-key")

    saved = tenant.client.put(
        "/api/settings",
        json={"api_key": "replacement-tenant-key", "custom_domain": "files.example.com"},
        headers=tenant.csrf_headers,
    )
    with database.connection() as connection:
        first_ciphertext = connection.execute(
            "SELECT encrypted_tmp_key FROM user_settings WHERE user_id = ?",
            (tenant.id,),
        ).fetchone()[0]

    preserved = tenant.client.put(
        "/api/settings",
        json={"api_key": "", "custom_domain": "cdn.example.com"},
        headers=tenant.csrf_headers,
    )
    with database.connection() as connection:
        second_ciphertext = connection.execute(
            "SELECT encrypted_tmp_key FROM user_settings WHERE user_id = ?",
            (tenant.id,),
        ).fetchone()[0]

    assert saved.status_code == 200
    assert saved.json() == {
        "key_configured": True,
        "custom_domain": "files.example.com",
    }
    assert preserved.json() == {
        "key_configured": True,
        "custom_domain": "cdn.example.com",
    }
    assert first_ciphertext == second_ciphertext
    assert "replacement-tenant-key" not in first_ciphertext
    assert "replacement-tenant-key" not in saved.text + preserved.text

    cleared = tenant.client.delete(
        "/api/settings/key", headers=tenant.csrf_headers
    )
    rejected = tenant.client.get("/api/files")

    assert cleared.json() == {
        "key_configured": False,
        "custom_domain": "cdn.example.com",
    }
    assert rejected.status_code == 400
    assert remote.factory_keys == []
    assert "replacement-tenant-key" not in cleared.text + rejected.text


def test_get_routes_require_active_user_and_every_mutation_requires_origin_and_csrf(
    cloud_app, make_tenant, remote: RecordingRemote
):
    unauthenticated = TestClient(cloud_app, base_url=PUBLIC_ORIGIN)
    assert unauthenticated.get("/api/settings").status_code == 401
    assert unauthenticated.get("/api/files").status_code == 401
    unauthenticated.close()

    tenant = make_tenant("csrf-user", "csrf-user-key")
    mutations = [
        ("PUT", "/api/settings", {"json": {"api_key": "", "custom_domain": "pan.cloudcode.xyz"}}),
        ("DELETE", "/api/settings/key", {}),
        ("POST", "/api/settings/test", {}),
        ("POST", "/api/uploads", {"files": {"file": ("x.txt", b"x")}}),
        ("POST", "/api/files/U1/download", {}),
        ("DELETE", "/api/files/U1", {}),
        ("POST", "/api/links", {"json": {"ukey": "U1"}}),
        ("DELETE", "/api/links/D1", {}),
    ]

    for method, path, arguments in mutations:
        response = tenant.client.request(method, path, **arguments)
        assert response.status_code == 403, (method, path, response.text)

    forced = make_tenant(
        "forced-password-user", "forced-key", must_change_password=True
    )
    assert forced.client.get("/api/settings").status_code == 403
    assert remote.factory_keys == []
    assert remote.calls == []


def test_each_remote_request_builds_a_fresh_client_with_only_the_active_users_key(
    make_tenant, remote: RecordingRemote
):
    first = make_tenant("first-tenant", "first-users-secret-key")
    second = make_tenant("second-tenant", "second-users-secret-key")

    responses = [
        first.client.get("/api/files?page=2"),
        second.client.get("/api/files?page=3"),
        first.client.get("/api/links?page=4"),
        second.client.get("/api/quota"),
    ]

    assert [response.status_code for response in responses] == [200, 200, 200, 200]
    assert remote.factory_keys == [first.key, second.key, first.key, second.key]
    assert remote.calls == [
        (first.key, "list_files", 2),
        (second.key, "list_files", 3),
        (first.key, "list_links", 4),
        (second.key, "quota"),
    ]
    assert responses[0].json()["data"][0]["source"] == "tmp"
    rendered = " ".join(response.text for response in responses)
    assert first.key not in rendered
    assert second.key not in rendered


@pytest.mark.parametrize("model", [0, 1, 2])
def test_cloud_upload_stages_a_file_object_and_accepts_only_tmp_models(
    model: int, make_tenant, remote: RecordingRemote
):
    tenant = make_tenant(f"upload-{model}", f"upload-key-{model}")

    response = tenant.client.post(
        "/api/uploads",
        data={"model": str(model)},
        files={"file": ("report.txt", b"stream-me", "text/plain")},
        headers=tenant.csrf_headers,
    )

    assert response.status_code == 200
    assert remote.uploads == [
        (tenant.key, "report.txt", model, "text/plain", b"stream-me")
    ]
    call = remote.calls[0]
    assert call[:5] == (tenant.key, "upload_file", "report.txt", model, "text/plain")
    assert not issubclass(call[5], bytes)


def test_cloud_upload_defaults_to_three_days_rejects_99_and_cleans_staging(
    config: CloudConfig, make_tenant, remote: RecordingRemote
):
    tenant = make_tenant("default-upload", "default-upload-key")

    uploaded = tenant.client.post(
        "/api/uploads",
        files={"file": ("report.txt", b"content", "text/plain")},
        headers=tenant.csrf_headers,
    )
    rejected = tenant.client.post(
        "/api/uploads",
        data={"model": "99"},
        files={"file": ("report.txt", b"content", "text/plain")},
        headers=tenant.csrf_headers,
    )
    empty = tenant.client.post(
        "/api/uploads",
        data={"model": "2"},
        files={"file": ("empty.txt", b"", "text/plain")},
        headers=tenant.csrf_headers,
    )

    assert uploaded.status_code == 200
    assert remote.uploads == [
        (tenant.key, "report.txt", 2, "text/plain", b"content")
    ]
    assert rejected.status_code == 422
    assert empty.status_code == 422
    assert all(upload[2] != 99 for upload in remote.uploads)
    assert config.storage_path is not None
    staging = config.storage_path / ".tmp-link-staging"
    assert not staging.exists() or list(staging.iterdir()) == []


def test_automatic_downloads_are_reused_and_hidden_only_for_their_tenant(
    cloud_app, make_tenant, remote: RecordingRemote
):
    first = make_tenant("download-first", "download-first-key")
    second = make_tenant("download-second", "download-second-key")
    repository = cloud_app.state.repository
    first_auto = repository.save_automatic_download_link(
        first.id,
        ukey="SHARED-UKEY",
        dkey="FIRST-AUTO",
        link="https://tmp/first-auto",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
    )
    repository.save_automatic_download_link(
        second.id,
        ukey="SHARED-UKEY",
        dkey="SECOND-AUTO",
        link="https://tmp/second-auto",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
    )
    remote.links[first.key] = [
        {"dkey": "FIRST-AUTO", "link": "https://tmp/first-auto"},
        {"dkey": "SECOND-AUTO", "link": "https://tmp/second-auto"},
        {"dkey": "MANUAL-DKEY", "link": "https://tmp/manual"},
    ]

    first_download = first.client.post(
        "/api/files/SHARED-UKEY/download", headers=first.csrf_headers
    )
    visible_links = first.client.get("/api/links")

    assert first_download.status_code == 200
    assert first_download.headers.get("location") is None
    assert first_download.json()["data"] == {
        "dkey": "FIRST-AUTO",
        "link": "https://tmp/first-auto",
        "source": "tmp",
    }
    assert [item["dkey"] for item in visible_links.json()["data"]] == [
        "SECOND-AUTO",
        "MANUAL-DKEY",
    ]
    assert all(
        call[1] != "create_download_link"
        for call in remote.calls
        if call[0] == first.key
    )
    assert repository.get_automatic_download_link(first.id, first_auto.dkey) == first_auto

    second_download = second.client.post(
        "/api/files/OTHER-UKEY/download", headers=second.csrf_headers
    )

    assert second_download.json()["data"]["link"] == f"https://tmp/{second.key}"
    assert repository.list_automatic_download_links(
        second.id, ukey="OTHER-UKEY"
    )
    assert repository.list_automatic_download_links(
        first.id, ukey="OTHER-UKEY"
    ) == []


def test_manual_links_stay_visible_and_cross_tenant_deletes_do_not_remove_auto_rows(
    cloud_app, make_tenant, remote: RecordingRemote
):
    first = make_tenant("link-first", "link-first-key")
    second = make_tenant("link-second", "link-second-key")
    repository = cloud_app.state.repository
    second_auto = repository.save_automatic_download_link(
        second.id,
        ukey="SECOND-UKEY",
        dkey="SECOND-PRIVATE-DKEY",
        link="https://tmp/second-private",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )

    created = first.client.post(
        "/api/links",
        json={"ukey": "FIRST-UKEY", "valid_time": 60, "download_limit": 2},
        headers=first.csrf_headers,
    )
    deleted = first.client.delete(
        "/api/links/SECOND-PRIVATE-DKEY", headers=first.csrf_headers
    )

    assert created.status_code == 200
    assert created.json()["data"]["source"] == "tmp"
    assert repository.list_automatic_download_links(
        first.id, ukey="FIRST-UKEY"
    ) == []
    assert deleted.status_code == 200
    assert repository.get_automatic_download_link(
        second.id, second_auto.dkey
    ) == second_auto
    assert (first.key, "delete_link", "SECOND-PRIVATE-DKEY", False) in remote.calls

    first_auto = repository.save_automatic_download_link(
        first.id,
        ukey="SAME-UKEY",
        dkey="FIRST-SAME-DKEY",
        link="https://tmp/first-same",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    second_same = repository.save_automatic_download_link(
        second.id,
        ukey="SAME-UKEY",
        dkey="SECOND-SAME-DKEY",
        link="https://tmp/second-same",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )

    response = first.client.delete(
        "/api/files/SAME-UKEY", headers=first.csrf_headers
    )

    assert response.status_code == 200
    assert repository.get_automatic_download_link(first.id, first_auto.dkey) is None
    assert repository.get_automatic_download_link(second.id, second_same.dkey) == second_same


@pytest.mark.parametrize(
    ("method", "path", "arguments"),
    [
        ("POST", "/api/files/bad%20ukey/download", {}),
        ("DELETE", "/api/files/bad.ukey", {}),
        ("POST", "/api/links", {"json": {"ukey": "../../secret"}}),
        ("DELETE", "/api/links/bad%20dkey", {}),
    ],
)
def test_remote_identifiers_are_validated_without_becoming_paths(
    method: str, path: str, arguments: dict, make_tenant, remote: RecordingRemote
):
    tenant = make_tenant("identifier-user", "identifier-key")

    response = tenant.client.request(
        method,
        path,
        headers=tenant.csrf_headers,
        **arguments,
    )

    assert response.status_code == 422
    assert remote.calls == []


def test_tmp_errors_are_fixed_and_never_render_keys_ciphertext_or_remote_urls(
    cloud_app, database: Database, make_tenant, remote: RecordingRemote
):
    tenant = make_tenant("error-user", "error-users-plaintext-key")
    with database.connection() as connection:
        ciphertext = connection.execute(
            "SELECT encrypted_tmp_key FROM user_settings WHERE user_id = ?",
            (tenant.id,),
        ).fetchone()[0]
    remote.errors[(tenant.key, "quota")] = TmpLinkBusinessError(
        f"remote echoed {tenant.key} and https://private.example/download"
    )

    response = tenant.client.get("/api/quota")

    assert response.status_code == 502
    assert response.json() == {"detail": "TMP.link request failed"}
    rendered = f"{response.text!r} {response.text} {cloud_app.state.repository!r}"
    assert tenant.key not in rendered
    assert ciphertext not in rendered
    assert "https://private.example/download" not in rendered
    with database.connection() as connection:
        audit_values = connection.execute(
            "SELECT event_type, target_type, target_id FROM audit_events"
        ).fetchall()
    assert tenant.key not in repr(audit_values)
    assert "https://private.example/download" not in repr(audit_values)
