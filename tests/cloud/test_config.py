from __future__ import annotations

import pytest

from app.cloud.config import (
    GLOBAL_QUOTA_BYTES,
    MAX_FILE_BYTES,
    MIN_FREE_BYTES,
    USER_QUOTA_BYTES,
    CloudConfig,
)


def cloud_environ(**overrides: str) -> dict[str, str]:
    environ = {
        "APP_MODE": "cloud",
        "SESSION_SECRET": "session-secret",
        "KEY_ENCRYPTION_KEY": "encryption-key",
        "DATABASE_PATH": "/var/lib/tmp-link-manager/app.db",
        "STORAGE_PATH": "/var/lib/tmp-link-manager/files",
        "PUBLIC_ORIGIN": "https://cloud.example.com",
    }
    environ.update(overrides)
    return environ


@pytest.mark.parametrize(
    "variable",
    [
        "SESSION_SECRET",
        "KEY_ENCRYPTION_KEY",
        "DATABASE_PATH",
        "STORAGE_PATH",
        "PUBLIC_ORIGIN",
    ],
)
def test_cloud_mode_rejects_each_missing_required_setting(variable: str):
    environ = cloud_environ(**{variable: ""})

    with pytest.raises(ValueError, match=variable):
        CloudConfig.from_env(environ)


def test_cloud_mode_uses_exact_default_storage_limits():
    config = CloudConfig.from_env(cloud_environ())

    assert config.max_file_bytes == MAX_FILE_BYTES == 200 * 1024 * 1024
    assert config.user_quota_bytes == USER_QUOTA_BYTES == 1024 * 1024 * 1024
    assert config.global_quota_bytes == GLOBAL_QUOTA_BYTES == 15 * 1024 * 1024 * 1024
    assert config.min_free_bytes == MIN_FREE_BYTES == 8 * 1024 * 1024 * 1024


def test_local_mode_does_not_require_cloud_settings():
    config = CloudConfig.from_env({"APP_MODE": "local"})

    assert config.mode == "local"
    assert config.session_secret is None
    assert config.key_encryption_key is None
    assert config.database_path is None
    assert config.storage_path is None
    assert config.public_origin is None


def test_default_mode_is_local_without_cloud_settings():
    config = CloudConfig.from_env({})

    assert config.mode == "local"
