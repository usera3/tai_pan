from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from cryptography.fernet import Fernet


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CLOUD_VARIABLES = {
    "APP_MODE",
    "SESSION_SECRET",
    "KEY_ENCRYPTION_KEY",
    "DATABASE_PATH",
    "STORAGE_PATH",
    "PUBLIC_ORIGIN",
}
IMPORT_SCRIPT = """
import json
from app.main import app
print(json.dumps({
    "mode": app.state.mode,
    "title": app.title,
    "routes": sorted(app.openapi()["paths"]),
}))
"""


def import_application(overrides: dict[str, str]) -> subprocess.CompletedProcess[str]:
    environment = {
        key: value for key, value in os.environ.items() if key not in CLOUD_VARIABLES
    }
    environment.update(overrides)
    return subprocess.run(
        [str(PROJECT_ROOT / ".venv/bin/python"), "-c", IMPORT_SCRIPT],
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def complete_cloud_environment(tmp_path: Path) -> dict[str, str]:
    return {
        "APP_MODE": "cloud",
        "SESSION_SECRET": "mode-selection-session-secret",
        "KEY_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
        "DATABASE_PATH": str(tmp_path / "cloud.db"),
        "STORAGE_PATH": str(tmp_path / "files"),
        "PUBLIC_ORIGIN": "https://cloud.example.com",
    }


@pytest.mark.parametrize("environment", [{}, {"APP_MODE": "local"}])
def test_default_and_explicit_local_imports_keep_existing_application(environment):
    result = import_application(environment)

    assert result.returncode == 0, result.stderr
    details = json.loads(result.stdout)
    assert details["mode"] == "local"
    assert details["title"] == "TMP Link Manager"
    assert "/api/settings" in details["routes"]
    assert "/api/auth/login" not in details["routes"]


def test_cloud_import_constructs_cloud_application_with_complete_config(tmp_path):
    result = import_application(complete_cloud_environment(tmp_path))

    assert result.returncode == 0, result.stderr
    details = json.loads(result.stdout)
    assert details["mode"] == "cloud"
    assert details["title"] == "TMP Link Manager Cloud"
    assert "/api/auth/login" in details["routes"]
    assert "/api/settings" in details["routes"]
    assert (tmp_path / "cloud.db").exists()


@pytest.mark.parametrize(
    "missing",
    [
        "SESSION_SECRET",
        "KEY_ENCRYPTION_KEY",
        "DATABASE_PATH",
        "STORAGE_PATH",
        "PUBLIC_ORIGIN",
    ],
)
def test_cloud_import_fails_closed_when_production_config_is_incomplete(
    tmp_path: Path, missing: str
):
    environment = complete_cloud_environment(tmp_path)
    session_secret = environment["SESSION_SECRET"]
    encryption_key = environment["KEY_ENCRYPTION_KEY"]
    environment.pop(missing)

    result = import_application(environment)

    assert result.returncode != 0
    assert missing in result.stderr
    assert session_secret not in result.stderr
    assert encryption_key not in result.stderr
