from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app


def test_static_application_shell_contains_required_controls(tmp_path: Path):
    client = TestClient(create_app(settings_path=tmp_path / "settings.json"))

    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    assert html.count('id="app"') == 1
    for view in ("dashboard", "files", "links", "settings"):
        assert f'data-view="{view}"' in html
    assert 'id="settings-form"' in html
    assert 'id="api-key"' in html
    assert 'id="custom-domain"' in html
    assert 'id="file-input"' in html and "multiple" in html
    assert '/static/vendor/lucide.min.js' in html
    assert "cdn.jsdelivr.net" not in html
    assert "unpkg.com" not in html


def test_frontend_source_does_not_persist_api_key_in_browser_storage():
    javascript = Path("app/static/app.js").read_text(encoding="utf-8")

    assert "localStorage" not in javascript
    assert "sessionStorage" not in javascript
    assert "document.cookie" not in javascript
    assert "indexedDB" not in javascript
    assert "innerHTML" not in javascript


def test_static_assets_are_served(tmp_path: Path):
    client = TestClient(create_app(settings_path=tmp_path / "settings.json"))

    assert client.get("/static/app.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/vendor/lucide.min.js").status_code == 200
