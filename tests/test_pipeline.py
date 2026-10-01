"""Turn 9: the pipeline -- a document and a term list in, a stored run out.

The claims here are about the *seam*, not about the parser or the store, which have their
own tests: a term list is loaded from the canonical text a CLI can read (and hashes to the
same list as the in-memory one), a run's artifacts are all where the record says, and the
hits come back **from the store** -- a record plus a store root is enough, with the
``.docx`` deleted, which is the D8 closure in its smallest form.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from wordextract.model import View
from wordextract.pipeline import DEFAULT_STORE, load_terms, run, stored_hits
from wordextract.store import ARTIFACT_KINDS, Store
from wordextract.terms import load_registry_text, term_list_hash

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
DOCUMENT = FIXTURES / "program_review_v3.docx"
EXAMPLE_REGISTRY = FIXTURES / "terms" / "synthetic.example.json"

#: ``program_review_v3.docx`` against the synthetic registry: see ``tests/test_store.py``.
FIXTURE_HITS = 3


def test_a_term_list_is_loaded_from_the_text_the_cli_reads(tmp_path):
    registry = load_terms(EXAMPLE_REGISTRY)
    assert term_list_hash(registry) == term_list_hash(
        load_registry_text(EXAMPLE_REGISTRY.read_text(encoding="utf-8"))
    )
    assert [group.canonical for group in registry.groups]


def test_a_run_stores_every_artifact_under_the_key_the_record_recomputes(tmp_path):
    record = run(DOCUMENT, EXAMPLE_REGISTRY, store_root=tmp_path / "store")
    store = Store(tmp_path / "store")
    assert [a.artifact_id for a in record.artifact_cache] == list(ARTIFACT_KINDS)
    assert {a.status.value for a in record.artifact_cache} == {"miss"}
    for artifact in record.artifact_cache:
        if artifact.artifact_id in ("parse", "chunks", "hits", "terms"):
            collection = getattr(store, artifact.artifact_id)
            assert collection.find(artifact.recomputed_key) is not None


def test_a_second_run_hits_every_artifact_and_is_recorded_anyway(tmp_path):
    root = tmp_path / "store"
    run(DOCUMENT, EXAMPLE_REGISTRY, store_root=root, run_id="first")
    second = run(DOCUMENT, EXAMPLE_REGISTRY, store_root=root, run_id="second")
    assert {a.status.value for a in second.artifact_cache} == {"hit"}
    assert Store(root).run_ids() == ["first", "second"]


def test_the_view_reaches_the_chunks_and_the_hits_and_nothing_else(tmp_path):
    """A view is in the chunk key and the hit key (both are cut *in* a view) and in neither
    the parse nor the term list."""
    root = tmp_path / "store"
    accepted = run(DOCUMENT, EXAMPLE_REGISTRY, store_root=root)
    original = run(DOCUMENT, EXAMPLE_REGISTRY, store_root=root, view=View.ORIGINAL)
    accepted_keys = {a.artifact_id: a.recomputed_key for a in accepted.artifact_cache}
    original_keys = {a.artifact_id: a.recomputed_key for a in original.artifact_cache}
    assert accepted_keys["chunks"] != original_keys["chunks"]
    assert accepted_keys["hits"] != original_keys["hits"]
    for kind in ("raw", "parse", "terms"):
        assert accepted_keys[kind] == original_keys[kind]
    assert (
        {a.artifact_id: a.status.value for a in original.artifact_cache}
        == {kind: "hit" for kind in ("raw", "parse", "terms")} | {"chunks": "miss", "hits": "miss"}
    )


def test_hits_are_read_back_and_need_no_document(tmp_path):
    """The record's own key plus the store is the whole closure (D8)."""
    source = tmp_path / DOCUMENT.name
    source.write_bytes(DOCUMENT.read_bytes())
    root = tmp_path / "store"
    record = run(source, EXAMPLE_REGISTRY, store_root=root)
    source.unlink()

    hits = stored_hits(record, store_root=root)
    assert len(hits) == FIXTURE_HITS
    assert [hit.group for hit in hits].count("subrogation") == 2


def test_hits_are_the_stored_records_not_a_recomputation(tmp_path):
    root = tmp_path / "store"
    record = run(DOCUMENT, EXAMPLE_REGISTRY, store_root=root)
    key = {a.artifact_id: a.recomputed_key for a in record.artifact_cache}["hits"]
    assert stored_hits(record, store_root=root) == Store(root).hits.find(key).hits


def test_a_store_without_the_hits_is_an_error_not_a_recomputation(tmp_path):
    root = tmp_path / "store"
    record = run(DOCUMENT, EXAMPLE_REGISTRY, store_root=root)
    for path in (root / "hits").glob("*.json"):
        path.unlink()
    with pytest.raises(LookupError, match="holds no hits"):
        stored_hits(record, store_root=root)


def test_a_record_missing_an_artifact_kind_is_a_key_error(tmp_path):
    record = run(DOCUMENT, EXAMPLE_REGISTRY, store_root=tmp_path / "store")
    trimmed = type(record)(
        run_id=record.run_id,
        hashed_inputs=record.hashed_inputs,
        recorded_inputs=record.recorded_inputs,
        artifact_cache=[a for a in record.artifact_cache if a.artifact_id != "hits"],
    )
    with pytest.raises(KeyError, match="recorded no 'hits' artifact"):
        stored_hits(trimmed, store_root=tmp_path / "store")


def test_the_default_store_is_one_directory_per_project():
    assert DEFAULT_STORE == Path(".wordextract")
