"""Codec: round-trips, strict/lenient decoding, and the schema-version envelope."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum

import pytest

from docextract_core.codec import SCHEMA_VERSION, CodecError, from_json, to_json


class Kind(str, Enum):
    A = "a"
    B = "b"


@dataclass
class Inner:
    n: int
    tags: list[str] = field(default_factory=list)


@dataclass
class Outer:
    name: str
    kind: Kind
    inner: Inner
    maybe: str | None = None
    items: list[Inner] = field(default_factory=list)
    opt_inner: Inner | None = None
    flag: bool = False
    kinds: set[Kind] = field(default_factory=set)


def _sample() -> Outer:
    return Outer(
        name="x",
        kind=Kind.B,
        inner=Inner(1, ["t1", "t2"]),
        maybe=None,
        items=[Inner(2), Inner(3, ["z"])],
        opt_inner=Inner(9),
        flag=True,
        kinds={Kind.A, Kind.B},
    )


def test_round_trip_nested_dataclasses_enums_optionals_lists_sets():
    src = _sample()
    assert from_json(Outer, to_json(src)) == src


def test_envelope_carries_schema_version_and_record():
    blob = json.loads(to_json(_sample()))
    assert blob["schema_version"] == SCHEMA_VERSION
    assert set(blob) == {"schema_version", "record"}


def test_strict_is_the_default_and_raises_on_extra_key():
    data = json.loads(to_json(_sample()))
    data["record"]["surprise"] = 1
    with pytest.raises(CodecError):
        from_json(Outer, json.dumps(data))


def test_strict_recurses_into_nested_records():
    data = json.loads(to_json(_sample()))
    data["record"]["inner"]["surprise"] = 1
    with pytest.raises(CodecError):
        from_json(Outer, json.dumps(data))


def test_lenient_mode_ignores_extra_key():
    data = json.loads(to_json(_sample()))
    data["record"]["surprise"] = 1
    assert from_json(Outer, json.dumps(data), strict=False) == _sample()


def test_newer_schema_version_raises():
    blob = to_json(_sample(), schema_version="2")
    with pytest.raises(CodecError):
        from_json(Outer, blob)


def test_same_or_older_schema_version_is_accepted():
    assert from_json(Outer, to_json(_sample(), schema_version="1")) == _sample()


def test_missing_envelope_raises():
    with pytest.raises(CodecError):
        from_json(Outer, json.dumps({"name": "x"}))


def test_non_object_payload_raises():
    with pytest.raises(CodecError):
        from_json(Outer, json.dumps({"schema_version": "1", "record": ["not", "an", "object"]}))
