from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.client import (
    TmpLinkBusinessError,
    TmpLinkConnectionError,
    TmpLinkTimeoutError,
)
from app.main import create_app
from app.models import ServiceResult


class FakeTmpLinkClient:
    def __init__(self):
        self.calls: list[tuple] = []
        self.error: Exception | None = None

    def _result(self, data):
        if self.error:
            raise self.error
        return ServiceResult(ok=True, data=data, message="")

    async def quota(self):
        self.calls.append(("quota",))
        return self._result({"quota": 1024})

    async def list_files(self, page=1):
        self.calls.append(("list_files", page))
        return self._result([{"ukey": "U1", "name": "report.txt", "size": 5}])

    async def list_links(self, page=1):
        self.calls.append(("list_links", page))
        return self._result([{"dkey": "D1", "link": "/download/D1"}])

    async def create_link(self, ukey, valid_time=None, download_limit=None):
        self.calls.append(("create_link", ukey, valid_time, download_limit))
        return self._result({"dkey": "D1", "link": "/download/D1"})

    async def delete_link(self, dkey, delete_file=False):
        self.calls.append(("delete_link", dkey, delete_file))
        return self._result({"deleted": True})

    async def create_download_link(self, ukey):
        self.calls.append(("create_download_link", ukey))
        return self._result({"dkey": "D1", "link": "/download/D1"})

    async def delete_file(self, ukey):
        self.calls.append(("delete_file", ukey))
        return self._result({"deleted": True})

    async def upload(self, file_name, content, model):
        self.calls.append(("upload", file_name, content, model))
        return self._result("UPLOADED-UKEY")


@pytest.fixture
def fake_remote():
    return FakeTmpLinkClient()


@pytest.fixture
def client(tmp_path: Path, fake_remote: FakeTmpLinkClient):
    app = create_app(
        settings_path=tmp_path / "settings.json",
        client_factory=lambda api_key: fake_remote,
    )
    return TestClient(app)


def configure(client: TestClient, key: str = "route-secret"):
    return client.put(
        "/api/settings",
        json={"api_key": key, "custom_domain": "pan.cloudcode.xyz"},
    )


def test_health_and_settings_do_not_expose_key(client: TestClient):
    assert client.get("/health").json() == {"status": "ok"}

    response = configure(client)

    assert response.status_code == 200
    assert response.json()["data"]["key_configured"] is True
    assert "route-secret" not in response.text
    assert "route-secret" not in client.get("/api/settings").text


def test_settings_empty_key_preserves_and_clear_is_explicit(client: TestClient):
    configure(client)
    response = client.put(
        "/api/settings",
        json={"api_key": "", "custom_domain": "files.example.com"},
    )
    assert response.json()["data"] == {
        "key_configured": True,
        "custom_domain": "files.example.com",
    }

    response = client.delete("/api/settings/key")
    assert response.json()["data"]["key_configured"] is False


def test_missing_key_is_rejected_before_remote_call(client, fake_remote):
    response = client.get("/api/quota")

    assert response.status_code == 400
    assert response.json()["ok"] is False
    assert fake_remote.calls == []


def test_proxy_routes_call_expected_remote_methods(client, fake_remote):
    configure(client)

    assert client.post("/api/settings/test").status_code == 200
    assert client.get("/api/quota").status_code == 200
    assert client.get("/api/files?page=2").json()["data"][0]["ukey"] == "U1"
    assert client.get("/api/links?page=3").json()["data"][0]["dkey"] == "D1"
    assert client.post(
        "/api/links",
        json={"ukey": "U1", "valid_time": 60, "download_limit": 3},
    ).status_code == 200
    assert client.delete("/api/links/D1?delete_file=true").status_code == 200
    assert client.post(
        "/api/uploads",
        data={"model": "99"},
        files={"file": ("report.txt", b"hello", "text/plain")},
    ).json()["data"] == "UPLOADED-UKEY"

    assert fake_remote.calls == [
        ("quota",),
        ("quota",),
        ("list_files", 2),
        ("list_links", 3),
        ("create_link", "U1", 60, 3),
        ("delete_link", "D1", True),
        ("upload", "report.txt", b"hello", 99),
    ]


def test_file_download_and_delete_routes_call_expected_methods(client, fake_remote):
    configure(client)

    download = client.post("/api/files/FILE%20UKEY/download")
    deleted = client.delete("/api/files/FILE%20UKEY")

    assert download.status_code == 200
    assert download.json() == {
        "ok": True,
        "data": {"dkey": "D1", "link": "/download/D1"},
        "message": "",
    }
    assert deleted.status_code == 200
    assert deleted.json()["data"] == {"deleted": True}
    assert deleted.json()["message"] == "File deleted"
    assert fake_remote.calls == [
        ("create_download_link", "FILE UKEY"),
        ("delete_file", "FILE UKEY"),
    ]


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (TmpLinkBusinessError("rejected"), 502),
        (TmpLinkConnectionError("offline"), 502),
        (TmpLinkTimeoutError("late"), 504),
    ],
)
def test_remote_errors_are_translated(client, fake_remote, error, expected_status):
    configure(client)
    fake_remote.error = error

    response = client.get("/api/quota")

    assert response.status_code == expected_status
    assert response.json()["ok"] is False
    assert "route-secret" not in response.text


def test_upload_rejects_invalid_model_and_empty_file(client, fake_remote):
    configure(client)

    invalid_model = client.post(
        "/api/uploads",
        data={"model": "5"},
        files={"file": ("report.txt", b"hello", "text/plain")},
    )
    empty_file = client.post(
        "/api/uploads",
        data={"model": "99"},
        files={"file": ("empty.txt", b"", "text/plain")},
    )

    assert invalid_model.status_code == 422
    assert empty_file.status_code == 422
    assert fake_remote.calls == []
