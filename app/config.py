from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


DEFAULT_CUSTOM_DOMAIN = "pan.cloudcode.xyz"
DOMAIN_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$"
)


def validate_domain(value: str) -> str:
    domain = value.strip().lower().rstrip(".")
    if not DOMAIN_PATTERN.fullmatch(domain):
        raise ValueError("custom domain must be a hostname without a URL scheme or path")
    return domain


@dataclass(frozen=True)
class AppSettings:
    api_key: str = ""
    custom_domain: str = DEFAULT_CUSTOM_DOMAIN


class SettingsStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> AppSettings:
        if not self.path.exists():
            return AppSettings()
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        return AppSettings(
            api_key=str(payload.get("api_key", "")),
            custom_domain=validate_domain(
                str(payload.get("custom_domain", DEFAULT_CUSTOM_DOMAIN))
            ),
        )

    def update(self, api_key: str, custom_domain: str) -> AppSettings:
        current = self.load()
        normalized_key = api_key.strip() or current.api_key
        settings = AppSettings(
            api_key=normalized_key,
            custom_domain=validate_domain(custom_domain),
        )
        self._save(settings)
        return settings

    def clear_key(self) -> AppSettings:
        current = self.load()
        settings = AppSettings(api_key="", custom_domain=current.custom_domain)
        self._save(settings)
        return settings

    def public_settings(self) -> dict[str, bool | str]:
        settings = self.load()
        return {
            "key_configured": bool(settings.api_key),
            "custom_domain": settings.custom_domain,
        }

    def _save(self, settings: AppSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            dir=self.path.parent,
            text=True,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(asdict(settings), handle, ensure_ascii=True, indent=2)
                handle.write("\n")
            self._restrict_permissions(temporary_path)
            temporary_path.replace(self.path)
            self._restrict_permissions(self.path)
        finally:
            temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _restrict_permissions(path: Path) -> None:
        try:
            path.chmod(0o600)
        except OSError:
            pass
