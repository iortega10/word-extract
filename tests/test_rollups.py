"""Turn 4: roll-ups -- one summary per section with content, one for the document.

The claims here are about **derivation**, not prose: every roll-up is keyed on the ordered
keys of the children it summarizes, the record restates what the children said rather than
what the model claimed to find, and the run record names only the *targets* while the
fan-in levels below them stay visible in ``SummaryRun.rollups``. Every test drives a
:class:`~wordextract.llm.CannedClient`, so nothing here touches a network.

Three properties are asserted rather than assumed:

* the **tree** is derived from the parse's sections and the chunk list alone (the two unit
  tests here build both by hand, so "which section owns a chunk" is checked without a
  fixture's chunker in the way);
* a **rejected** child's roll-up is kept under its key, renders to its parent as
  ``NO_SUMMARY_TEXT`` and still contributes its key and chunk ids -- unknown stays unknown;
* the **key** moves with every member -- children, model, parameters, prompt, roll-up
  version, fan-in, output schema -- so an old store can never answer for a new family.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from wordextract.llm import CANNED_ANSWER, CannedClient
from wordextract.model import RollupArtifact, RollupRejection, Section
from wordextract.pipeline import run, stored_chunks, stored_parse
from wordextract.store import Store, rollup_key
from wordextract.summarize import (
    NO_SUMMARY_TEXT,
    _Child,
    _rollup_fields,
    _union,
    params_hash,
    prompt_hash,
    prompt_text,
    rollup_prompt_hash,
    rollup_prompt_text,
    rollup_tree,
    summarize,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
DOCUMENT = FIXTURES / "program_review_v3.docx"
EXAMPLE_REGISTRY = FIXTURES / "terms" / "synthetic.example.json"

#: ``program_review_v3.docx``'s accepted view: see ``tests/test_pipeline.py``.
FIXTURE_CHUNKS = 5

MODEL = "canned"


def _client(output=CANNED_ANSWER) -> CannedClient:
    return CannedClient(output=output)


def _rejecting_client() -> CannedClient:
    """Answers a chunk prompt and refuses a roll-up prompt: the rejection path, only there."""
    return CannedClient(
        output=lambda prompt, **_: (
            CANNED_ANSWER if not prompt.startswith("# Roll up") else "this is not json"
        )
    )


def _pass(store_root, record, *, client=None, **kwargs):
    return summarize(
        Store(store_root),
        record,
        client=client if client is not None else _client(),
        model=MODEL,
        **kwargs,
    )


def _bytes(root: Path) -> dict[str, bytes]:
    """Every file under ``root``, by relative path -- what a hit must not touch."""
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _units(unit) -> list:
    """Every unit of a tree in preorder: the document first, then sections and chunks."""
    return [unit, *(child for sub in unit.children for child in _units(sub))]


def _target_units(unit) -> list:
    """Every non-chunk unit: the sections with content, and (first) the document."""
    return [candidate for candidate in _units(unit) if candidate.kind != "chunk"]


def _ingest(tmp_path) -> tuple[Path, object]:
    root = tmp_path / "store"
    record = run(DOCUMENT, EXAMPLE_REGISTRY, store_root=root, run_id="ingest")
    return root, record


# --- the tree: the parse's sections and the chunk list, and nothing else ----------------


def test_the_tree_keeps_only_sections_that_hold_content_and_nests_them():
    """A section with no chunk in its whole subtree drops; a heading's own chunk is its
    section's; a chunk whose first node is no section's belongs to the document alone."""
    sections = [
        Section(  # holds nothing directly, but nests a section that does
            heading_id="hA",
            title="A",
            level=0,
            node_ids=[],
            children=[
                Section(heading_id="hB", title="B", level=1, node_ids=["p3"], children=[])
            ],
        ),
        Section(heading_id="hC", title="C", level=0, node_ids=[], children=[]),
    ]
    chunks = [
        SimpleNamespace(id="c1", node_ids=["p0"]),  # preamble: before the first heading
        SimpleNamespace(id="c2", node_ids=["hA"]),  # the heading chunk: A's, not loose
        SimpleNamespace(id="c3", node_ids=["p3"]),  # B's paragraph: B's, not A's
    ]

    tree = rollup_tree(SimpleNamespace(sections=sections), chunks, document_id="doc1")
    assert (tree.kind, tree.id) == ("document", "doc1")
    assert [(unit.kind, unit.id) for unit in tree.children] == [
        ("chunk", "c1"),
        ("section", "hA"),
    ]
    a = tree.children[1]
    assert [(unit.kind, unit.id) for unit in a.children] == [("chunk", "c2"), ("section", "hB")]
    assert [(unit.kind, unit.id) for unit in a.children[1].children] == [("chunk", "c3")]
    # hC held nothing anywhere under it: absent from the tree, never summarized as empty
    assert all(unit.id != "hC" for unit in _units(tree))


def test_the_tree_of_a_document_with_no_chunks_is_empty_not_a_rollup_of_nothing():
    tree = rollup_tree(SimpleNamespace(sections=[]), [], document_id="doc1")
    assert tree.children == () and _units(tree) == [tree]


# --- one pass stores one roll-up per target, keyed on its children ----------------------


def test_a_pass_stores_one_rollup_per_target_keyed_on_its_children(tmp_path):
    root, record = _ingest(tmp_path)
    parsed = stored_parse(record, store_root=root)
    chunks = stored_chunks(record, store_root=root)
    assert len(chunks) == FIXTURE_CHUNKS
    document_id = record.hashed_inputs.source_content_hash
    tree = rollup_tree(parsed, chunks, document_id)
    targets = _target_units(tree)
    # the document, the title section and its four sections -- all six hold content
    assert len(targets) == 6 and targets[0].kind == "document"

    result = _pass(root, record)
    store = Store(root)
    entries = [a for a in result.record.artifact_cache if a.artifact_id.startswith("rollup:")]
    assert {a.artifact_id for a in entries} == {f"rollup:{unit.id}" for unit in targets}
    assert set(store.rollups.list()) == {a.recomputed_key for a in entries}
    assert store.rollup_rejections.list() == []

    summary_keys = {outcome.chunk_id: outcome.key for outcome in result.outcomes}
    by_target = {outcome.target_id: outcome for outcome in result.rollups}
    assert set(by_target) == {unit.id for unit in targets}
    for unit in targets:
        outcome = by_target[unit.id]
        artifact = store.rollups.find(outcome.key)
        assert isinstance(artifact, RollupArtifact)
        assert (artifact.target_kind, artifact.target_id) == (unit.kind, unit.id)
        assert artifact.non_authoritative is True
        assert artifact.document_id == document_id
        assert artifact.model_id == MODEL
        assert artifact.prompt_hash == rollup_prompt_hash() != prompt_hash()
        assert artifact.params_hash == params_hash({"temperature": 0, "max_tokens": 700})
        assert artifact.prompt_ref and artifact.response_ref
        # the record restates its children: keys in order, chunks flattened in order
        if unit.children and all(child.kind == "chunk" for child in unit.children):
            assert artifact.child_keys == [summary_keys[child.id] for child in unit.children]
            assert artifact.child_chunk_ids == [child.id for child in unit.children]
        if unit.kind == "document":
            assert artifact.child_chunk_ids == [chunk.id for chunk in chunks]

    # the family chains up: a section over sections carries their roll-up keys
    title = tree.children[0]
    title_record = store.rollups.find(by_target[title.id].key)
    assert title_record.child_keys == [
        store.rollups.find(by_target[child.id].key).key for child in title.children
    ]
    document_record = store.rollups.find(by_target[tree.id].key)
    assert document_record.child_keys == [title_record.key]


def test_a_second_pass_reuses_every_rollup_and_rewrites_no_byte(tmp_path):
    root, record = _ingest(tmp_path)
    first = _pass(root, record)
    before = _bytes(root / "rollups")

    again = _client()
    result = _pass(root, record, client=again)
    assert again.calls == []
    assert [outcome.called for outcome in result.rollups] == [False] * len(first.rollups)
    assert {outcome.key for outcome in result.rollups} == {
        outcome.key for outcome in first.rollups
    }
    assert _bytes(root / "rollups") == before


# --- the derived fields: children say, the model never ---------------------------------


def test_the_has_pending_union_is_true_then_unknown_then_false():
    """Turn 2's tri-state rule propagated: one True wins, one unknown beats all-False."""
    assert _union([True, False, None]) is True
    assert _union([None, False, False]) is None
    assert _union([False, False]) is False
    assert _union([True, True]) is True


def test_the_rollup_fields_restate_the_children_and_nothing_the_model_wrote():
    children = [
        _Child(key="k1", label="chunk:a", text="one", has_pending=True, chunk_ids=("a", "b")),
        _Child(key="k2", label="section:s", text="two", has_pending=None, chunk_ids=("c",)),
    ]
    assert _rollup_fields(children) == {
        "child_keys": ["k1", "k2"],
        "child_chunk_ids": ["a", "b", "c"],
        "has_pending": True,  # one True among the children, unknown or not
    }


def test_the_rollup_prompt_is_its_own_versioned_file():
    assert rollup_prompt_text().startswith("# Roll up")
    assert rollup_prompt_text() != prompt_text()
    assert rollup_prompt_hash() != prompt_hash()


# --- a rejected roll-up is recorded, and its parent is told -----------------------------


def test_a_rejected_rollup_is_stored_under_its_key_and_the_parent_renders_the_placeholder(
    tmp_path,
):
    root, record = _ingest(tmp_path)
    parsed = stored_parse(record, store_root=root)
    chunks = stored_chunks(record, store_root=root)
    targets = _target_units(
        rollup_tree(parsed, chunks, record.hashed_inputs.source_content_hash)
    )

    rejecting = _rejecting_client()
    result = _pass(root, record, client=rejecting)
    store = Store(root)
    assert store.rollups.list() == []
    assert len(store.rollup_rejections.list()) == len(result.rollups) == len(targets)

    for outcome in result.rollups:
        rejection = store.rollup_rejections.find(outcome.key)
        assert isinstance(rejection, RollupRejection)
        assert outcome.rejection == rejection and outcome.rollup is None
        assert outcome.called is True
        assert rejection.document_id == record.hashed_inputs.source_content_hash
        assert rejection.model_id == MODEL and rejection.prompt_ref
        assert rejection.child_chunk_ids  # kept even on the failure

    # the document's roll-up is asked last, and every child it read was a rejection: its
    # prompt says so in the words the parent actually reads
    rollup_prompts = [
        call.prompt for call in rejecting.calls if call.prompt.startswith("# Roll up")
    ]
    assert rollup_prompts and NO_SUMMARY_TEXT in rollup_prompts[-1]
    assert all(
        entry.status.value == "miss"
        for entry in result.record.artifact_cache
        if entry.artifact_id.startswith("rollup:")
    )


def test_a_stored_rollup_rejection_is_a_hit_until_it_is_evicted(tmp_path):
    root, record = _ingest(tmp_path)
    parsed = stored_parse(record, store_root=root)
    chunks = stored_chunks(record, store_root=root)
    tree = rollup_tree(parsed, chunks, record.hashed_inputs.source_content_hash)
    document = tree  # the root: kind "document"

    _pass(root, record, client=_rejecting_client())
    store = Store(root)
    document_key = _stored_key(store, document.id)
    assert document_key is not None

    quiet = _client()
    result = _pass(root, record, client=quiet)
    assert quiet.calls == []  # a stored rejection counts as a hit, like Turn 1's
    assert all(not outcome.called for outcome in result.rollups)

    # buying a retry means evicting: the next pass calls only the evicted target
    store.rollup_rejections.evict(document_key)
    buying = _client()
    result = _pass(root, record, client=buying)
    rollup_calls = [call for call in buying.calls if call.prompt.startswith("# Roll up")]
    assert len(buying.calls) == 1 and len(rollup_calls) == 1
    assert sum(1 for outcome in result.rollups if outcome.called) == 1

    final = store.rollups.find(document_key)
    assert isinstance(final, RollupArtifact)
    # the still-rejected child contributes its key and chunk ids under NO_SUMMARY_TEXT
    assert len(final.child_keys) == 1
    rejected_child = store.rollup_rejections.find(final.child_keys[0])
    assert isinstance(rejected_child, RollupRejection)
    assert final.child_chunk_ids == rejected_child.child_chunk_ids
    assert NO_SUMMARY_TEXT in rollup_calls[0].prompt


def _stored_key(store: Store, target_id: str) -> str | None:
    """The key a target's roll-up (or rejection) is stored under, from either pair."""
    for key in store.rollups.list():
        record = store.rollups.find(key)
        if record.target_id == target_id:
            return key
    for key in store.rollup_rejections.list():
        record = store.rollup_rejections.find(key)
        if record.target_id == target_id:
            return key
    return None


# --- fan-in: batches below, targets above ----------------------------------------------


def test_fan_in_groups_children_and_the_run_record_still_names_only_the_targets(tmp_path):
    """With a forced fan-in of two, the four-section roll-up nests: two intermediate
    records, one final, one run-record entry -- and the intermediates report the target
    they belong to, so the family stays inspectable in ``SummaryRun.rollups``."""
    from wordextract import summarize as summarize_module

    root, record = _ingest(tmp_path)
    parsed = stored_parse(record, store_root=root)
    chunks = stored_chunks(record, store_root=root)
    tree = rollup_tree(parsed, chunks, record.hashed_inputs.source_content_hash)
    targets = _target_units(tree)
    title = tree.children[0]
    assert title.kind == "section" and len(title.children) == 4

    def two_at_a_time(items):
        return [items[index : index + 2] for index in range(0, len(items), 2)]

    original = summarize_module.rollup_batches
    summarize_module.rollup_batches = two_at_a_time
    try:
        result = _pass(root, record)
    finally:
        summarize_module.rollup_batches = original

    store = Store(root)
    entries = [a for a in result.record.artifact_cache if a.artifact_id.startswith("rollup:")]
    assert {a.artifact_id for a in entries} == {f"rollup:{unit.id}" for unit in targets}
    assert len(store.rollups.list()) > len(targets)  # intermediates are stored too

    title_outcomes = [outcome for outcome in result.rollups if outcome.target_id == title.id]
    assert len(title_outcomes) == 3  # two batches, then their roll-up
    assert all(outcome.target_kind == "section" for outcome in title_outcomes)
    entry = next(a for a in entries if a.artifact_id == f"rollup:{title.id}")
    final = store.rollups.find(entry.recomputed_key)
    assert isinstance(final, RollupArtifact)
    intermediates = title_outcomes[:2]  # execution order: batches, then the final
    assert entry.recomputed_key == title_outcomes[-1].key
    assert final.child_keys == [outcome.key for outcome in intermediates]
    assert final.child_chunk_ids == [unit.id for unit in _units(title) if unit.kind == "chunk"]
    # the chunks were summarized once; the fan-in is roll-up calls only
    assert result.called == FIXTURE_CHUNKS and result.hits == 0


# --- the key: every member moves it -----------------------------------------------------


def test_the_rollup_key_moves_with_every_member():
    base = dict(
        child_keys=["k1", "k2"],
        model_id="m",
        model_params_hash="p",
        prompt_hash="q",
    )
    key = rollup_key(**base)
    others = {k: v for k, v in base.items() if k != "child_keys"}
    assert rollup_key(child_keys=["k2", "k1"], **others) != key  # order is content
    assert rollup_key(child_keys=["k1", "k3"], **others) != key  # members are content
    assert rollup_key(**{**base, "model_id": "other"}) != key
    assert rollup_key(**{**base, "model_params_hash": "other"}) != key
    assert rollup_key(**{**base, "prompt_hash": "other"}) != key
    assert rollup_key(**base, rollup_version="2") != key
    assert rollup_key(**base, fan_in=16) != key
    assert rollup_key(**base, output_schema_version="2") != key
