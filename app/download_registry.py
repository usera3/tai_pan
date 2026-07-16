from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class DownloadLinkRecord:
    ukey: str
    dkey: str
    link: str
    expires_at: float

    def as_remote_data(self) -> dict[str, str]:
        return {"dkey": self.dkey, "link": self.link}


class DownloadLinkRegistry:
    def __init__(self, path: Path):
        self.path = Path(path)

    def active_for(
        self,
        ukey: str,
        *,
        now: float | None = None,
        minimum_remaining: float = 3600,
    ) -> DownloadLinkRecord | None:
        current_time = time.time() if now is None else now
        matches = [
            record
            for record in self._load()
            if record.ukey == ukey
            and record.expires_at - current_time >= minimum_remaining
        ]
        return max(matches, key=lambda record: record.expires_at, default=None)

    def hidden_dkeys(self) -> set[str]:
        return {record.dkey for record in self._load()}

    def remember(
        self,
        ukey: str,
        dkey: str,
        link: str,
        *,
        expires_at: float,
    ) -> DownloadLinkRecord:
        record = DownloadLinkRecord(
            ukey=ukey,
            dkey=dkey,
            link=link,
            expires_at=expires_at,
        )
        records = [item for item in self._load() if item.dkey != dkey]
        records.append(record)
        self._save(records)
        return record

    def forget_ukey(self, ukey: str) -> None:
        records = [record for record in self._load() if record.ukey != ukey]
        self._save(records)

    def _load(self) -> list[DownloadLinkRecord]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            raw_records = payload.get("links", [])
            return [
                DownloadLinkRecord(
                    ukey=str(item["ukey"]),
                    dkey=str(item["dkey"]),
                    link=str(item["link"]),
                    expires_at=float(item["expires_at"]),
                )
                for item in raw_records
                if isinstance(item, dict)
            ]
        except (OSError, ValueError, TypeError, KeyError):
            return []

    def _save(self, records: list[DownloadLinkRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            dir=self.path.parent,
            text=True,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(
                    {"version": 1, "links": [asdict(record) for record in records]},
                    handle,
                    ensure_ascii=True,
                    indent=2,
                )
                handle.write("\n")
            temporary_path.chmod(0o600)
            temporary_path.replace(self.path)
            self.path.chmod(0o600)
        finally:
            temporary_path.unlink(missing_ok=True)
