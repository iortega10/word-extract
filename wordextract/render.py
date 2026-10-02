"""Turn 0: a chunk's own text in a view, and the union with revision markup.

Two renderings of one chunk, both derived from the stored ``ParseResult`` and nothing else,
so a caller holding only the store rebuilds exactly what a run wrote:

``chunk_text`` is the chunk's bytes **in a view** -- what ``Chunk.size`` and
``Chunk.content_hash`` are over. ``render_union_markup`` is the **union** with the revision
history visible around the text, plus the comment context and the revision manifest: the
summarizer's input (D5 -- the union with revision markup plus a revision manifest), where
"accepted view only" would hide a pending deletion.

Both walks are the walker's own order, never a second opinion: ``Chunk.node_ids`` is the
chunk's subtree in document order, so filtering it to the leaf kinds is the document order,
and a leaf's text is the view projection of its single union span (2b). The comments are the
ones the **chunker's** containment rule attaches to the chunk, so a chunk's rendering and
its ``context_hash`` cannot disagree about which comments the chunk holds.

Formats (fixed -- they are what a versioned prompt is written against):

* a leaf's text is its elementary spans in union order, each wrapped, outermost revision
  first, in ``[[<kind> author="<author>" date="<date>"]]`` ... ``[[/<kind>]]``, where
  ``<kind>`` is ``ins``, ``del``, ``moveFrom`` or ``moveTo`` and a move adds
  ``group="<move_group_id>"``. ``date`` is omitted when it is ``None``, and so is ``group``.
  An id the parse has no record for renders ``author=""`` with no date. Adjacent spans whose
  stacks are identical share one wrapper. Text under no revision is bare; leaves are joined
  by ``"\\n"`` in node order.
* comment context follows the text: one line per comment the chunk holds, sorted by
  ``para_id``, ``[[comment by="<author>" date="<date>" resolved="<true|false|unknown>"]]<anchor
  text> => <comment text>[[/comment]]`` (the anchor and comment text are the document's own, unescaped: a newline in
  either spans lines, and a literal ``[[/comment]]`` in the text is not neutralised -- the
  format escapes attribute values only, so a consumer must not treat the markers as
  tamper-proof).
* the revision manifest follows that: one line per revision touching the chunk, ordered by
  its first span's union offset and then its id,
  ``[[revision id="..." kind="..." author="..." date="..." group="..."]]<text_excerpt>
  [[/revision]]``, where ``<text_excerpt>`` is the entry's excerpt exactly as
  ``pending_changes`` computed it -- union text under the revision, cut to
  ``pending.EXCERPT_LIMIT`` characters with ``pending.TRUNCATION_MARKER`` appended when
  longer (the marker is part of the format and pinned by ``RENDER_VERSION``).

Only attribute **values** are escaped (``"`` as ``&quot;``, ``]`` as ``&#93;``); the text
between the wrappers is the document's own, unaltered -- a summary is keyed by these bytes,
so escaping the text would change what "the document says" means. The three parts are joined
by ``"\\n"``, and an empty part (a chunk with no comments, an empty paragraph) adds no line.

The manifest's entries are Turn 2's ``pending_changes`` (``revision_id``, ``kind``,
``author``, ``date``, ``spans``, ``text_excerpt``, ``move_group_id``), owned and derived by
``pending.py`` and consumed here, so the summarizer's ``pending_changes`` and this manifest
are one derivation and cannot drift. An id with no record is rendered but not attributed:
counting the revisions no chunk accounts for is ``pending.document_pending``'s document
level -- one chunk cannot answer it, and this module does not try.

No version constant gates a *key* here: a summary is keyed by these bytes, so a format change
invalidates summaries by changing the bytes. ``RENDER_VERSION`` exists so that change is
**visible** -- to the behavior ledger and to a reviewer, who can see that the format every
prompt was written against moved -- rather than only showing up as a cache miss.
"""
from __future__ import annotations

from bisect import bisect_right
from typing import Any, Mapping, Sequence

from .model import (
    Chunk,
    Comment,
    ElementarySpan,
    Node,
    NodeKind,
    ParseResult,
    Revision,
    UnionStream,
    View,
)
from .pending import pending_changes
from .versions import RENDER_VERSION
from .views import project
from .walker import TERMINATOR

#: The kinds that carry text of their own (2b): the leaves a chunk's bytes are made of.
_LEAF_KINDS = frozenset({NodeKind.HEADING, NodeKind.PARA, NodeKind.LIST_ITEM})

#: What a chunk's leaves are joined by -- the chunker's join, and a view's paragraph break.
_JOIN = "\n"

#: ``Comment.resolved`` is tri-state: Word says resolved, says open, or says nothing.
_RESOLVED: dict[bool | None, str] = {None: "unknown", True: "true", False: "false"}


def chunk_text(parsed: ParseResult, chunk: Chunk, view: View) -> str:
    """``chunk``'s own text in ``view``: its leaves' view text joined by ``"\\n"``.

    The chunk record holds ids and hashes, not bytes, so this is where the bytes are put
    back -- and back exactly as the chunker made them, which is what ``Chunk.size`` and
    ``Chunk.content_hash`` are over: the member leaves (paragraph, heading, list item) in
    the order ``node_ids`` lists them, each read off the view projection of *its* union
    span. An empty paragraph contributes the empty string, so a cell or a list stays as
    many lines as it has paragraphs.

    The join is flat because ``node_ids`` is the chunk's subtree in document order with no
    repetition: a block's own text is its leaves joined by the same character one level
    down, so joining every leaf once is the same string.
    """
    leaves = _leaves(parsed, chunk)
    if not leaves:
        return ""
    stream = _stream(parsed, leaves[0].part_id)
    return _JOIN.join(_leaf_text(stream, leaf, view) for leaf in leaves)


def render_union_markup(
    parsed: ParseResult, chunk: Chunk, comments: Sequence[Comment] | None = None
) -> str:
    """The chunk's union text with revision markup, then comment context, then the manifest.

    ``comments`` defaults to the document's own comment set; a caller passing a subset (the
    store's record, a filtered set) gets that subset's context and no others. The format is
    the module docstring's, exactly -- a summary key is taken over these bytes, so the
    rendering is a contract and not a preference.
    """
    leaves = _leaves(parsed, chunk)
    if not leaves:
        return ""
    stream = _stream(parsed, leaves[0].part_id)
    revisions = {revision.id: revision for revision in parsed.revisions}
    parts = [
        _JOIN.join(_leaf_markup(stream, leaf, revisions) for leaf in leaves),
        *(_comment_line(comment) for comment in _comments_in(parsed, leaves, stream, comments)),
        *(_manifest_line(entry) for entry in pending_changes(parsed, chunk)),
    ]
    return _JOIN.join(part for part in parts if part)


# --- the chunk's leaves, and the part they live in ------------------------------------


def _leaves(parsed: ParseResult, chunk: Chunk) -> list[Node]:
    """The chunk's text-bearing nodes, in the order ``node_ids`` lists them.

    A node id the parse no longer holds is skipped rather than guessed at: a chunk out of an
    older parse names nodes this walk did not produce, and inventing text for them would be
    worse than a short rendering.
    """
    by_id = {node.id: node for node in parsed.nodes}
    return [
        node
        for node in (by_id.get(node_id) for node_id in chunk.node_ids)
        if node is not None and node.kind in _LEAF_KINDS
    ]


def _stream(parsed: ParseResult, part_id: str) -> UnionStream:
    """The part's union stream. A chunk of a part that did not stream has no text at all."""
    for stream in parsed.union_streams:
        if stream.part_id == part_id:
            return stream
    raise ValueError(f"part {part_id!r} has no union stream in this document")


def _leaf_text(stream: UnionStream, leaf: Node, view: View) -> str:
    if not leaf.spans:
        return ""
    span = leaf.spans[0]
    return project(stream, view, span.start, span.end).text


# --- the union, with the revision stacks wrapped around it ----------------------------


def _leaf_markup(stream: UnionStream, leaf: Node, revisions: Mapping[str, Revision]) -> str:
    if not leaf.spans:
        return ""
    span = leaf.spans[0]
    return "".join(
        _wrapped(text, stack, revisions)
        for text, stack in _pieces(stream, span.start, span.end)
    )


def _overlapping(stream: UnionStream, start: int, end: int) -> list[ElementarySpan]:
    """The elementary spans that overlap the union range ``[start, end)``, in order.

    The spans tile the union, so the first one that overlaps is the first whose end is past
    ``start`` -- a bisect, not a scan over the part.
    """
    index = bisect_right(stream.spans, start, key=lambda span: span.end)
    found: list[ElementarySpan] = []
    while index < len(stream.spans) and stream.spans[index].start < end:
        found.append(stream.spans[index])
        index += 1
    return found


def _pieces(stream: UnionStream, start: int, end: int) -> list[tuple[str, list[str]]]:
    """``[start, end)``'s union text as ``(text, stack)`` pieces.

    Each span is clipped to the range, and two adjacent pieces with identical stacks are one
    piece: that is "adjacent spans with the identical stack share one wrapper". The walker
    already coalesces the spans it emits itself, so this is the case a *clipped* range can
    still create.
    """
    pieces: list[tuple[str, list[str]]] = []
    for span in _overlapping(stream, start, end):
        low, high = max(span.start, start), min(span.end, end)
        if low >= high:
            continue
        stack = list(span.stack)
        if pieces and pieces[-1][1] == stack:
            pieces[-1] = (pieces[-1][0] + stream.text[low:high], stack)
        else:
            pieces.append((stream.text[low:high], stack))
    return pieces


def _wrapped(text: str, stack: Sequence[str], revisions: Mapping[str, Revision]) -> str:
    """``text`` under ``stack``, opened outermost-first and closed innermost-first."""
    if not stack:
        return text
    opens: list[str] = []
    closes: list[str] = []
    for ident in stack:
        kind, attributes = _revision_attrs(ident, revisions)
        opens.append(f"[[{kind}{attributes}]]")
        closes.append(f"[[/{kind}]]")
    return "".join(opens) + text + "".join(reversed(closes))


def _revision_attrs(ident: str, revisions: Mapping[str, Revision]) -> tuple[str, str]:
    """A revision id's kind and its attribute list, from the record or from the id alone."""
    record = revisions.get(ident)
    kind = record.kind.value if record is not None else _kind_of(ident)
    attributes = [f' author="{_attr(record.author if record is not None else "")}"']
    if record is not None and record.date is not None:
        attributes.append(f' date="{_attr(record.date)}"')
    if record is not None and record.move_group_id is not None:
        attributes.append(f' group="{_attr(record.move_group_id)}"')
    return kind, "".join(attributes)


def _kind_of(ident: str) -> str:
    """A revision id's kind: ``<kind>:<w:id>`` (or ``<kind>:noid<n>``), so its own prefix."""
    return ident.split(":", 1)[0]


def _attr(value: str) -> str:
    """An attribute value, escaped as the format fixes it: ``"`` and ``]``, and no more."""
    return value.replace('"', "&quot;").replace("]", "&#93;")


# --- the comments the chunk holds ------------------------------------------------------


def _comments_in(
    parsed: ParseResult,
    leaves: Sequence[Node],
    stream: UnionStream,
    comments: Sequence[Comment] | None,
) -> list[Comment]:
    """The comments that *start* in the chunk, sorted by ``para_id`` -- the chunker's rule.

    "Starts" is containment: the chunk whose covered ranges hold ``anchor.start`` is the one
    the range opens in, so a range spanning two chunks belongs to the first of them. The
    ranges are merged leaves (see :func:`_covered`), which is the same coverage the chunker
    measures ``context_hash`` over -- a comment anchored on a paragraph terminator inside the
    chunk is the chunk's, and one anchored in a row group's skipped rows is not.
    """
    chosen = parsed.comments if comments is None else comments
    ranges = _covered(leaves, stream)
    found = [
        comment
        for comment in chosen
        if comment.anchor is not None
        and comment.anchor.part_id == stream.part_id
        and any(low <= comment.anchor.start < high for low, high in ranges)
    ]
    found.sort(key=lambda comment: comment.para_id)
    return found


def _covered(leaves: Sequence[Node], stream: UnionStream) -> list[tuple[int, int]]:
    """The union ranges the chunk covers: its leaves' spans merged across terminators.

    A chunk is a set of blocks, and its covered range is each block's first leaf's start to
    its last leaf's end -- so a terminator *inside* a block is covered, and so is the one
    between two blocks. Merging a leaf into the previous range whenever only paragraph
    terminators lie between them reproduces exactly that, including the oversize table's
    case: there a chunk's leaves skip the rows an earlier group took, so the ranges do
    **not** merge into one and a comment in a skipped row stays out.
    """
    ranges: list[tuple[int, int]] = []
    for leaf in leaves:
        if not leaf.spans:
            continue
        span = leaf.spans[0]
        if ranges and _terminators_only(stream, ranges[-1][1], span.start):
            ranges[-1] = (ranges[-1][0], span.end)
        else:
            ranges.append((span.start, span.end))
    return ranges


def _terminators_only(stream: UnionStream, low: int, high: int) -> bool:
    """Whether ``[low, high)`` is paragraph terminators and nothing else (possibly empty)."""
    if low >= high:
        return True
    return all(
        not span.stack and stream.text[span.start : span.end] == TERMINATOR
        for span in _overlapping(stream, low, high)
    )


def _comment_line(comment: Comment) -> str:
    attributes = [f' by="{_attr(comment.author)}"']
    if comment.date is not None:
        attributes.append(f' date="{_attr(comment.date)}"')
    attributes.append(f' resolved="{_RESOLVED[comment.resolved]}"')
    return (
        f"[[comment{''.join(attributes)}]]"
        f"{comment.anchor_text} => {comment.text}[[/comment]]"
    )


# --- the revision manifest line (the entries themselves come from pending.py) ----------


def _manifest_line(entry: Mapping[str, Any]) -> str:
    """One manifest line: the revision's own facts, then the text it covers.

    ``date`` and ``group`` are omitted when they are ``None`` -- an unknown date is stated by
    the attribute's absence, not as ``null``, so a reader never has to tell a null from a
    string spelled "null".
    """
    attributes = [
        f' id="{_attr(str(entry["revision_id"]))}"',
        f' kind="{_attr(_value(entry["kind"]))}"',
        f' author="{_attr(str(entry["author"]))}"',
    ]
    if entry.get("date") is not None:
        attributes.append(f' date="{_attr(str(entry["date"]))}"')
    if entry.get("move_group_id") is not None:
        attributes.append(f' group="{_attr(str(entry["move_group_id"]))}"')
    return f"[[revision{''.join(attributes)}]]{entry['text_excerpt']}[[/revision]]"


def _value(kind: Any) -> str:
    """A record's kind as the format spells it: an enum's value, or the string itself."""
    return kind.value if hasattr(kind, "value") else str(kind)


def comments_in(parsed: ParseResult, chunk: Chunk) -> list[Comment]:
    """The comments the chunk holds, by the chunker's containment rule (Turn 5).

    Exactly the comments :func:`render_union_markup` cites for this chunk and no
    others -- the same merged coverage (:func:`_covered`), so a comment anchored on
    a paragraph terminator inside the chunk is this chunk's and one anchored in a
    row group's skipped rows is not. The chunker's public identity is its
    contiguity, so containment is where the equivalence with
    :meth:`wordextract.chunker.Document.chunk_containing` stops. A chunk with no
    leaves holds nothing; the anchors are those leaves' paragraph covers.
    """
    leaves = _leaves(parsed, chunk)
    if not leaves:
        return []
    stream = _stream(parsed, leaves[0].part_id)
    return _comments_in(parsed, leaves, stream, None)


#: Public names for the helpers the retrieval layer shares with this module.
chunk_leaves = _leaves
leaf_text = _leaf_text
part_stream = _stream

__all__ = [
    "RENDER_VERSION",
    "chunk_leaves",
    "chunk_text",
    "comments_in",
    "leaf_text",
    "part_stream",
    "render_union_markup",
]
