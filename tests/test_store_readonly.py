"""Turn 0a: the read-only store -- a query layer opens a store it did not build.

A read-only open must be *invisible*: the bytes of every file it reads stay what they were,
and no file's mtime moves, because a reader that rewrites (or merely re-creates a directory
it found already there) makes "the store is what the run wrote" untestable. The failure
mode it exists to prevent is the opposite one: a store path that is a typo reading as an
empty, valid-looking store, so a read-only open of a root that is not there raises instead
of creating it.

The state snapshot is ``tests/test_store.py``'s -- ``(bytes, mtime_ns)`` per file -- because
"nothing was touched" is exactly that pair and not "the JSON still compares equal".
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from docextract_core import ReadOnlyError

from wordextract.store import Store, ingest
from wordextract.terms import load_registry_text, registry_hashes

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
DOCUMENT = FIXTURES / "program_review_v3.docx"
OTHER_DOCUMENT = FIXTURES / "edge_cases.docx"
EXAMPLE_REGISTRY = FIXTURES / "terms" / "synthetic.example.json"


def _registry():
    return load_registry_text(EXAMPLE_REGISTRY.read_text(encoding="utf-8"))


def _state(root: Path) -> dict[str, tuple[bytes, int]]:
    """Every file under ``root`` by POSIX relative path, as ``(bytes, mtime_ns)``."""
    return {
        p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _populated(tmp_path: Path) -> Store:
    store = Store(tmp_path / "store")
    ingest(store, DOCUMENT, registry=_registry())
    return store


def test_opening_a_populated_store_read_only_changes_no_byte_and_no_mtime(tmp_path):
    """Every artifact is readable through the read-only handle, and nothing moved.

    The reads are the ones a query layer makes: the run log, the archive index, the term
    lists and the keyed artifacts. A read-only ``Collection`` still reads *by* the index --
    the lookup is a lookup, not a directory scan -- so the index has to be read, and read
    only.
    """
    store = _populated(tmp_path)
    before = _state(store.root)

    read = Store(store.root, read_only=True)
    assert read.read_only and read.parse.read_only and read.terms.read_only
    assert read.run_ids() == store.run_ids()
    assert read.raw_index() == store.raw_index()
    assert registry_hashes(read.terms) == registry_hashes(store.terms)
    assert read.parse.list() == store.parse.list()
    assert read.chunks.list() == store.chunks.list()
    assert read.hits.list() == store.hits.list()
    assert read.load_run(read.run_ids()[0]) == store.load_run(store.run_ids()[0])

    assert _state(store.root) == before


def test_a_read_only_store_refuses_to_write_and_says_so(tmp_path):
    """The mode is not a hint: the mutating surface raises rather than no-op.

    Including ``evict``, which is the one write that is also a *repair* (an unreadable
    record is evicted so the next run rewrites it) -- repairing is a writer's job, so a
    read-only caller gets the error instead of a half-repaired store.
    """
    store = _populated(tmp_path)
    record = store.load_run(store.run_ids()[0])
    before = _state(store.root)

    read = Store(store.root, read_only=True)
    with pytest.raises(ReadOnlyError):
        read.parse.evict(record.hashed_inputs.source_content_hash)
    with pytest.raises(ReadOnlyError):
        read.runs.save(record)
    with pytest.raises(ReadOnlyError):
        read.record_run(record)
    with pytest.raises(ReadOnlyError):
        read.terms.save(_registry())

    assert _state(store.root) == before


def test_a_read_only_open_of_a_missing_root_raises_and_creates_nothing(tmp_path):
    """A store that is not there is not an empty store: it is a mistake."""
    root = tmp_path / "nothing" / "here"
    with pytest.raises(ReadOnlyError, match="read-only"):
        Store(root, read_only=True)
    assert not root.exists()
    assert not (tmp_path / "nothing").exists()


def test_a_read_only_open_names_the_collection_directory_that_is_missing(tmp_path):
    """Half a store is named as such -- the collection that is missing is the error."""
    root = tmp_path / "half"
    (root / "parse").mkdir(parents=True)
    with pytest.raises(ReadOnlyError, match="chunks"):
        Store(root, read_only=True)


def test_a_read_only_open_sees_every_document_and_run_the_store_has(tmp_path):
    """The read handle is not tied to one run: every document's artifacts are addressed."""
    store = _populated(tmp_path)
    ingest(store, OTHER_DOCUMENT, registry=_registry(), run_id="second")
    read = Store(store.root, read_only=True)
    assert read.run_ids() == store.run_ids()
    assert len(read.run_ids()) == 2
    assert read.load_run("second") == store.load_run("second")
    assert len(read.parse.list()) == len(store.parse.list()) == 2
    assert read.terms.list() == store.terms.list()


def test_a_read_only_store_without_the_turn_4_directories_reads_empty_and_still_refuses_to_write(
    tmp_path,
):
    """``rollups/`` and ``rollup_rejections/`` are new with Turn 4: a pre-rollup store does
    not have them, and "no roll-ups yet" is an empty read, not a broken store -- so they are
    the one known exception to the eager check (``allow_missing=True``). Everything else is
    unchanged: the store still opens only because every collection it always had is there,
    and the missing pair still refuses to be written through."""
    store = _populated(tmp_path)
    shutil.rmtree(store.root / "rollups")
    shutil.rmtree(store.root / "rollup_rejections")

    read = Store(store.root, read_only=True)
    assert read.rollups.list() == [] and read.rollup_rejections.list() == []
    assert read.run_ids() == store.run_ids()
    assert read.parse.list() == store.parse.list()

    with pytest.raises(ReadOnlyError, match="read-only"):
        read.rollups.save(object())
    with pytest.raises(ReadOnlyError, match="read-only"):
        read.rollup_rejections.evict("a-key")
    # the refused writes did not quietly create what was missing
    assert not (store.root / "rollups").exists()
    assert not (store.root / "rollup_rejections").exists()
