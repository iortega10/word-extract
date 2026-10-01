"""Turn 2: ``pending_changes`` -- which revisions touch a chunk, and what no chunk can.

Two derivations, both read off the union stacks (never off the raw union, never off the
revision records alone):

* :func:`~wordextract.pending.pending_changes` -- one chunk's manifest entries in the
  spec's shape. The pollution chunk of ``program_review_v3`` pins the whole entry --
  span offsets into the committed union stream, author, date, order -- because that is
  the chunk the D5 story turns on; the nested and moved fixtures pin the cases a flat
  document cannot show (nesting, move groups); two synthetic packages pin what no
  fixture needs (the id whose record is gone, the excerpt cut, the tie-break);
* :func:`~wordextract.pending.document_pending` -- the document roll-up: every revision
  the supplied chunk set never carries, and the two attribution gap ids that mean "a
  revision no chunk can show". No chunks is the honest answer "none of them", not an
  error; a gap about parts is not a pending gap.

The end-to-end claim is D5: a summary record carries the deletion beside it that the
canned prose never mentions -- the record is the only place a reader would learn the
deletion exists. ``SummaryArtifact.has_pending`` is the tri-state flag over it: ``None``
when a record predates Turn 2, never a stale ``False``.

``program_review_v3.docx`` is committed and its union stream is stable, so its span
offsets are pinned here the way other suites pin fixture sizes: as ground truth the next
change has to argue with.
"""
from __future__ import annotations

import zipfile
from dataclasses import replace
from pathlib import Path

from wordextract import opc
from wordextract.chunker import DEFAULT_PARAMS, chunk
from wordextract.llm import CANNED_ANSWER, CannedClient
from wordextract.model import SummaryArtifact, View
from wordextract.pending import (
    EXCERPT_LIMIT,
    TRUNCATION_MARKER,
    DocumentPending,
    document_pending,
    pending_changes,
)
from wordextract.pipeline import run, stored_chunks, stored_parse
from wordextract.render import chunk_text
from wordextract.store import Store
from wordextract.summarize import summarize
from wordextract.walker import walk_document

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MODEL = FIXTURES / "model"
DOCUMENT = FIXTURES / "program_review_v3.docx"
EXAMPLE_REGISTRY = FIXTURES / "terms" / "synthetic.example.json"

RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
W_ATTRS = f'xmlns:w="{opc.W_NS}" xmlns:w14="{opc.W14_NS}" xmlns:r="{opc.R_NS}"'

#: The author and date both revisions in ``program_review_v3`` carry.
ALVAREZ = ("R. Alvarez", "2026-09-01T10:00:00Z")


def _docx(tmp_path: Path, body: str, name: str = "synth.docx") -> Path:
    """A minimal package: one body part, nothing a case here does not need."""
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
            f'<Relationship Id="rId1" Type="{opc.RT_OFFICE_DOCUMENT}" '
            'Target="word/document.xml"/>'
            "</Relationships>"
        ),
        "word/document.xml": f"<w:document {W_ATTRS}><w:body>{body}</w:body></w:document>",
    }
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as archive:
        for member, data in members.items():
            archive.writestr(member, data.encode("utf-8"))
    return path


def _parse(path: Path):
    """``path``'s parse, its body part id, and its chunks under the default params."""
    package = opc.Package(path)
    parsed = walk_document(package)
    part_id = package.document.part_id
    return parsed, part_id, chunk(parsed, part_id, params=DEFAULT_PARAMS)


def _synth(tmp_path: Path, body: str):
    """``body``'s parse and its single chunk -- one paragraph, one chunk, by construction."""
    parsed, _part_id, chunks = _parse(_docx(tmp_path, body))
    assert len(chunks) == 1
    return parsed, chunks[0]


def _entries(parsed, chunks) -> list[dict]:
    """Every manifest entry across ``chunks``, in chunk order."""
    return [entry for one in chunks for entry in pending_changes(parsed, one)]


def _pollution(parsed, chunks):
    """The one ``program_review_v3`` chunk the tracked revision lives in."""
    (found,) = [
        one for one in chunks if "Pollution:" in chunk_text(parsed, one, View.ACCEPTED)
    ]
    return found


# --- one chunk's entries ----------------------------------------------------------------


def test_the_pollution_chunk_names_exactly_the_two_revisions_it_holds():
    """Span offsets, author, date and order, pinned against the committed stream."""
    parsed, _part_id, chunks = _parse(DOCUMENT)
    entries = pending_changes(parsed, _pollution(parsed, chunks))
    assert entries == [
        {
            "revision_id": "del:900",
            "kind": "del",
            "author": ALVAREZ[0],
            "date": ALVAREZ[1],
            "spans": [{"part_id": "officeDocument:0", "start": 673, "end": 694}],
            "text_excerpt": "excluded in all cases",
            "move_group_id": None,
        },
        {
            "revision_id": "ins:901",
            "kind": "ins",
            "author": ALVAREZ[0],
            "date": ALVAREZ[1],
            "spans": [{"part_id": "officeDocument:0", "start": 694, "end": 734}],
            "text_excerpt": "excluded except for hostile-fire release",
            "move_group_id": None,
        },
    ]


def test_nested_revisions_each_get_their_own_entries_and_their_own_excerpt():
    """A revision inside a revision is not merged: each id carries the text under it.

    ``ins:2``'s excerpt contains ``del:3``'s text because the union really says that;
    ``del:3``'s excerpt is only its own span. Order follows the first span's start.
    """
    parsed, _part_id, chunks = _parse(MODEL / "nested_revisions.docx")
    entries = _entries(parsed, chunks)
    assert [entry["revision_id"] for entry in entries] == ["ins:2", "del:3", "del:4", "ins:5"]
    assert [entry["text_excerpt"] for entry in entries] == [
        "Coverage is excludedapplies worldwide.",
        "is excluded",
        "Draft clause Final clause is void.",
        "Final clause",
    ]
    assert all(entry["author"] == "A. Ito" for entry in entries)
    assert all(entry["date"] == "2026-01-01T00:00:00Z" for entry in entries)
    assert all(entry["move_group_id"] is None for entry in entries)


def test_both_halves_of_a_moved_passage_carry_the_same_move_group():
    parsed, _part_id, chunks = _parse(MODEL / "move.docx")
    entries = _entries(parsed, chunks)
    assert [(entry["revision_id"], entry["kind"]) for entry in entries] == [
        ("moveFrom:5", "moveFrom"),
        ("moveTo:6", "moveTo"),
    ]
    assert {entry["move_group_id"] for entry in entries} == {"mg1"}
    assert {entry["author"] for entry in entries} == {"E. Nakamura"}
    assert {entry["text_excerpt"] for entry in entries} == {"Section 4 "}


def test_an_id_with_no_record_behind_it_is_still_rendered_from_the_id_alone(tmp_path):
    """A stack id no :class:`Revision` stands behind: kind from the id, no author, no date."""
    parsed, one = _synth(
        tmp_path,
        '<w:p><w:del w:id="7" w:author="A" w:date="2020-01-01T00:00:00Z">'
        "<w:r><w:delText>gone</w:delText></w:r></w:del></w:p>",
    )
    entries = pending_changes(replace(parsed, revisions=[]), one)
    assert entries == [
        {
            "revision_id": "del:7",
            "kind": "del",
            "author": "",
            "date": None,
            "spans": [{"part_id": "officeDocument:0", "start": 0, "end": 4}],
            "text_excerpt": "gone",
            "move_group_id": None,
        }
    ]


def test_a_long_deletion_is_cut_to_the_excerpt_limit_with_the_marker(tmp_path):
    parsed, one = _synth(
        tmp_path,
        '<w:p><w:del w:id="9" w:author="A" w:date="2020-01-01T00:00:00Z">'
        f'<w:r><w:delText>{"D" * 210}</w:delText></w:r></w:del></w:p>',
    )
    (entry,) = pending_changes(parsed, one)
    assert entry["text_excerpt"] == "D" * EXCERPT_LIMIT + TRUNCATION_MARKER
    assert len(entry["text_excerpt"]) == EXCERPT_LIMIT + len(TRUNCATION_MARKER)
    # the cut is the excerpt's, not the span's: the entry still reports the whole range
    assert entry["spans"] == [{"part_id": "officeDocument:0", "start": 0, "end": 210}]


def test_an_excerpt_exactly_at_the_limit_keeps_no_marker(tmp_path):
    parsed, one = _synth(
        tmp_path,
        '<w:p><w:del w:id="9" w:author="A" w:date="2020-01-01T00:00:00Z">'
        f'<w:r><w:delText>{"E" * EXCERPT_LIMIT}</w:delText></w:r></w:del></w:p>',
    )
    (entry,) = pending_changes(parsed, one)
    assert entry["text_excerpt"] == "E" * EXCERPT_LIMIT
    assert TRUNCATION_MARKER not in entry["text_excerpt"]


def test_a_chunk_naming_no_nodes_touches_no_revision():
    parsed, _part_id, chunks = _parse(DOCUMENT)
    pollution = _pollution(parsed, chunks)
    assert pending_changes(parsed, replace(pollution, node_ids=[])) == []


def test_a_part_whose_union_stream_did_not_survive_reports_nothing():
    """Documented best-effort: ``render_union_markup`` refuses this loudly first, so the
    summarize path never reaches here -- but if asked, the answer is ``[]``, not a guess.
    """
    parsed, _part_id, chunks = _parse(DOCUMENT)
    pollution = _pollution(parsed, chunks)
    assert pending_changes(replace(parsed, union_streams=[]), pollution) == []


def test_two_revisions_opening_at_the_same_offset_sort_by_id(tmp_path):
    """The insertion opens first in document order; at an equal first span, the id decides."""
    parsed, one = _synth(
        tmp_path,
        '<w:p><w:ins w:id="1" w:author="A" w:date="2020-01-01T00:00:00Z">'
        '<w:del w:id="2" w:author="A" w:date="2020-01-01T00:00:00Z">'
        "<w:r><w:delText>inner</w:delText></w:r></w:del></w:ins></w:p>",
    )
    entries = pending_changes(parsed, one)
    assert [entry["revision_id"] for entry in entries] == ["del:2", "ins:1"]
    assert [span["start"] for entry in entries for span in entry["spans"]] == [0, 0]


# --- what no chunk accounts for ----------------------------------------------------------


def test_a_chunk_set_covering_the_document_attributes_every_revision():
    parsed, _part_id, chunks = _parse(DOCUMENT)
    pollution = _pollution(parsed, chunks)
    assert document_pending(parsed, chunks) == DocumentPending(unattributed_revisions=0)

    # every chunk but the pollution one: neither revision was shown, so both are unattributed
    others = [one for one in chunks if one is not pollution]
    assert document_pending(parsed, others) == DocumentPending(unattributed_revisions=2)
    # no chunk set at all is the honest "none of them", not an error
    assert document_pending(parsed, []) == DocumentPending(unattributed_revisions=2)


def test_a_paragraph_mark_revision_is_unattributed_and_never_in_any_chunk():
    """A record on no stack: counted once, named by its gap, never rendered anywhere."""
    parsed, _part_id, chunks = _parse(MODEL / "deleted_paragraph_mark.docx")
    assert document_pending(parsed, chunks) == DocumentPending(
        unattributed_revisions=1, unattributed_gaps=["paragraph_mark_revision"]
    )
    assert _entries(parsed, chunks) == []


def test_a_formatting_only_change_counts_no_revision_and_names_the_gap():
    """Zero revisions, one named cause: a gap is not a count (rule 8)."""
    parsed, _part_id, chunks = _parse(MODEL / "unrecorded_revision_kinds.docx")
    assert document_pending(parsed, chunks) == DocumentPending(
        unattributed_revisions=0, unattributed_gaps=["unrecorded_revision_kind"]
    )


def test_a_gap_that_is_about_parts_is_not_a_pending_gap():
    """Only the two attribution gaps belong here; a part gap says nothing about revisions."""
    parsed, _part_id, chunks = _parse(DOCUMENT)
    flagged = replace(parsed, known_gaps=["textbox", "paragraph_mark_revision"])
    assert document_pending(flagged, chunks).unattributed_gaps == ["paragraph_mark_revision"]


# --- what a summary record carries (D5) ---------------------------------------------------


def _artifact(pending_value) -> SummaryArtifact:
    return SummaryArtifact(
        key="key",
        chunk_id="chunk",
        document_id="document",
        summary="summary",
        topics=[],
        open_questions=[],
        model_id="canned",
        prompt_hash="",
        params_hash="",
        prompt_ref="",
        response_ref="",
        pending_changes=pending_value,
    )


def test_has_pending_is_the_tri_state_of_what_the_record_holds():
    """``None`` is *not computed* (a record written before Turn 2), never a stale ``False``."""
    assert _artifact(None).has_pending is None
    assert _artifact([]).has_pending is False
    assert _artifact([{"revision_id": "del:900"}]).has_pending is True


def test_a_summary_record_carries_the_deletions_its_canned_prose_never_mentions(tmp_path):
    """D5 end to end: whatever the model's answer omitted sits beside it, in the record."""
    root = tmp_path / "store"
    record = run(DOCUMENT, EXAMPLE_REGISTRY, store_root=root, run_id="ingest")
    parsed = stored_parse(record, store_root=root)
    chunks = stored_chunks(record, store_root=root)
    pollution = _pollution(parsed, chunks)
    clean = next(one for one in chunks if one is not pollution)

    result = summarize(
        Store(root),
        record,
        client=CannedClient(output=CANNED_ANSWER),
        model="canned",
    )
    by_id = {outcome.chunk_id: outcome for outcome in result.outcomes}

    summary = by_id[pollution.id].summary
    assert summary.pending_changes == pending_changes(parsed, pollution)
    assert [entry["revision_id"] for entry in summary.pending_changes] == ["del:900", "ins:901"]
    assert summary.has_pending is True
    # the canned answer never heard of the deletion; the record beside it still states it
    prose = summary.summary.lower()
    assert "deletion" not in prose
    assert "excluded in all cases" not in prose

    untouched = by_id[clean.id].summary
    assert untouched.pending_changes == []
    assert untouched.has_pending is False
