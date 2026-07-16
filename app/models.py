from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ServiceResult:
    ok: bool
    data: Any
    message: str = ""

