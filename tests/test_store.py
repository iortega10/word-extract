"""Turn 7: the content-addressed store, the run record, and idempotent re-ingest.

Every claim here is a *behavioral* one about the store, not a fixture fact: an artifact is
named after the key its own inputs recompute to, a second ingest of the same bytes rewrites
nothing, and a hit survives the ``.docx`` being deleted. The document and term list are the
shipped fixtures (``program_review_v3.docx``: three hits, ``named partner`` once and
``termination`` twice); the second document is ``edge_cases.docx``.

Nothing here hand-builds a registry with a *different* hash to make a key move by hand:
``term_list_hash`` is derived from the term list, so a new term list must be a new term list.
The other inputs are varied one field at a time (``dataclasses.replace``), which is the only
way to say "this key reads this input and no other" -- and the flip table is asserted to
cover every ``HashedInputs`` field, so a field added without a key decision fails here.

Views, not registries, are what proves the invalidation is end-to-end: ``View.ORIGINAL``
re-uses the parse (the walk does not depend on the view) and re-chunks (the bytes of a
chunk, and therefore its id, do).
"""
from __future__ import annotations

import dataclasses
import platform
from pathlib import Path

import pytest

from docextract_core import CORE_VERSION, content_hash, from_json, to_json

from wordextract import opc
from wordextract.chunker import DEFAULT_PARAMS, ChunkParams, chunk, params_hash
from wordextract.model import ParseResult, RunRecord, TermGroup, View
from wordextract.store import (
    ARTIFACT_KINDS,
    Store,
    artifact_keys,
    body_part_id,
    hashed_inputs,
    ingest,
    recorded_inputs,
)
from wordextract.terms import (
    TermRegistry,
    compile_registry,
    load_registry,
    load_registry_text,
    match_document,
    registry_hashes,
    term_list_hash,
)
from wordextract.versions import (
    MATCHER_VERSION,
    OUTPUT_SCHEMA_VERSION,
    HashedInputs,
)
from wordextract.walker import walk_document

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
DOCUMENT = FIXTURES / "program_review_v3.docx"
OTHER_DOCUMENT = FIXTURES / "edge_cases.docx"
EXAMPLE_REGISTRY = FIXTURES / "terms" / "synthetic.example.json"

#: The fixture document's hits: `named partner` once, `termination` twice (a move's two
#: locations, so the count is the matcher's and not a query's).
FIXTURE_HITS = 3


def _registry() -> TermRegistry:
    return load_registry_text(EXAMPLE_REGISTRY.read_text(encoding="utf-8"))


def _state(root: Path, *, exclude: tuple[str, ...] = ()) -> dict[str, tuple[bytes, int]]:
    """Every file under ``root`` by POSIX relative path, as ``(bytes, mtime_ns)``.

    ``exclude`` names a directory or file to leave out: ``runs`` for every artifact claim
    (a run record is written every time and named by a fresh run id), and
    ``raw/index.json`` for the byte-level replay across two stores (its ``ingested_at`` is
    core's one wall-clock field, and two stores archived at different seconds differ there
    and nowhere else).
    """
    return {
        p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
        and not any(
            p.relative_to(root).as_posix() == name
            or p.relative_to(root).as_posix().startswith(name + "/")
            for name in exclude
        )
    }


def _bytes(root: Path, *, exclude: tuple[str, ...] = ()) -> dict[str, bytes]:
    """``_state``'s bytes alone: content, which two stores must agree on byte for byte.

    Two stores on disk differ in every mtime by construction, so a replay across stores is
    a claim about bytes only -- the mtime half of ``_state`` is for the no-op re-ingest,
    where the same file is asked not to have been touched.
    """
    return {path: data for path, (data, _mtime) in _state(root, exclude=exclude).items()}


def _statuses(record: RunRecord) -> dict[str, str]:
    return {a.artifact_id: a.status.value for a in record.artifact_cache}


def _keys(record: RunRecord) -> dict[str, str]:
    return {a.artifact_id: a.recomputed_key for a in record.artifact_cache}


def _key(record: RunRecord, kind: str) -> str:
    return _keys(record)[kind]


def _fresh(tmp_path: Path) -> Store:
    return Store(tmp_path / "store")


# --- a fresh run, and an unchanged one ------------------------------------------------


def test_a_fresh_run_misses_every_artifact_and_stores_each_under_its_own_key(tmp_path):
    store = _fresh(tmp_path)
    registry = _registry()
    record = ingest(store, DOCUMENT, registry=registry)

    assert [a.artifact_id for a in record.artifact_cache] == list(ARTIFACT_KINDS)
    assert _statuses(record) == {kind: "miss" for kind in ARTIFACT_KINDS}
    assert set(artifact_keys(record.hashed_inputs)) == set(ARTIFACT_KINDS)
    assert _key(record, "raw") == record.hashed_inputs.source_content_hash == content_hash(DOCUMENT)
    assert _key(record, "terms") == record.hashed_inputs.term_list_hash == term_list_hash(registry)

    # each artifact is its own key's file -- the key is a field because the file is named
    # after it, so a record reached by its recomputed key cannot be another record.
    for kind, collection in (
        ("parse", store.parse),
        ("chunks", store.chunks),
        ("hits", store.hits),
    ):
        stored = collection.find(_key(record, kind))
        assert stored is not None
        assert stored.key == _key(record, kind)

    assert load_registry(store.terms, record.hashed_inputs.term_list_hash) == registry
    assert registry_hashes(store.terms) == [record.hashed_inputs.term_list_hash]
    archived = store.root / "raw" / f"{record.hashed_inputs.source_content_hash}.docx"
    assert archived.read_bytes() == DOCUMENT.read_bytes()
    assert store.load_run(record.run_id) == record


def test_the_stored_artifacts_are_what_a_live_run_derives(tmp_path):
    store = _fresh(tmp_path)
    registry = _registry()
    record = ingest(store, DOCUMENT, registry=registry)

    parsed = store.parse.find(_key(record, "parse")).parsed
    assert parsed == walk_document(opc.Package(DOCUMENT))
    assert body_part_id(parsed) == "officeDocument:0"
    assert store.chunks.find(_key(record, "chunks")).chunks == chunk(
        parsed, body_part_id(parsed), view=View.ACCEPTED, params=DEFAULT_PARAMS
    )
    hits = store.hits.find(_key(record, "hits")).hits
    assert hits == match_document(compile_registry(registry), parsed)
    assert len(hits) == FIXTURE_HITS

    # the store holds the matcher's record, not a query's view of it: hits are stored
    # unfolded, so every hit the matcher reported is still there.
    assert [hit.group for hit in hits] == ["named partner", "termination", "termination"]


def test_a_no_op_re_ingest_hits_everything_and_writes_only_a_run_record(tmp_path):
    store = _fresh(tmp_path)
    registry = _registry()
    first = ingest(store, DOCUMENT, registry=registry)
    before = _state(store.root)

    second = ingest(store, DOCUMENT, registry=registry)
    after = _state(store.root)

    assert _statuses(second) == {kind: "hit" for kind in ARTIFACT_KINDS}
    assert _keys(second) == _keys(first)
    assert second.run_id != first.run_id

    # not "equal bytes": the same bytes at the same mtime. A decode/re-encode round trip
    # would show up here as a touched file even when the value came back equal.
    assert set(after) - set(before) == {f"runs/{second.run_id}.json"}
    assert not set(before) - set(after)
    assert all(after[path] == before[path] for path in before)

    assert store.run_ids() == sorted([first.run_id, second.run_id])
    assert store.load_run(second.run_id) == second


def test_replaying_the_same_document_into_a_second_store_is_byte_identical(tmp_path):
    first_store, second_store = _fresh(tmp_path), Store(tmp_path / "other")
    registry = _registry()

    first = ingest(first_store, DOCUMENT, registry=registry)
    ingest(first_store, DOCUMENT, registry=registry, view=View.ORIGINAL)
    other = ingest(second_store, DOCUMENT, registry=registry)
    ingest(second_store, DOCUMENT, registry=registry, view=View.ORIGINAL)

    artifacts = ("runs", "raw/index.json")
    assert _bytes(first_store.root, exclude=artifacts) == _bytes(
        second_store.root, exclude=artifacts
    )

    # the excluded run log is still the same run, minus the id it was named by
    assert first.hashed_inputs == other.hashed_inputs
    assert _statuses(first) == _statuses(other)
    assert _keys(first) == _keys(other)


# --- one input, one set of keys -------------------------------------------------------


#: Which artifacts' keys move when one hashed input changes. An input that moves no key is
#: one nothing hashed, and an input that moves a key whose artifact cannot read it would
#: invalidate that artifact for nothing. Every ``HashedInputs`` field is here exactly once.
KEY_MOVES: dict[str, tuple[str, ...]] = {
    "source_content_hash": ("raw", "parse", "chunks", "hits"),
    # chunks and hits are derived from the stored parse, so anything that moves the parse
    # moves them (the heading rules reach hits through the node ids, which embed the kind)
    "spec_parser_version": ("parse", "chunks", "hits"),
    "textmodel_version": ("parse", "chunks", "hits"),
    "view_id": ("chunks", "hits"),
    "heading_ruleset_version": ("parse", "chunks", "hits"),
    "chunker_version": ("chunks",),
    "chunker_params_hash": ("chunks",),
    "term_list_hash": ("hits", "terms"),
    "matcher_version": ("hits",),
    # Phase 1 produces no summary, so nothing keys on the summary group yet.
    "summarizer_version": (),
    "model_id": (),
    "model_params_hash": (),
    "prompt_hash": (),
    "output_schema_version": (),
}


def _flipped(inputs: HashedInputs, field: str) -> HashedInputs:
    """``inputs`` with one field changed to a value that is plainly not the old one."""
    values = {
        "source_content_hash": "0" * 64,
        "view_id": View.SUPERSEDED.value,
        "chunker_params_hash": params_hash(ChunkParams(size_cap=1)),
        "output_schema_version": "flip",
    }
    return dataclasses.replace(inputs, **{field: values.get(field, "flip")})


def test_the_key_flip_table_covers_every_hashed_input():
    assert set(KEY_MOVES) == {f.name for f in dataclasses.fields(HashedInputs)}


@pytest.mark.parametrize("field,moved", sorted(KEY_MOVES.items()))
def test_one_changed_hashed_input_moves_exactly_the_keys_that_read_it(field, moved):
    inputs = hashed_inputs(
        content_hash(DOCUMENT),
        view=View.ACCEPTED,
        params=DEFAULT_PARAMS,
        term_list=term_list_hash(_registry()),
    )
    flipped = _flipped(inputs, field)

    assert getattr(flipped, field) != getattr(inputs, field)
    base = artifact_keys(inputs)
    assert tuple(
        kind for kind in ARTIFACT_KINDS if artifact_keys(flipped)[kind] != base[kind]
    ) == moved


def test_hashed_inputs_leaves_the_summary_group_empty_and_the_versions_real():
    inputs = hashed_inputs(
        content_hash(DOCUMENT),
        view=View.ACCEPTED,
        params=DEFAULT_PARAMS,
        term_list="0" * 64,
    )

    assert (
        inputs.summarizer_version,
        inputs.model_id,
        inputs.model_params_hash,
        inputs.prompt_hash,
    ) == ("", "", "", "")
    assert inputs.output_schema_version == OUTPUT_SCHEMA_VERSION
    assert inputs.view_id == View.ACCEPTED.value
    assert inputs.chunker_params_hash == params_hash(DEFAULT_PARAMS)
    assert inputs.matcher_version == MATCHER_VERSION
    assert inputs.term_list_hash == "0" * 64


def test_recorded_inputs_are_written_down_and_hash_into_nothing(tmp_path):
    store = _fresh(tmp_path)
    registry = _registry()
    first = ingest(store, DOCUMENT, registry=registry, recorded={"host": "one"})
    before = _state(store.root, exclude=("runs",))

    second = ingest(
        store, DOCUMENT, registry=registry, recorded={"host": "two", "operator": "ivan"}
    )

    assert _statuses(second) == {kind: "hit" for kind in ARTIFACT_KINDS}
    assert _keys(second) == _keys(first)
    assert second.hashed_inputs == first.hashed_inputs
    assert _state(store.root, exclude=("runs",)) == before

    assert first.recorded_inputs["host"] == "one"
    assert second.recorded_inputs["host"] == "two"
    assert second.recorded_inputs["operator"] == "ivan"
    assert second.recorded_inputs == recorded_inputs({"host": "two", "operator": "ivan"})
    assert second.recorded_inputs["core_version"] == CORE_VERSION
    assert second.recorded_inputs["python_version"] == platform.python_version()
    for ambient in (
        "core_git_rev",
        "package_git_rev",
        "lxml_version",
        "core_distribution_version",
    ):
        assert ambient in second.recorded_inputs


# --- what a key change means end to end -----------------------------------------------


def test_a_second_view_re_uses_the_parse_and_re_chunks_and_re_matches(tmp_path):
    store = _fresh(tmp_path)
    registry = _registry()
    accepted = ingest(store, DOCUMENT, registry=registry)
    accepted_chunks = store.chunks.find(_key(accepted, "chunks")).chunks
    accepted_hits = store.hits.find(_key(accepted, "hits")).hits

    original = ingest(store, DOCUMENT, registry=registry, view=View.ORIGINAL)

    assert _statuses(original) == {
        "raw": "hit",
        "parse": "hit",
        "chunks": "miss",
        "hits": "miss",
        "terms": "hit",
    }
    # the walk does not consult a view, so the parse is the same artifact
    assert _key(original, "parse") == _key(accepted, "parse")
    # the view's bytes are what the chunker cut, so the chunks are not
    assert _key(original, "chunks") != _key(accepted, "chunks")
    original_chunks = store.chunks.find(_key(original, "chunks")).chunks
    assert original_chunks != accepted_chunks

    # the hits artifact's key carries the view; the record it holds does not, because a
    # hit is stored for every view it is in (D6) and the view's id is one input too many.
    assert _key(original, "hits") != _key(accepted, "hits")
    assert store.hits.find(_key(original, "hits")).hits == accepted_hits

    # both views' chunks re-derive offline from the one stored parse
    parsed = store.parse.find(_key(accepted, "parse")).parsed
    assert chunk(parsed, body_part_id(parsed), view=View.ORIGINAL, params=DEFAULT_PARAMS) == (
        original_chunks
    )


def test_a_new_term_list_re_parses_and_re_chunks_nothing(tmp_path):
    store = _fresh(tmp_path)
    registry = _registry()
    first = ingest(store, DOCUMENT, registry=registry)
    before = _state(store.root)

    wider = TermRegistry(groups=[*registry.groups, TermGroup(canonical="chargeback")])
    second = ingest(store, DOCUMENT, registry=wider)
    after = _state(store.root)

    assert _statuses(second) == {
        "raw": "hit",
        "parse": "hit",
        "chunks": "hit",
        "hits": "miss",
        "terms": "miss",
    }
    assert _key(second, "terms") == second.hashed_inputs.term_list_hash
    assert _key(second, "terms") != _key(first, "terms")
    assert _key(second, "hits") != _key(first, "hits")
    for kind in ("raw", "parse", "chunks"):
        assert _key(second, kind) == _key(first, kind)

    # the term list is one new file, the hits are one new file, and every artifact of the
    # first run is untouched -- the two index.json files that gained a mapping aside.
    assert all(
        after[path] == before[path]
        for path in before
        if not path.startswith(("hits/", "terms/"))
    )
    assert set(after) - set(before) == {
        f"runs/{second.run_id}.json",
        f"hits/{_key(second, 'hits')}.json",
        f"terms/{second.hashed_inputs.term_list_hash}.json",
    }
    assert registry_hashes(store.terms) == sorted(
        [_key(first, "terms"), second.hashed_inputs.term_list_hash]
    )


def test_a_different_document_shares_no_artifact_but_the_term_list(tmp_path):
    store = _fresh(tmp_path)
    registry = _registry()
    one = ingest(store, DOCUMENT, registry=registry)
    two = ingest(store, OTHER_DOCUMENT, registry=registry)

    for kind in ("raw", "parse", "chunks", "hits"):
        assert _key(two, kind) != _key(one, kind)
    # the term list is not derived from the document, so it is the same one file
    assert _key(two, "terms") == _key(one, "terms")
    assert registry_hashes(store.terms) == [_key(one, "terms")]

    parsed = store.parse.find(_key(two, "parse")).parsed
    assert parsed == walk_document(opc.Package(OTHER_DOCUMENT))
    assert store.chunks.find(_key(two, "chunks")).chunks == chunk(
        parsed, body_part_id(parsed), view=View.ACCEPTED, params=DEFAULT_PARAMS
    )


# --- offline reproduction -------------------------------------------------------------


def test_a_hit_is_reproducible_with_the_docx_gone(tmp_path):
    source = tmp_path / "pinned.docx"
    source.write_bytes(DOCUMENT.read_bytes())
    store = _fresh(tmp_path)
    record = ingest(store, source, registry=_registry())

    source.unlink()

    parsed = store.parse.find(_key(record, "parse")).parsed
    registry = load_registry(store.terms, record.hashed_inputs.term_list_hash)
    assert registry is not None
    assert store.chunks.find(_key(record, "chunks")).chunks == chunk(
        parsed, body_part_id(parsed), view=View.ACCEPTED, params=DEFAULT_PARAMS
    )
    assert store.hits.find(_key(record, "hits")).hits == match_document(
        compile_registry(registry), parsed
    )
    # the source bytes survive in the archive, under the hash the run was keyed by, so the
    # same document from anywhere still misses on nothing
    archived = store.root / "raw" / f"{record.hashed_inputs.source_content_hash}.docx"
    assert archived.read_bytes() == DOCUMENT.read_bytes()

    moved = tmp_path / "moved.docx"
    moved.write_bytes(DOCUMENT.read_bytes())
    again = ingest(store, moved, registry=_registry())
    assert _statuses(again) == {kind: "hit" for kind in ARTIFACT_KINDS}


def test_the_body_is_the_union_stream_the_chunks_are_cut_from(tmp_path):
    assert body_part_id(walk_document(opc.Package(DOCUMENT))) == "officeDocument:0"
    assert body_part_id(ParseResult(union_streams=[], nodes=[], comments=[], revisions=[])) == ""


# --- the run log ----------------------------------------------------------------------


def test_the_run_record_round_trips_through_the_codec_and_the_run_log(tmp_path):
    store = _fresh(tmp_path)
    record = ingest(store, DOCUMENT, registry=_registry(), run_id="0" * 32)

    assert from_json(RunRecord, to_json(record)) == record
    assert store.load_run(record.run_id) == record
    assert store.run_ids() == [record.run_id]
    assert store.load_run("f" * 32) is None

    # the log is not a cache: re-recording a run overwrites that run, never a second one
    assert store.record_run(record) == record
    assert store.run_ids() == [record.run_id]
