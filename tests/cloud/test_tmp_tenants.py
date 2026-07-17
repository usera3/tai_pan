from __future__ import annotations

import asyncio
import concurrent.futures
from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from stat import S_IMODE
from typing import Any, BinaryIO

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

import app.cloud.routes.tmp_files as tmp_files_routes
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
        started = self._remote.download_started.get(self._api_key)
        if started is not None:
            started.set()
        release = self._remote.download_release.get(self._api_key)
        if release is not None:
            await release.wait()
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
        staged_path = Path(file.name)
        self._remote.upload_permissions.append(
            (
                S_IMODE(staged_path.parent.stat().st_mode),
                S_IMODE(staged_path.stat().st_mode),
            )
        )
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
        self.upload_permissions: list[tuple[int, int]] = []
        self.files: dict[str, list[dict]] = {}
        self.links: dict[str, Any] = {}
        self.downloads: dict[str, Any] = {}
        self.errors: dict[tuple[str, str], BaseException] = {}
        self.download_started: dict[str, asyncio.Event] = {}
        self.download_release: dict[str, asyncio.Event] = {}

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

    def create(
        username: str,
        key: str,
        *,
        must_change_password: bool = False,
        application=None,
    ) -> Tenant:
        application = application or cloud_app
        repository = application.state.repository
        user = repository.create_user(
            username,
            application.state.password_service.hash("tenant password"),
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
        client = TestClient(application, base_url=PUBLIC_ORIGIN)
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


def limited_cloud_app(
    config: CloudConfig,
    database: Database,
    remote: RecordingRemote,
    *,
    max_file_bytes: int,
):
    application = create_cloud_app(
        replace(config, max_file_bytes=max_file_bytes),
        database,
    )
    application.state.tmp_client_factory = remote.factory
    return application


def multipart_body(content: bytes, filename: str = "upload.bin") -> tuple[bytes, str]:
    boundary = "task-five-boundary"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("ascii") + content + f"\r\n--{boundary}--\r\n".encode("ascii")
    return body, f"multipart/form-data; boundary={boundary}"


async def invoke_asgi_upload(
    application,
    *,
    headers: dict[str, str],
    body_chunks: list[bytes],
) -> tuple[int, bytes, int]:
    receive_calls = 0
    pending = list(body_chunks)
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal receive_calls
        receive_calls += 1
        if pending:
            body = pending.pop(0)
            return {
                "type": "http.request",
                "body": body,
                "more_body": bool(pending),
            }
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/api/uploads",
        "raw_path": b"/api/uploads",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (name.lower().encode("latin-1"), value.encode("latin-1"))
            for name, value in headers.items()
        ],
        "client": ("198.51.100.25", 50000),
        "server": ("cloud.example.com", 443),
        "app": application,
    }
    await application(scope, receive, send)
    start = next(message for message in sent if message["type"] == "http.response.start")
    response_body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    return start["status"], response_body, receive_calls


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "expected_status"),
    [("unauthenticated", 401), ("bad-csrf", 403), ("forced-password", 403)],
)
async def test_upload_guard_rejects_before_reading_the_request_body(
    case: str,
    expected_status: int,
    config: CloudConfig,
    make_tenant,
    remote: RecordingRemote,
):
    tenant = make_tenant(
        f"guard-{case}",
        f"guard-key-{case}",
        must_change_password=case == "forced-password",
    )
    body, content_type = multipart_body(b"must-not-be-read")
    headers = {
        "Content-Type": content_type,
        "Content-Length": str(len(body)),
        "X-Forwarded-For": "127.0.0.1",
    }
    if case != "unauthenticated":
        headers["Cookie"] = f"session=session-guard-{case}"
        headers["Origin"] = PUBLIC_ORIGIN
        headers["X-CSRF-Token"] = (
            "wrong-csrf" if case == "bad-csrf" else tenant.csrf_token
        )

    status_code, _, receive_calls = await invoke_asgi_upload(
        tenant.client.app,
        headers=headers,
        body_chunks=[body],
    )

    assert status_code == expected_status
    assert receive_calls == 0
    assert remote.calls == []
    assert config.storage_path is not None
    staging = config.storage_path / ".tmp-link-staging"
    assert not staging.exists() or list(staging.iterdir()) == []


@pytest.mark.asyncio
async def test_upload_guard_rejects_an_obviously_large_content_length_without_receiving(
    config: CloudConfig,
    database: Database,
    make_tenant,
    remote: RecordingRemote,
):
    application = limited_cloud_app(
        config,
        database,
        remote,
        max_file_bytes=3,
    )
    tenant = make_tenant(
        "content-length-guard",
        "content-length-key",
        application=application,
    )
    body, content_type = multipart_body(b"abc")
    status_code, _, receive_calls = await invoke_asgi_upload(
        application,
        headers={
            **tenant.csrf_headers,
            "Cookie": "session=session-content-length-guard",
            "Content-Type": content_type,
            "Content-Length": "1000000",
        },
        body_chunks=[body],
    )

    assert status_code == 413
    assert receive_calls == 0
    assert remote.calls == []


@pytest.mark.asyncio
async def test_upload_guard_caps_chunked_multipart_before_unbounded_parsing(
    config: CloudConfig,
    database: Database,
    make_tenant,
    remote: RecordingRemote,
):
    application = limited_cloud_app(
        config,
        database,
        remote,
        max_file_bytes=3,
    )
    tenant = make_tenant(
        "chunked-upload-guard",
        "chunked-upload-key",
        application=application,
    )
    body, content_type = multipart_body(b"x" * (70 * 1024))

    status_code, _, receive_calls = await invoke_asgi_upload(
        application,
        headers={
            **tenant.csrf_headers,
            "Cookie": "session=session-chunked-upload-guard",
            "Content-Type": content_type,
        },
        body_chunks=[body[:1024], body[1024:]],
    )

    assert status_code == 413
    assert receive_calls > 0
    assert remote.calls == []
    assert application.state.config.storage_path is not None
    staging = application.state.config.storage_path / ".tmp-link-staging"
    assert not staging.exists() or list(staging.iterdir()) == []


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
        (tenant.key, "report.txt", 1, "text/plain", b"content")
    ]
    assert rejected.status_code == 422
    assert empty.status_code == 422
    assert all(upload[2] != 99 for upload in remote.uploads)
    assert config.storage_path is not None
    staging = config.storage_path / ".tmp-link-staging"
    assert not staging.exists() or list(staging.iterdir()) == []


def test_cloud_upload_enforces_exact_file_limit_normalizes_name_and_permissions(
    config: CloudConfig,
    database: Database,
    make_tenant,
    remote: RecordingRemote,
):
    application = limited_cloud_app(
        config,
        database,
        remote,
        max_file_bytes=3,
    )
    tenant = make_tenant(
        "bounded-staging-upload",
        "bounded-staging-key",
        application=application,
    )

    accepted = tenant.client.post(
        "/api/uploads",
        files={"file": ("../../report.txt", b"abc", "text/plain")},
        headers=tenant.csrf_headers,
    )
    rejected = tenant.client.post(
        "/api/uploads",
        files={"file": ("too-large.txt", b"abcd", "text/plain")},
        headers=tenant.csrf_headers,
    )

    assert accepted.status_code == 200
    assert rejected.status_code == 413
    assert remote.uploads == [
        (tenant.key, "report.txt", 1, "text/plain", b"abc")
    ]
    assert remote.upload_permissions == [(0o700, 0o600)]
    assert application.state.config.storage_path is not None
    staging = application.state.config.storage_path / ".tmp-link-staging"
    assert staging.is_dir()
    assert list(staging.iterdir()) == []


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("../../report.txt", "report.txt"),
        ("C:\\private\\safe-测试.txt", "safe-测试.txt"),
        ("unsafe\x00name\n.txt", "unsafe_name_.txt"),
        ("", "upload.bin"),
        (".", "upload.bin"),
        ("..", "upload.bin"),
    ],
)
def test_upload_basename_removes_path_components_and_control_characters(
    filename: str, expected: str
):
    assert tmp_files_routes._upload_basename(filename) == expected


def test_cloud_upload_forwards_the_sanitized_unicode_basename(
    make_tenant, remote: RecordingRemote
):
    tenant = make_tenant("sanitized-upload", "sanitized-upload-key")

    response = tenant.client.post(
        "/api/uploads",
        files={"file": ("C:\\private\\safe-测试.txt", b"content", "text/plain")},
        headers=tenant.csrf_headers,
    )

    assert response.status_code == 200
    assert remote.uploads == [
        (tenant.key, "safe-测试.txt", 1, "text/plain", b"content")
    ]


def test_upload_remote_failure_cleans_staging(
    config: CloudConfig,
    make_tenant,
    remote: RecordingRemote,
):
    tenant = make_tenant("failed-upload", "failed-upload-key")
    remote.errors[(tenant.key, "upload_file")] = TmpLinkBusinessError(
        "remote failure with https://private.example/file"
    )

    response = tenant.client.post(
        "/api/uploads",
        files={"file": ("failed.txt", b"content", "text/plain")},
        headers=tenant.csrf_headers,
    )

    assert response.status_code == 502
    assert config.storage_path is not None
    staging = config.storage_path / ".tmp-link-staging"
    assert staging.is_dir()
    assert list(staging.iterdir()) == []


def test_upload_cancellation_cleans_staging(
    config: CloudConfig,
    make_tenant,
    remote: RecordingRemote,
):
    tenant = make_tenant("cancelled-upload", "cancelled-upload-key")
    remote.errors[(tenant.key, "upload_file")] = asyncio.CancelledError()

    # TestClient maps task cancellation to concurrent.futures.CancelledError on
    # Python 3.10, while newer stacks may preserve asyncio.CancelledError.
    with pytest.raises((asyncio.CancelledError, concurrent.futures.CancelledError)):
        tenant.client.post(
            "/api/uploads",
            files={"file": ("cancelled.txt", b"content", "text/plain")},
            headers=tenant.csrf_headers,
        )

    assert config.storage_path is not None
    staging = config.storage_path / ".tmp-link-staging"
    assert staging.is_dir()
    assert list(staging.iterdir()) == []


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


@pytest.mark.asyncio
async def test_concurrent_downloads_across_app_instances_create_one_remote_link(
    cloud_app,
    config: CloudConfig,
    database: Database,
    make_tenant,
    remote: RecordingRemote,
):
    tenant = make_tenant("concurrent-download", "concurrent-download-key")
    second_application = create_cloud_app(config, Database(database.path))
    second_application.state.tmp_client_factory = remote.factory
    started = asyncio.Event()
    release = asyncio.Event()
    remote.download_started[tenant.key] = started
    remote.download_release[tenant.key] = release
    headers = {
        **tenant.csrf_headers,
        "Cookie": "session=session-concurrent-download",
    }

    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=cloud_app),
            base_url=PUBLIC_ORIGIN,
        ) as first_client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=second_application),
            base_url=PUBLIC_ORIGIN,
        ) as second_client,
    ):
        first_pending = asyncio.create_task(
            first_client.post(
                "/api/files/SAME-UKEY/download",
                headers=headers,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        second_pending = asyncio.create_task(
            second_client.post(
                "/api/files/SAME-UKEY/download",
                headers=headers,
            )
        )
        await asyncio.sleep(0.1)
        release.set()
        first_response, second_response = await asyncio.gather(
            first_pending,
            second_pending,
        )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert first_response.json()["data"] == second_response.json()["data"]
    creates = [
        call
        for call in remote.calls
        if call[:3] == (tenant.key, "create_download_link", "SAME-UKEY")
    ]
    assert len(creates) == 1
    with database.connection() as connection:
        real_rows = connection.execute(
            "SELECT COUNT(*) FROM automatic_download_links WHERE user_id = ? AND ukey = ?",
            (tenant.id, "SAME-UKEY"),
        ).fetchone()[0]
        claims = connection.execute(
            "SELECT COUNT(*) FROM automatic_download_claims WHERE user_id = ? AND ukey = ?",
            (tenant.id, "SAME-UKEY"),
        ).fetchone()[0]
    assert real_rows == 1
    assert claims == 0


@pytest.mark.asyncio
async def test_download_claim_heartbeat_keeps_cross_instance_waiters_from_creating_duplicates(
    cloud_app,
    config: CloudConfig,
    database: Database,
    make_tenant,
    remote: RecordingRemote,
    monkeypatch: pytest.MonkeyPatch,
):
    tenant = make_tenant("heartbeat-download", "heartbeat-download-key")
    second_application = create_cloud_app(config, Database(database.path))
    second_application.state.tmp_client_factory = remote.factory
    monkeypatch.setattr(
        tmp_files_routes, "DOWNLOAD_CLAIM_LIFETIME", timedelta(seconds=1)
    )
    monkeypatch.setattr(
        tmp_files_routes, "DOWNLOAD_CLAIM_RENEW_SECONDS", 0.1, raising=False
    )
    monkeypatch.setattr(tmp_files_routes, "DOWNLOAD_CLAIM_WAIT_SECONDS", 3.0)
    monkeypatch.setattr(tmp_files_routes, "DOWNLOAD_CLAIM_POLL_SECONDS", 0.02)
    started = asyncio.Event()
    release = asyncio.Event()
    remote.download_started[tenant.key] = started
    remote.download_release[tenant.key] = release
    headers = {
        **tenant.csrf_headers,
        "Cookie": "session=session-heartbeat-download",
    }

    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=cloud_app),
            base_url=PUBLIC_ORIGIN,
        ) as first_client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=second_application),
            base_url=PUBLIC_ORIGIN,
        ) as second_client,
    ):
        first_pending = asyncio.create_task(
            first_client.post("/api/files/SAME-UKEY/download", headers=headers)
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        await asyncio.sleep(1.2)
        second_pending = asyncio.create_task(
            second_client.post("/api/files/SAME-UKEY/download", headers=headers)
        )
        await asyncio.sleep(0.2)
        creates = [
            call
            for call in remote.calls
            if call[:3] == (tenant.key, "create_download_link", "SAME-UKEY")
        ]
        assert len(creates) == 1
        release.set()
        first_response, second_response = await asyncio.gather(
            first_pending,
            second_pending,
        )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert first_response.json()["data"] == second_response.json()["data"]


@pytest.mark.asyncio
async def test_download_claims_for_different_users_do_not_block_each_other(
    cloud_app,
    make_tenant,
    remote: RecordingRemote,
):
    first = make_tenant("parallel-first", "parallel-first-key")
    second = make_tenant("parallel-second", "parallel-second-key")
    started = asyncio.Event()
    release = asyncio.Event()
    remote.download_started[first.key] = started
    remote.download_release[first.key] = release

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=cloud_app),
        base_url=PUBLIC_ORIGIN,
    ) as client:
        first_pending = asyncio.create_task(
            client.post(
                "/api/files/FIRST-UKEY/download",
                headers={
                    **first.csrf_headers,
                    "Cookie": "session=session-parallel-first",
                },
            )
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        try:
            second_response = await asyncio.wait_for(
                client.post(
                    "/api/files/SECOND-UKEY/download",
                    headers={
                        **second.csrf_headers,
                        "Cookie": "session=session-parallel-second",
                    },
                ),
                timeout=1,
            )
        finally:
            release.set()
        first_response = await first_pending

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert (second.key, "create_download_link", "SECOND-UKEY") in remote.calls


def test_failed_download_creation_releases_the_persistent_claim(
    cloud_app,
    database: Database,
    make_tenant,
    remote: RecordingRemote,
):
    tenant = make_tenant("failed-download", "failed-download-key")
    remote.errors[(tenant.key, "create_download_link")] = TmpLinkBusinessError(
        "remote create failed"
    )

    failed = tenant.client.post(
        "/api/files/RETRY-UKEY/download",
        headers=tenant.csrf_headers,
    )
    remote.errors.clear()
    retried = tenant.client.post(
        "/api/files/RETRY-UKEY/download",
        headers=tenant.csrf_headers,
    )

    assert failed.status_code == 502
    assert retried.status_code == 200
    with database.connection() as connection:
        claims = connection.execute(
            "SELECT COUNT(*) FROM automatic_download_claims WHERE user_id = ? AND ukey = ?",
            (tenant.id, "RETRY-UKEY"),
        ).fetchone()[0]
    assert claims == 0


def test_download_reuses_exactly_one_hour_but_not_null_expiry(
    cloud_app,
    make_tenant,
    remote: RecordingRemote,
    monkeypatch: pytest.MonkeyPatch,
):
    tenant = make_tenant("download-boundary", "download-boundary-key")
    repository = cloud_app.state.repository

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is not None else NOW.replace(tzinfo=None)

    monkeypatch.setattr(tmp_files_routes, "datetime", FrozenDateTime)
    repository.save_automatic_download_link(
        tenant.id,
        ukey="EXACT-UKEY",
        dkey="EXACT-DKEY",
        link="https://tmp/exact",
        expires_at=NOW + timedelta(hours=1),
    )
    repository.save_automatic_download_link(
        tenant.id,
        ukey="NULL-UKEY",
        dkey="NULL-DKEY",
        link="https://tmp/null",
        expires_at=None,
    )

    exact = tenant.client.post(
        "/api/files/EXACT-UKEY/download",
        headers=tenant.csrf_headers,
    )
    null_expiry = tenant.client.post(
        "/api/files/NULL-UKEY/download",
        headers=tenant.csrf_headers,
    )

    assert exact.json()["data"]["dkey"] == "EXACT-DKEY"
    assert null_expiry.json()["data"]["dkey"] != "NULL-DKEY"
    assert [
        call
        for call in remote.calls
        if call[1] == "create_download_link"
    ] == [(tenant.key, "create_download_link", "NULL-UKEY")]


@pytest.mark.parametrize("items_key", ["data", "list"])
def test_wrapped_remote_links_filter_actual_items_and_preserve_wrapper_metadata(
    items_key: str,
    cloud_app,
    make_tenant,
    remote: RecordingRemote,
):
    first = make_tenant(f"wrapped-first-{items_key}", f"wrapped-first-key-{items_key}")
    second = make_tenant(
        f"wrapped-second-{items_key}",
        f"wrapped-second-key-{items_key}",
    )
    repository = cloud_app.state.repository
    repository.save_automatic_download_link(
        first.id,
        ukey="FIRST-UKEY",
        dkey="FIRST-HIDDEN",
        link="https://tmp/first-hidden",
        expires_at=NOW + timedelta(hours=24),
    )
    repository.save_automatic_download_link(
        second.id,
        ukey="SECOND-UKEY",
        dkey="SECOND-VISIBLE",
        link="https://tmp/second-visible",
        expires_at=NOW + timedelta(hours=24),
    )
    remote.links[first.key] = {
        items_key: [
            {"dkey": "FIRST-HIDDEN", "link": "https://tmp/first-hidden"},
            {"dkey": "SECOND-VISIBLE", "link": "https://tmp/second-visible"},
            "invalid-item",
            {"dkey": "MANUAL", "link": "https://tmp/manual"},
        ],
        "page": 3,
        "total": 4,
    }

    response = first.client.get("/api/links?page=3")

    assert response.status_code == 200
    wrapper = response.json()["data"]
    assert wrapper == {
        items_key: [
            {
                "dkey": "SECOND-VISIBLE",
                "link": "https://tmp/second-visible",
                "source": "tmp",
            },
            {
                "dkey": "MANUAL",
                "link": "https://tmp/manual",
                "source": "tmp",
            },
        ],
        "page": 3,
        "total": 4,
    }
    assert "source" not in wrapper


@pytest.mark.parametrize("items_key", [None, "data", "list"])
def test_link_listing_drops_malformed_items_and_keeps_valid_aliases(
    items_key: str | None,
    cloud_app,
    make_tenant,
    remote: RecordingRemote,
):
    tenant = make_tenant(f"malformed-links-{items_key}", f"malformed-links-key-{items_key}")
    items = [
        {},
        {"dkey": "", "link": "https://tmp/empty-key"},
        {"dkey": [], "link": "https://tmp/unhashable-key"},
        {"dkey": "NONSTRING", "link": ""},
        {"dkey": "NONSTRING", "link": []},
        {"dkey": "VALID-DKEY", "link": "https://tmp/valid"},
        {"direct_key": "VALID-DIRECT", "url": "https://tmp/direct"},
    ]
    remote.links[tenant.key] = (
        items if items_key is None else {items_key: items, "page": 2, "total": len(items)}
    )

    response = tenant.client.get("/api/links?page=2")

    assert response.status_code == 200
    data = response.json()["data"]
    visible = data if items_key is None else data[items_key]
    assert visible == [
        {"dkey": "VALID-DKEY", "link": "https://tmp/valid", "source": "tmp"},
        {
            "direct_key": "VALID-DIRECT",
            "url": "https://tmp/direct",
            "source": "tmp",
        },
    ]
    if items_key is not None:
        assert data["page"] == 2
        assert data["total"] == len(items)


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
