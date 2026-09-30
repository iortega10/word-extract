"""Dataclass <-> JSON codec with a schema-versioned envelope and strict decoding.

Two deliberate departures from the form-extract reference (model.py:383-431):

- **Strict by default.** Unknown keys raise instead of being silently dropped,
  which is what let a record from a newer core read by an older one lose fields
  and produce a false cache hit (design Open risk #1).
- **Versioned envelope.** Every top-level blob carries ``schema_version``;
  decoding a blob newer than the reader supports, or older than
  ``MIN_SUPPORTED_VERSION``, raises.
"""
from __future__ import annotations

import json
import types
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any, TypeVar, Union, get_args, get_origin, get_type_hints

SCHEMA_VERSION = "3"
#: Oldest schema a persisted record may carry. Older blobs are rejected, not decoded
#: with new fields silently defaulted (design Open risk #1: version skew that
#: produces a wrong identity or a false cache hit).
MIN_SUPPORTED_VERSION = "3"

T = TypeVar("T")

_UNION_TYPES = (Union, types.UnionType)
_RECORD_KEY = "record"
_VERSION_KEY = "schema_version"


class CodecError(ValueError):
    """Raised on malformed blobs, unknown keys (strict mode), or version skew."""


def encode(obj: Any) -> Any:
    if isinstance(obj, Enum):
        return obj.value
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: encode(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, (list, tuple)):
        return [encode(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return sorted((encode(v) for v in obj), key=lambda v: json.dumps(v, sort_keys=True))
    if isinstance(obj, dict):
        return {k: encode(v) for k, v in obj.items()}
    return obj


def decode(tp: Any, val: Any, *, strict: bool = True) -> Any:
    if val is None:
        return None
    origin = get_origin(tp)
    if origin in _UNION_TYPES:
        args = [a for a in get_args(tp) if a is not type(None)]
        return decode(args[0], val, strict=strict) if args else val
    if origin is list:
        item_tp = get_args(tp)[0] if get_args(tp) else Any
        return [decode(item_tp, v, strict=strict) for v in val]
    if origin in (set, frozenset):
        item_tp = get_args(tp)[0] if get_args(tp) else Any
        return {decode(item_tp, v, strict=strict) for v in val}
    if origin is dict:
        key_tp, val_tp = get_args(tp) if get_args(tp) else (Any, Any)
        return {decode(key_tp, k, strict=strict): decode(val_tp, v, strict=strict) for k, v in val.items()}
    if isinstance(tp, type) and issubclass(tp, Enum):
        return tp(val)
    if isinstance(tp, type) and is_dataclass(tp):
        if not isinstance(val, dict):
            raise CodecError(f"expected an object for {tp.__name__}, got {type(val).__name__}")
        known = {f.name for f in fields(tp)}
        if strict:
            unknown = set(val) - known
            if unknown:
                raise CodecError(f"unknown keys for {tp.__name__}: {sorted(unknown)}")
        hints = get_type_hints(tp)
        return tp(**{k: decode(hints.get(k, Any), v, strict=strict) for k, v in val.items() if k in known})
    return val


def to_json(obj: Any, *, schema_version: str = SCHEMA_VERSION, indent: int | None = 2) -> str:
    blob = {_VERSION_KEY: schema_version, _RECORD_KEY: encode(obj)}
    return json.dumps(blob, indent=indent, sort_keys=True, ensure_ascii=False)


def from_json(
    cls: type[T],
    text: str,
    *,
    strict: bool = True,
    schema_version: str = SCHEMA_VERSION,
    min_version: str = MIN_SUPPORTED_VERSION,
) -> T:
    data = json.loads(text)
    if not isinstance(data, dict) or _VERSION_KEY not in data or _RECORD_KEY not in data:
        raise CodecError(f"blob is missing the {_VERSION_KEY}/{_RECORD_KEY} envelope")
    if _is_newer(data[_VERSION_KEY], schema_version):
        raise CodecError(
            f"blob schema_version {data[_VERSION_KEY]!r} is newer than supported {schema_version!r}"
        )
    if _is_newer(min_version, data[_VERSION_KEY]):
        raise CodecError(
            f"blob schema_version {data[_VERSION_KEY]!r} is older than the minimum supported {min_version!r}"
        )
    return decode(cls, data[_RECORD_KEY], strict=strict)


def _is_newer(blob_version: Any, supported_version: Any) -> bool:
    try:
        return _parse_version(blob_version) > _parse_version(supported_version)
    except (AttributeError, ValueError):
        return str(blob_version) > str(supported_version)


def _parse_version(version: Any) -> tuple[int, ...]:
    return tuple(int(part) for part in str(version).split("."))
