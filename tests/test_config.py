from pathlib import Path

import pytest

from app.config import SettingsStore


def test_settings_store_never_exposes_saved_key(tmp_path: Path):
    store = SettingsStore(tmp_path / "settings.json")
    store.update(api_key="secret-value", custom_domain="pan.cloudcode.xyz")

    public = store.public_settings()

    assert public == {"key_configured": True, "custom_domain": "pan.cloudcode.xyz"}
    assert "secret-value" not in repr(public)


def test_empty_key_update_preserves_existing_key(tmp_path: Path):
    store = SettingsStore(tmp_path / "settings.json")
    store.update(api_key="first-key", custom_domain="pan.cloudcode.xyz")
    store.update(api_key="", custom_domain="files.example.com")

    assert store.load().api_key == "first-key"
    assert store.load().custom_domain == "files.example.com"


def test_clear_key_removes_only_key(tmp_path: Path):
    store = SettingsStore(tmp_path / "settings.json")
    store.update(api_key="secret-value", custom_domain="files.example.com")

    store.clear_key()

    assert store.load().api_key == ""
    assert store.load().custom_domain == "files.example.com"


@pytest.mark.parametrize(
    "domain",
    ["", "https://pan.example.com", "pan.example.com/path", "pan example.com"],
)
def test_settings_store_rejects_invalid_domain(tmp_path: Path, domain: str):
    store = SettingsStore(tmp_path / "settings.json")

    with pytest.raises(ValueError, match="custom domain"):
        store.update(api_key="value", custom_domain=domain)
