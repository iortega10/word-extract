"""Turn 2: ``pending_changes`` -- which revisions touch a chunk, derived from the union stacks.

A :class:`~wordextract.model.Revision` has no span and no text: id, kind, author, date, move
group, ancestors.  "The revisions touching a chunk" therefore cannot be read off the records
-- it is derived from the chunk's own union ranges: the elementary spans its leaves cover,
the ancestor stacks those spans carry, and the union text underneath them.  Nothing here
reads a model's words: the pending facts are parse facts whatever the summary's prose says
(D5).

Two derivations live here:

* :func:`pending_changes` -- one chunk's entries in the spec's shape
  (``revision_id``, ``kind``, ``author``, ``date``, ``spans``, ``text_excerpt``,
  ``move_group_id``), ordered by the first span's start then id.  This is what
  :func:`wordextract.render.render_union_markup` renders as the manifest and what every new
  summary record carries.  ``text_excerpt`` is the union text of the elementary spans whose
  stacks carry the revision -- the text *under* it, so a nested revision's excerpt includes
  its descendants' text -- truncated to ``EXCERPT_LIMIT`` characters with
  ``TRUNCATION_MARKER`` appended when it is longer (the manifest's format line pins the
  marker verbatim).  An id on a stack with no record renders its facts from the id alone
  (``kind`` is the id's prefix, ``author`` is ``""``, dates are ``None``): rendered, never
  invented.

* :func:`document_pending` -- what **no** chunk accounts for.  A paragraph-mark revision (the
  ``paragraph_mark_revision`` gap) is a record with no stack and no node link, and tracked
  formatting changes (the ``unrecorded_revision_kind`` gap) are not records at all.  Neither
  can appear in any chunk's entries, so they are counted and named at document level instead
  of being silently absent (rule 8): ``unattributed_revisions`` is the number of revision
  records whose ids none of the supplied chunks' stacks carry, ``unattributed_gaps`` the
  attribution-relevant ids the parse recorded.  The chunks cover the **body** part only, so a
  tracked change in a header, footer, footnote or endnote is counted here too, with no gap id
  to explain it: "unattributed" means "no chunk shows it", not "a paragraph mark".  Zero and
  ``[]`` are real answers; a caller
  that could not load the parse reports ``None`` itself -- this function always answers.

The small helpers over the union stream (which spans overlap, which nodes are a chunk's
leaves) are duplicated per module on purpose -- render, views and terms do the same -- so
each module reads without reaching across its neighbors: ``pending`` must not import
``render`` (the manifest would become circular), and ``render`` imports ``pending``.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Any, Iterable

from .model import (
    Chunk,
    ElementarySpan,
    NodeKind,
    ParseResult,
    UnionStream,
)
from .walker import GAP_PARAGRAPH_MARK_REVISION, GAP_UNRECORDED_REVISION_KIND

__all__ = [
    "EXCERPT_LIMIT",
    "TRUNCATION_MARKER",
    "UNATTRIBUTED_GAPS",
    "DocumentPending",
    "document_pending",
    "pending_changes",
]

#: The maximum length of a ``text_excerpt``.  Excerpts are the chunk's union text under the
#: revision, so they are bounded by chunk size, not by the revision: a deleted paragraph can
#: be long, and a summary record must not carry pages of it.  200 covers the corpus's longest
#: excerpt by a wide margin -- the truncation is for real documents, not for fixtures.
EXCERPT_LIMIT = 200

#: Appended to an excerpt cut at ``EXCERPT_LIMIT``.  Pinned verbatim by the manifest's
#: format line and by ``render.RENDER_VERSION``.
TRUNCATION_MARKER = "…[truncated]"

#: The known-gap ids that mean "a revision no chunk can account for".  Both gaps are
#: attribution gaps: the first is a record that never reaches a stack, the second is
#: formatting-only change that is not a record.  Everything else in ``known_gaps`` (a part
#: absent, a content type refused, replies never emitted) is about *parts* and does not
#: belong on a document's pending line.
UNATTRIBUTED_GAPS: tuple[str, ...] = (
    GAP_PARAGRAPH_MARK_REVISION,
    GAP_UNRECORDED_REVISION_KIND,
)

#: What a "leaf" is for chunk coverage -- matches the chunker's union units.
_LEAF_KINDS = frozenset({NodeKind.HEADING, NodeKind.PARA, NodeKind.LIST_ITEM})


@dataclass(frozen=True)
class DocumentPending:
    """What no chunk of a document accounts for (rule 8, never silently absent).

    ``unattributed_revisions`` counts revision *records* the supplied chunks' stacks never
    carry -- currently paragraph-mark revisions, whose records exist but are attached to
    nothing.  ``unattributed_gaps`` names the attribution-relevant known gaps the parse
    recorded, so a document whose only revision is formatting-only change reports
    ``(0, [unrecorded_revision_kind])``: zero revisions, one named cause.  Not persisted
    and not part of the codec contracts -- a derived, in-memory fact.
    """

    unattributed_revisions: int
    unattributed_gaps: list[str] = field(default_factory=list)


def pending_changes(parsed: ParseResult, chunk: Chunk) -> list[dict[str, Any]]:
    """One chunk's revision entries: the shape the manifest renders and every summary carries.

    Derived from the chunk's union ranges -- the same algorithm ``render`` used to build
    manifest entries inline (Turn 2 lifted it out so summarize, catalog and the manifest
    share one derivation).  Returns ``[]`` for a chunk with no text or a part whose stream
    did not survive (``render_union_markup`` refuses the latter outright before asking
    here); entries are dicts of plain JSON scalars so a stored summary round-trips
    byte-for-byte.
    """
    first, spans, text = _stacked(parsed, chunk)
    if not first:
        return []
    revisions = {revision.id: revision for revision in parsed.revisions}
    entries: list[dict[str, Any]] = []
    for ident in sorted(first, key=lambda ident: (first[ident], ident)):
        record = revisions.get(ident)
        entries.append(
            {
                "revision_id": ident,
                "kind": record.kind.value if record is not None else _kind_of(ident),
                "author": record.author if record is not None else "",
                "date": record.date if record is not None else None,
                "spans": spans[ident],
                "text_excerpt": _excerpt("".join(text[ident])),
                "move_group_id": record.move_group_id if record is not None else None,
            }
        )
    return entries


def document_pending(parsed: ParseResult, chunks: Iterable[Chunk]) -> DocumentPending:
    """What none of ``chunks`` accounts for: an unattributed count and the gap ids.

    Attribution is the union of the revision ids on the elementary stacks of each supplied
    chunk's leaves; every revision record outside that union is counted.  Ids on stacks
    without records are ignored -- they are not records, so they cannot be unattributed
    records (they are already rendered by :func:`pending_changes`).  ``chunks`` is whatever
    the caller could load: a caller with no chunk sets gets every revision counted, which
    is the honest answer for "which revisions did you show me? None."
    """
    attributed: set[str] = set()
    for chunk in chunks:
        first, _spans, _text = _stacked(parsed, chunk)
        attributed.update(first)
    return DocumentPending(
        unattributed_revisions=sum(
            1 for revision in parsed.revisions if revision.id not in attributed
        ),
        unattributed_gaps=[
            gap for gap in parsed.known_gaps if gap in UNATTRIBUTED_GAPS
        ],
    )


def _stacked(
    parsed: ParseResult, chunk: Chunk
) -> tuple[dict[str, int], dict[str, list[dict[str, Any]]], dict[str, list[str]]]:
    """The revision ids the chunk touches, per id: first span start, clipped spans, texts.

    Spans are clipped to their leaf so every span is inside the chunk (a nested span's
    start can precede the leaf that contains it); they are plain dicts with the part id, so
    the shape is what ``codec.encode`` would write for a ``Span`` and a stored summary
    round-trips exactly.  ``({}, {}, {})`` when the chunk has no text or its part did not
    stream -- there is nothing under the stacks to report.
    """
    leaves = _leaves(parsed, chunk)
    if not leaves:
        return {}, {}, {}
    stream = _stream(parsed, leaves[0].part_id)
    if stream is None:
        return {}, {}, {}
    first: dict[str, int] = {}
    spans: dict[str, list[dict[str, Any]]] = {}
    text: dict[str, list[str]] = {}
    for leaf in leaves:
        if not leaf.spans:
            continue
        leaf_span = leaf.spans[0]
        for span in _overlapping(stream, leaf_span.start, leaf_span.end):
            low = max(span.start, leaf_span.start)
            high = min(span.end, leaf_span.end)
            if low >= high:
                continue
            for ident in span.stack:
                if ident not in first:
                    first[ident] = span.start
                    spans[ident] = []
                    text[ident] = []
                spans[ident].append(
                    {"part_id": stream.part_id, "start": low, "end": high}
                )
                text[ident].append(stream.text[low:high])
    return first, spans, text


def _excerpt(text: str) -> str:
    """``text`` cut to ``EXCERPT_LIMIT`` with ``TRUNCATION_MARKER`` appended when longer."""
    if len(text) <= EXCERPT_LIMIT:
        return text
    return text[:EXCERPT_LIMIT] + TRUNCATION_MARKER


def _kind_of(ident: str) -> str:
    """The kind implied by a revision id when no record stands behind it ("del:900")."""
    return ident.split(":", 1)[0]


def _leaves(parsed: ParseResult, chunk: Chunk) -> list[Any]:
    """The chunk's leaf nodes (heading, paragraph, list item) in ``chunk.node_ids`` order.

    Ids not in the tree (a removed node, a synthesized id) are skipped: nothing renders
    where there is nothing left -- same rule as ``render._leaves``.
    """
    by_id = {node.id: node for node in parsed.nodes}
    return [
        node
        for node in (by_id.get(node_id) for node_id in chunk.node_ids)
        if node is not None and node.kind in _LEAF_KINDS
    ]


def _stream(parsed: ParseResult, part_id: str) -> UnionStream | None:
    """The part's union stream, or ``None`` for a part that did not stream."""
    for stream in parsed.union_streams:
        if stream.part_id == part_id:
            return stream
    return None


def _overlapping(stream: UnionStream, start: int, end: int) -> list[ElementarySpan]:
    """Every elementary span overlapping ``[start, end)``, in document order.

    The spans tile the union in order, so the first one that overlaps is the first whose end
    is past ``start`` -- a bisect, not a scan over the part (same rule as
    ``render._overlapping``).
    """
    index = bisect_right(stream.spans, start, key=lambda span: span.end)
    spans: list[ElementarySpan] = []
    while index < len(stream.spans) and stream.spans[index].start < end:
        spans.append(stream.spans[index])
        index += 1
    return spans
