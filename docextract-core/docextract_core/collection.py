"""Generic keyed record collection. **Provisional.**

Generalized from a single consumer's three methods (form-extract store.py:88-137:
``find_instance`` / ``save_instance`` / ``load_instance``). Having one consumer
means the surface is a guess: it may change when a second arrives. Hashing,
codec and archive behavior are frozen; this class is not.

``read_only`` is the one mode a *reader* needs and a writer must never get by
accident: a query layer opens a store it did not build, so opening it must not
create or touch anything. A read-only collection's directory has to be there
already (that is what "this is a store" means) and every mutating method raises
:class:`ReadOnlyError` instead of writing.

``allow_missing`` relaxes exactly that one check, for a *known-new* collection
inside a store that predates it: the store is real, this directory just does not
exist yet, and an empty read is the honest answer (Phase 2, Turn 4 -- ``rollups/``
in a pre-rollup store). It is consulted only when ``read_only`` is set, and never
widened to the collections the store was originally validated by: a store opened
read-only still refuses to open when a collection it always had is missing.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Generic, TypeVar

from .codec import from_json, to_json
from .jsonio import read_json, write_json

T = TypeVar("T")

_INDEX = "index.json"


class ReadOnlyError(RuntimeError):
    """A write was asked of a collection (or store) opened read-only."""


class Collection(Generic[T]):
    def __init__(
        self,
        root: str | Path,
        cls: type[T],
        *,
        id_of: Callable[[T], str],
        key_of: Callable[[T], str] | None = None,
        read_only: bool = False,
        allow_missing: bool = False,
    ) -> None:
        self.root = Path(root)
        self.cls = cls
        self._id_of = id_of
        self._key_of = key_of
        self.read_only = read_only
        if read_only:
            if not self.root.is_dir() and not allow_missing:
                raise ReadOnlyError(
                    f"read-only collection has no directory: {self.root} -- a read-only "
                    f"open never creates one"
                )
        else:
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
        if self.read_only:
            raise ReadOnlyError(f"{self.root} is read-only: refusing to save")
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

    def evict(self, key: str) -> bool:
        """Remove the record stored under ``key`` and its index entry; True when anything was.

        For a record that can no longer be read -- written by an older schema, say -- so the
        next ``save`` writes a fresh one instead of tripping over the stale file. A key the
        collection does not know is not an error.
        """
        if self.read_only:
            raise ReadOnlyError(f"{self.root} is read-only: refusing to evict")
        index = read_json(self._index_path, {})
        record_id = index.pop(key, None)
        removed = record_id is not None
        if removed:
            write_json(self._index_path, index)
        for name in {record_id, key} - {None}:
            path = self._path(name)
            if path.exists():
                path.unlink()
                removed = True
        return removed

    def load(self, record_id: str) -> T | None:
        path = self._path(record_id)
        if not path.exists():
            return None
        return from_json(self.cls, path.read_text(encoding="utf-8"))

    def list(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.json") if p.name != _INDEX)
