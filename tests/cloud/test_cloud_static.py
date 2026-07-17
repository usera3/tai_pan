from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.cloud.app import create_cloud_app
from app.cloud.config import CloudConfig
from app.cloud.db import Database
from app.main import create_app


PUBLIC_ORIGIN = "https://cloud.example.com"


@pytest.fixture
def cloud_app(tmp_path: Path):
    config = CloudConfig(
        mode="cloud",
        session_secret="static-test-session-secret",
        key_encryption_key=Fernet.generate_key().decode("ascii"),
        database_path=tmp_path / "cloud.db",
        storage_path=tmp_path / "files",
        public_origin=PUBLIC_ORIGIN,
    )
    return create_cloud_app(config, Database(config.database_path))


def test_cloud_root_and_static_assets_are_served(cloud_app):
    with TestClient(cloud_app, base_url=PUBLIC_ORIGIN) as client:
        root = client.get("/")
        stylesheet = client.get("/static/cloud.css")
        script = client.get("/static/cloud.js")
        icons = client.get("/static/vendor/lucide.min.js")

    assert root.status_code == 200
    assert 'id="auth-shell"' in root.text
    assert '/static/cloud.css' in root.text
    assert '/static/cloud.js' in root.text
    assert stylesheet.status_code == script.status_code == icons.status_code == 200
    assert stylesheet.headers["content-type"].startswith("text/css")
    assert "javascript" in script.headers["content-type"]


def test_cloud_static_contract_has_security_and_accessibility_hooks():
    static_dir = Path(__file__).parents[2] / "app" / "static"
    html = (static_dir / "cloud.html").read_text(encoding="utf-8")
    css = (static_dir / "cloud.css").read_text(encoding="utf-8")
    script = (static_dir / "cloud.js").read_text(encoding="utf-8")

    for forbidden in ("localStorage", "sessionStorage", "indexedDB", "document.cookie"):
        assert forbidden not in script
    assert "X-CSRF-Token" in script
    assert 'type="password"' in html
    assert 'autocomplete="current-password"' in html
    assert 'autocomplete="new-password"' in html
    assert "vendor/lucide.min.js" in html
    assert "letter-spacing: 0" in css
    assert "linear-gradient" not in css
    assert "radial-gradient" not in css
    assert "border-radius: 999" not in css
    assert "font-size: clamp" not in css
    assert "font-size: calc" not in css


def test_cloud_download_get_is_registered_without_removing_post_compatibility(cloud_app):
    operations = cloud_app.openapi()["paths"]["/api/files/{ukey}/download"]

    assert "get" in operations
    assert "head" in operations
    assert "post" in operations


def test_cloud_static_contract_uses_three_day_default_and_resets_user_scoped_ui():
    static_dir = Path(__file__).parents[2] / "app" / "static"
    html = (static_dir / "cloud.html").read_text(encoding="utf-8")
    script = (static_dir / "cloud.js").read_text(encoding="utf-8")

    assert '<option value="1" selected>3 天</option>' in html
    assert '<option value="2">7 天</option>' in html
    assert 'id="password-logout-button"' in html
    for required in (
        "abortActiveUploads",
        "closeUserDialogs",
        "state.uploads = []",
        "state.users = []",
        "state.invitations = []",
        "state.confirmAction = null",
        'api("/api/cloud/quota")',
        'method: "HEAD"',
    ):
        assert required in script


def test_local_root_and_assets_are_unchanged(tmp_path: Path):
    local_app = create_app(settings_path=tmp_path / "settings.json")
    with TestClient(local_app) as client:
        root = client.get("/")
        app_script = client.get("/static/app.js")
        cloud_page = client.get("/static/cloud.html")

    assert root.status_code == 200
    assert 'id="app" class="app-shell"' in root.text
    assert "/static/app.js" in root.text
    assert app_script.status_code == 200
    assert cloud_page.status_code == 200
    assert 'id="auth-shell"' in cloud_page.text
