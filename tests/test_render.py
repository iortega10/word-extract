"""Turn 0b: rendering a chunk -- its own view text, and the union with revision markup.

Every claim here is re-derived from something the renderer did not write:

* a chunk's **own bytes** -- ``chunk_text`` -- against the walker's node tree and the view
  projection (and against the ``size``/``content_hash`` the chunker computed for itself), so
  a renderer that dropped a leaf, or took a document-order shortcut, fails here;
* a chunk's **comment context** against the ``context_hash`` the chunker folded those very
  comments into, so the containment rule in the renderer and the one in the chunker cannot
  drift apart without a test going red;
* the **markup** itself against hand-typed strings, off the revision fixtures
  (``fixtures/model/``) and off the hand-edited pair (``fixtures/program_review_v3.docx``).

The shapes no fixture holds -- an id the parse has no record for, an attribute value that
needs escaping, a chunk of a part that did not stream -- are built here, so the rule is
testable without a Word file.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from docextract_core import sha256_json

from wordextract import opc, render
from wordextract.chunker import DEFAULT_PARAMS, ChunkParams, chunk
from wordextract.model import (
    Chunk,
    Comment,
    ElementarySpan,
    Node,
    NodeKind,
    ParseResult,
    Span,
    UnionStream,
    View,
)
from wordextract.views import project
from wordextract.walker import TERMINATOR, walk_document

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ALL_DOCX = sorted(FIXTURES.rglob("*.docx"))
V3 = FIXTURES / "program_review_v3.docx"
V2 = FIXTURES / "program_review_v2.docx"
MOVE = FIXTURES / "model" / "move.docx"
NESTED = FIXTURES / "model" / "nested_revisions.docx"

#: Text-bearing leaves: the kinds that carry text of their own (2b), which is all a chunk's
#: bytes can be made of -- a table's rows and cells are containers over these.
LEAF_KINDS = frozenset({NodeKind.PARA, NodeKind.LIST_ITEM})


def _parsed(path: Path) -> tuple[ParseResult, str]:
    """``path``'s walk and its body part id -- the part the section tree covers."""
    package = opc.Package(path)
    return walk_document(package), package.document.part_id


def _stream(parsed: ParseResult, part_id: str) -> UnionStream:
    return next(stream for stream in parsed.union_streams if stream.part_id == part_id)


def _chunks(path: Path):
    parsed, part_id = _parsed(path)
    return parsed, part_id, chunk(parsed, part_id, params=DEFAULT_PARAMS)


def _only(path: Path, needle: str) -> tuple[ParseResult, Chunk]:
    """The one chunk of ``path`` whose accepted text holds ``needle``."""
    parsed, part_id, chunks = _chunks(path)
    found = [
        one
        for one in chunks
        if needle in render.chunk_text(parsed, one, View.ACCEPTED)
    ]
    assert len(found) == 1, f"{needle!r} is not in exactly one chunk of {path.name}"
    return parsed, found[0]


# --- the re-derivations the renderer is checked against -------------------------------


def _text(parsed: ParseResult, part_id: str, view: View, one: Chunk) -> str:
    """``one``'s own view text, re-derived from the union stream and the view projection.

    The walker's records are in document order, so walking them and keeping the leaves the
    chunk names rebuilds the string -- a leaf with no span of its own (an empty paragraph)
    contributing the empty string, which is a line of its own in the join.
    """
    stream = _stream(parsed, part_id)
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


def _covered(parsed: ParseResult, part_id: str, one: Chunk) -> list[tuple[int, int]]:
    """The union ranges ``one`` covers: its leaves' spans, merged across terminators.

    A chunk is a set of blocks, so its covered range is each block's first leaf's start to
    its last leaf's end -- and two leaves in *different* blocks end up in different ranges
    whenever anything but a paragraph terminator lies between them.
    """
    stream = _stream(parsed, part_id)
    by_id = {node.id: node for node in parsed.nodes}
    ranges: list[tuple[int, int]] = []
    for node_id in one.node_ids:
        node = by_id.get(node_id)
        if node is None or node.kind not in LEAF_KINDS | {NodeKind.HEADING}:
            continue
        if not node.spans:
            continue
        span = node.spans[0]
        if ranges and _terminators(stream, ranges[-1][1], span.start):
            ranges[-1] = (ranges[-1][0], span.end)
        else:
            ranges.append((span.start, span.end))
    return ranges


def _terminators(stream: UnionStream, low: int, high: int) -> bool:
    """Whether ``[low, high)`` is paragraph terminators and nothing else (possibly empty)."""
    if low >= high:
        return True
    return all(
        not span.stack and stream.text[span.start : span.end] == TERMINATOR
        for span in stream.spans
        if span.start < high and span.end > low
    )


def _held(parsed: ParseResult, part_id: str, one: Chunk, comment: Comment) -> bool:
    """The chunker's containment: the chunk whose covered ranges hold the anchor's start."""
    if comment.anchor is None or comment.anchor.part_id != part_id:
        return False
    return any(
        low <= comment.anchor.start < high
        for low, high in _covered(parsed, part_id, one)
    )


def _facts(comments: list[Comment]) -> list[list[str]]:
    """The ``context_hash`` input for exactly these comments, the chunker's own field list."""
    facts = [
        [
            comment.para_id,
            comment.author,
            comment.initials,
            comment.date or "",
            comment.anchor_text,
            comment.text,
            comment.threading_status.value,
            comment.resolved_identity or "",
            comment.parent_id or "",
            "resolved" if comment.resolved else "open",
        ]
        for comment in comments
    ]
    facts.sort()
    return facts


def _comment_line(comment: Comment) -> str:
    """One comment's context line, as the format fixes it -- spelled out, not imported."""
    resolved = "unknown" if comment.resolved is None else str(comment.resolved).lower()
    date = f' date="{comment.date}"' if comment.date is not None else ""
    author = comment.author.replace('"', "&quot;").replace("]", "&#93;")
    return (
        f'[[comment by="{author}"{date} resolved="{resolved}"]]'
        f"{comment.anchor_text} => {comment.text}[[/comment]]"
    )


# --- packages built here, so a shape is testable without a Word file ------------------

RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
W_ATTRS = f'xmlns:w="{opc.W_NS}" xmlns:w14="{opc.W14_NS}" xmlns:r="{opc.R_NS}"'


def _docx(
    tmp_path: Path, body: str, *, comments: str | None = None, name: str = "synth.docx"
) -> Path:
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


def _synth(tmp_path: Path, body: str, *, comments: str | None = None):
    """``body``'s parse, its body part id and its single chunk."""
    path = _docx(tmp_path, body, comments=comments)
    package = opc.Package(path)
    parsed = walk_document(package)
    chunks = chunk(parsed, package.document.part_id, params=DEFAULT_PARAMS)
    assert len(chunks) == 1
    return parsed, chunks[0]


# --- the chunk's own text is the text its size and its hash are over ------------------


@pytest.mark.parametrize("path", ALL_DOCX, ids=lambda path: path.stem)
def test_every_chunks_text_is_the_text_its_size_and_hash_are_over(path):
    """``chunk_text`` rebuilds the chunker's own bytes, for every chunk of every fixture.

    The check walks the walker's nodes in document order and the renderer walks the chunk's
    ``node_ids``, so the two derivations agree only if ``node_ids`` really is the subtree in
    document order -- and the hash is the chunker's own, over the string as it wrote it.
    """
    parsed, part_id, chunks = _chunks(path)
    for one in chunks:
        text = _text(parsed, part_id, View.ACCEPTED, one)
        assert render.chunk_text(parsed, one, View.ACCEPTED) == text
        assert one.size == len(text)
        assert one.content_hash == sha256_json(
            {
                "text": text,
                "view_id": View.ACCEPTED.value,
                "textmodel_version": one.textmodel_version,
            }
        )


def test_a_leaf_with_no_span_is_a_line_of_its_own(tmp_path):
    """An empty paragraph contributes its empty string, not nothing: the join keeps the line."""
    parsed, one = _synth(tmp_path, "<w:p/><w:p><w:r><w:t>after</w:t></w:r></w:p>")
    assert render.chunk_text(parsed, one, View.ACCEPTED) == "\nafter"
    assert render.render_union_markup(parsed, one) == "\nafter"
    assert one.size == 6


def test_a_chunk_with_no_leaves_renders_as_nothing(tmp_path):
    """A chunk that names no node at all -- not a shape the chunker writes -- has no text."""
    parsed, _ = _synth(tmp_path, "<w:p><w:r><w:t>x</w:t></w:r></w:p>")
    empty = Chunk(id="c", content_hash="h", context_hash="h")
    assert render.chunk_text(parsed, empty, View.ACCEPTED) == ""
    assert render.render_union_markup(parsed, empty) == ""


def test_a_chunk_of_a_part_that_did_not_stream_is_refused():
    """A part with no union stream has no text to render, and saying so beats an empty string."""
    node_id = "hash:" + "0" * 64 + ":0"
    parsed = ParseResult(
        union_streams=[UnionStream(part_id="officeDocument:0", text="x\n")],
        nodes=[
            Node(
                id=node_id,
                kind=NodeKind.PARA,
                part_id="footnote:0",
                source_ref="p",
                spans=[Span(part_id="footnote:0", start=0, end=1)],
            )
        ],
    )
    chunked = Chunk(id="c", content_hash="h", context_hash="h", node_ids=[node_id])
    with pytest.raises(ValueError, match="footnote:0"):
        render.chunk_text(parsed, chunked, View.ACCEPTED)
    with pytest.raises(ValueError, match="footnote:0"):
        render.render_union_markup(parsed, chunked)


# --- the three views, and the union that shows the edit -------------------------------


def test_the_views_are_three_keys_and_the_union_is_the_one_that_shows_the_edit():
    """The v3 pollution edit: accepted keeps the insertion, original the deletion, and the
    union holds both -- which is why the summarizer reads the union (D5).
    """
    parsed, one = _only(V3, "hostile-fire release")
    assert render.chunk_text(parsed, one, View.ACCEPTED) == (
        "Pollution: excluded except for hostile-fire release"
    )
    assert render.chunk_text(parsed, one, View.ORIGINAL) == (
        "Pollution: excluded in all cases"
    )
    assert render.chunk_text(parsed, one, View.SUPERSEDED) == ""
    assert one.size == 51
    assert one.view_id == View.ACCEPTED.value

    markup = render.render_union_markup(parsed, one)
    assert "excluded in all cases" in markup
    assert "excluded except for hostile-fire release" in markup


def test_the_pollution_chunks_markup_is_the_format_it_says_it_is():
    """One chunk of the hand-edited pair, spelled out: markup, comment context, manifest.

    The manifest is Turn 2's provisional derivation (``pending_changes``), so this string is
    the one place the whole rendering is pinned -- a change to either format lands here.
    """
    parsed, one = _only(V3, "hostile-fire release")
    assert render.render_union_markup(parsed, one) == "\n".join(
        [
            'Pollution: [[del author="R. Alvarez" date="2026-09-01T10:00:00Z"]]'
            "excluded in all cases[[/del]]"
            '[[ins author="R. Alvarez" date="2026-09-01T10:00:00Z"]]'
            "excluded except for hostile-fire release[[/ins]]",
            '[[comment by="M. Chen" date="2026-01-01T00:00:00Z" resolved="unknown"]]'
            "Pollution:  => Pollution exclusion wording changed - flag for carrier sign-off."
            "[[/comment]]",
            '[[revision id="del:900" kind="del" author="R. Alvarez" '
            'date="2026-09-01T10:00:00Z"]]excluded in all cases[[/revision]]',
            '[[revision id="ins:901" kind="ins" author="R. Alvarez" '
            'date="2026-09-01T10:00:00Z"]]excluded except for hostile-fire release[[/revision]]',
        ]
    )


def test_the_hand_edited_pair_differs_by_its_marks_and_not_by_its_comments():
    """v2 is v3 before the edit: the manifest appears with the edit, the comment does not move.

    The comment's facts are the same in both documents -- so the context a summary key is
    folded over is the same text in both -- while the marks the edit left add a manifest and
    change the key. The order is the union's: the deletion's text comes first.
    """
    before, before_chunk = _only(V2, "Pollution: ")
    after, after_chunk = _only(V3, "hostile-fire release")
    assert before.revisions == []
    before_lines = render.render_union_markup(before, before_chunk).split("\n")
    after_lines = render.render_union_markup(after, after_chunk).split("\n")
    assert before_lines == [
        "Pollution: excluded in all cases",
        '[[comment by="M. Chen" date="2026-01-01T00:00:00Z" resolved="unknown"]]'
        "Pollution:  => Pollution exclusion wording changed - flag for carrier sign-off."
        "[[/comment]]",
    ]
    assert [line for line in after_lines if line.startswith("[[comment ")] == [
        line for line in before_lines if line.startswith("[[comment ")
    ]
    assert [line for line in after_lines if line.startswith("[[revision ")] == [
        '[[revision id="del:900" kind="del" author="R. Alvarez" '
        'date="2026-09-01T10:00:00Z"]]excluded in all cases[[/revision]]',
        '[[revision id="ins:901" kind="ins" author="R. Alvarez" '
        'date="2026-09-01T10:00:00Z"]]excluded except for hostile-fire release[[/revision]]',
    ]
    assert before_chunk.content_hash != after_chunk.content_hash
    assert before_chunk.context_hash != after_chunk.context_hash


# --- nesting, moves, and no revisions at all -----------------------------------------


def test_a_nested_revision_wraps_in_stack_order_and_the_manifest_lists_the_text_under_it():
    """``fixtures/model/nested_revisions.docx``: an ``ins`` inside a ``del`` and vice versa.

    The wrapper is the outer revision first and closes innermost first, so the nesting is
    readable; the manifest entry for an outer revision carries the text *under* it, which is
    what makes a summary of the manifest a summary of the edit and not of the mark.
    """
    parsed, one = _only(NESTED, "worldwide")
    assert render.render_union_markup(parsed, one).split("\n") == [
        '[[ins author="A. Ito" date="2026-01-01T00:00:00Z"]]Coverage [[/ins]]'
        '[[ins author="A. Ito" date="2026-01-01T00:00:00Z"]]'
        '[[del author="A. Ito" date="2026-01-01T00:00:00Z"]]is excluded[[/del]]'
        "[[/ins]]"
        '[[ins author="A. Ito" date="2026-01-01T00:00:00Z"]]applies worldwide.[[/ins]]',
        '[[del author="A. Ito" date="2026-01-01T00:00:00Z"]]Draft clause [[/del]]'
        '[[del author="A. Ito" date="2026-01-01T00:00:00Z"]]'
        '[[ins author="A. Ito" date="2026-01-01T00:00:00Z"]]Final clause[[/ins]]'
        "[[/del]]"
        '[[del author="A. Ito" date="2026-01-01T00:00:00Z"]] is void.[[/del]]',
        '[[revision id="ins:2" kind="ins" author="A. Ito" date="2026-01-01T00:00:00Z"]]'
        "Coverage is excludedapplies worldwide.[[/revision]]",
        '[[revision id="del:3" kind="del" author="A. Ito" date="2026-01-01T00:00:00Z"]]'
        "is excluded[[/revision]]",
        '[[revision id="del:4" kind="del" author="A. Ito" date="2026-01-01T00:00:00Z"]]'
        "Draft clause Final clause is void.[[/revision]]",
        '[[revision id="ins:5" kind="ins" author="A. Ito" date="2026-01-01T00:00:00Z"]]'
        "Final clause[[/revision]]",
    ]
    assert [one.id for one in parsed.revisions] == ["ins:2", "del:3", "del:4", "ins:5"]


def test_a_move_pair_is_two_revisions_one_group_and_neither_is_paired_with_the_other():
    """``fixtures/model/move.docx``: the walker synthesizes nothing, so a move is two marks.

    Both carry the ``w:name`` they were wrapped by, which is the only thing that pairs them
    -- and the manifest keeps them apart, as two entries under one group.
    """
    parsed, one = _only(MOVE, "Section 4")
    assert [revision.id for revision in parsed.revisions] == ["moveFrom:5", "moveTo:6"]
    assert [revision.move_group_id for revision in parsed.revisions] == ["mg1", "mg1"]
    assert [revision.ancestors for revision in parsed.revisions] == [[], []]
    assert render.chunk_text(parsed, one, View.ACCEPTED) == "\nSection 4 "
    assert render.render_union_markup(parsed, one).split("\n") == [
        '[[moveFrom author="E. Nakamura" date="2026-01-01T00:00:00Z" group="mg1"]]'
        "Section 4 [[/moveFrom]]",
        '[[moveTo author="E. Nakamura" date="2026-01-01T00:00:00Z" group="mg1"]]'
        "Section 4 [[/moveTo]]",
        '[[revision id="moveFrom:5" kind="moveFrom" author="E. Nakamura" '
        'date="2026-01-01T00:00:00Z" group="mg1"]]Section 4 [[/revision]]',
        '[[revision id="moveTo:6" kind="moveTo" author="E. Nakamura" '
        'date="2026-01-01T00:00:00Z" group="mg1"]]Section 4 [[/revision]]',
    ]


def test_a_document_with_no_revisions_renders_as_the_text_it_is(tmp_path):
    """No revision, no comment, no manifest line: the rendering is the text and nothing else."""
    parsed, one = _synth(tmp_path, "<w:p><w:r><w:t>plain text</w:t></w:r></w:p>")
    markup = render.render_union_markup(parsed, one)
    assert markup == "plain text"
    assert markup == render.chunk_text(parsed, one, View.ACCEPTED)
    assert "[[" not in markup


def test_an_id_with_no_revision_record_still_renders_but_is_not_attributed():
    """A mark the parse kept no record for: the id names its own kind, and nothing else.

    Ids come off the spans' ancestor stacks, so an id out of an older parse (or out of a
    store that dropped a record) renders as its kind with an empty author rather than
    disappearing -- the text under it is the document's, whoever wrote it.
    """
    node_id = "hash:" + "0" * 64 + ":0"
    parsed = ParseResult(
        union_streams=[
            UnionStream(
                part_id="officeDocument:0",
                text="just text\n",
                spans=[ElementarySpan(0, 9, ["ins:7"])],
            )
        ],
        nodes=[
            Node(
                id=node_id,
                kind=NodeKind.PARA,
                part_id="officeDocument:0",
                source_ref="p",
                spans=[Span(part_id="officeDocument:0", start=0, end=9)],
            )
        ],
    )
    assert parsed.revisions == []
    chunked = Chunk(id="c", content_hash="h", context_hash="h", node_ids=[node_id])
    assert render.render_union_markup(parsed, chunked).split("\n") == [
        '[[ins author=""]]just text[[/ins]]',
        '[[revision id="ins:7" kind="ins" author=""]]just text[[/revision]]',
    ]


def test_a_revision_with_no_date_and_no_group_leaves_those_attributes_out(tmp_path):
    """Word's absence is rendered as absence: no ``date=""``, no ``group=""``."""
    body = (
        "<w:p>"
        '<w:ins w:id="4" w:author="Nobody"><w:r><w:t>kept</w:t></w:r></w:ins>'
        "</w:p>"
    )
    parsed, one = _synth(tmp_path, body)
    assert render.render_union_markup(parsed, one).split("\n") == [
        '[[ins author="Nobody"]]kept[[/ins]]',
        '[[revision id="ins:4" kind="ins" author="Nobody"]]kept[[/revision]]',
    ]


# --- escaping: the attribute values, and never the text -------------------------------


def test_attribute_values_are_escaped_and_the_documents_own_text_is_not(tmp_path):
    """A quote or a ``]`` in an author cannot break the wrapper out of its own attribute.

    The text between the wrappers stays the document's: escaping it would change what a
    summary keyed on these bytes is a summary *of*.
    """
    body = (
        "<w:p>"
        '<w:ins w:id="4" w:author="A&quot;B]C" w:date="2026-01-01T00:00:00Z">'
        "<w:r><w:t>x]y</w:t></w:r></w:ins>"
        "</w:p>"
    )
    parsed, one = _synth(tmp_path, body)
    assert render.render_union_markup(parsed, one).split("\n") == [
        '[[ins author="A&quot;B&#93;C" date="2026-01-01T00:00:00Z"]]x]y[[/ins]]',
        '[[revision id="ins:4" kind="ins" author="A&quot;B&#93;C" '
        'date="2026-01-01T00:00:00Z"]]x]y[[/revision]]',
    ]


def test_a_comment_line_carries_the_anchor_the_words_and_the_escaping(tmp_path):
    """``anchor text => comment text``, with the author escaped and the text untouched."""
    body = (
        "<w:p><w:commentRangeStart w:id=\"1\"/>"
        "<w:r><w:t>keep]this</w:t></w:r>"
        '<w:commentRangeEnd w:id="1"/><w:r><w:commentReference w:id="1"/></w:r></w:p>'
    )
    comments = (
        '<w:comments xmlns:w="%s" xmlns:w14="%s">'
        '<w:comment w:id="1" w:author="Ada &quot;Ace&quot; Lovelace" w:initials="AL" '
        'w:date="2026-02-03T04:05:06Z"><w:p w14:paraId="0000000A">'
        "<w:r><w:t>Why ] is this?</w:t></w:r></w:p></w:comment>"
        "</w:comments>" % (opc.W_NS, opc.W14_NS)
    )
    parsed, one = _synth(tmp_path, body, comments=comments)
    assert render.render_union_markup(parsed, one).split("\n") == [
        "keep]this",
        '[[comment by="Ada &quot;Ace&quot; Lovelace" date="2026-02-03T04:05:06Z" '
        'resolved="unknown"]]keep]this => Why ] is this?[[/comment]]',
    ]


# --- the comment context is the chunker's, and can be narrowed -------------------------


@pytest.mark.parametrize("path", ALL_DOCX, ids=lambda path: path.stem)
def test_every_chunks_comment_context_is_what_the_chunker_hashed(path):
    """The rendered comment lines are exactly the comments ``context_hash`` folds in.

    The chunker's containment is re-derived here off the chunk's covered ranges, and the
    facts are rebuilt into ``context_hash``: a comment the renderer listed but the chunker
    did not (or the other way round) changes the hash and fails, whatever either one claims.
    """
    parsed, part_id, chunks = _chunks(path)
    for one in chunks:
        markup = render.render_union_markup(parsed, one)
        lines = [line for line in markup.split("\n") if line.startswith("[[comment ")]
        here = sorted(
            (c for c in parsed.comments if _held(parsed, part_id, one, c)),
            key=lambda comment: comment.para_id,
        )
        assert lines == [_comment_line(comment) for comment in here]
        assert one.context_hash == sha256_json(
            {"content_hash": one.content_hash, "comments": _facts(here)}
        )


def _anchored(ident: str, text: str, para_id: str) -> tuple[str, str]:
    """One paragraph with the comment range ``ident`` around it, and that comment's XML."""
    body = (
        f'<w:p><w:commentRangeStart w:id="{ident}"/><w:r><w:t>{text}</w:t></w:r>'
        f'<w:commentRangeEnd w:id="{ident}"/>'
        f'<w:r><w:commentReference w:id="{ident}"/></w:r></w:p>'
    )
    comment = (
        f'<w:comment w:id="{ident}" w:author="D. Okafor" w:initials="DO" '
        f'w:date="2026-05-06T07:08:09Z"><w:p w14:paraId="{para_id}">'
        f"<w:r><w:t>{text}?</w:t></w:r></w:p></w:comment>"
    )
    return body, comment


def test_each_chunks_context_is_its_own_comment_and_no_other_chunks(tmp_path):
    """Two blocks, two comments, two chunks: containment is per chunk, not per document.

    Each paragraph is longer than the cap, so each is a chunk of its own; the comment that
    starts in one is that one's context and the other's is not -- which is exactly what the
    chunker folded into each ``context_hash``.
    """
    first_body, first_comment = _anchored("1", "first " * 10, "0000000A")
    second_body, second_comment = _anchored("2", "second " * 10, "0000000B")
    package = opc.Package(
        _docx(
            tmp_path,
            first_body + second_body,
            comments=(
                f'<w:comments xmlns:w="{opc.W_NS}" xmlns:w14="{opc.W14_NS}">'
                f"{first_comment}{second_comment}</w:comments>"
            ),
        )
    )
    parsed = walk_document(package)
    chunks = chunk(
        parsed,
        package.document.part_id,
        params=ChunkParams(size_cap=20, min_size=0),
    )
    assert len(chunks) == 2
    rendered = [render.render_union_markup(parsed, one) for one in chunks]
    assert "first " * 10 in rendered[0]
    assert "second " * 10 in rendered[1]
    assert rendered[0].count("[[comment ") == 1
    assert rendered[1].count("[[comment ") == 1
    assert "first ?" in rendered[0] and "first ?" not in rendered[1]
    assert "second ?" in rendered[1] and "second ?" not in rendered[0]


def test_a_comment_anchored_in_another_part_is_no_chunks_context():
    """The chunks are the body's, so a comment anchored in a footnote is not their context."""
    node_id = "hash:" + "0" * 64 + ":0"
    parsed = ParseResult(
        union_streams=[
            UnionStream(
                part_id="officeDocument:0",
                text="body\n",
                spans=[ElementarySpan(0, 5)],
            )
        ],
        nodes=[
            Node(
                id=node_id,
                kind=NodeKind.PARA,
                part_id="officeDocument:0",
                source_ref="p",
                spans=[Span(part_id="officeDocument:0", start=0, end=4)],
            )
        ],
        comments=[_comment("footnote:0")],
    )
    chunked = Chunk(id="c", content_hash="h", context_hash="h", node_ids=[node_id])
    assert render.render_union_markup(parsed, chunked) == "body"
    assert render.render_union_markup(parsed, chunked, comments=parsed.comments) == "body"

    same_part = ParseResult(
        union_streams=parsed.union_streams,
        nodes=parsed.nodes,
        comments=[_comment("officeDocument:0")],
    )
    assert render.render_union_markup(same_part, chunked).split("\n") == [
        "body",
        '[[comment by="D. Okafor" date="2026-05-06T07:08:09Z" resolved="unknown"]]'
        "body => Is this right?[[/comment]]",
    ]


def _comment(part_id: str) -> Comment:
    """One comment whose anchor covers the whole body paragraph, in ``part_id``."""
    return Comment(
        para_id="0000000A",
        author="D. Okafor",
        initials="DO",
        date="2026-05-06T07:08:09Z",
        anchor=Span(part_id=part_id, start=0, end=4),
        anchor_text="body",
        text="Is this right?",
    )


def test_the_comment_set_can_be_narrowed_by_the_caller():
    """``comments`` is the caller's set: a caller passing none of them gets no context."""
    parsed, one = _only(V3, "hostile-fire release")
    full = render.render_union_markup(parsed, one)
    assert any(line.startswith("[[comment ") for line in full.split("\n"))
    assert not any(
        line.startswith("[[comment ")
        for line in render.render_union_markup(parsed, one, comments=[]).split("\n")
    )
    other = next(c for c in parsed.comments if c.anchor.start == 64)
    assert not any(
        line.startswith("[[comment ")
        for line in render.render_union_markup(parsed, one, comments=[other]).split("\n")
    )
    only = next(c for c in parsed.comments if "Pollution" in c.anchor_text)
    narrow = render.render_union_markup(parsed, one, comments=[only])
    assert [line for line in narrow.split("\n") if line.startswith("[[comment ")] == [
        _comment_line(only)
    ]


def test_comment_text_is_the_documents_own_so_a_newline_or_a_closing_marker_passes_through(tmp_path):
    """Pins the documented limit: only attribute values are escaped, never the text."""
    body = (
        '<w:p><w:commentRangeStart w:id="1"/><w:r><w:t>anchor</w:t></w:r>'
        '<w:commentRangeEnd w:id="1"/><w:r><w:commentReference w:id="1"/></w:r></w:p>'
    )
    comments = (
        '<w:comments xmlns:w="%s" xmlns:w14="%s">'
        '<w:comment w:id="1" w:author="A" w:initials="A"><w:p w14:paraId="0000000A">'
        "<w:r><w:t>x [[/comment]] y</w:t></w:r></w:p>"
        '<w:p w14:paraId="0000000B"><w:r><w:t>second para</w:t></w:r></w:p></w:comment>'
        "</w:comments>" % (opc.W_NS, opc.W14_NS)
    )
    parsed, one = _synth(tmp_path, body, comments=comments)
    out = render.render_union_markup(parsed, one)
    assert "x [[/comment]] y" in out
    assert out.count("[[/comment]]") == 2
