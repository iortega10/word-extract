"""Content-addressed hashing primitives (sha256)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def content_hash(path: str | Path) -> str:
    """sha256 of a file's bytes, streamed in 1 MiB chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_json(obj) -> str:
    """sha256 of the canonical (sorted-key) JSON encoding of ``obj``."""
    payload = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return sha256_text(payload)
