"""Turn 2a: per-part union streams against the 0a hand-typed literals.

The walker is asked for nothing but the union stream. Paragraph extents are derived
*here*, out of the spans themselves -- a single-character span whose text is the
terminator and whose stack is empty ends a paragraph -- so this check never leans on
a paragraph API the walker does not have yet, and cannot agree with the walker by
sharing its paragraph logic.

The three view strings of a paragraph need the view projection and gap-closing
(Turn 3) and are deliberately not asserted here.

Turn 2b adds the node tree -- kinds, spans, ``child_ids``, ids and their fallbacks.
Every 2b claim is re-derived here either from the node's own ``source_ref`` (the part's
XML, read through the namespace-canonicalizing helpers) or from the union stream, so a
check can never agree with the walker by sharing its code.
"""
from __future__ import annotations

import re
import zipfile
from collections import Counter
from pathlib import Path

import pytest

from docextract_core import sha256_json

from wordextract import opc, walker as walker_mod
from wordextract.evals import labels
from wordextract.model import IdStability, NodeKind, UnionStream
from wordextract.walker import (
    TERMINATOR,
    union_stream,
    union_streams,
    walk_document,
    walk_package,
)

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
        (
            span.start - start,
            span.end - start,
            tuple(span.stack),
            stream.text[span.start : span.end],
        )
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


#: The known-gap ids the walker itself can report (2a + 2b). Every other id a sidecar
#: names belongs to a later slice -- the walker has no code path that produces it.
WALKER_OWNED_GAPS = {
    "textbox",
    "unrecognized_container",
    "revision_missing_id",
    "inline_sdt_transparent",
    "duplicate_content_id_churn",
}


@pytest.mark.parametrize("name", MODEL_NAMES)
def test_walker_gaps_are_the_ones_the_sidecar_names_for_walker_owned_ids(name):
    """A subset: only the ids the walker owns are compared; later slices own the rest.

    A sidecar's ``known_gaps`` documents what its document is *about*, so a fixture may
    name a hazard its own content does not trigger (``mixed_para_ids`` names the churn
    its two fallbacks could cause). The reverse never holds: the walker may not report
    a gap the fixture does not declare, nor one it has no code path for.
    """
    sidecar = labels.load_sidecar(MODEL / f"{name}.expected.json")
    assert set(_gaps(name)) <= WALKER_OWNED_GAPS & set(sidecar.known_gaps), name


def test_the_corpus_reports_every_gap_id_the_walker_owns():
    """Ownership is earned per id: some fixture in the corpus has to report it."""
    reported = set()
    for path in ALL_DOCX:
        reported |= set(walk_package(opc.Package(path))[1])
    assert reported == WALKER_OWNED_GAPS


def test_the_walker_can_only_report_ids_this_file_counts_as_its_own():
    constants = {name: value for name, value in vars(walker_mod).items() if name.startswith("GAP_")}
    assert set(constants.values()) == WALKER_OWNED_GAPS


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


# --- 2b: packages built here, so a claim is testable without a Word file -------

TEXT_KINDS = frozenset({NodeKind.PARA, NodeKind.HEADING, NodeKind.LIST_ITEM})
#: The kinds with a span of their own: a paragraph slice, or a cell's own content.
ADDRESSING_KINDS = TEXT_KINDS | {NodeKind.CELL}
RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
W_ATTRS = f'xmlns:w="{opc.W_NS}" xmlns:w14="{opc.W14_NS}" xmlns:r="{opc.R_NS}"'


def _docx(tmp_path: Path, body: str, *, numbering: str | None = None, name: str = "synth.docx"):
    """A minimal, valid package: only the parts the walker reads, nothing else.

    A part is resolved by relationship type plus existence and XML is parsed by its
    extension, so ``word/numbering.xml`` needs no ``[Content_Types].xml`` override --
    and nothing beyond this body can influence what a walk does.
    """
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
        "word/_rels/document.xml.rels": (
            f'<Relationships xmlns="{RELS_NS}">'
            + (
                f'<Relationship Id="rId2" Type="{opc.RT_NUMBERING}" Target="numbering.xml"/>'
                if numbering is not None
                else ""
            )
            + "</Relationships>"
        ),
        "word/document.xml": f"<w:document {W_ATTRS}><w:body>{body}</w:body></w:document>",
    }
    if numbering is not None:
        members["word/numbering.xml"] = f"<w:numbering {W_ATTRS}>{numbering}</w:numbering>"
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as archive:
        for member, data in members.items():
            archive.writestr(member, data.encode("utf-8"))
    return path


def _p(
    text: str,
    *,
    para_id: str | None = None,
    num_id: str | None = None,
    ilvl: int = 0,
    style: str | None = None,
) -> str:
    """One ``w:p``, optionally carrying Word's own paraId, a numPr or a pStyle."""
    properties = ""
    if style is not None:
        properties += f'<w:pStyle w:val="{style}"/>'
    if num_id is not None:
        properties += f'<w:numPr><w:ilvl w:val="{ilvl}"/><w:numId w:val="{num_id}"/></w:numPr>'
    attrs = f' w14:paraId="{para_id}"' if para_id is not None else ""
    pre = f"<w:pPr>{properties}</w:pPr>" if properties else ""
    return f"<w:p{attrs}>{pre}<w:r><w:t>{text}</w:t></w:r></w:p>"


def _lvl(ilvl: int, fmt: str, text: str, start: int = 1) -> str:
    return (
        f'<w:lvl w:ilvl="{ilvl}"><w:start w:val="{start}"/><w:numFmt w:val="{fmt}"/>'
        f'<w:lvlText w:val="{text}"/></w:lvl>'
    )


def _abstract_num(ident: str, *levels: str) -> str:
    return f'<w:abstractNum w:abstractNumId="{ident}">' + "".join(levels) + "</w:abstractNum>"


def _num(num_id: str, abstract: str, *overrides: str) -> str:
    return (
        f'<w:num w:numId="{num_id}"><w:abstractNumId w:val="{abstract}"/>'
        + "".join(overrides)
        + "</w:num>"
    )


def _synth_labels(tmp_path: Path, body: str, numbering: str) -> list:
    package = opc.Package(_docx(tmp_path, body, numbering=numbering))
    return walk_document(package).nodes


# --- numbering labels ----------------------------------------------------------


def test_the_sample_bullets_are_list_items_with_a_bullet_label():
    """Word's own bullet list: one bullet label per item, no level on the paragraph."""
    items = [n for n in walk_document(opc.Package(SAMPLE)).nodes if n.kind is NodeKind.LIST_ITEM]
    assert len(items) == 8
    assert {n.numbering_label for n in items} == {"\u2022"}
    assert {n.style for n in items} == {"ListParagraph"}
    assert {n.level for n in items} == {None}
    assert all(len(n.spans) == 1 and n.spans[0].end > n.spans[0].start for n in items)


def test_the_label_counts_decimally_and_nests_by_level(tmp_path):
    """``%1.%2`` reads the counters of levels 0 and 1; a shallower item resets deeper."""
    body = (
        _p("One", num_id="1")
        + _p("Two", num_id="1")
        + _p("Two one", num_id="1", ilvl=1)
        + _p("Three", num_id="1")
        + _p("Three one", num_id="1", ilvl=1)
    )
    numbering = _abstract_num("1", _lvl(0, "decimal", "%1."), _lvl(1, "decimal", "%1.%2")) + _num(
        "1", "1"
    )
    nodes = _synth_labels(tmp_path, body, numbering)
    assert [n.kind for n in nodes] == [NodeKind.LIST_ITEM] * 5
    assert [n.numbering_label for n in nodes] == ["1.", "2.", "2.1", "3.", "3.1"]


def test_w_start_and_a_start_override_set_the_first_label(tmp_path):
    """A level's ``w:start`` is where its counter starts; a ``w:lvlOverride`` replaces it."""
    body = _p("Five", num_id="2") + _p("Six", num_id="2") + _p("Seven", num_id="3")
    override = '<w:lvlOverride w:ilvl="0"><w:startOverride w:val="7"/></w:lvlOverride>'
    numbering = (
        _abstract_num("2", _lvl(0, "decimal", "%1.", start=5))
        + _num("2", "2")
        + _num("3", "2", override)
    )
    assert [n.numbering_label for n in _synth_labels(tmp_path, body, numbering)] == [
        "5.",
        "6.",
        "7.",
    ]


def test_a_lower_roman_format_renders_its_own_glyphs(tmp_path):
    body = _p("First", num_id="4") + _p("Second", num_id="4")
    numbering = _abstract_num("4", _lvl(0, "lowerRoman", "%1)")) + _num("4", "4")
    assert [n.numbering_label for n in _synth_labels(tmp_path, body, numbering)] == ["i)", "ii)"]


def test_a_num_fmt_of_none_and_an_unknown_num_id_both_lack_a_label(tmp_path):
    """Still a list item -- what is missing is the label, not the kind."""
    body = _p("Unnumbered", num_id="5") + _p("Unknown", num_id="99")
    numbering = _abstract_num("5", _lvl(0, "none", "%1.")) + _num("5", "5")
    nodes = _synth_labels(tmp_path, body, numbering)
    assert [n.kind for n in nodes] == [NodeKind.LIST_ITEM, NodeKind.LIST_ITEM]
    assert [n.numbering_label for n in nodes] == [None, None]


def test_a_num_id_of_zero_is_not_a_list_item(tmp_path):
    """``w:numId 0`` is Word's "no numbering", not a list to label."""
    numbering = _abstract_num("1", _lvl(0, "decimal", "%1.")) + _num("1", "1")
    nodes = _synth_labels(tmp_path, _p("Cleared", num_id="0"), numbering)
    assert [n.kind for n in nodes] == [NodeKind.PARA]
    assert nodes[0].numbering_label is None


# --- kinds, read back off each node's own XML ---------------------------------


#: A second reading of the kind rule, from locals rather than from the walker's tables.
KIND_BY_LOCAL = {
    "tbl": NodeKind.TABLE,
    "tr": NodeKind.ROW,
    "tc": NodeKind.CELL,
    "hdr": NodeKind.HEADER,
    "ftr": NodeKind.FOOTER,
    "footnote": NodeKind.FOOTNOTE,
    "endnote": NodeKind.FOOTNOTE,
    "sdt": NodeKind.SDT,
}

HEADING_STYLE = re.compile(r"heading\s*([1-9])", re.IGNORECASE)


def _w_child(element, local: str):
    """The element's own ``w:``-family child of this local name, strict or not."""
    if element is None:
        return None
    for child in element:
        if isinstance(child.tag, str) and opc.is_w(child, local):
            return child
    return None


def _w_val(element, local: str) -> str | None:
    child = _w_child(element, local)
    return None if child is None else opc.wattr(child, "val")


def _element_at(package: opc.Package, source_ref: str):
    """The XML element a ``source_ref`` points at: part name, then one step per component."""
    name, _, pointer = source_ref.partition("#")
    element = package.parts[name].tree
    for step in filter(None, pointer.split("/")):
        local, _, index = step.partition("[")
        element = [child for child in element if isinstance(child.tag, str)][int(index[:-1])]
        assert opc.local_name(element) == local, source_ref
    return element


def _kind_from_xml(element) -> NodeKind:
    """The kind rule read off the element itself: numPr, then style, then the local name."""
    local = opc.local_name(element)
    if local != "p":
        return KIND_BY_LOCAL[local]
    properties = _w_child(element, "pPr")
    num_id = _w_val(_w_child(properties, "numPr"), "numId")
    if num_id is not None and num_id != "0":
        return NodeKind.LIST_ITEM
    style = _w_val(properties, "pStyle")
    if style is not None and HEADING_STYLE.fullmatch(style):
        return NodeKind.HEADING
    return NodeKind.PARA


def test_every_node_kind_is_exercised_by_the_corpus():
    kinds = set()
    for path in ALL_DOCX:
        kinds |= {node.kind for node in walk_document(opc.Package(path)).nodes}
    assert kinds == set(NodeKind)


@pytest.mark.parametrize("path", ALL_DOCX, ids=lambda p: p.name)
def test_a_nodes_kind_is_re_derivable_from_its_own_source_ref(path):
    package = opc.Package(path)
    nodes = walk_document(package).nodes
    assert nodes, path
    for node in nodes:
        element = _element_at(package, node.source_ref)
        assert node.kind is _kind_from_xml(element), node.source_ref


# --- spans ---------------------------------------------------------------------


@pytest.mark.parametrize("path", ALL_DOCX, ids=lambda p: p.name)
def test_text_nodes_are_exactly_the_streams_paragraph_slices(path):
    """The paragraph-like nodes of a part tile its text, and nothing else does."""
    parsed = walk_document(opc.Package(path))
    for stream in parsed.union_streams:
        slices = sorted((start, end) for start, end, _ in _paragraph_slices(stream))
        got = sorted(
            (node.spans[0].start, node.spans[0].end)
            for node in parsed.nodes
            if node.part_id == stream.part_id and node.kind in TEXT_KINDS
        )
        assert got == slices, stream.part_id


@pytest.mark.parametrize("path", ALL_DOCX, ids=lambda p: p.name)
def test_only_the_addressing_kinds_have_spans_and_they_lie_in_their_own_part(path):
    parsed = walk_document(opc.Package(path))
    streams = {stream.part_id: stream for stream in parsed.union_streams}
    for node in parsed.nodes:
        if node.kind not in ADDRESSING_KINDS:
            assert node.spans == [], node.source_ref
            continue
        assert len(node.spans) == 1, node.source_ref
        (span,) = node.spans
        assert span.part_id == node.part_id, node.source_ref
        assert 0 <= span.start <= span.end <= len(streams[node.part_id].text), node.source_ref


def _text_leaves(by_id, node):
    """A node's own text nodes -- its descendants of a text kind, in document order."""
    if node.kind in TEXT_KINDS:
        return [node]
    leaves = []
    for child_id in node.child_ids:
        leaves.extend(_text_leaves(by_id, by_id[child_id]))
    return leaves


@pytest.mark.parametrize("path", ALL_DOCX, ids=lambda p: p.name)
def test_a_cell_addresses_exactly_the_text_under_it(path):
    """A cell's span is its content: first text offset to the last text end plus one."""
    parsed = walk_document(opc.Package(path))
    streams = {stream.part_id: stream for stream in parsed.union_streams}
    by_id = {node.id: node for node in parsed.nodes}
    for node in parsed.nodes:
        if node.kind is not NodeKind.CELL:
            continue
        (span,) = node.spans
        leaves = _text_leaves(by_id, node)
        if not leaves:
            assert span.start == span.end, node.source_ref
            continue
        text = streams[node.part_id].text
        offsets = [(leaf.spans[0].start, leaf.spans[0].end) for leaf in leaves]
        assert span.start == min(start for start, _ in offsets), node.source_ref
        assert span.end == max(end for _, end in offsets) + 1, node.source_ref
        # a cell's text is its children's text plus their terminators, nothing else
        assert span.end - span.start == sum(
            len(text[start:end]) + 1 for start, end in offsets
        ), node.source_ref


# --- child_ids ------------------------------------------------------------------


def _pointer(node) -> tuple[str, ...]:
    """A node's source_ref as its components: part-local steps, outermost first."""
    _, _, pointer = node.source_ref.partition("#")
    return tuple(filter(None, pointer.split("/")))


@pytest.mark.parametrize("path", ALL_DOCX, ids=lambda p: p.name)
def test_child_ids_are_the_nearest_source_ref_ancestors_in_document_order(path):
    """Parentage re-derived from ``source_ref`` alone: the nearest enclosing node."""
    nodes = walk_document(opc.Package(path)).nodes
    order = {node.id: index for index, node in enumerate(nodes)}
    pointers = {node.id: _pointer(node) for node in nodes}
    for node in nodes:
        mine = pointers[node.id]
        expected = []
        for other in nodes:
            if other.part_id != node.part_id or other.id == node.id:
                continue
            pointer = pointers[other.id]
            if len(pointer) <= len(mine) or pointer[: len(mine)] != mine:
                continue
            ancestors = [
                candidate
                for candidate in nodes
                if candidate.part_id == node.part_id
                and candidate.id != other.id
                and len(pointers[candidate.id]) < len(pointer)
                and pointer[: len(pointers[candidate.id])] == pointers[candidate.id]
            ]
            if ancestors and max(ancestors, key=lambda c: len(pointers[c.id])).id == node.id:
                expected.append(other.id)
        expected.sort(key=order.__getitem__)
        assert node.child_ids == expected, node.source_ref


@pytest.mark.parametrize("path", ALL_DOCX, ids=lambda p: p.name)
def test_ids_are_unique_and_the_tree_is_one_forest_of_children(path):
    nodes = walk_document(opc.Package(path)).nodes
    assert len({node.id for node in nodes}) == len(nodes), path
    children = {node.id: node.child_ids for node in nodes}
    parents = Counter(child_id for child_ids in children.values() for child_id in child_ids)
    assert all(count == 1 for count in parents.values()), path
    seen = set()
    roots = [node.id for node in nodes if node.id not in parents]
    assert roots, path
    pending = list(roots)
    while pending:
        ident = pending.pop()
        assert ident not in seen, "a node is reachable from two roots"
        seen.add(ident)
        pending.extend(children[ident])
    assert seen == set(children), "an orphan is in no root's tree"


@pytest.mark.parametrize("path", ALL_DOCX, ids=lambda p: p.name)
def test_a_childs_text_lies_inside_its_parents_and_in_order(path):
    """A node with an address addresses its descendants' text: in order, and only it."""
    parsed = walk_document(opc.Package(path))
    by_id = {node.id: node for node in parsed.nodes}
    for node in parsed.nodes:
        if not node.spans:
            continue
        (span,) = node.spans
        offset = span.start
        for child_id in node.child_ids:
            leaves = _text_leaves(by_id, by_id[child_id])
            if not leaves:
                continue
            assert leaves[0].spans[0].start >= offset, node.source_ref
            offset = leaves[-1].spans[0].end
            assert offset <= span.end, node.source_ref


# --- ids and their fallbacks ----------------------------------------------------


def _node_text(node, streams, by_id) -> str:
    """The text the id recipe hashes: a text node's span, a container's content."""
    if node.spans:
        stream = streams[node.part_id]
        return stream.text[node.spans[0].start : node.spans[0].end]
    leaves = _text_leaves(by_id, node)
    if not leaves:
        return ""
    stream = streams[node.part_id]
    return stream.text[leaves[0].spans[0].start : leaves[-1].spans[0].end + 1]


@pytest.mark.parametrize("path", ALL_DOCX, ids=lambda p: p.name)
def test_every_id_is_re_derived_from_para_id_then_content_hash_then_path(path):
    """The id recipe, recomputed: Word's paraId, else a content hash, else the ref."""
    package = opc.Package(path)
    parsed = walk_document(package)
    streams = {stream.part_id: stream for stream in parsed.union_streams}
    by_id = {node.id: node for node in parsed.nodes}
    used: set[str] = set()
    counters: dict[tuple[str, str], int] = {}
    for node in parsed.nodes:
        para_id = _element_at(package, node.source_ref).get(f"{{{opc.W14_NS}}}paraId") or None
        text = _node_text(node, streams, by_id)
        if para_id is not None and para_id not in used:
            ident, stability, occurrence = para_id, IdStability.PARAID, 0
        elif text:
            base = sha256_json([node.part_id, node.kind.value, text, node.style, node.level])
            occurrence = counters.get((node.part_id, base), 0)
            counters[(node.part_id, base)] = occurrence + 1
            ident, stability = f"hash:{base}:{occurrence}", IdStability.CONTENT_HASH
        else:
            ident, stability, occurrence = f"path:{node.source_ref}", IdStability.PATH, 0
        used.add(ident)
        assert (node.id, node.id_stability, node.occurrence_index) == (
            ident,
            stability,
            occurrence,
        ), node.source_ref


def test_the_mixed_para_ids_sidecar_names_the_fallback_of_each_paragraph():
    sidecar = labels.load_sidecar(MODEL / "mixed_para_ids.expected.json")
    nodes = walk_document(opc.Package(MODEL / "mixed_para_ids.docx")).nodes
    expected = {entry["index"]: entry for entry in sidecar.annotations["id_expectations"]}
    assert sorted(expected) == list(range(len(nodes)))
    for index, node in enumerate(nodes):
        entry = expected[index]
        if entry["fallback"] == "paraid":
            assert (node.id, node.id_stability) == (entry["para_id"], IdStability.PARAID)
        else:
            assert (entry["para_id"], entry["fallback"]) == (None, "content_hash")
            assert node.id.startswith("hash:") and node.id_stability is IdStability.CONTENT_HASH


def test_mixed_para_ids_names_the_churn_its_own_content_does_not_trigger():
    """The fixture documents the hazard; its two fallbacks differ, so nothing churns."""
    parsed = walk_document(opc.Package(MODEL / "mixed_para_ids.docx"))
    assert "duplicate_content_id_churn" not in parsed.known_gaps
    fallbacks = [n for n in parsed.nodes if n.id_stability is IdStability.CONTENT_HASH]
    assert len(fallbacks) == 2
    assert {n.id.rsplit(":", 1)[0] for n in fallbacks} != {fallbacks[0].id.rsplit(":", 1)[0]}


def test_a_repeated_para_id_falls_through_to_the_content_hash(tmp_path):
    """A paraId already spent is not reused: the second paragraph hashes instead."""
    body = _p("First.", para_id="00000001") + _p("Second.", para_id="00000001")
    parsed = walk_document(opc.Package(_docx(tmp_path, body)))
    first, second = parsed.nodes
    assert (first.id, first.id_stability) == ("00000001", IdStability.PARAID)
    assert second.id.startswith("hash:") and second.id_stability is IdStability.CONTENT_HASH
    assert second.occurrence_index == 0
    # distinct texts, so the two ids are distinct and no churn is claimed
    assert len({first.id, second.id}) == 2
    assert parsed.known_gaps == []


def test_duplicate_content_ids_churn_and_the_later_node_is_ordinal_one(tmp_path):
    body = _p("Same text.") + _p("Same text.")
    parsed = walk_document(opc.Package(_docx(tmp_path, body)))
    first, second = parsed.nodes
    assert first.id.endswith(":0") and second.id.endswith(":1")
    base = first.id[: -len(":0")]
    assert second.id == f"{base}:1"
    assert parsed.known_gaps == ["duplicate_content_id_churn"]


def test_inserting_a_duplicate_ahead_moves_the_later_nodes_id(tmp_path):
    """The ordinal is document order, so a node's content-hash id is not stable
    against an equal-content node being inserted ahead of it -- which is the churn."""
    two = _p("Repeated.") + _p("Repeated.")

    def ids(body: str, name: str) -> list[str]:
        package = opc.Package(_docx(tmp_path, body, name=name))
        return [node.id for node in walk_document(package).nodes]

    before = ids(two, "two.docx")
    assert before[0].endswith(":0") and before[1].endswith(":1")
    base = before[0][: -len(":0")]
    after = ids(_p("Repeated.") + two, "three.docx")
    assert after == [f"{base}:0", f"{base}:1", f"{base}:2"]
    # the paragraph that read :1 before the insertion now reads :2
    assert before[1] != after[2]


def test_the_sample_churns_on_words_own_footnote_separator_stubs():
    """Word writes an empty separator and continuationSeparator note in every note
    part: two notes of identical content, so their content-hash ids collide."""
    package = opc.Package(SAMPLE)
    parsed = walk_document(package)
    assert parsed.known_gaps == ["duplicate_content_id_churn"]
    for part in (package.footnotes, package.endnotes):
        assert part is not None and part.has_text is False
        notes = [
            node
            for node in parsed.nodes
            if node.part_id == part.part_id and node.kind is NodeKind.FOOTNOTE
        ]
        assert len(notes) == 2
        assert sorted(node.occurrence_index for node in notes) == [0, 1]
        assert len({node.id for node in notes}) == 2
        assert len({node.id.rsplit(":", 1)[0] for node in notes}) == 1


# --- content controls -----------------------------------------------------------


def test_a_block_content_control_is_a_node_and_an_inline_one_is_transparent():
    package = opc.Package(MODEL / "content_controls.docx")
    parsed = walk_document(package)
    assert parsed.known_gaps == ["inline_sdt_transparent"]
    # the inline control is transparent: it leaves no node and no boundary, so the
    # paragraph it sits in reads as one uninterrupted literal
    document = next(s for s in parsed.union_streams if s.part_id == package.document.part_id)
    inline = next(
        node for node in parsed.nodes if node.source_ref == f"{package.document.name}#body[0]/p[0]"
    )
    assert document.text[inline.spans[0].start : inline.spans[0].end] == "Before inline text after."
    # the block control is a node of its own, addressing no text but owning its content
    controls = [node for node in parsed.nodes if node.kind is NodeKind.SDT]
    assert len(controls) == 1
    (control,) = controls
    assert control.spans == []
    assert len(control.child_ids) == 1
    child = next(node for node in parsed.nodes if node.id == control.child_ids[0])
    assert child.kind is NodeKind.PARA
    assert document.text[child.spans[0].start : child.spans[0].end] == "Inside the content control."


# --- determinism ----------------------------------------------------------------


@pytest.mark.parametrize("path", ALL_DOCX, ids=lambda p: p.name)
def test_walking_the_same_package_twice_is_identical(path):
    first = walk_document(opc.Package(path))
    second = walk_document(opc.Package(path))
    assert first.known_gaps == second.known_gaps
    assert first.union_streams == second.union_streams
    assert first.nodes == second.nodes


# --- the gap doc and the code agree --------------------------------------------


def test_the_gap_doc_documents_exactly_the_walker_owned_ids():
    text = (Path(__file__).resolve().parents[1] / "docs" / "design" / "phase1-gaps.md").read_text(
        encoding="utf-8"
    )
    owned, _, declared = text.partition("## Declared by fixtures")
    assert declared, "the doc keeps the two sections apart"
    documented = set(re.findall(r"^- \*\*`([a-z_]+)`\*\*", owned, re.MULTILINE))
    assert documented == WALKER_OWNED_GAPS
    for gap in WALKER_OWNED_GAPS:
        assert f"`{gap}`" not in declared, gap
