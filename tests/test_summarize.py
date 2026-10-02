"""Turn 1: the summarizer -- one summary per chunk, cached on the rendered input.

The claims here are about the **cache**, not the model: a summary is keyed by the exact
bytes a model was given (:func:`~wordextract.render.render_union_markup`'s rendering), so a
hit is a claim the store can check and a miss is the only thing that costs a call. Every
test drives a :class:`~wordextract.llm.CannedClient`, so nothing here touches a network.

Three of them are read off ground truth the code did not write:

* the **hand-typed pair sidecar** (``fixtures/program_review_pair.json``) says which chunks
  the v2/v3 edits move -- a rewritten comment and a body paragraph -- and which control
  chunk no edit touches, so "only the affected chunk re-summarizes" is the sidecar's own
  claim and not a round trip through the store;
* a synthetic pair built here differs **only in a tracked deletion**, so the accepted view's
  ``content_hash`` is identical while the summary key must move -- the one case revision 1's
  ``content_hash``-based key got wrong;
* the archived call is re-read from disk and re-decoded, so "the stored summary is the
  archived answer" is checked against the bytes the archive holds.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from docextract_core import CodecError, ReadOnlyError, from_json, sha256_text

from wordextract import opc
from wordextract.chunker import DEFAULT_PARAMS, chunk
from wordextract.cli import main
from wordextract.llm import CANNED_ANSWER, CannedClient
from wordextract.model import RunRecord, SummaryArtifact, SummaryRejection, View
from wordextract.pipeline import run, stored_chunks, stored_parse
from wordextract.render import chunk_text, render_union_markup
from wordextract.store import SUMMARY_VIEW_ID, Store, summary_key
from wordextract.summarize import (
    DEFAULT_MODEL_PARAMS,
    build_prompt,
    input_hash,
    params_hash,
    parse_output,
    prompt_hash,
    prompt_text,
    summarize,
)
from wordextract.terms import TermGroup, TermRegistry, dump_registry, load_registry_text
from wordextract.walker import walk_document

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
DOCUMENT = FIXTURES / "program_review_v3.docx"
DOCUMENT_V2 = FIXTURES / "program_review_v2.docx"
PAIR_SIDECAR = FIXTURES / "program_review_pair.json"
EXAMPLE_REGISTRY = FIXTURES / "terms" / "synthetic.example.json"

#: ``program_review_v3.docx``'s accepted view: see ``tests/test_pipeline.py``.
FIXTURE_CHUNKS = 5

MODEL = "canned"


def _client(output=CANNED_ANSWER, *, tokens=None) -> CannedClient:
    return CannedClient(output=output, tokens=tokens)


def _pass(store_root, record, *, client=None, model=MODEL, **kwargs):
    return summarize(
        Store(store_root),
        record,
        client=client if client is not None else _client(),
        model=model,
        **kwargs,
    )


def _keys(store_root) -> list[str]:
    return sorted(Store(store_root).summaries.list())


def _bytes(root: Path) -> dict[str, bytes]:
    """Every file under ``root``, by relative path -- what a pass must not touch on a hit."""
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# --- the pass stores one summary per accepted-view chunk ------------------------------


def test_a_pass_summarizes_every_accepted_view_chunk_and_records_a_miss_for_each(tmp_path):
    root = tmp_path / "store"
    record = run(DOCUMENT, EXAMPLE_REGISTRY, store_root=root, run_id="ingest")
    chunks = stored_chunks(record, store_root=root)
    assert len(chunks) == FIXTURE_CHUNKS

    result = _pass(root, record)
    assert [outcome.chunk_id for outcome in result.outcomes] == [chunk.id for chunk in chunks]
    assert result.called == FIXTURE_CHUNKS
    assert result.hits == 0 and result.rejected == 0

    store = Store(root)
    stored = store.summaries.list()
    assert len(stored) == FIXTURE_CHUNKS
    for outcome in result.outcomes:
        summary = store.summaries.find(outcome.key)
        assert isinstance(summary, SummaryArtifact)
        assert summary.key == outcome.key
        assert summary.chunk_id == outcome.chunk_id
        assert summary.document_id == record.hashed_inputs.source_content_hash
        assert summary.summary and isinstance(summary.topics, list)


def test_the_pass_run_record_names_one_summary_artifact_per_chunk(tmp_path):
    """``ARTIFACT_KINDS`` stays the five Phase 1 kinds; the pass adds one summary entry per
    chunk and one ``rollup:`` entry per target -- the sections with content, then the
    document (no fixture reaches the fan-in threshold, so here every roll-up is a target)."""
    root = tmp_path / "store"
    record = run(DOCUMENT, EXAMPLE_REGISTRY, store_root=root)
    result = _pass(root, record)

    ids = [a.artifact_id for a in result.record.artifact_cache]
    assert ids == [f"summary:{outcome.chunk_id}" for outcome in result.outcomes] + [
        f"rollup:{outcome.target_id}" for outcome in result.rollups
    ]
    assert result.rollups[-1].target_kind == "document"
    assert {a.status.value for a in result.record.artifact_cache} == {"miss"}
    # the pass's own hashed inputs name the call that produced the summaries
    assert result.record.hashed_inputs.model_id == MODEL
    assert result.record.hashed_inputs.prompt_hash == prompt_hash()
    assert result.record.hashed_inputs.model_params_hash == params_hash(DEFAULT_MODEL_PARAMS)
    assert result.record.hashed_inputs.summarizer_version
    # and it is a stored run like any other
    assert Store(root).load_run(result.record.run_id) is not None


# --- a hit costs nothing --------------------------------------------------------------


def test_a_second_pass_is_all_hits_and_makes_no_call(tmp_path):
    root = tmp_path / "store"
    record = run(DOCUMENT, EXAMPLE_REGISTRY, store_root=root)
    _pass(root, record)
    before = _bytes(root / "summaries")

    second = _client()
    result = _pass(root, record, client=second)
    assert result.called == 0
    assert result.hits == FIXTURE_CHUNKS
    assert second.calls == []
    # nothing was rewritten: every stored summary is byte for byte what it was
    assert _bytes(root / "summaries") == before


def test_a_hit_returns_the_stored_record_and_not_a_new_one(tmp_path):
    root = tmp_path / "store"
    record = run(DOCUMENT, EXAMPLE_REGISTRY, store_root=root)
    first = _pass(root, record)

    # a client that would answer differently: a hit must never reach it
    result = _pass(root, record, client=_client('{"schema_version": "1", "record": {}}'))
    assert result.called == 0
    for outcome, original in zip(result.outcomes, first.outcomes):
        assert outcome.summary == original.summary


# --- the key is over the rendered input, not the chunk's own bytes --------------------


def test_editing_only_a_tracked_deletion_changes_the_key(tmp_path):
    """The case a ``content_hash`` key got wrong: accepted text equal, key must move.

    Two documents differ only in the text of a tracked ``del``. The accepted view drops the
    deletion, so both chunks have the same ``content_hash`` and the same id -- but the union
    the model reads differs, so the summary key must differ.
    """
    alpha = _only_chunk(tmp_path / "alpha", _del_body("alpha"))
    gamma = _only_chunk(tmp_path / "gamma", _del_body("gamma"))

    assert alpha["text"] == gamma["text"] == "beta"
    assert alpha["content_hash"] == gamma["content_hash"]
    assert alpha["markup"] != gamma["markup"]
    assert alpha["key"] != gamma["key"]


def test_one_summary_per_chunk_not_per_view(tmp_path):
    """The original view has the same boundaries and the same node ids, so its chunks
    render to the same bytes: a second pass in the other view is all hits."""
    root = tmp_path / "store"
    accepted = run(DOCUMENT, EXAMPLE_REGISTRY, store_root=root, run_id="accepted")
    _pass(root, accepted)

    original = run(DOCUMENT, EXAMPLE_REGISTRY, store_root=root, view=View.ORIGINAL, run_id="original")
    assert [c.node_ids for c in stored_chunks(original, store_root=root)] == [
        c.node_ids for c in stored_chunks(accepted, store_root=root)
    ]
    # Summarizing the original-view record is refused (its chunk ids are not the accepted
    # view's), but its chunks would key to exactly the summaries already stored.
    parsed = stored_parse(original, store_root=root)
    would_be = {
        summary_key(
            input_hash(render_union_markup(parsed, c)),
            model_id=MODEL,
            model_params_hash=params_hash(DEFAULT_MODEL_PARAMS),
            prompt_hash=prompt_hash(),
        )
        for c in stored_chunks(original, store_root=root)
    }
    assert would_be == set(Store(root).summaries.list())
    assert len(Store(root).summaries.list()) == FIXTURE_CHUNKS


# --- flipping each member of the key --------------------------------------------------


RENDERING = "one paragraph of text, no markup at all"

#: The non-rendering half of a summary key, fixed so one member can be flipped at a time.
_KEY_MEMBERS = {"model_id": "model", "model_params_hash": "params", "prompt_hash": "prompt"}


def _key(rendering: str = RENDERING, **members) -> str:
    return summary_key(input_hash(rendering), **{**_KEY_MEMBERS, **members})


@pytest.mark.parametrize(
    "flip",
    [
        pytest.param(lambda: _key(RENDERING + "!"), id="input_hash"),
        pytest.param(lambda: _key(model_id="another"), id="model_id"),
        pytest.param(lambda: _key(model_params_hash="other"), id="model_params_hash"),
        pytest.param(lambda: _key(prompt_hash="other"), id="prompt_hash"),
        pytest.param(lambda: _key(summarizer_version="2"), id="summarizer_version"),
        pytest.param(lambda: _key(output_schema_version="2"), id="output_schema_version"),
    ],
)
def test_flipping_a_key_member_changes_the_key(flip):
    assert flip() != _key()


def test_a_changed_parameter_re_summarizes_the_chunk(tmp_path):
    """``model_params_hash`` reaches the key through the call's own parameters.

    The roll-ups re-key too: their children's summary keys are members of theirs, so the
    second pass pays for the chunks **and** for every roll-up over them.
    """
    root = tmp_path / "store"
    record = run(DOCUMENT, EXAMPLE_REGISTRY, store_root=root)
    _pass(root, record, params={"temperature": 0, "max_tokens": 700})

    again = _client()
    result = _pass(root, record, client=again, params={"temperature": 0, "max_tokens": 400})
    assert result.called == FIXTURE_CHUNKS
    rollup_calls = [call for call in again.calls if call.prompt.startswith("# Roll up")]
    assert len(rollup_calls) == len(result.rollups)
    assert len(again.calls) == FIXTURE_CHUNKS + len(result.rollups)


def test_the_view_id_is_the_rendering_its_own_and_not_a_documents_view(tmp_path):
    assert SUMMARY_VIEW_ID == "union-markup"
    assert SUMMARY_VIEW_ID not in {view.value for view in View}


# --- only the affected chunk re-summarizes (the hand-typed pair) -----------------------


def _pair():
    sidecar = json.loads(PAIR_SIDECAR.read_text(encoding="utf-8"))
    return {entry["chunk"] for entry in sidecar["changed"]}, {
        entry["chunk"] for entry in sidecar["unchanged"]
    }


def test_a_comment_only_edit_re_summarizes_only_the_chunk_it_annotates(tmp_path):
    """The sidecar's cheaper half: a rewritten comment re-keys the chunk it annotates and
    nothing else -- the chunk's own bytes, and so its id, are the same in both revisions."""
    changed, unchanged = _pair()
    root = tmp_path / "store"
    v3 = run(DOCUMENT, EXAMPLE_REGISTRY, store_root=root, run_id="v3")
    _pass(root, v3)

    v2 = run(DOCUMENT_V2, EXAMPLE_REGISTRY, store_root=root, run_id="v2")
    result = _pass(root, v2)
    called = {index for index, outcome in enumerate(result.outcomes) if outcome.called}

    assert changed <= called and called <= changed
    for index in unchanged:
        assert not result.outcomes[index].called
    # the comment-annotated chunk kept its id: only the context it carries moved
    comment_chunk = min(changed)
    assert result.outcomes[comment_chunk].chunk_id == _pass_outcome_id(v3, root, comment_chunk)


def test_a_body_edit_re_keys_its_chunk_and_leaves_the_rest_a_hit(tmp_path):
    """The pair's tracked change moves one chunk's own text, so its id moves with it; every
    other chunk -- the control included -- stays a hit."""
    changed, unchanged = _pair()
    body_chunk = max(changed)
    root = tmp_path / "store"
    v3 = run(DOCUMENT, EXAMPLE_REGISTRY, store_root=root, run_id="v3")
    _pass(root, v3)

    v2 = run(DOCUMENT_V2, EXAMPLE_REGISTRY, store_root=root, run_id="v2")
    result = _pass(root, v2)

    assert result.outcomes[body_chunk].called
    assert result.outcomes[body_chunk].chunk_id != _pass_outcome_id(v3, root, body_chunk)
    assert sum(1 for outcome in result.outcomes if outcome.called) == len(changed)
    for index in unchanged:
        assert not result.outcomes[index].called
        assert _render(v2, root, index) == _render(v3, root, index)


# --- the term list is not the summary's business ---------------------------------------


def test_adding_a_term_list_re_summarizes_nothing(tmp_path):
    root = tmp_path / "store"
    record = run(DOCUMENT, EXAMPLE_REGISTRY, store_root=root, run_id="small")
    keyed = {outcome.chunk_id: outcome.key for outcome in _pass(root, record).outcomes}

    bigger = tmp_path / "bigger.json"
    registry = load_registry_text(EXAMPLE_REGISTRY.read_text(encoding="utf-8"))
    bigger.write_text(
        dump_registry(TermRegistry(groups=[*registry.groups, TermGroup(canonical="shortfall")])),
        encoding="utf-8",
    )
    grown = run(DOCUMENT, bigger, store_root=root, run_id="big")
    result = _pass(root, grown)

    assert result.called == 0 and result.hits == FIXTURE_CHUNKS
    assert {outcome.chunk_id: outcome.key for outcome in result.outcomes} == keyed


# --- the archived call is the summary's provenance -------------------------------------


def test_a_replayed_archived_call_reproduces_the_stored_summary(tmp_path):
    root = tmp_path / "store"
    record = run(DOCUMENT, EXAMPLE_REGISTRY, store_root=root)
    result = _pass(root, record)
    parsed = stored_parse(record, store_root=root)
    chunks = {chunk.id: chunk for chunk in stored_chunks(record, store_root=root)}

    for outcome in result.outcomes:
        summary = outcome.summary
        # the archived response, re-decoded, is the stored summary's own fields
        archived = (root / summary.response_ref).read_text(encoding="utf-8")
        answer = parse_output(archived)
        assert answer.summary == summary.summary
        assert answer.topics == summary.topics
        assert answer.open_questions == summary.open_questions
        # and the archived prompt is what this chunk's rendering produces
        prompt = (root / summary.prompt_ref).read_text(encoding="utf-8")
        assert prompt == build_prompt(render_union_markup(parsed, chunks[summary.chunk_id]))
        assert prompt.startswith(prompt_text())


def test_the_summary_record_names_the_call_but_no_wall_clock(tmp_path):
    root = tmp_path / "store"
    record = run(DOCUMENT, EXAMPLE_REGISTRY, store_root=root)
    (outcome,) = _pass(root, record).outcomes[:1]
    summary = outcome.summary

    assert (root / summary.prompt_ref).is_file()
    assert (root / summary.response_ref).is_file()
    assert summary.prompt_hash == prompt_hash()
    assert summary.params_hash == params_hash(DEFAULT_MODEL_PARAMS)
    assert summary.model_id == MODEL
    fields = {field for field in SummaryArtifact.__dataclass_fields__}
    assert "latency_ms" not in fields and "call_id" not in fields


def test_the_tokens_the_client_reported_are_recorded(tmp_path):
    root = tmp_path / "store"
    record = run(DOCUMENT, EXAMPLE_REGISTRY, store_root=root)
    result = _pass(root, record, client=_client(tokens=17))
    assert {outcome.summary.tokens for outcome in result.outcomes} == {17}

    # the canned client reports none by default, and a made-up cost is never recorded
    other = tmp_path / "other"
    silent = _pass(other, run(DOCUMENT, EXAMPLE_REGISTRY, store_root=other))
    assert {outcome.summary.tokens for outcome in silent.outcomes} == {None}


# --- a rejected answer is recorded, never stored ---------------------------------------


#: Answers the strict codec must reject. These are all *shape* failures: the envelope, an
#: unknown key, a missing required field, a schema from another version. (Value *types* are
#: not among them -- ``decode`` checks keys and required-ness, not scalars -- which is a
#: codec-level gap reported with this turn, not something the summarizer papers over.)
BAD_ANSWERS = [
    pytest.param("not json at all", id="not-json"),
    pytest.param('{"schema_version": "1", "record": {"summary": "only a summary"}}', id="missing-fields"),
    pytest.param(
        '{"schema_version": "1", "record": {"summary": "x", "topics": [], '
        '"open_questions": [], "extra": 1}}',
        id="unknown-key",
    ),
    pytest.param(
        '{"schema_version": "9", "record": {"summary": "x", "topics": [], "open_questions": []}}',
        id="other-schema",
    ),
    pytest.param(
        '{"schema_version": "1", "record": {"summary": "x"}}',
        id="missing-open-questions",
    ),
]


@pytest.mark.parametrize("answer", BAD_ANSWERS)
def test_malformed_output_is_recorded_as_a_rejection_and_no_summary_is_stored(tmp_path, answer):
    root = tmp_path / "store"
    record = run(DOCUMENT, EXAMPLE_REGISTRY, store_root=root)
    result = _pass(root, record, client=_client(answer))

    assert result.rejected == FIXTURE_CHUNKS
    store = Store(root)
    assert store.summaries.list() == []
    assert len(store.rejections.list()) == FIXTURE_CHUNKS
    for outcome in result.outcomes:
        rejection = store.rejections.find(outcome.key)
        assert isinstance(rejection, SummaryRejection)
        assert rejection.error and ":" in rejection.error
        assert (root / rejection.response_ref).read_text(encoding="utf-8") == answer


def test_a_recorded_rejection_is_a_cache_hit_and_is_not_retried(tmp_path):
    """A rejected answer is cached under the key a summary would have taken, so a later pass
    with the same key calls nobody -- a transient model failure is not retried until the key
    moves (a real model, prompt, parameter, rendering or schema change)."""
    root = tmp_path / "store"
    record = run(DOCUMENT, EXAMPLE_REGISTRY, store_root=root)
    rejected = _pass(root, record, client=_client("not json"))
    assert rejected.called == FIXTURE_CHUNKS and rejected.rejected == FIXTURE_CHUNKS

    cached = _client()
    again = _pass(root, record, client=cached)
    assert again.called == 0 and cached.calls == []
    assert again.hits == FIXTURE_CHUNKS and again.rejected == FIXTURE_CHUNKS
    assert Store(root).summaries.list() == []


def test_a_summary_and_a_rejection_never_share_a_key(tmp_path):
    """The two live in one key space, so a key holds at most one of them: a pass that answers
    properly stores summaries under its keys and leaves the rejections under theirs."""
    root = tmp_path / "store"
    record = run(DOCUMENT, EXAMPLE_REGISTRY, store_root=root)
    _pass(root, record, client=_client("not json"))

    # a different model re-keys every chunk, so the summaries land under new keys while the
    # rejections stay where they were -- each key still holding exactly one record
    result = _pass(root, record, client=_client(), model="other")
    assert result.called == FIXTURE_CHUNKS and result.rejected == 0

    store = Store(root)
    summary_keys = set(store.summaries.list())
    rejection_keys = set(store.rejections.list())
    assert len(summary_keys) == FIXTURE_CHUNKS and len(rejection_keys) == FIXTURE_CHUNKS
    assert summary_keys.isdisjoint(rejection_keys)


def test_an_unreadable_record_is_evicted_and_replaced_under_its_key(tmp_path):
    """A record the codec can no longer read (written under an older schema) is missing, not a
    hit: the chunk is called again, and a rejection takes the key the summary held -- the
    unreadable file does not outlive the record that replaced it."""
    root = tmp_path / "store"
    record = run(DOCUMENT, EXAMPLE_REGISTRY, store_root=root)
    (target,) = _pass(root, record).outcomes[:1]
    key = target.key
    (root / "summaries" / f"{key}.json").write_text(
        json.dumps({"schema_version": "0", "record": {}}), encoding="utf-8"
    )

    result = _pass(root, record, client=_client("not json"))
    assert result.called == 1 and result.hits == FIXTURE_CHUNKS - 1
    assert result.rejected == 1
    assert not (root / "summaries" / f"{key}.json").exists()
    assert (root / "rejections" / f"{key}.json").is_file()
    assert len(Store(root).summaries.list()) == FIXTURE_CHUNKS - 1
    assert Store(root).rejections.list() == [key]


# --- the pass refuses a store it may not write -----------------------------------------


def test_a_read_only_store_is_refused_before_any_call(tmp_path):
    root = tmp_path / "store"
    record = run(DOCUMENT, EXAMPLE_REGISTRY, store_root=root)
    _pass(root, record)

    client = _client()
    with pytest.raises(ReadOnlyError):
        summarize(Store(root, read_only=True), record, client=client, model=MODEL)
    assert client.calls == []


def test_a_run_ingested_in_another_view_is_refused_before_any_call(tmp_path):
    """Summaries are per accepted-view chunk; an original-view record's chunk ids differ."""
    root = tmp_path / "store"
    record = run(DOCUMENT, EXAMPLE_REGISTRY, store_root=root, view=View.ORIGINAL)

    client = _client()
    with pytest.raises(ValueError, match="accepted-view"):
        summarize(Store(root), record, client=client, model=MODEL)
    assert client.calls == []


# --- the output the prompt asks for ----------------------------------------------------


def test_the_output_schema_is_bounded_on_both_sides():
    assert parse_output(CANNED_ANSWER).summary
    for version in ("0", "2"):
        payload = json.loads(CANNED_ANSWER)
        payload["schema_version"] = version
        with pytest.raises(CodecError):
            parse_output(json.dumps(payload))


def test_the_prompt_is_the_versioned_file_with_the_rendering_appended_verbatim():
    text = prompt_text()
    assert text and prompt_hash() == sha256_text(text)
    # the prompt is a prefix and the rendering is appended verbatim: a document's own braces
    # and percent signs are never read as a format string
    assert build_prompt("{not a format} %s") == f"{text}{{not a format}} %s\n"


# --- the CLI ----------------------------------------------------------------------------


def test_summarize_ingests_then_prints_the_pass_run_record(tmp_path, capsys):
    store = tmp_path / "store"
    argv = [
        "summarize",
        str(DOCUMENT),
        "--terms",
        str(EXAMPLE_REGISTRY),
        "--store",
        str(store),
        "--client",
        "canned",
    ]
    assert main(argv) == 0
    printed = capsys.readouterr()
    assert printed.err == ""
    record = from_json(RunRecord, printed.out)
    prefixes = [a.artifact_id.split(":")[0] for a in record.artifact_cache]
    assert prefixes == ["summary"] * FIXTURE_CHUNKS + ["rollup"] * (len(prefixes) - FIXTURE_CHUNKS)
    assert len(prefixes) > FIXTURE_CHUNKS  # the roll-up targets are named too
    assert len(Store(store).summaries.list()) == FIXTURE_CHUNKS
    assert len(Store(store).rollups.list()) == len(prefixes) - FIXTURE_CHUNKS


def test_the_summarize_client_is_required_and_an_unknown_one_exits_two(tmp_path, capsys):
    store = tmp_path / "store"
    with pytest.raises(SystemExit) as missing:
        main(["summarize", str(DOCUMENT), "--terms", str(EXAMPLE_REGISTRY), "--store", str(store)])
    assert missing.value.code == 2

    argv = [
        "summarize",
        str(DOCUMENT),
        "--terms",
        str(EXAMPLE_REGISTRY),
        "--store",
        str(store),
        "--client",
        "nope",
    ]
    assert main(argv) == 2
    printed = capsys.readouterr()
    assert printed.out == ""
    assert "unknown client" in printed.err


@pytest.mark.parametrize("flag", ["--view", "--run-id"])
def test_summarize_takes_neither_a_view_nor_a_run_id(tmp_path, flag):
    argv = [
        "summarize",
        str(DOCUMENT),
        "--terms",
        str(EXAMPLE_REGISTRY),
        "--store",
        str(tmp_path / "store"),
        "--client",
        "canned",
        flag,
        "x",
    ]
    with pytest.raises(SystemExit) as failed:
        main(argv)
    assert failed.value.code == 2


# --- helpers: a synthetic package whose only edit is a tracked deletion ----------------


RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
W_ATTRS = f'xmlns:w="{opc.W_NS}" xmlns:w14="{opc.W14_NS}" xmlns:r="{opc.R_NS}"'


def _del_body(deleted: str) -> str:
    """One paragraph: a tracked deletion, then an insertion, differing only in ``deleted``."""
    return (
        "<w:p>"
        '<w:del w:id="1" w:author="A. Ito" w:date="2026-01-01T00:00:00Z">'
        f"<w:r><w:t>{deleted}</w:t></w:r></w:del>"
        '<w:ins w:id="2" w:author="A. Ito" w:date="2026-01-01T00:00:00Z">'
        "<w:r><w:t>beta</w:t></w:r></w:ins>"
        "</w:p>"
    )


def _only_chunk(tmp_path: Path, body: str) -> dict[str, str]:
    """The one chunk of a minimal package holding ``body``: its text, hash, markup and key."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    members = {
        "[Content_Types].xml": (
            f'<Types xmlns="{TYPES_NS}">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            f'<Relationships xmlns="{RELS_NS}">'
            f'<Relationship Id="rId1" Type="{opc.RT_OFFICE_DOCUMENT}" Target="word/document.xml"/>'
            "</Relationships>"
        ),
        "word/document.xml": f"<w:document {W_ATTRS}><w:body>{body}</w:body></w:document>",
    }
    path = tmp_path / "synth.docx"
    with zipfile.ZipFile(path, "w") as archive:
        for member, data in members.items():
            archive.writestr(member, data.encode("utf-8"))

    package = opc.Package(path)
    parsed = walk_document(package)
    (one,) = chunk(parsed, package.document.part_id, params=DEFAULT_PARAMS)
    markup = render_union_markup(parsed, one)
    return {
        "text": chunk_text(parsed, one, View.ACCEPTED),
        "content_hash": one.content_hash,
        "markup": markup,
        "key": summary_key(input_hash(markup), model_id="model", model_params_hash="p", prompt_hash="q"),
    }


def _pass_outcome_id(record, store_root, index: int) -> str:
    return stored_chunks(record, store_root=store_root)[index].id


def _render(record, store_root, index: int) -> str:
    parsed = stored_parse(record, store_root=store_root)
    return render_union_markup(parsed, stored_chunks(record, store_root=store_root)[index])
