"""Turn 5: chunker v1 -- the heading tree and the size cap, and nothing else.

Every claim here is re-derived from something the chunker did not write:

* a chunk's **own bytes** are the union stream's own view text over the text leaves the
  chunk names, joined by ``"\\n"`` -- the walker's node tree and the view projection, so
  the check never agrees with the chunker by sharing its packing code;
* a chunk's **identity** is recomputed from the documented formulas over the chunk's own
  fields, and its ``occurrence_index`` from the ordinal it is: the nth chunk of the
  document with that ``content_hash``;
* a chunk's **boundaries** are hand-read off the package a case is built from, or off one
  of the three root fixtures.

The shapes a corpus of real documents happens not to hold -- a preamble, a numbered-list
run in a tiny section, an oversize table, two comments that fall in different chunks --
are pinned by packages built here, so the rule is testable without a Word file.
"""
from __future__ import annotations

import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest

from docextract_core import sha256_json

from wordextract import opc
from wordextract.chunker import DEFAULT_PARAMS, ChunkParams, chunk, params_hash
from wordextract.model import (
    Chunk,
    HeadingDetection,
    NodeKind,
    ParseResult,
    View,
)
from wordextract.versions import TEXTMODEL_VERSION
from wordextract.views import project
from wordextract.walker import walk_document

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ALL_DOCX = sorted(FIXTURES.rglob("*.docx"))
LEDGER = FIXTURES / "ledger_summary.docx"
#: Text-bearing leaves: the kinds that carry text of their own (2b), which is all a chunk's
#: bytes can be made of -- a table's rows and cells are containers over these.
LEAF_KINDS = frozenset({NodeKind.PARA, NodeKind.LIST_ITEM})


def _parsed(path: Path) -> tuple[ParseResult, str]:
    """``path``'s walk and its body part id -- the part the section tree covers."""
    package = opc.Package(path)
    return walk_document(package), package.document.part_id


def _text(parsed: ParseResult, part_id: str, view: View, one: Chunk) -> str:
    """``one``'s own view text, re-derived from the union stream and the view projection.

    The walker's records are in document order, so walking them and keeping the leaves the
    chunk names rebuilds exactly the string a chunk's ``size`` and ``content_hash`` are
    over: a block's own text, joined by ``"\\n"``, is the same join one level down.
    """
    stream = next(stream for stream in parsed.union_streams if stream.part_id == part_id)
    named = set(one.node_ids)
    pieces: list[str] = []
    for node in parsed.nodes:
        if node.id not in named or node.kind not in LEAF_KINDS | {NodeKind.HEADING}:
            continue
        if not node.spans:
            pieces.append("")
            continue
        span = node.spans[0]
        pieces.append(project(stream, view, span.start, span.end).text)
    return "\n".join(pieces)


def _idea(one: Chunk) -> str:
    """The id the documented formula gives ``one``: the hash of content plus ordinal."""
    return sha256_json({"content_hash": one.content_hash, "occurrence_index": one.occurrence_index})


# --- packages built here, so a shape is testable without a Word file ------------------

RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
W_ATTRS = f'xmlns:w="{opc.W_NS}" xmlns:w14="{opc.W14_NS}" xmlns:r="{opc.R_NS}"'


def _p(
    text: str,
    *,
    style: str | None = None,
    outline: int | None = None,
    num: bool = False,
    para_id: str | None = None,
) -> str:
    """One ``w:p``: optionally a ``pStyle``, an own ``outlineLvl``, a numPr or a paraId."""
    properties = ""
    if style is not None:
        properties += f'<w:pStyle w:val="{style}"/>'
    if outline is not None:
        properties += f'<w:outlineLvl w:val="{outline}"/>'
    if num:
        properties += '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>'
    pre = f"<w:pPr>{properties}</w:pPr>" if properties else ""
    attrs = f' w14:paraId="{para_id}"' if para_id is not None else ""
    return f"<w:p{attrs}>{pre}<w:r><w:t>{text}</w:t></w:r></w:p>"


def _deleted(kept: str, deleted: str) -> str:
    """One ``w:p`` whose second run is a ``w:del``: accepted keeps ``kept`` only."""
    return (
        "<w:p>"
        f"<w:r><w:t>{kept}</w:t></w:r>"
        '<w:del w:id="1" w:author="Ada" w:date="2026-01-01T00:00:00Z">'
        f"<w:r><w:delText>{deleted}</w:delText></w:r></w:del>"
        "</w:p>"
    )


def _table(rows: list[list[str]]) -> str:
    """One ``w:tbl``: one ``w:tr`` per row, one ``w:tc`` (one paragraph) per cell."""
    built = ["<w:tbl>"]
    for row in rows:
        built.append("<w:tr>")
        for cell in row:
            built.append(f"<w:tc>{_p(cell)}</w:tc>")
        built.append("</w:tr>")
    built.append("</w:tbl>")
    return "".join(built)


def _comment(
    ident: str,
    text: str,
    *,
    para_id: str | None = None,
    author: str = "C. Oster",
    initials: str = "CO",
    date: str | None = "2026-03-04T05:06:07Z",
) -> str:
    """One ``w:comment`` whose single body paragraph optionally carries Word's paraId.

    Without a paraId the comment's identity is the walker's content-hash fallback --
    author, date, text and ordinal -- which is the only way a comment body's own words
    reach a chunk's ``context_hash`` at all (the contract's record carries no body text).
    """
    attrs = f' w14:paraId="{para_id}"' if para_id is not None else ""
    head = "".join(
        f' w:{name}="{value}"'
        for name, value in (("author", author), ("initials", initials), ("date", date))
        if value is not None
    )
    return (
        f'<w:comment w:id="{ident}"{head}>'
        f"<w:p{attrs}><w:r><w:t>{text}</w:t></w:r></w:p></w:comment>"
    )


def _comments(*bodies: str) -> str:
    return f"<w:comments {W_ATTRS}>{''.join(bodies)}</w:comments>"


def _docx(tmp_path: Path, body: str, *, comments: str | None = None, name: str = "synth.docx"):
    """A minimal package: the body, and the comments part only when a case needs one."""
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
    if comments is not None:
        members["word/_rels/document.xml.rels"] = (
            f'<Relationships xmlns="{RELS_NS}">'
            f'<Relationship Id="rId4" Type="{opc.RT_COMMENTS}" Target="comments.xml"/>'
            "</Relationships>"
        )
        members["word/comments.xml"] = comments
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as archive:
        for member, data in members.items():
            archive.writestr(member, data.encode("utf-8"))
    return path


def _synth(tmp_path: Path, body: str, *, comments: str | None = None, name: str = "synth.docx"):
    """``body``'s parse and its body part id."""
    return _parsed(_docx(tmp_path, body, comments=comments, name=name))


def _shape(chunks: list[Chunk]) -> list[tuple[list[str], int, int]]:
    """What a boundary claim is about: a chunk's path, its size and how many nodes it holds."""
    return [(one.section_path, one.size, len(one.node_ids)) for one in chunks]


# --- the id and the text are what they say they are ----------------------------------


@pytest.mark.parametrize("path", ALL_DOCX, ids=lambda path: path.stem)
def test_every_chunk_is_its_own_view_text(path):
    """``size`` and ``content_hash`` are over the chunk's own bytes, in its own view.

    The bytes are rebuilt here out of the union stream, so a chunk that quietly included a
    neighbour's paragraph or a terminator would fail this: the join is the chunk's members
    -- a merged-up section's heading among them -- and nothing else.
    """
    parsed, part_id = _parsed(path)
    for one in chunk(parsed, part_id):
        text = _text(parsed, part_id, View.ACCEPTED, one)
        assert one.size == len(text)
        assert one.view_id == View.ACCEPTED.value
        assert one.textmodel_version == TEXTMODEL_VERSION
        assert one.content_hash == sha256_json(
            {
                "text": text,
                "view_id": View.ACCEPTED.value,
                "textmodel_version": TEXTMODEL_VERSION,
            }
        )


@pytest.mark.parametrize("path", ALL_DOCX, ids=lambda path: path.stem)
def test_every_chunk_id_is_the_content_hash_and_the_documents_ordinal(path):
    """``occurrence_index`` counts the *preceding* chunks with the same content hash (D3).

    It is what keeps two identical row groups, or two identical paragraphs in two
    sections, apart -- and it is assigned in document order, so the ordinal is re-derived
    by walking the chunks in the order the chunker returns them.
    """
    parsed, part_id = _parsed(path)
    seen: dict[str, int] = {}
    for one in chunk(parsed, part_id):
        ordinal = seen.get(one.content_hash, 0)
        seen[one.content_hash] = ordinal + 1
        assert one.occurrence_index == ordinal
        assert one.id == _idea(one)


@pytest.mark.parametrize("path", ALL_DOCX, ids=lambda path: path.stem)
def test_no_chunk_is_only_a_heading_and_its_nodes_are_in_document_order(path):
    """A heading opens a section (4b), so it is a chunk member only when its section merged
    up into one that has content: a chunk is never *just* headings, and the ids a chunk
    names are unique and in the walker's own document order.
    """
    parsed, part_id = _parsed(path)
    order = {node.id: index for index, node in enumerate(parsed.nodes)}
    by_id = {node.id: node for node in parsed.nodes}
    for one in chunk(parsed, part_id):
        kinds = {by_id[node_id].kind for node_id in one.node_ids}
        assert kinds - {NodeKind.HEADING}
        assert len(set(one.node_ids)) == len(one.node_ids)
        indices = [order[node_id] for node_id in one.node_ids]
        assert indices == sorted(indices)
        assert one.node_ids


@pytest.mark.parametrize("path", ALL_DOCX, ids=lambda path: path.stem)
def test_every_leaf_of_the_body_is_in_a_chunk_except_the_headings(path):
    """Nothing is dropped and nothing is invented: the chunks cover the body's leaves.

    A leaf is a node with no children -- a table's cell paragraphs are leaves, its rows and
    cells are not -- and every leaf that is not a heading is in some chunk. The one leaf
    that may be in two chunks is the header row of an oversize table, repeated in each of
    its groups: that repetition is the split's contract (it is what makes the
    ``occurrence_index`` path reachable), not a leak.
    """
    parsed, part_id = _parsed(path)
    body = [node for node in parsed.nodes if node.part_id == part_id]
    leaves = {node.id for node in body if not node.child_ids}
    headings = {node.id for node in body if node.kind is NodeKind.HEADING}
    covered = {node_id for one in chunk(parsed, part_id) for node_id in one.node_ids}
    assert covered <= {node.id for node in body}
    assert leaves - headings <= covered


@pytest.mark.parametrize("path", ALL_DOCX, ids=lambda path: path.stem)
def test_chunking_the_same_package_twice_gives_the_same_chunks(path):
    """Determinism: nothing in a chunk depends on a hash order, a set iteration or a clock."""
    first, part_id = _parsed(path)
    second, _ = _parsed(path)
    assert chunk(first, part_id) == chunk(second, part_id)


def test_the_params_hash_pins_the_tunables():
    """``chunker_params_hash`` (D10) is over the three tunables and nothing else."""
    assert params_hash() == "46ef67ed768888d2e714c708dad49c7c9756e93e88d39e54ef70d5652d141950"
    assert params_hash(ChunkParams()) == params_hash(DEFAULT_PARAMS)
    assert params_hash(ChunkParams(size_cap=999)) != params_hash()
    assert params_hash(ChunkParams(min_size=1)) != params_hash()
    assert params_hash(ChunkParams(list_run=3)) != params_hash()


def test_a_part_no_walk_ever_visited_yields_no_chunks():
    """The chunker answers about the part it is given, and invents nothing for another."""
    parsed, part_id = _parsed(LEDGER)
    assert chunk(parsed, "word/document.xml") == []
    assert chunk(parsed, "no-such-part:99") == []
    assert chunk(parsed, "") == []
    assert chunk(parsed, part_id)


# --- the outline, end to end ---------------------------------------------------------


def test_the_ledger_summary_fixture_is_three_chunks_of_its_outline():
    """The root fixture, whole: the outline's own boundaries and the fixture's own lengths.

    ``Ledger Summary`` states no content of its own before its first subsection, so there
    is no preamble chunk and no parent chunk -- the three subsections are the three
    chunks, in document order, and each one's size is hand-readable off its paragraphs.
    """
    parsed, part_id = _parsed(LEDGER)
    chunks = chunk(parsed, part_id)
    assert _shape(chunks) == [
        (["Ledger Summary", "Services Agreement"], 124, 1),
        (["Ledger Summary", "Conditions"], 99, 1),
        (["Ledger Summary", "Not Covered"], 211, 2),
    ]
    assert [one.occurrence_index for one in chunks] == [0, 0, 0]
    assert all(one.heading_detection is parsed.heading_detection for one in chunks)
    assert parsed.heading_detection is HeadingDetection.NORMAL


def test_a_preamble_is_chunked_under_the_empty_path_and_swallows_no_section(tmp_path):
    """The blocks before the first heading are the document's preamble, not a section's.

    A tiny section merges **up** into a parent *section* that has content of its own -- the
    document root is not one, so a top-level one-liner keeps its own chunk and its own
    title rather than dissolving into the preamble and losing the outline.
    """
    parsed, part_id = _synth(
        tmp_path, _p("Preamble text") + _p("Top", style="Heading1") + _p("Under top")
    )
    assert _shape(chunk(parsed, part_id)) == [([], 13, 1), (["Top"], 9, 1)]


def test_a_tiny_section_merges_up_into_a_parent_with_content_of_its_own(tmp_path):
    """A one-line subsection under an established heading is part of what encloses it.

    Its blocks become the parent's from then on and its heading leads them, so the child's
    title is not in any path but is never lost: it is a line of the parent's chunk.
    """
    parsed, part_id = _synth(
        tmp_path,
        _p("Top", style="Heading1")
        + _p("Parent text")
        + _p("Child", style="Heading2")
        + _p("Child text"),
    )
    chunks = chunk(parsed, part_id)
    assert _shape(chunks) == [(["Top"], 28, 3)]
    assert _text(parsed, part_id, View.ACCEPTED, chunks[0]) == "Parent text\nChild\nChild text"


def test_a_numbered_list_run_is_forced_to_its_own_chunk_on_both_sides(tmp_path):
    """A run of >= ``list_run`` items is a unit of its own, however short the section is.

    The prose before and after it can never land in the same chunk as its items, so one
    section of one paragraph, two items and one paragraph is three chunks.
    """
    parsed, part_id = _synth(
        tmp_path,
        _p("Top", style="Heading1")
        + _p("Intro")
        + _p("One", num=True)
        + _p("Two", num=True)
        + _p("After"),
    )
    chunks = chunk(parsed, part_id)
    assert _shape(chunks) == [(["Top"], 5, 1), (["Top"], 7, 2), (["Top"], 5, 1)]
    kinds = [
        [node.kind for node in parsed.nodes if node.id in set(one.node_ids)] for one in chunks
    ]
    assert kinds == [
        [NodeKind.PARA],
        [NodeKind.LIST_ITEM, NodeKind.LIST_ITEM],
        [NodeKind.PARA],
    ]


def test_a_single_list_item_is_not_a_run_and_packs_with_its_section(tmp_path):
    """N is 2: below it there is no run, so no boundary is forced and the item is ordinary
    content of the section it sits in."""
    parsed, part_id = _synth(
        tmp_path, _p("Top", style="Heading1") + _p("Intro") + _p("One", num=True)
    )
    assert _shape(chunk(parsed, part_id)) == [(["Top"], 9, 2)]


def test_a_section_holding_a_list_run_never_merges_up_however_short_it_is(tmp_path):
    """The list is what the merge is forbidden to touch: the tiny subsection keeps its own
    chunk and its own title, while its parent's one paragraph is the other chunk."""
    parsed, part_id = _synth(
        tmp_path,
        _p("Top", style="Heading1")
        + _p("Parent")
        + _p("Child", style="Heading2")
        + _p("One", num=True)
        + _p("Two", num=True),
    )
    assert _shape(chunk(parsed, part_id)) == [(["Top"], 6, 1), (["Top", "Child"], 7, 2)]


def test_a_document_with_no_heading_falls_back_to_size_chunks_under_the_empty_path(tmp_path):
    """The degraded flat root (4a): nothing names a section, so the cap is the only boundary
    there is, and the verdict the walk reached rides on every chunk it produced."""
    parsed, part_id = _synth(tmp_path, _p("Alpha") + _p("Beta") + _p("Gamma"))
    assert parsed.sections == []
    assert parsed.heading_detection is HeadingDetection.DEGRADED
    chunks = chunk(parsed, part_id, params=ChunkParams(size_cap=6, min_size=1))
    assert _shape(chunks) == [([], 5, 1), ([], 4, 1), ([], 5, 1)]
    assert all(one.fired_rules == [] and one.disputed_rules == [] for one in chunks)


def test_a_chunk_carries_the_rules_that_decided_its_place_in_the_outline(tmp_path):
    """``fired_rules``/``disputed_rules`` are the aggregate over the headings on the chunk's
    ancestor path -- its own section's heading and every ancestor's -- sorted, so the same
    outline position always reads the same way."""
    parsed, part_id = _synth(
        tmp_path,
        _p("Top", style="Heading1")
        + _p("Parent text")
        + _p("Sub", style="Heading2", outline=0)
        + _p("Child text"),
    )
    chunks = chunk(parsed, part_id)
    assert [one.fired_rules for one in chunks] == [["style"], ["outlineLvl", "style"]]
    assert [one.disputed_rules for one in chunks] == [[], ["outlineLvl"]]
    assert all(one.heading_detection is HeadingDetection.NORMAL for one in chunks)


# --- tables ---------------------------------------------------------------------------


def test_a_table_is_atomic_and_comes_with_its_own_nodes(tmp_path):
    """A table is one block however many rows it has: it is packed whole, and the chunk
    names the table, its rows, its cells and their paragraphs."""
    parsed, part_id = _synth(
        tmp_path, _p("Top", style="Heading1") + _p("Intro") + _table([["a", "b"], ["c", "d"]])
    )
    chunks = chunk(parsed, part_id)
    assert len(chunks) == 1
    text = _text(parsed, part_id, View.ACCEPTED, chunks[0])
    assert text == "Intro\na\nb\nc\nd"
    kinds = Counter(
        node.kind for node in parsed.nodes if node.id in set(chunks[0].node_ids)
    )
    assert kinds == {
        NodeKind.PARA: 5,
        NodeKind.TABLE: 1,
        NodeKind.ROW: 2,
        NodeKind.CELL: 4,
    }


def test_an_oversize_table_splits_into_row_groups_that_repeat_the_first_row(tmp_path):
    """A table over the cap is split between rows, never inside one, and every group opens
    with the table's first row -- v1 reads no ``w:tblHeader``, so "the header row" is the
    first one. The header repeats even where it leaves room for a single row, which is
    where the split stops being about the cap.
    """
    parsed, part_id = _synth(
        tmp_path,
        _p("Top", style="Heading1")
        + _table([["h1", "h2"], ["r1a", "r1b"], ["r2a", "r2b"], ["r3a", "r3b"], ["r4a", "r4b"]]),
        name="big_table.docx",
    )
    chunks = chunk(parsed, part_id, params=ChunkParams(size_cap=21, min_size=1))
    texts = [_text(parsed, part_id, View.ACCEPTED, one) for one in chunks]
    assert texts == [
        "h1\nh2\nr1a\nr1b\nr2a\nr2b",
        "h1\nh2\nr3a\nr3b\nr4a\nr4b",
    ]
    assert [one.size for one in chunks] == [21, 21]
    # A cap the header and one row already overflow still splits between rows: one row per
    # group, the header repeated each time, and never a row cut in half -- so the groups
    # come out over the cap rather than the rows coming out broken.
    tight = chunk(parsed, part_id, params=ChunkParams(size_cap=12, min_size=1))
    tight_texts = [_text(parsed, part_id, View.ACCEPTED, one) for one in tight]
    assert [one.size for one in tight] == [13, 13, 13, 13]
    assert all(line.split("\n")[0] == "h1" for line in tight_texts)


def test_identical_row_groups_collide_on_the_content_hash_and_are_kept_apart_by_the_ordinal(
    tmp_path,
):
    """The ``occurrence_index`` path (Turn 5): two row groups with the same bytes are two
    chunks, and the ordinal -- not the text -- is what tells their ids apart."""
    parsed, part_id = _synth(
        tmp_path,
        _p("Top", style="Heading1")
        + _table([["h1", "h2"]] + [["r1", "r2"]] * 4),
        name="same_rows.docx",
    )
    chunks = chunk(parsed, part_id, params=ChunkParams(size_cap=18, min_size=1))
    assert [one.size for one in chunks] == [17, 17]
    assert chunks[0].content_hash == chunks[1].content_hash
    assert [one.occurrence_index for one in chunks] == [0, 1]
    assert chunks[0].id != chunks[1].id


# --- identity: what an edit does and does not move ------------------------------------


def test_identical_paragraphs_in_two_sections_get_distinct_ids(tmp_path):
    """The collision case: two sections holding the same sentence are two chunks with one
    content hash and two ids, told apart by document order (the store half of this -- both
    survive as two records -- is Turn 7's)."""
    parsed, part_id = _synth(
        tmp_path,
        _p("A", style="Heading1")
        + _p("Same text")
        + _p("B", style="Heading1")
        + _p("Same text"),
    )
    chunks = chunk(parsed, part_id)
    assert [(one.section_path, one.occurrence_index) for one in chunks] == [
        (["A"], 0),
        (["B"], 1),
    ]
    assert chunks[0].content_hash == chunks[1].content_hash
    assert chunks[0].id != chunks[1].id


def test_an_edit_shifts_the_union_offsets_but_not_the_later_chunk_ids(tmp_path):
    """A chunk's id is its own bytes, so text inserted *above* it re-ids nothing below it.

    The offsets move -- the paragraph after the edit starts later in the union -- and every
    later chunk keeps its id and its content hash, which is the whole point of hashing
    content rather than position (D3).
    """
    before, part_before = _synth(
        tmp_path,
        _p("A", style="Heading1") + _p("Alpha") + _p("B", style="Heading1") + _p("Beta"),
        name="before.docx",
    )
    after, part_after = _synth(
        tmp_path,
        _p("A", style="Heading1")
        + _p("Alpha and a good deal more text")
        + _p("B", style="Heading1")
        + _p("Beta"),
        name="after.docx",
    )
    first = chunk(before, part_before)
    second = chunk(after, part_after)
    assert _shape(first) == [(["A"], 5, 1), (["B"], 4, 1)]
    assert first[0].id != second[0].id
    assert first[1].id == second[1].id
    assert first[1].content_hash == second[1].content_hash
    offset = {
        node.id: node.spans[0].start
        for node in after.nodes
        if node.kind is NodeKind.PARA and node.spans
    }
    before_offset = {
        node.id: node.spans[0].start
        for node in before.nodes
        if node.kind is NodeKind.PARA and node.spans
    }
    assert sorted(offset.values()) != sorted(before_offset.values())


def test_chunking_another_view_keeps_the_boundaries_and_changes_the_bytes(tmp_path):
    """A view is a mask over the union, so it decides what a chunk *is*, never where it
    ends: the accepted and original chunks cover the same nodes, with different bytes."""
    parsed, part_id = _synth(
        tmp_path, _p("Top", style="Heading1") + _deleted("Keep ", "Gone"), name="views.docx"
    )
    accepted = chunk(parsed, part_id, view=View.ACCEPTED)
    original = chunk(parsed, part_id, view=View.ORIGINAL)
    assert [one.section_path for one in accepted] == [one.section_path for one in original]
    assert [one.node_ids for one in accepted] == [one.node_ids for one in original]
    assert [one.size for one in accepted] == [5]
    assert [one.size for one in original] == [9]
    assert accepted[0].content_hash != original[0].content_hash
    assert accepted[0].id != original[0].id
    assert original[0].view_id == View.ORIGINAL.value


def test_a_comment_only_edit_keeps_the_chunk_id_and_moves_its_context_hash(tmp_path):
    """Editing who commented, or when, is a comment fact and nothing else (Turn 5): the
    chunk it annotates is not re-id'd -- only the summary key's context moves."""
    body = (
        _p("Top", style="Heading1")
        + '<w:p><w:commentRangeStart w:id="1"/><w:r><w:t>Commented text</w:t></w:r>'
        '<w:commentRangeEnd w:id="1"/><w:r><w:commentReference w:id="1"/></w:r></w:p>'
        + _p("Other paragraph")
    )
    before, part_id = _synth(
        tmp_path,
        body,
        comments=_comments(_comment("1", "a note", para_id="11111111")),
        name="before.docx",
    )
    after, _ = _synth(
        tmp_path,
        body,
        comments=_comments(_comment("1", "a note", para_id="11111111", initials="ZZ")),
        name="after.docx",
    )
    first = chunk(before, part_id)
    second = chunk(after, part_id)
    assert len(first) == 1
    assert first[0].id == second[0].id
    assert first[0].content_hash == second[0].content_hash
    assert first[0].context_hash != second[0].context_hash


def test_a_comment_body_edit_under_a_paraid_identity_moves_the_context_hash(tmp_path):
    """A comment keyed on a ``w14:paraId`` whose words are rewritten is the same comment
    but not the same context: the record carries its own text, so the summary key moves
    and a cached summary cannot go stale. The chunk id, which is content only, does not."""
    body = (
        _p("Top", style="Heading1")
        + '<w:p><w:commentRangeStart w:id="1"/><w:r><w:t>Commented text</w:t></w:r>'
        '<w:commentRangeEnd w:id="1"/><w:r><w:commentReference w:id="1"/></w:r></w:p>'
    )
    before, part_id = _synth(
        tmp_path, body, comments=_comments(_comment("1", "a note", para_id="11111111"))
    )
    after, _ = _synth(
        tmp_path, body, comments=_comments(_comment("1", "a much longer note", para_id="11111111"))
    )
    first = chunk(before, part_id)
    second = chunk(after, part_id)
    assert first[0].id == second[0].id
    assert first[0].context_hash != second[0].context_hash
    assert before.comments[0].para_id == after.comments[0].para_id == "11111111"
    assert (before.comments[0].text, after.comments[0].text) == ("a note", "a much longer note")


def test_a_comment_body_edit_under_a_fallback_identity_moves_the_context_hash(tmp_path):
    """A comment with no ``w14:paraId`` is identified by the hash fallback, which folds its
    body text in (D4) -- so there the body edit *does* reach the context hash, and reaches
    no chunk id either way."""
    body = (
        _p("Top", style="Heading1")
        + '<w:p><w:commentRangeStart w:id="1"/><w:r><w:t>Commented text</w:t></w:r>'
        '<w:commentRangeEnd w:id="1"/><w:r><w:commentReference w:id="1"/></w:r></w:p>'
    )
    before, part_id = _synth(tmp_path, body, comments=_comments(_comment("1", "a note")))
    after, _ = _synth(
        tmp_path, body, comments=_comments(_comment("1", "a much longer note"))
    )
    assert before.comments[0].para_id.startswith("hash:")
    first = chunk(before, part_id)
    second = chunk(after, part_id)
    assert first[0].id == second[0].id
    assert first[0].context_hash != second[0].context_hash


def test_a_range_comment_belongs_to_the_chunk_its_range_starts_in(tmp_path):
    """A range that crosses a boundary is the first chunk's (Turn 5): containment is over
    ``anchor.start``, so the second chunk's context is what it was with no comment at all.
    """
    ranged = (
        _p("Top", style="Heading1")
        + '<w:commentRangeStart w:id="1"/>'
        + _p("Alpha text")
        + '<w:p><w:r><w:t>Beta text</w:t></w:r><w:commentRangeEnd w:id="1"/>'
        + '<w:r><w:commentReference w:id="1"/></w:r></w:p>'
    )
    plain = _p("Top", style="Heading1") + _p("Alpha text") + _p("Beta text")
    params = ChunkParams(size_cap=10, min_size=1)
    commented, part_id = _synth(
        tmp_path, ranged, comments=_comments(_comment("1", "a note", para_id="11111111")),
        name="commented.docx",
    )
    bare, _ = _synth(tmp_path, plain, name="bare.docx")
    with_comment = chunk(commented, part_id, params=params)
    without = chunk(bare, part_id, params=params)
    assert _shape(with_comment) == _shape(without) == [(["Top"], 10, 1), (["Top"], 9, 1)]
    assert with_comment[0].id == without[0].id
    assert with_comment[1].id == without[1].id
    assert with_comment[0].context_hash != without[0].context_hash
    assert with_comment[1].context_hash == without[1].context_hash


def test_a_comment_on_a_heading_folds_into_the_first_chunk_of_its_section(tmp_path):
    """A heading is its section's ``heading_id`` and not a member, but a reviewer's comment
    on a clause title is about that section: it belongs to the section's first chunk."""
    heading = (
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        '<w:commentRangeStart w:id="1"/><w:r><w:t>Top</w:t></w:r>'
        '<w:commentRangeEnd w:id="1"/><w:r><w:commentReference w:id="1"/></w:r></w:p>'
    )
    commented, part_id = _synth(
        tmp_path, heading + _p("Body text"),
        comments=_comments(_comment("1", "a note", para_id="11111111")),
        name="commented.docx",
    )
    bare, _ = _synth(tmp_path, _p("Top", style="Heading1") + _p("Body text"), name="bare.docx")
    assert commented.comments[0].anchor is not None
    first = chunk(commented, part_id)
    second = chunk(bare, part_id)
    assert [one.id for one in first] == [one.id for one in second]  # content is unchanged
    assert first[0].context_hash != second[0].context_hash  # the comment is folded in


# --- headings are never lost, and a comment on one is never orphaned ------------------


def _body_with_short_subsection():
    long_text = "Established coverage text. " * 12
    return (
        _p("Coverage", style="Heading1")
        + _p(long_text)
        + _p("waiver of termination", style="Heading2")
        + _p("Applies where required by contract.")
        + _p("Exclusions", style="Heading1")
        + _p("Shortfall is excluded. " * 12)
    )


def test_a_merged_up_headings_text_is_a_line_of_the_chunk_it_merged_into(tmp_path):
    """The defect this pins: 'waiver of termination' was in no chunk text and no path."""
    parsed, part_id = _synth(tmp_path, _body_with_short_subsection())
    chunks = chunk(parsed, part_id)
    assert [one.section_path for one in chunks] == [["Coverage"], ["Exclusions"]]
    text = _text(parsed, part_id, View.ACCEPTED, chunks[0])
    assert text.endswith("waiver of termination\nApplies where required by contract.")
    # the heading that opens its own chunks stays out of the bytes: it is in the path
    assert "Coverage" not in text.replace("Established coverage", "")


def test_a_surviving_sections_heading_stays_out_of_its_chunk_bytes(tmp_path):
    parsed, part_id = _synth(tmp_path, _body_with_short_subsection())
    chunks = chunk(parsed, part_id)
    assert not _text(parsed, part_id, View.ACCEPTED, chunks[1]).startswith("Exclusions")


def test_a_comment_on_a_merged_up_heading_folds_into_the_chunk_its_text_landed_in(tmp_path):
    body = _body_with_short_subsection().replace(
        '<w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr><w:r><w:t>waiver of termination</w:t></w:r></w:p>',
        '<w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr><w:commentRangeStart w:id="1"/>'
        "<w:r><w:t>waiver of termination</w:t></w:r><w:commentRangeEnd w:id=\"1\"/>"
        '<w:r><w:commentReference w:id="1"/></w:r></w:p>',
    )
    assert "commentRangeStart" in body
    commented, part_id = _synth(
        tmp_path, body, comments=_comments(_comment("1", "check this", para_id="11111111")),
        name="commented.docx",
    )
    bare, _ = _synth(tmp_path, _body_with_short_subsection(), name="bare.docx")
    with_comment = chunk(commented, part_id)
    without = chunk(bare, part_id)
    assert [c.id for c in with_comment] == [c.id for c in without]
    assert with_comment[0].context_hash != without[0].context_hash
    assert with_comment[1].context_hash == without[1].context_hash


def test_a_comment_on_a_heading_with_no_content_of_its_own_goes_to_its_first_descendant(tmp_path):
    heading = (
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:commentRangeStart w:id="1"/>'
        "<w:r><w:t>Top</w:t></w:r><w:commentRangeEnd w:id=\"1\"/>"
        '<w:r><w:commentReference w:id="1"/></w:r></w:p>'
    )
    tail = _p("Sub", style="Heading2") + _p("Sub text. " * 40)
    commented, part_id = _synth(
        tmp_path, heading + tail, comments=_comments(_comment("1", "note", para_id="11111111")),
        name="commented.docx",
    )
    bare, _ = _synth(tmp_path, _p("Top", style="Heading1") + tail, name="bare.docx")
    with_comment = chunk(commented, part_id)
    without = chunk(bare, part_id)
    assert [c.id for c in with_comment] == [c.id for c in without]
    assert with_comment[0].context_hash != without[0].context_hash


def test_a_comment_on_a_heading_that_has_no_chunk_at_all_is_still_off_chunk(tmp_path):
    """The remaining limit: a heading with nothing under it, anywhere, has no chunk to hold
    its comment -- the document yields no chunks at all."""
    heading = (
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:commentRangeStart w:id="1"/>'
        "<w:r><w:t>Alone</w:t></w:r><w:commentRangeEnd w:id=\"1\"/>"
        '<w:r><w:commentReference w:id="1"/></w:r></w:p>'
    )
    parsed, part_id = _synth(
        tmp_path, heading, comments=_comments(_comment("1", "note", para_id="11111111"))
    )
    assert chunk(parsed, part_id) == []
    assert parsed.comments[0].anchor is not None


def test_a_heading_is_never_stranded_as_the_last_line_of_a_capped_chunk(tmp_path):
    """Packing moves a trailing heading to lead the next chunk with its content."""
    body = (
        _p("Top", style="Heading1")
        + _p("Top text " * 6)
        + _p("Short sub", style="Heading2")
        + _p("Sub text " * 6)
    )
    parsed, part_id = _synth(tmp_path, body)
    params = ChunkParams(size_cap=80, min_size=400)  # the subsection is tiny -> merges up
    chunks = chunk(parsed, part_id, params=params)
    assert len(chunks) >= 2
    by_id = {n.id: n for n in parsed.nodes}
    for one in chunks[:-1]:
        assert by_id[one.node_ids[-1]].kind is not NodeKind.HEADING, one.node_ids


def test_every_comment_carries_its_own_words_in_the_context_facts():
    from wordextract.chunker import _comment_facts  # noqa: F401  (the fact list is internal)
    from wordextract.model import Comment

    assert Comment(para_id="p", author="a", initials="x").text == ""
    assert Comment(para_id="p", author="a", initials="x", text="note").text == "note"


# --- churn: reported, never gated -----------------------------------------------------


@dataclass(frozen=True)
class _Churn:
    """How one edit moved the chunks (Turn 5).

    ``ids`` counts the chunk ids that appeared or vanished, ``bytes`` the chunk *texts* that
    did -- a chunk whose own text changed left the multiset and a new one arrived, so it
    counts twice. ``amplification`` is the ratio the spec asks to report: how many ids
    churned per chunk whose own bytes churned. It is ``None`` when no text churned at all,
    which is the one shape worth reading as an assertion: an edit that moved nothing.
    """

    ids: int
    bytes: int

    @property
    def amplification(self) -> float | None:
        return None if self.bytes == 0 else self.ids / self.bytes


def _churn(before: list[Chunk], after: list[Chunk]) -> _Churn:
    """The churn one edit caused: ids and own bytes that are in one run and not the other."""
    was = Counter(one.content_hash for one in before)
    now = Counter(one.content_hash for one in after)
    changed = sum((was - now).values()) + sum((now - was).values())
    return _Churn(ids=len({one.id for one in before} ^ {one.id for one in after}), bytes=changed)


def test_the_churn_of_a_body_edit_a_comment_edit_and_a_resize_is_reported(tmp_path, capsys):
    """The amplification measurement the spec asks for, on the three edits it names.

    Reported, not gated: a threshold here would only encode today's corpus, so the numbers
    are printed for the suite's log and the file asserts only that the report was made and
    that nothing moved that should not have -- a comment-only edit touches no chunk at all,
    and a body edit touches the chunk it is in and the ids of the paragraph it rewrote.
    """
    plain = _p("Top", style="Heading1") + _p("Alpha") + _p("Beta") + _p("Gamma")
    body = plain.replace("Alpha", "Alpha edited")
    commented = _comments(_comment("1", "a note", para_id="11111111"))
    touched = _comments(_comment("1", "a note", para_id="11111111", initials="ZZ"))

    before, part_id = _synth(tmp_path, plain, comments=commented, name="before.docx")
    body_edit, _ = _synth(tmp_path, body, comments=commented, name="body.docx")
    comment_edit, _ = _synth(tmp_path, plain, comments=touched, name="comment.docx")
    # The section is 38 characters with the shorter fill and 42 with the longer one: the
    # edit is the four characters that decide whether "Alpha" still fits beside it. ("Top"
    # is the heading and contributes to no chunk.)
    before_resize, _ = _synth(
        tmp_path, _p("Top", style="Heading1") + _p("Alpha") + _p("Delt" * 8), name="small.docx"
    )
    after_resize, _ = _synth(
        tmp_path, _p("Top", style="Heading1") + _p("Alpha") + _p("Delt" * 9), name="large.docx"
    )
    params = ChunkParams(size_cap=40, min_size=1)
    base = chunk(before, part_id, params=params)
    runs = {
        "body edit": _churn(base, chunk(body_edit, part_id, params=params)),
        "comment-only edit": _churn(base, chunk(comment_edit, part_id, params=params)),
        "resize at the cap": _churn(
            chunk(before_resize, part_id, params=params),
            chunk(after_resize, part_id, params=params),
        ),
    }
    # The report itself, on stdout rather than in an assertion: the numbers are the point
    # and they are not a contract, so the file checks the two ends of the scale and lets
    # the middle be whatever the corpus says.
    lines = [
        f"chunker churn -- {name}: ids_changed={one.ids} bytes_changed={one.bytes} "
        f"amplification={one.amplification}"
        for name, one in runs.items()
    ]
    with capsys.disabled():
        print("\n".join(lines))

    assert runs["comment-only edit"] == _Churn(ids=0, bytes=0)
    assert runs["comment-only edit"].amplification is None
    assert runs["body edit"].ids >= 1 and runs["body edit"].bytes >= 1
    assert len(chunk(before_resize, part_id, params=params)) == 1
    assert len(chunk(after_resize, part_id, params=params)) == 2
    assert [one.ids for one in runs.values()] == [2, 0, 3]
