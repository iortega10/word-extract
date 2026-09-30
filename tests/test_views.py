"""Turn 3: the view masks, gap-closing and the view-to-union offset map.

The projection is asked for nothing but a part's union stream: paragraph extents are
derived *here*, out of the spans themselves (a single-character span whose text is the
terminator and whose stack is empty ends a paragraph), exactly as Turn 2a derives them,
so no check can agree with the walker by sharing paragraph logic the walker does not
have yet. Everything a projection is compared against is a hand-typed literal -- the
0a sidecar view strings, the spec's worked example A, or a package built here -- never
another projection.

The spec's worked examples B to E are pinned by the 0a sidecars (``clause`` B, C, D, E)
and example A, which no fixture carries, by :data:`EXAMPLE_A` in this module.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from wordextract import opc
from wordextract.evals import labels
from wordextract.model import ElementarySpan, Span, UnionStream, View
from wordextract.views import (
    DEL_FAMILY,
    INS_FAMILY,
    VIEWS,
    Projection,
    keeps,
    project,
    revision_kind,
)
from wordextract.walker import TERMINATOR, union_streams

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MODEL = FIXTURES / "model"
MODEL_NAMES = sorted(p.name[: -len(".expected.json")] for p in MODEL.glob("*.expected.json"))
ALL_DOCX = sorted(FIXTURES.rglob("*.docx"))

#: ``text-model-spec.md`` section 6 example A, hand-typed: two disjoint revision runs
#: with no separator between them in the union, so a raw-union match would invent
#: ``recoverysubrogation``. No 0a fixture covers it.
EXAMPLE_A = ("right of recoverysubrogation", "right of subrogation", "right of recovery", "")

#: The examples the build spec's "every worked example A to E" is checked against.
WORKED_EXAMPLES = ("A", "B", "C", "D", "E")

CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _paragraph_slices(stream: UnionStream) -> list[tuple[int, int, list]]:
    """The paragraph extents of ``stream`` as ``(start, end, spans)``.

    ``end`` is the paragraph's terminator offset, so the terminator itself is not in
    ``spans`` -- the same derivation Turn 2a uses, kept here so this module never
    leans on a paragraph API the walker does not have yet.
    """
    paragraphs: list[tuple[int, int, list]] = []
    start = 0
    spans = []
    for span in stream.spans:
        if span.stack or stream.text[span.start : span.end] != TERMINATOR:
            spans.append(span)
            continue
        assert span.end - span.start == 1, f"terminator spans are one character: {span}"
        paragraphs.append((start, span.start, spans))
        start = span.end
        spans = []
    assert start == len(stream.text), "a stream must end with a terminator span"
    return paragraphs


def _stream(*pieces: tuple[list[str], str]) -> UnionStream:
    """A union stream from ``(stack, text)`` pieces; the offsets are derived here."""
    text = ""
    spans: list[ElementarySpan] = []
    for stack, piece in pieces:
        spans.append(ElementarySpan(len(text), len(text) + len(piece), list(stack)))
        text += piece
    return UnionStream("word/document.xml", text, spans)


def _example_a() -> UnionStream:
    return _stream(
        ([], "right of "),
        (["del:1"], "recovery"),
        (["ins:2"], "subrogation"),
        ([], TERMINATOR),
    )


def _document_stream(path: Path) -> UnionStream:
    package = opc.Package(path)
    return next(s for s in union_streams(package) if s.part_id == package.document.part_id)


def _model_stream(name: str) -> UnionStream:
    return _document_stream(MODEL / f"{name}.docx")


def _synth(tmp_path: Path, body: str) -> Path:
    """A minimal package: the document part and the two parts every package needs.

    A part is resolved by relationship type, so nothing beyond the body can influence
    what a walk reads: a claim about a construct built here is a claim about the
    walker and the projection only.
    """
    members = {
        "[Content_Types].xml": (
            f'<Types xmlns="{CONTENT_TYPES_NS}">'
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
        "word/document.xml": (
            f'<w:document xmlns:w="{opc.W_NS}"><w:body>{body}</w:body></w:document>'
        ),
    }
    path = tmp_path / "views.docx"
    with zipfile.ZipFile(path, "w") as archive:
        for member, data in members.items():
            archive.writestr(member, data.encode("utf-8"))
    return path


def _projections(path: Path) -> list[tuple[str, str, str, tuple]]:
    """Every part of ``path``, projected through every view (the determinism snapshot)."""
    snapshots = []
    for stream in union_streams(opc.Package(path)):
        for view in VIEWS:
            projection = project(stream, view)
            snapshots.append((stream.part_id, view.value, projection.text, projection.runs))
    return snapshots


# --- the mask (§3) --------------------------------------------------------------

def test_the_two_revision_families_are_the_spec_sets():
    assert DEL_FAMILY == {"del", "moveFrom"}
    assert INS_FAMILY == {"ins", "moveTo"}
    assert VIEWS == (View.ACCEPTED, View.ORIGINAL, View.SUPERSEDED)


def test_a_revision_kind_is_the_id_up_to_its_first_colon():
    assert revision_kind("del:12") == "del"
    assert revision_kind("moveFrom:3") == "moveFrom"


@pytest.mark.parametrize(
    "stack,kept",
    [
        ([], (True, True, False)),
        (["ins:1"], (True, False, False)),
        (["del:1"], (False, True, False)),
        (["del:1", "ins:2"], (False, False, True)),
        (["ins:2", "del:1"], (False, False, True)),
        (["moveTo:3"], (True, False, False)),
        (["moveFrom:4"], (False, True, False)),
        (["moveFrom:4", "moveTo:3"], (False, False, True)),
        (["ins:1", "del:2", "moveTo:3"], (False, False, True)),
    ],
)
def test_the_mask_matrix(stack, kept):
    """Accepted drops del-family ancestry, original drops ins-family, superseded needs
    both -- and an unmarked span is in accepted and original but never superseded."""
    assert tuple(keeps(stack, view) for view in VIEWS) == kept


def test_an_unknown_view_is_an_error_not_a_fourth_meaning():
    with pytest.raises(ValueError):
        keeps([], "both")


# --- the worked examples (§6) ---------------------------------------------------

def test_example_a_views_are_the_spec_strings():
    union, accepted, original, superseded = EXAMPLE_A
    stream = _example_a()
    assert stream.text == union + TERMINATOR
    (start, end, _), = _paragraph_slices(stream)
    for view, expected in (
        (View.ACCEPTED, accepted),
        (View.ORIGINAL, original),
        (View.SUPERSEDED, superseded),
    ):
        assert project(stream, view, start, end).text == expected
        # the false adjacency a raw-union match would find is in no view at all
        assert "recoverysubrogation" not in project(stream, view).text


def test_example_a_whole_part_keeps_the_terminator_in_every_view():
    stream = _example_a()
    assert project(stream, View.ACCEPTED).text == "right of subrogation" + TERMINATOR
    assert project(stream, View.ORIGINAL).text == "right of recovery" + TERMINATOR
    assert project(stream, View.SUPERSEDED).text == TERMINATOR


def test_no_placeholder_is_inserted_where_the_deleted_space_was():
    """§8: gap-closing keeps no placeholder. With the space inside the deleted run,
    the accepted view is Word's own rendering ``right ofsubrogation`` -- the miss is
    documented, not papered over with a space the union does not have."""
    stream = _stream(
        ([], "right of"),
        (["del:1"], " "),
        (["ins:2"], "subrogation"),
        ([], TERMINATOR),
    )
    (start, end, _), = _paragraph_slices(stream)
    accepted = project(stream, View.ACCEPTED, start, end)
    assert accepted.text == "right ofsubrogation"
    assert project(stream, View.ORIGINAL, start, end).text == "right of "
    assert project(stream, View.SUPERSEDED, start, end).text == ""
    assert [(run.view_start, run.view_end) for run in accepted.runs] == [(0, 8), (8, 19)]


def test_a_break_inside_a_deleted_run_goes_with_the_run_and_never_splits_it(tmp_path):
    """§4/§8: every ``w:br`` is content inside the paragraph, so it is elided with the
    run that carries it, creates no boundary, and is one ``U+000B`` in the original."""
    body = (
        '<w:p><w:r><w:t xml:space="preserve">Before</w:t></w:r>'
        '<w:del w:id="1" w:author="A. Ito" w:date="2026-01-01T00:00:00Z">'
        '<w:r><w:delText xml:space="preserve">X</w:delText></w:r>'
        '<w:r><w:br w:type="page"/></w:r>'
        '<w:r><w:delText xml:space="preserve">Y</w:delText></w:r></w:del>'
        '<w:r><w:t xml:space="preserve">after</w:t></w:r></w:p>'
    )
    stream = _document_stream(_synth(tmp_path, body))
    assert stream.text == "BeforeX\u000bYafter" + TERMINATOR
    assert [
        span.stack for span in stream.spans if "\u000b" in stream.text[span.start : span.end]
    ] == [["del:1"]]
    assert len(_paragraph_slices(stream)) == 1
    accepted = project(stream, View.ACCEPTED)
    assert accepted.text == "Beforeafter" + TERMINATOR
    assert [(run.view_start, run.view_end, run.union_start) for run in accepted.runs] == [
        (0, 6, 0),
        (6, 11, 9),
        (11, 12, 14),
    ]
    assert project(stream, View.ORIGINAL).text == "BeforeX\u000bYafter" + TERMINATOR
    assert project(stream, View.SUPERSEDED).text == TERMINATOR


def test_the_spec_worked_examples_a_to_e_are_all_pinned_somewhere():
    """A here, B to E by the 0a sidecars, so "every worked example" cannot quietly
    shrink to the fixture-backed ones."""
    clauses = {"A"} | {
        label.clause
        for name in MODEL_NAMES
        for label in labels.load_sidecar(MODEL / f"{name}.expected.json").paragraphs
    }
    assert sorted(clauses & set(WORKED_EXAMPLES)) == list(WORKED_EXAMPLES)


# --- the 0a literals (§6, §7) ---------------------------------------------------

@pytest.mark.parametrize("name", MODEL_NAMES)
def test_model_sidecar_paragraph_views_are_the_hand_typed_literals(name):
    sidecar = labels.load_sidecar(MODEL / f"{name}.expected.json")
    stream = _model_stream(name)
    paragraphs = _paragraph_slices(stream)
    assert len(paragraphs) == len(sidecar.paragraphs)
    for (start, end, _), label in zip(paragraphs, sidecar.paragraphs):
        assert stream.text[start:end] == label.union
        for view, expected in zip(VIEWS, (label.accepted, label.original, label.superseded)):
            assert project(stream, view, start, end).text == expected, (name, label.index, view)


@pytest.mark.parametrize("name", MODEL_NAMES)
def test_model_sidecar_whole_part_projection_joins_the_literals_with_terminators(name):
    """§7: the whole-part projection is the per-paragraph view texts joined by the
    kept terminators -- a paragraph a view drops entirely still contributes its line."""
    sidecar = labels.load_sidecar(MODEL / f"{name}.expected.json")
    stream = _model_stream(name)
    for view, column in zip(
        VIEWS,
        (
            [label.accepted for label in sidecar.paragraphs],
            [label.original for label in sidecar.paragraphs],
            [label.superseded for label in sidecar.paragraphs],
        ),
    ):
        expected = "".join(text + TERMINATOR for text in column)
        assert project(stream, view).text == expected, (name, view)


def test_an_inserted_paragraph_deletion_leaves_an_empty_line_in_accepted():
    """The superseded text of ``nested_revisions`` is in neither view, and the
    paragraph it fills contributes its boundary alone."""
    stream = _model_stream("nested_revisions")
    assert project(stream, View.ACCEPTED).text == (
        "Coverage applies worldwide." + TERMINATOR + TERMINATOR
    )
    assert project(stream, View.SUPERSEDED).text == (
        "is excluded" + TERMINATOR + "Final clause" + TERMINATOR
    )


# --- the offset map (§7) --------------------------------------------------------

def test_the_offset_map_covers_the_union_the_view_elided():
    stream = _model_stream("nested_revisions")
    accepted = project(stream, View.ACCEPTED)
    span = accepted.union_range(0, 27)
    assert (span.part_id, span.start, span.end) == (stream.part_id, 0, 38)
    assert stream.text[span.start : span.end] == "Coverage is excludedapplies worldwide."
    assert accepted.text[0:27] == "Coverage applies worldwide."


def test_a_view_range_inside_one_run_maps_to_exactly_that_text():
    stream = _example_a()
    accepted = project(stream, View.ACCEPTED)
    assert accepted.union_range(9, 20) == Span(stream.part_id, 17, 28)
    assert stream.text[17:28] == accepted.text[9:20] == "subrogation"


def test_the_offset_map_refuses_ranges_it_cannot_address():
    accepted = project(_example_a(), View.ACCEPTED)
    with pytest.raises(ValueError):
        accepted.union_range(3, 3)
    with pytest.raises(ValueError):
        accepted.union_range(0, len(accepted.text) + 1)


@pytest.mark.parametrize("path", ALL_DOCX, ids=lambda p: p.name)
def test_projection_runs_tile_the_view_text_and_map_back_to_it(path):
    """Every retained span is one run, the runs tile the view text exactly, and a run's
    text is the union's at the offset it maps to."""
    for stream in union_streams(opc.Package(path)):
        for view in VIEWS:
            projection = project(stream, view)
            assert isinstance(projection, Projection)
            offset = 0
            for run in projection.runs:
                assert run.view_start == offset, (stream.part_id, view, run)
                assert run.view_end > run.view_start
                assert run.union_start + (run.view_end - run.view_start) <= len(stream.text)
                assert (
                    stream.text[run.union_start : run.union_start + (run.view_end - run.view_start)]
                    == projection.text[run.view_start : run.view_end]
                )
                offset = run.view_end
            assert offset == len(projection.text), (stream.part_id, view)


@pytest.mark.parametrize("path", ALL_DOCX, ids=lambda p: p.name)
def test_a_range_inside_one_run_round_trips_through_the_map(path):
    for stream in union_streams(opc.Package(path)):
        for view in VIEWS:
            projection = project(stream, view)
            for run in projection.runs:
                for start, end in (
                    (run.view_start, run.view_end),
                    (run.view_start, run.view_start + 1),
                    (run.view_end - 1, run.view_end),
                ):
                    span = projection.union_range(start, end)
                    assert span.part_id == stream.part_id
                    assert stream.text[span.start : span.end] == projection.text[start:end]


@pytest.mark.parametrize("path", ALL_DOCX, ids=lambda p: p.name)
def test_gap_closing_never_reaches_across_a_paragraph(path):
    """A paragraph's own view text is what its own range projects, and the whole-part
    projection is those texts plus one kept terminator each -- in every view."""
    for stream in union_streams(opc.Package(path)):
        paragraphs = _paragraph_slices(stream)
        for view in VIEWS:
            joined = "".join(
                project(stream, view, start, end).text + TERMINATOR for start, end, _ in paragraphs
            )
            assert project(stream, view).text == joined, (stream.part_id, view)


@pytest.mark.parametrize("path", ALL_DOCX, ids=lambda p: p.name)
def test_projection_is_deterministic_for_a_package(path):
    assert _projections(path) == _projections(path)
