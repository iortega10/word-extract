"""Turn 2a: per-part union streams against the 0a hand-typed literals.

The walker is asked for nothing but the union stream. Paragraph extents are derived
*here*, out of the spans themselves -- a single-character span whose text is the
terminator and whose stack is empty ends a paragraph -- so this check never leans on
a paragraph API the walker does not have yet, and cannot agree with the walker by
sharing its paragraph logic.

The three view strings of a paragraph need the view projection and gap-closing
(Turn 3) and are deliberately not asserted here.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from wordextract import opc
from wordextract.evals import labels
from wordextract.model import UnionStream
from wordextract.walker import TERMINATOR, union_stream, union_streams, walk_package

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MODEL = FIXTURES / "model"
MODEL_NAMES = sorted(p.name[: -len(".expected.json")] for p in MODEL.glob("*.expected.json"))
SAMPLE = FIXTURES / "samples" / "review_sample.docx"
ALL_DOCX = sorted(FIXTURES.rglob("*.docx"))
REVISION_KINDS = ("ins", "del", "moveFrom", "moveTo")


def _paragraph_slices(stream: UnionStream) -> list[tuple[int, int, list]]:
    """The paragraph extents of ``stream`` as ``(start, end, spans)``.

    ``end`` is the paragraph's terminator offset, so ``stream.text[start:end]`` is the
    paragraph's union literal and the terminator itself is not in ``spans``.
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


def _span_tuples(stream: UnionStream, spans, start: int):
    """One paragraph's spans as the sidecar labels them: offsets relative to the
    paragraph's start, the terminator not among them."""
    return [
        (span.start - start, span.end - start, tuple(span.stack), stream.text[span.start : span.end])
        for span in spans
    ]


@pytest.mark.parametrize("path", ALL_DOCX, ids=lambda p: p.name)
def test_spans_tile_every_union_exactly(path):
    """The elementary spans of a part tile its literal text with no gap and no overlap."""
    streams = union_streams(opc.Package(path))
    assert streams, path
    for stream in streams:
        assert stream.part_id and stream.text, stream.part_id
        offset = 0
        for span in stream.spans:
            assert span.start == offset, (stream.part_id, span)
            assert span.end > span.start, (stream.part_id, span)
            offset = span.end
        assert offset == len(stream.text), stream.part_id
        assert _paragraph_slices(stream), stream.part_id


@pytest.mark.parametrize("name", MODEL_NAMES)
def test_model_sidecar_union_literals_and_spans(name):
    """Every 0a literal paragraph: its union text, offsets, stacks and span text."""
    sidecar = labels.load_sidecar(MODEL / f"{name}.expected.json")
    assert sidecar.terminator == TERMINATOR
    package = opc.Package(MODEL / f"{name}.docx")
    stream = union_streams(package)[0]
    assert stream.part_id == package.document.part_id, name
    paragraphs = _paragraph_slices(stream)
    assert len(paragraphs) == len(sidecar.paragraphs), name
    revisions = {label.id for label in sidecar.revisions if label.id}
    for index, ((start, end, spans), label) in enumerate(zip(paragraphs, sidecar.paragraphs)):
        assert label.index == index, name
        assert stream.text[start:end] == label.union, (name, index)
        assert _span_tuples(stream, spans, start) == [
            (span.start, span.end, span.stack, span.text) for span in label.spans
        ], (name, index)
        for span in spans:
            for ident in span.stack:
                kind = ident.split(":", 1)[0]
                assert kind in REVISION_KINDS, (name, ident)
                assert ident in revisions, (name, ident, sorted(revisions))


@pytest.mark.parametrize("name", MODEL_NAMES)
def test_paragraph_terminators_are_empty_stack_and_their_own_span(name):
    """One terminator per paragraph, empty stack, never coalesced with content."""
    stream = union_streams(opc.Package(MODEL / f"{name}.docx"))[0]
    terminators = [
        span for span in stream.spans if stream.text[span.start : span.end] == TERMINATOR
    ]
    assert len(terminators) == len(_paragraph_slices(stream)), name
    for span in terminators:
        assert span.stack == [], (name, span)
        assert span.end - span.start == 1, (name, span)
    assert stream.text.count(TERMINATOR) == len(terminators), name


def test_a_part_without_a_paragraph_has_no_stream():
    package = opc.Package(MODEL / "empty_parts.docx")
    assert union_stream(None) is None
    assert union_stream(package.footnotes) is None
    assert union_stream(package.endnotes) is None
    assert [stream.part_id for stream in union_streams(package)] == [package.document.part_id]


def test_separator_only_note_parts_still_yield_their_terminators():
    """An existing part with no text is not an absent part: it addresses its breaks."""
    package = opc.Package(SAMPLE)
    footnotes = union_stream(package.footnotes)
    assert footnotes is not None
    assert footnotes.text == TERMINATOR * 2, footnotes.text
    assert package.footnotes.has_text is False
    endnotes = union_stream(package.endnotes)
    assert endnotes is not None
    assert endnotes.text == TERMINATOR * 2, endnotes.text
    assert package.endnotes.has_text is False
    assert union_streams(package)[0].part_id == package.document.part_id


def test_comments_part_is_addressed_per_part():
    """A comment's own text is union text in the comments part, never in the document."""
    package = opc.Package(MODEL / "comment_in_deletion.docx")
    part_ids = [stream.part_id for stream in union_streams(package)]
    assert package.comments is not None
    assert part_ids == [package.document.part_id, package.comments.part_id]


def test_text_box_content_is_not_in_any_host_stream():
    """``w:txbxContent`` lives in its own fragment, so host order is undisturbed."""
    streams = union_streams(opc.Package(MODEL / "text_box.docx"))
    assert not any("box text" in stream.text for stream in streams), [s.text for s in streams]


def test_strict_namespaces_get_the_same_union():
    """A strict package is not a different text model (producer-variance site 2)."""
    strict = union_streams(opc.Package(MODEL / "strict_namespaces.docx"))[0]
    sidecar = labels.load_sidecar(MODEL / "strict_namespaces.expected.json")
    assert strict.text == "".join(label.union + TERMINATOR for label in sidecar.paragraphs)


# --- gaps are reported, never silent -------------------------------------------


def _gaps(name: str) -> list[str]:
    return walk_package(opc.Package(MODEL / f"{name}.docx"))[1]


@pytest.mark.parametrize("name", MODEL_NAMES)
def test_walker_gaps_are_the_ones_the_sidecar_names_for_walker_owned_ids(name):
    """Only the ids the 2a walker owns are compared; later slices own the rest."""
    owned = {"textbox", "unrecognized_container", "revision_missing_id"}
    sidecar = labels.load_sidecar(MODEL / f"{name}.expected.json")
    assert set(_gaps(name)) == owned & set(sidecar.known_gaps), name


def test_a_text_box_is_recorded_as_a_gap_not_dropped_silently():
    assert _gaps("text_box") == ["textbox"]


def test_an_unknown_container_that_holds_text_is_recorded():
    package = opc.Package(MODEL / "unrecognized_container.docx")
    streams, gaps = walk_package(package)
    assert gaps == ["unrecognized_container"]
    assert "HIDDEN" not in streams[0].text


def test_a_revision_without_an_id_gets_a_deterministic_id_and_a_gap():
    package = opc.Package(MODEL / "revision_missing_id.docx")
    streams, gaps = walk_package(package)
    assert gaps == ["revision_missing_id"]
    assert [span.stack for span in streams[0].spans[:3]] == [[], ["ins:noid0"], ["del:noid1"]]
    # deterministic: a second walk yields the same ids
    again, _ = walk_package(opc.Package(MODEL / "revision_missing_id.docx"))
    assert again[0].spans == streams[0].spans


@pytest.mark.parametrize(
    "name", ["nested_field_in_instruction", "transparent_containers", "nested_revisions", "move"]
)
def test_handled_constructs_report_no_gap(name):
    assert _gaps(name) == []


def test_nested_field_result_inside_an_outer_instruction_is_not_content():
    """Every open field must have passed its separate (spec 6E)."""
    stream = union_streams(opc.Package(MODEL / "nested_field_in_instruction.docx"))[0]
    assert "INNER-RESULT-IN-OUTER-INSTRUCTION" not in stream.text
    assert stream.text.startswith("A OUTER-RESULT Z" + TERMINATOR)


def test_the_underwriting_sample_and_older_fixtures_report_no_unrecognized_container():
    """No real-shaped document loses text to the allow-list."""
    for path in ALL_DOCX:
        _, gaps = walk_package(opc.Package(path))
        if path.name != "unrecognized_container.docx":
            assert "unrecognized_container" not in gaps, (path.name, gaps)
        if path.name != "revision_missing_id.docx":
            assert "revision_missing_id" not in gaps, (path.name, gaps)
