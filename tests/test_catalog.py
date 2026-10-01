"""Turn 0d: the document catalog -- what a store holds, read off what it already wrote.

The catalog's job is to be the *only* thing that has to enumerate a store: every claim here is
that a row is complete and that its keys are real.

* complete: one row per document, however many runs, views and term lists it was ingested
  with -- and every view and term list *any* run used is listed, not only the latest run's;
* real: each key in a row is looked up in the store (``find`` returns a record), so a row
  cannot name an artifact that was never written. The keys are compared against the run
  record's own ``recomputed_key``s, which is where the store's key discipline comes from;
* derived: the same call on a read-only store returns the same rows and moves no file's bytes
  or mtime, because nothing here is stored (Turn 0a's read-only mode is the query layer's).

The three half-known states are tested rather than assumed: a document whose bytes are
archived but whose walk never produced a run, a run whose bytes are no longer archived, and a
run record written under a schema this code cannot decode. Each is reported as what it is.

No fixture literal is read here and no run record is hand-built: every row comes from a real
``ingest`` of the shipped fixtures, so the catalog is tested against the store Phase 1 writes.
"""
from __future__ import annotations

from pathlib import Path

from docextract_core import archive_raw, content_hash

from wordextract.catalog import DocumentEntry, list_documents
from wordextract.chunker import ChunkParams
from wordextract.model import TermGroup, View
from wordextract.store import Store, ingest
from wordextract.terms import TermRegistry, load_registry_text, term_list_hash

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
DOCUMENT = FIXTURES / "program_review_v3.docx"
OTHER_DOCUMENT = FIXTURES / "edge_cases.docx"
EXAMPLE_REGISTRY = FIXTURES / "terms" / "synthetic.example.json"


def _registry() -> TermRegistry:
    return load_registry_text(EXAMPLE_REGISTRY.read_text(encoding="utf-8"))


def _other_registry() -> TermRegistry:
    """A second term list, distinct by construction: a term list is only ever its own hash."""
    return TermRegistry(groups=[TermGroup(canonical="loss ratio")])


def _keys(record) -> dict[str, str]:
    return {artifact.artifact_id: artifact.recomputed_key for artifact in record.artifact_cache}


def _state(root: Path) -> dict[str, tuple[bytes, int]]:
    """Every file under ``root`` by POSIX relative path, as ``(bytes, mtime_ns)``."""
    return {
        p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


# --- one row per document -------------------------------------------------------------


def test_an_empty_store_holds_no_document(tmp_path):
    """The empty case is the empty list, not an error and not a fictitious row."""
    assert list_documents(Store(tmp_path / "store")) == []


def test_a_document_is_one_row_named_by_its_content_hash_and_its_filename(tmp_path):
    store = Store(tmp_path / "store")
    ingest(store, DOCUMENT, registry=_registry(), run_id="only")

    (row,) = list_documents(store)
    assert row.document_id == content_hash(DOCUMENT)
    assert row.title == DOCUMENT.name
    assert row.latest_run_id == "only"


def test_a_document_ingested_twice_is_one_row_and_reports_the_latest_run(tmp_path):
    """Two runs, one document: the row is the document, and its run is the later one.

    ``first`` and ``second`` are also the run ids in order, so the answer does not rest on
    the mtimes alone (the run log carries no clock, so a tie is broken by run id).
    """
    root = tmp_path / "store"
    ingest(Store(root), DOCUMENT, registry=_registry(), run_id="first")
    ingest(Store(root), DOCUMENT, registry=_registry(), run_id="second")

    rows = list_documents(Store(root))
    assert [row.document_id for row in rows] == [content_hash(DOCUMENT)]
    assert rows[0].latest_run_id == "second"


def test_documents_are_sorted_by_title_and_each_row_is_its_own_document(tmp_path):
    store = Store(tmp_path / "store")
    ingest(store, DOCUMENT, registry=_registry())
    ingest(store, OTHER_DOCUMENT, registry=_registry())

    rows = list_documents(store)
    assert [row.title for row in rows] == [OTHER_DOCUMENT.name, DOCUMENT.name]
    hashes = [content_hash(OTHER_DOCUMENT), content_hash(DOCUMENT)]
    assert [row.document_id for row in rows] == hashes


# --- every view and every term list ----------------------------------------------------


def test_every_view_and_term_list_ingested_is_listed_with_its_stored_keys(tmp_path):
    """The row is the union over runs: a second view and a second term list both appear.

    The chunks key moves with the view and not with the term list, the hits key with both --
    which is exactly what the store's keys do, so the row can be read as the key shape.
    """
    store = Store(tmp_path / "store")
    accepted = ingest(store, DOCUMENT, registry=_registry(), run_id="accepted")
    original = ingest(
        store, DOCUMENT, registry=_registry(), view=View.ORIGINAL, run_id="original"
    )
    other_terms = ingest(store, DOCUMENT, registry=_other_registry(), run_id="other-terms")

    (row,) = list_documents(store)
    views = {view.view: view for view in row.views}
    assert list(views) == [View.ACCEPTED.value, View.ORIGINAL.value]

    assert views[View.ACCEPTED.value].chunks == _keys(accepted)["chunks"]
    assert views[View.ORIGINAL.value].chunks == _keys(original)["chunks"]
    assert views[View.ACCEPTED.value].chunks != views[View.ORIGINAL.value].chunks

    assert views[View.ACCEPTED.value].hits == {
        term_list_hash(_registry()): _keys(accepted)["hits"],
        term_list_hash(_other_registry()): _keys(other_terms)["hits"],
    }
    assert views[View.ORIGINAL.value].hits == {term_list_hash(_registry()): _keys(original)["hits"]}
    assert list(views[View.ACCEPTED.value].hits) == sorted(views[View.ACCEPTED.value].hits)


def test_re_chunking_a_view_moves_the_row_to_the_chunks_of_the_latest_run(tmp_path):
    """Two chunk records for one view: the row names the newer, and the older stays stored.

    Re-cutting a view (chunker params are an input of the chunks key, not of the hits key) is
    the one case where "which run is latest" decides a key in the row, so it is pinned here.
    """
    root = tmp_path / "store"
    ingest(Store(root), DOCUMENT, registry=_registry(), run_id="first")
    re_cut = ingest(
        Store(root),
        DOCUMENT,
        registry=_registry(),
        params=ChunkParams(size_cap=500),
        run_id="second",
    )

    (row,) = list_documents(Store(root))
    (view,) = row.views
    assert row.latest_run_id == "second"
    assert view.chunks == _keys(re_cut)["chunks"]
    assert len(Store(root).chunks.list()) == 2
    assert Store(root).chunks.find(view.chunks) is not None


def test_a_row_names_only_artifacts_the_store_actually_holds(tmp_path):
    """Every key in a row is looked up: a row cannot point at an artifact nobody wrote."""
    store = Store(tmp_path / "store")
    record = ingest(store, DOCUMENT, registry=_registry())

    (row,) = list_documents(store)
    (view,) = row.views
    assert view.chunks == _keys(record)["chunks"] == store.chunks.list()[0]
    (hits_key,) = view.hits.values()
    assert hits_key == _keys(record)["hits"] == store.hits.list()[0]
    assert store.chunks.find(view.chunks) is not None
    assert store.hits.find(hits_key) is not None
    assert term_list_hash(_registry()) in store.terms.list()


# --- derived, and honest about what is missing -----------------------------------------


def test_deriving_the_catalog_reads_a_read_only_store_and_writes_nothing(tmp_path):
    """The query layer's mode (Turn 0a): same rows, no file touched, no directory made."""
    store = Store(tmp_path / "store")
    ingest(store, DOCUMENT, registry=_registry())
    before = _state(store.root)

    read = Store(store.root, read_only=True)
    rows = list_documents(read)
    assert [row.title for row in rows] == [DOCUMENT.name]
    assert _state(store.root) == before
    assert list_documents(read) == rows


def test_a_document_with_no_run_is_listed_with_no_views_and_no_run_id(tmp_path):
    """Archived bytes and no run: a walk that failed leaves exactly this, and it is a row."""
    store = Store(tmp_path / "store")
    archive_raw(store.root, DOCUMENT)

    (row,) = list_documents(store)
    assert row == DocumentEntry(
        document_id=content_hash(DOCUMENT), title=DOCUMENT.name, latest_run_id="", views=[]
    )


def test_a_run_whose_bytes_are_no_longer_archived_reports_an_empty_title(tmp_path):
    """The run is evidence the document was ingested; the archive no longer says what it was
    called, and the row says it does not know rather than guessing a name."""
    store = Store(tmp_path / "store")
    ingest(store, DOCUMENT, registry=_registry(), run_id="only")
    (store.root / "raw" / "index.json").write_text("{}", encoding="utf-8")

    (row,) = list_documents(store)
    assert (row.title, row.latest_run_id) == ("", "only")
    assert row.views and row.views[0].chunks


def test_a_run_record_that_cannot_be_decoded_is_skipped_not_reported(tmp_path):
    """An unreadable run is no run: it is left out rather than reported with empty fields."""
    store = Store(tmp_path / "store")
    ingest(store, DOCUMENT, registry=_registry(), run_id="readable")
    (store.root / "runs" / "zz-unreadable.json").write_text(
        '{"schema_version": "1", "record": {}}', encoding="utf-8"
    )

    rows = list_documents(store)
    assert [row.latest_run_id for row in rows] == ["readable"]


# --- Turn 2: what no chunk of the document accounts for ----------------------------------


def test_a_document_whose_revisions_all_touch_chunks_reports_no_pending(tmp_path):
    """The row's pending fields are computed from the parse and chunk sets it holds.

    ``program_review_v3``'s two revisions both live in a chunk, so the answer is zero
    with an empty list -- an observed zero, never an uncomputed ``None``.
    """
    store = Store(tmp_path / "store")
    ingest(store, DOCUMENT, registry=_registry(), run_id="only")

    (row,) = list_documents(store)
    assert row.unattributed_revisions == 0
    assert row.unattributed_gaps == []


def test_pending_is_not_computed_when_a_row_has_no_parse_key(tmp_path):
    """Archived bytes and no run: no parse was ever read, so the fields stay ``None``.

    "Did not read" must not read as "no revisions" (rule 8).
    """
    store = Store(tmp_path / "store")
    archive_raw(store.root, DOCUMENT)

    (row,) = list_documents(store)
    assert row.unattributed_revisions is None
    assert row.unattributed_gaps is None
