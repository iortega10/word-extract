"""Generic keyed record collection. **Provisional.**

Generalized from a single consumer's three methods (form-extract store.py:88-137:
``find_instance`` / ``save_instance`` / ``load_instance``). Having one consumer
means the surface is a guess: it may change when a second arrives. Hashing,
codec and archive behavior are frozen; this class is not.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Generic, TypeVar

from .codec import from_json, to_json
from .jsonio import read_json, write_json

T = TypeVar("T")

_INDEX = "index.json"


class Collection(Generic[T]):
    def __init__(
        self,
        root: str | Path,
        cls: type[T],
        *,
        id_of: Callable[[T], str],
        key_of: Callable[[T], str] | None = None,
    ) -> None:
        self.root = Path(root)
        self.cls = cls
        self._id_of = id_of
        self._key_of = key_of
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def _index_path(self) -> Path:
        return self.root / _INDEX

    def _path(self, record_id: str) -> Path:
        return self.root / f"{record_id}.json"

    def find(self, key: str) -> T | None:
        """Look up by idempotency key; None when ``key_of`` is unset or key unknown."""
        record_id = read_json(self._index_path, {}).get(key)
        return self.load(record_id) if record_id else None

    def save(self, record: T) -> tuple[T, bool]:
        """Idempotent when ``key_of`` is set: an existing key returns (existing, False)."""
        key = self._key_of(record) if self._key_of else None
        if key is not None:
            existing = self.find(key)
            if existing is not None:
                return existing, False
        record_id = self._id_of(record)
        self._path(record_id).write_text(to_json(record), encoding="utf-8")
        if key is not None:
            index = read_json(self._index_path, {})
            index[key] = record_id
            write_json(self._index_path, index)
        return record, True

    def load(self, record_id: str) -> T | None:
        path = self._path(record_id)
        if not path.exists():
            return None
        return from_json(self.cls, path.read_text(encoding="utf-8"))

    def list(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.json") if p.name != _INDEX)
