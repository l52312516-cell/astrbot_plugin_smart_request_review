from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class JsonStore:
    """Small atomic JSON store for plugin-owned state."""

    def __init__(self, path: Path, default: Any):
        self.path = path
        self.default = default

    def load(self) -> Any:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value
        except (OSError, ValueError, TypeError):
            return self.default.copy() if isinstance(self.default, dict) else self.default

    def save(self, value: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

