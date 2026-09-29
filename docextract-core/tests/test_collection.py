"""Collection[T]: idempotent keyed save, load, find, list."""
from __future__ import annotations

from dataclasses import dataclass

from docextract_core.collection import Collection


@dataclass
class Rec:
    rec_id: str
    key: str
    payload: str = ""


def _collection(tmp_path) -> Collection[Rec]:
    return Collection(tmp_path / "records", Rec, id_of=lambda r: r.rec_id, key_of=lambda r: r.key)


def test_save_load_find_roundtrip(tmp_path):
    coll = _collection(tmp_path)
    rec = Rec("r1", "k1", "hello")

    saved, created = coll.save(rec)
    assert created is True
    assert saved == rec
    assert coll.load("r1") == rec
    assert coll.find("k1") == rec
    assert coll.list() == ["r1"]


def test_save_is_idempotent_on_key_collision(tmp_path):
    coll = _collection(tmp_path)
    coll.save(Rec("r1", "k1"))

    saved, created = coll.save(Rec("r2", "k1"))
    assert created is False
    assert saved.rec_id == "r1"
    assert not (coll.root / "r2.json").exists()


def test_load_missing_returns_none(tmp_path):
    coll = _collection(tmp_path)
    assert coll.load("nope") is None
    assert coll.find("nope") is None


def test_without_key_of_save_always_writes(tmp_path):
    coll = Collection(tmp_path / "plain", Rec, id_of=lambda r: r.rec_id)
    assert coll.save(Rec("r1", "k1"))[1] is True
    assert coll.save(Rec("r1", "k1"))[1] is True
