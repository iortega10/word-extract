"""Archives are content-addressed and idempotent (same bytes, same path, no rewrite)."""
from __future__ import annotations

import json

from docextract_core.archive import archive_llm_call, archive_raw


def test_archive_raw_is_content_addressed_and_idempotent(tmp_path):
    src = tmp_path / "doc.docx"
    src.write_bytes(b"hello world")
    root = tmp_path / "store"

    r1 = archive_raw(root, src)
    r2 = archive_raw(root, src)

    assert r1.content_hash == r2.content_hash
    assert r1.storage_ref == r2.storage_ref
    assert (root / r1.storage_ref).read_bytes() == b"hello world"

    index = json.loads((root / "raw" / "index.json").read_text(encoding="utf-8"))
    assert len(index) == 1


def test_archive_raw_does_not_rewrite_an_existing_blob(tmp_path):
    src = tmp_path / "doc.docx"
    src.write_bytes(b"hello world")
    root = tmp_path / "store"

    record = archive_raw(root, src)
    dest = root / record.storage_ref
    dest.write_bytes(b"tampered")  # a second archive call must not restore it

    archive_raw(root, src)
    assert dest.read_bytes() == b"tampered"


def test_archive_llm_call_is_content_addressed_and_does_not_rewrite(tmp_path):
    root = tmp_path / "store"
    kw = dict(purpose="summary", model="m", params={"temperature": 0}, prompt="P", response="R")

    call1 = archive_llm_call(root, **kw, tokens=10, latency_ms=5)
    call2 = archive_llm_call(root, **kw)

    assert call1.prompt_hash == call2.prompt_hash
    assert call1.response_ref == call2.response_ref
    assert (root / call1.prompt_ref).read_text(encoding="utf-8") == "P"
    assert (root / call1.response_ref).read_text(encoding="utf-8") == "R"
    assert call1.tokens == 10

    (root / call1.prompt_ref).write_text("tampered", encoding="utf-8")
    archive_llm_call(root, **kw)
    assert (root / call1.prompt_ref).read_text(encoding="utf-8") == "tampered"
