"""Read/write canonical JSON on disk (one flag set, replacing the duplicate pair)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_json(path: str | Path, default: Any = None) -> Any:
    path = Path(path)
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: str | Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8"
    )
