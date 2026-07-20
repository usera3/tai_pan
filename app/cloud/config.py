from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


MAX_FILE_BYTES = 200 * 1024 * 1024
USER_QUOTA_BYTES = 1024 * 1024 * 1024
GLOBAL_QUOTA_BYTES = 15 * 1024 * 1024 * 1024
MIN_FREE_BYTES = 8 * 1024 * 1024 * 1024


@dataclass(frozen=True)
class CloudConfig:
    mode: str
    session_secret: str | None
    key_encryption_key: str | None
    database_path: Path | None
    storage_path: Path | None
    public_origin: str | None
    max_file_bytes: int = MAX_FILE_BYTES
    user_quota_bytes: int = USER_QUOTA_BYTES
    global_quota_bytes: int = GLOBAL_QUOTA_BYTES
    min_free_bytes: int = MIN_FREE_BYTES

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> CloudConfig:
        mode = environ.get("APP_MODE", "local").strip().lower()
        if mode not in {"local", "cloud"}:
            raise ValueError("APP_MODE must be either 'local' or 'cloud'")
        if mode == "local":
            return cls(
                mode="local",
                session_secret=None,
                key_encryption_key=None,
                database_path=None,
                storage_path=None,
                public_origin=None,
            )

        required = {
            "SESSION_SECRET": environ.get("SESSION_SECRET", "").strip(),
            "KEY_ENCRYPTION_KEY": environ.get("KEY_ENCRYPTION_KEY", "").strip(),
            "DATABASE_PATH": environ.get("DATABASE_PATH", "").strip(),
            "STORAGE_PATH": environ.get("STORAGE_PATH", "").strip(),
            "PUBLIC_ORIGIN": environ.get("PUBLIC_ORIGIN", "").strip(),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(f"cloud mode requires {', '.join(missing)}")

        return cls(
            mode="cloud",
            session_secret=required["SESSION_SECRET"],
            key_encryption_key=required["KEY_ENCRYPTION_KEY"],
            database_path=Path(required["DATABASE_PATH"]),
            storage_path=Path(required["STORAGE_PATH"]),
            public_origin=required["PUBLIC_ORIGIN"],
        )
