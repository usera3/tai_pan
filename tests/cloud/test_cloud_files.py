from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.cloud.app import create_cloud_app
from app.cloud.config import CloudConfig
from app.cloud.db import Database
from app.models import ServiceResult


PUBLIC_ORIGIN = "https://cloud.example.com"
NOW = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)


class RecordingTmpFactory:
    def __init__(self) -> None:
        self.keys: list[str] = []
        self.calls: list[tuple[object, ...]] = []

    def __call__(self, key: str):
        self.keys.append(key)
        factory = self

        class Client:
            async def list_files(self, page: int = 1):
                factory.calls.append(("list_files", page))
                return ServiceResult(
                    ok=True,
                    data=[{"ukey": "TMP-1", "name": "temporary.txt"}],
                )

            async def create_download_link(self, ukey: str):
                factory.calls.append(("create_download_link", ukey))
                return ServiceResult(
                    ok=True,
                    data={"dkey": "DKEY", "link": "https://tmp.invalid/file"},
                )

            async def delete_file(self, ukey: str):
                factory.calls.append(("delete_file", ukey))
                return ServiceResult(ok=True, data={"deleted": True})

            async def upload_file(self, *args):
                factory.calls.append(("upload_file", *args))
                return ServiceResult(ok=True, data={"ukey": "TMP-UPLOAD"})

        return Client()


@dataclass
class Tenant:
    id: str
    csrf_token: str
    client: TestClient

    @property
    def headers(self) -> dict[str, str]:
        return {"Origin": PUBLIC_ORIGIN, "X-CSRF-Token": self.csrf_token}


@pytest.fixture
def config(tmp_path: Path) -> CloudConfig:
    return CloudConfig(
        mode="cloud",
        session_secret="task-six-session-secret",
        key_encryption_key=Fernet.generate_key().decode("ascii"),
        database_path=tmp_path / "cloud.db",
        storage_path=tmp_path / "files",
        public_origin=PUBLIC_ORIGIN,
        max_file_bytes=8,
        user_quota_bytes=12,
        global_quota_bytes=20,
        min_free_bytes=0,
    )


@pytest.fixture
def cloud_app(config: CloudConfig):
    assert config.database_path is not None
    application = create_cloud_app(config, Database(config.database_path))
    application.state.tmp_client_factory = RecordingTmpFactory()
    return application


@pytest.fixture
def make_tenant(cloud_app):
    clients: list[TestClient] = []

    def create(
        username: str,
        *,
        role: str = "user",
        tmp_key: str | None = None,
        must_change_password: bool = False,
    ):
        repository = cloud_app.state.repository
        user = repository.create_user(
            username,
            cloud_app.state.password_service.hash("tenant password"),
            role=role,
            must_change_password=must_change_password,
            now=NOW,
        )
        if tmp_key is not None:
            repository.save_user_settings(user.id, tmp_key=tmp_key, now=NOW)
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
            "session", session_token, domain="cloud.example.com", path="/"
        )
        clients.append(client)
        return Tenant(user.id, csrf_token, client)

    yield create

    for client in clients:
        client.close()


def cloud_upload(tenant: Tenant, name: str, content: bytes, content_type="text/plain"):
    return tenant.client.post(
        "/api/uploads",
        data={"storage": "cloud"},
        files={"file": (name, content, content_type)},
        headers=tenant.headers,
    )


def test_cloud_quota_is_authenticated_owner_scoped_and_redacted(cloud_app, make_tenant):
    owner = make_tenant("quota-owner")
    other = make_tenant("quota-other")
    uploaded = cloud_upload(owner, "quota.txt", b"quota").json()["data"]

    owner_response = owner.client.get("/api/cloud/quota")
    other_response = other.client.get("/api/cloud/quota")
    with TestClient(cloud_app, base_url=PUBLIC_ORIGIN) as anonymous:
        unauthenticated = anonymous.get("/api/cloud/quota")

    assert owner_response.status_code == other_response.status_code == 200
    assert owner_response.json() == {
        "ok": True,
        "data": {"used": 5, "total": cloud_app.state.config.user_quota_bytes},
        "message": "",
    }
    assert other_response.json()["data"]["used"] == 0
    assert unauthenticated.status_code == 401
    rendered = owner_response.text
    assert uploaded["id"] not in rendered
    assert str(cloud_app.state.config.storage_path) not in rendered
    assert cloud_app.state.config.session_secret not in rendered


def test_permanent_upload_needs_no_tmp_key_and_never_constructs_tmp_client(
    cloud_app, make_tenant
):
    tenant = make_tenant("permanent-upload")
    factory = cloud_app.state.tmp_client_factory

    response = cloud_upload(tenant, "../../report.txt", b"content")

    assert response.status_code == 200
    assert factory.keys == []
    assert factory.calls == []
    assert response.json()["data"] == {
        "id": response.json()["data"]["id"],
        "name": "report.txt",
        "content_type": "text/plain",
        "size": 7,
        "sha256": response.json()["data"]["sha256"],
        "source": "cloud",
    }
    assert "storage_path" not in response.text
    assert str(cloud_app.state.config.storage_path) not in response.text


def test_file_listing_source_is_explicit_and_all_works_without_tmp_key(
    cloud_app, make_tenant
):
    tenant = make_tenant("cloud-list-no-key")
    uploaded = cloud_upload(tenant, "cloud.txt", b"cloud")
    assert uploaded.status_code == 200

    cloud_only = tenant.client.get("/api/files?source=cloud")
    combined = tenant.client.get("/api/files?source=all")

    assert cloud_only.status_code == 200
    assert combined.status_code == 200
    assert cloud_only.json()["data"] == combined.json()["data"]
    assert [item["source"] for item in combined.json()["data"]] == ["cloud"]
    assert cloud_app.state.tmp_client_factory.keys == []
    assert all("storage_path" not in item for item in combined.json()["data"])


def test_all_listing_is_flat_and_labels_every_tmp_and_cloud_item(
    cloud_app, make_tenant
):
    tenant = make_tenant("combined-list", tmp_key="tenant-tmp-key")
    assert cloud_upload(tenant, "cloud.txt", b"cloud").status_code == 200

    response = tenant.client.get("/api/files?source=all&page=2")

    assert response.status_code == 200
    assert [item["source"] for item in response.json()["data"]] == [
        "tmp",
        "cloud",
    ]
    assert cloud_app.state.tmp_client_factory.keys == ["tenant-tmp-key"]
    assert cloud_app.state.tmp_client_factory.calls == [("list_files", 2)]


def test_cloud_download_is_owner_only_uses_accel_redirect_and_safe_unicode_header(
    cloud_app, make_tenant
):
    owner = make_tenant("download-owner")
    other = make_tenant("download-other")
    admin = make_tenant("download-admin", role="admin")
    uploaded = cloud_upload(owner, "../safe-\u6d4b\u8bd5.txt", b"abc").json()["data"]
    with cloud_app.state.database.connection() as connection:
        connection.execute(
            """
            UPDATE cloud_files SET original_name = ?, content_type = ? WHERE id = ?
            """,
            (
                "safe-\u6d4b\u8bd5\r\n.txt",
                "text/plain\r\nX-Injected: unsafe",
                uploaded["id"],
            ),
        )
    path = f"/api/files/{uploaded['id']}/download?source=cloud"

    response = owner.client.post(path, headers=owner.headers)

    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-type"] == "application/octet-stream"
    assert "x-injected" not in response.headers
    assert response.headers["x-accel-redirect"].startswith(
        f"/_protected_files/users/{owner.id}/"
    )
    disposition = response.headers["content-disposition"]
    assert "\r" not in disposition and "\n" not in disposition
    assert "filename*=UTF-8''" + quote("safe-\u6d4b\u8bd5__.txt", safe="") in disposition
    assert str(cloud_app.state.config.storage_path) not in repr(response.headers)

    denied = [
        other.client.post(path, headers=other.headers),
        admin.client.post(path, headers=admin.headers),
        owner.client.post(
            "/api/files/00000000-0000-0000-0000-000000000000/download?source=cloud",
            headers=owner.headers,
        ),
    ]
    assert [item.status_code for item in denied] == [403, 403, 403]
    assert len({item.text for item in denied}) == 1
    assert str(cloud_app.state.config.storage_path) not in " ".join(
        item.text for item in denied
    )


def test_cloud_download_head_preflight_enforces_owner_and_active_authentication(
    cloud_app, make_tenant
):
    owner = make_tenant("head-owner")
    other = make_tenant("head-other")
    admin = make_tenant("head-admin", role="admin")
    forced = make_tenant("head-forced", must_change_password=True)
    uploaded = cloud_upload(owner, "head.txt", b"head").json()["data"]
    path = f"/api/files/{uploaded['id']}/download?source=cloud"

    owner_get = owner.client.get(path)
    owner_head = owner.client.head(path)
    with TestClient(cloud_app, base_url=PUBLIC_ORIGIN) as anonymous:
        responses = [
            other.client.get(path),
            other.client.head(path),
            admin.client.get(path),
            admin.client.head(path),
            forced.client.get(path),
            forced.client.head(path),
            anonymous.get(path),
            anonymous.head(path),
            owner.client.head(
                "/api/files/00000000-0000-0000-0000-000000000000/download?source=cloud"
            ),
        ]

    assert owner_get.status_code == owner_head.status_code == 200
    assert owner_head.content == b""
    assert [response.status_code for response in responses] == [
        403,
        403,
        403,
        403,
        403,
        403,
        401,
        401,
        403,
    ]


def test_cloud_delete_cleans_metadata_when_disk_is_missing_and_then_returns_same_403(
    cloud_app, make_tenant
):
    tenant = make_tenant("delete-owner")
    uploaded = cloud_upload(tenant, "delete.txt", b"abc").json()["data"]
    record = cloud_app.state.repository.get_cloud_file(tenant.id, uploaded["id"])
    assert record is not None
    storage_root = cloud_app.state.config.storage_path
    assert storage_root is not None
    storage_root.joinpath(*PurePosixPath(record.storage_path).parts).unlink()

    deleted = tenant.client.delete(
        f"/api/files/{uploaded['id']}?source=cloud", headers=tenant.headers
    )
    missing_download = tenant.client.post(
        f"/api/files/{uploaded['id']}/download?source=cloud", headers=tenant.headers
    )
    missing_delete = tenant.client.delete(
        f"/api/files/{uploaded['id']}?source=cloud", headers=tenant.headers
    )

    assert deleted.status_code == 200
    assert deleted.json() == {
        "ok": True,
        "data": {"id": uploaded["id"], "source": "cloud"},
        "message": "File deleted",
    }
    assert missing_download.status_code == missing_delete.status_code == 403
    assert missing_download.text == missing_delete.text
    assert cloud_app.state.repository.get_cloud_file(tenant.id, uploaded["id"]) is None
    events = cloud_app.state.repository.list_audit_events(tenant.id)
    assert [(event.event_type, event.target_id) for event in events] == [
        ("cloud_file_deleted", uploaded["id"])
    ]
    assert str(storage_root) not in deleted.text + repr(events)


def test_cloud_delete_denies_other_user_and_admin_while_disk_file_is_present(
    cloud_app, make_tenant
):
    owner = make_tenant("delete-present-owner")
    other = make_tenant("delete-present-other")
    admin = make_tenant("delete-present-admin", role="admin")
    uploaded = cloud_upload(owner, "private.txt", b"private").json()["data"]
    record = cloud_app.state.repository.get_cloud_file(owner.id, uploaded["id"])
    assert record is not None
    storage_root = cloud_app.state.config.storage_path
    assert storage_root is not None
    stored_path = storage_root.joinpath(*PurePosixPath(record.storage_path).parts)
    path = f"/api/files/{uploaded['id']}?source=cloud"

    denied = [
        other.client.delete(path, headers=other.headers),
        admin.client.delete(path, headers=admin.headers),
    ]

    assert [response.status_code for response in denied] == [403, 403]
    assert denied[0].text == denied[1].text
    assert stored_path.read_bytes() == b"private"
    assert (
        cloud_app.state.repository.get_cloud_file(owner.id, uploaded["id"]) == record
    )
    assert cloud_app.state.repository.list_audit_events(owner.id) == []


def test_identifier_shape_never_selects_storage_backend(cloud_app, make_tenant):
    tenant = make_tenant("explicit-source")
    uploaded = cloud_upload(tenant, "cloud.txt", b"abc").json()["data"]

    default_download = tenant.client.post(
        f"/api/files/{uploaded['id']}/download", headers=tenant.headers
    )
    cloud_with_tmp_shape = tenant.client.post(
        "/api/files/TMP_LIKE/download?source=cloud", headers=tenant.headers
    )

    assert default_download.status_code == 400
    assert default_download.json() == {
        "detail": "TMP.link API key is not configured"
    }
    assert cloud_with_tmp_shape.status_code == 403
    assert cloud_app.state.repository.get_cloud_file(tenant.id, uploaded["id"]) is not None


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("GET", "/api/files?source=invalid", {}),
        (
            "POST",
            "/api/uploads",
            {"data": {"storage": "invalid"}, "files": {"file": ("x", b"x")}},
        ),
        ("POST", "/api/files/id/download?source=invalid", {}),
        ("DELETE", "/api/files/id?source=invalid", {}),
    ],
)
def test_invalid_source_selection_is_rejected(
    method: str, path: str, kwargs: dict, make_tenant
):
    tenant = make_tenant("invalid-source")

    response = tenant.client.request(method, path, headers=tenant.headers, **kwargs)

    assert response.status_code == 422
