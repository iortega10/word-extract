"""Contract test: replay form-extract's record shapes through Collection and the archives.

The seam is proven from form-extract's side: build a real InstanceRecord /
TemplateRecord / BatchRecord and push them through the core's generic
Collection and archives. Per the design (D9), form-extract's migration onto the
core is a separate PR, so if ``formextract`` is not importable here (a bare
clone of word-extract), skip rather than import across repos.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Sibling checkout (../form-extract) is picked up automatically when present.
_sibling = Path(__file__).resolve().parents[3] / "form-extract"
if (_sibling / "formextract").is_dir() and str(_sibling) not in sys.path:
    sys.path.insert(0, str(_sibling))

formextract_model = pytest.importorskip("formextract.model")
formextract_store = pytest.importorskip("formextract.store")

from docextract_core.archive import archive_llm_call, archive_raw  # noqa: E402
from docextract_core.collection import Collection  # noqa: E402


def _instance():
    return formextract_model.InstanceRecord(
        instance_id="inst1",
        schema_version="1",
        pipeline_version=formextract_model.PIPELINE_VERSION,
        run_id="run1",
        created_at="2024-01-01T00:00:00Z",
        source=formextract_model.SourceInfo(
            content_hash="abc", original_filename="a.pdf", mime="pdf", size=1
        ),
    )


def test_instance_record_replays_through_collection(tmp_path):
    coll = Collection(
        tmp_path / "instances",
        formextract_model.InstanceRecord,
        id_of=lambda r: r.instance_id,
        key_of=lambda r: r.idempotency_key,
    )
    saved, created = coll.save(_instance())
    assert created is True
    assert coll.find("abc:1").instance_id == "inst1"

    collision = _instance()
    collision.instance_id = "inst2"
    _, created2 = coll.save(collision)
    assert created2 is False
    assert coll.load("inst1") is not None


def test_template_and_batch_replay_through_collection(tmp_path):
    tpl = formextract_model.TemplateRecord(
        template_id="t1",
        family_id="f1",
        version=1,
        status=formextract_model.TemplateStatus.DRAFT,
        backends=["pdf_text"],
        schema_version="1",
        perception_version="1",
    )
    templates = Collection(
        tmp_path / "templates", formextract_model.TemplateRecord, id_of=lambda r: r.template_id
    )
    templates.save(tpl)
    assert templates.load("t1").template_id == "t1"

    batch = formextract_model.BatchRecord(batch_id="b1", members=["i1"], formed_at="now")
    batches = Collection(
        tmp_path / "batches", formextract_model.BatchRecord, id_of=lambda r: r.batch_id
    )
    batches.save(batch)
    assert batches.load("b1").members == ["i1"]


def test_form_extract_raw_and_llm_archives_replay_through_core(tmp_path):
    src = tmp_path / "a.pdf"
    src.write_bytes(b"%PDF-1.4")
    raw = archive_raw(tmp_path / "store", src, mime="pdf")
    assert raw.content_hash and (tmp_path / "store" / raw.storage_ref).exists()

    call = archive_llm_call(
        tmp_path / "store",
        purpose="cold_binding",
        model="m",
        params={"temperature": 0},
        prompt="PROMPT",
        response="RESPONSE",
    )
    assert (tmp_path / "store" / call.prompt_ref).read_text(encoding="utf-8") == "PROMPT"
