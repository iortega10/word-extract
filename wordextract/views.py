"""Turn 3: views -- the masks over a part's union, gap-closing and the offset map.

``text-model-spec.md`` sections 3, 4 and 7. A view is a **mask**, never the content
(D5): ``accepted`` keeps a span unless a del-family (``del``, ``moveFrom``) revision
encloses it, ``original`` keeps it unless an ins-family (``ins``, ``moveTo``) one
does, and ``superseded`` -- mask and fixture only in Phase 1, never a matcher target
(D6) -- keeps only a span both families enclose: text that was inserted and then
deleted.

Applying a mask elides the spans it drops **entirely** -- no placeholder -- and
concatenates what is left. That is gap-closing, and it happens **within a paragraph
only** (section 4). It never crosses a paragraph boundary, which is why a terminator,
the empty-stack ``"\\n"`` elementary span that ends a paragraph, survives every mask:
a break is a boundary, not text a view has an opinion about, and keeping it is what
stops two paragraphs fusing in any view -- including ``superseded``, whose mask no
empty stack satisfies.

The union is therefore never matched or summarized directly: it is only the address
space a projection maps back to (section 4, consequences).

Nothing here stores a view: :func:`project` derives one on demand from a part's
:class:`~wordextract.model.UnionStream`.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from typing import Sequence

from .model import ElementarySpan, Span, UnionStream, View
from .walker import TERMINATOR

#: ``text-model-spec.md`` section 3: a revision is del-family if accepting the
#: revision removes its text, ins-family if accepting it adds the text.
DEL_FAMILY = frozenset({"del", "moveFrom"})
INS_FAMILY = frozenset({"ins", "moveTo"})

#: The three views, in the order every projection of a part is reported in.
VIEWS: tuple[View, ...] = (View.ACCEPTED, View.ORIGINAL, View.SUPERSEDED)


def revision_kind(revision_id: str) -> str:
    """The kind of an ancestor-stack id, which is ``<kind>:<w:id>`` (D2)."""
    return revision_id.split(":", 1)[0]


def keeps(stack: Sequence[str], view: View) -> bool:
    """Whether ``view``'s mask keeps a span whose ancestor stack is ``stack``.

    Section 3, on the stacks of section 2: ``accepted`` unless a del-family revision
    encloses the span, ``original`` unless an ins-family one does, ``superseded`` only
    when both do. A view is a mask over the union, so nothing here reads text.
    """
    kinds = {revision_kind(revision_id) for revision_id in stack}
    deleted = not kinds.isdisjoint(DEL_FAMILY)
    inserted = not kinds.isdisjoint(INS_FAMILY)
    if view is View.ACCEPTED:
        return not deleted
    if view is View.ORIGINAL:
        return not inserted
    if view is View.SUPERSEDED:
        return deleted and inserted
    raise ValueError(f"unknown view: {view!r}")


@dataclass(frozen=True)
class Run:
    """One retained elementary span of a projection, and where it came from.

    ``view_start``/``view_end`` are offsets into the projection's own text and
    ``union_start`` the offset that text starts at in the union, so inside a run the
    map is linear: ``union_start + (view_offset - view_start)``.
    """

    view_start: int
    view_end: int
    union_start: int


@dataclass(frozen=True)
class Projection:
    """One union range projected through one view's mask, gap-closed (sections 3, 4, 7).

    ``text`` is the projection's own text -- the view text of the range. ``runs``
    tile it exactly (``runs[i].view_end == runs[i + 1].view_start``) and carry the
    offset map back to union coordinates: a run is one retained span, so a run's text
    is contiguous in both spaces and the gap between two runs is text this view
    elided.
    """

    view: View
    part_id: str
    text: str
    runs: tuple[Run, ...] = ()

    def union_range(self, start: int, end: int) -> Span:
        """The union address of a non-empty range of this view's text.

        A range that crosses an elided gap maps to the span from its first retained
        character to its last: the union text between them was removed *from this
        view*, so the address covers it. That is all the map claims -- it says where
        the text was, never that the union reads as the view does.
        """
        if not 0 <= start < end <= len(self.text):
            raise ValueError(f"view range [{start}, {end}) is not a range of {len(self.text)}")
        first = self._run_at(start)
        last = self._run_at(end - 1)
        return Span(
            part_id=self.part_id,
            start=first.union_start + start - first.view_start,
            end=last.union_start + end - last.view_start,
        )

    def union_spans(self, start: int, end: int) -> list[Span]:
        """The union addresses of a non-empty range of this view's text, one per run.

        Where :meth:`union_range` returns the one span that *covers* the range --
        elided text included -- this returns the text the range is actually made of: one
        span for each retained run it overlaps, in order, adjacent runs that are
        contiguous in the union merged. A hit on ``right of subrogation`` in the
        accepted view is the spans of ``right of `` and ``subrogation``, never the
        deleted ``recovery`` between them. This is the ``spans`` a term hit carries
        (design D6); a citation built on it never highlights text the view removed.
        """
        if not 0 <= start < end <= len(self.text):
            raise ValueError(f"view range [{start}, {end}) is not a range of {len(self.text)}")
        spans: list[Span] = []
        index = bisect_right(self.runs, start, key=lambda run: run.view_end)
        while index < len(self.runs) and self.runs[index].view_start < end:
            run = self.runs[index]
            low = max(start, run.view_start)
            high = min(end, run.view_end)
            first = run.union_start + low - run.view_start
            last = run.union_start + high - run.view_start
            if spans and spans[-1].end == first:
                spans[-1] = Span(part_id=self.part_id, start=spans[-1].start, end=last)
            else:
                spans.append(Span(part_id=self.part_id, start=first, end=last))
            index += 1
        return spans

    def retained_before(self, union_offset: int) -> int:
        """The view offset ``union_offset`` maps to: how much this view kept before it.

        The inverse of the map :meth:`union_range` states forwards, for a caller that has
        union offsets and wants the view's (6c's ``view_spans`` are whole-part view offsets,
        so a node's union span has to be read *into* the view it is matched in). A run's
        ``view_start`` is already the count of retained characters before it -- the runs
        tile the view text -- so the answer is that count plus the run's own part before
        the offset.

        An offset **no run covers** -- text this view elided -- maps to where the view
        resumes: gap-closing is a concatenation, so the offset where elided text was is the
        offset the text after it now sits at. That makes a range's two ends map to equal
        view offsets exactly when the view kept nothing of it, which is how a caller tells
        an elided location (nothing to match) from a kept one.
        """
        index = bisect_right(self.runs, union_offset, key=lambda run: run.union_start)
        if index == 0:
            return 0
        run = self.runs[index - 1]
        return run.view_start + min(union_offset - run.union_start, run.view_end - run.view_start)

    def _run_at(self, offset: int) -> Run:
        index = bisect_right(self.runs, offset, key=lambda run: run.view_end)
        if index < len(self.runs) and self.runs[index].view_start <= offset:
            return self.runs[index]
        raise ValueError(f"no retained character at view offset {offset}")


def project(
    stream: UnionStream, view: View, start: int = 0, end: int | None = None
) -> Projection:
    """Project ``stream``'s union range ``[start, end)`` through ``view``'s mask.

    One paragraph's range gives that paragraph's view text: the spans the mask elides
    are removed entirely and the rest concatenated in order (section 4). The whole
    part gives the whole-part projection the offset map is stated over (section 7):
    terminators kept, so it is the per-paragraph view texts joined by ``"\\n"`` and
    gap-closing never crosses a paragraph. A range need not start or end on a span
    edge: it is clipped to the spans it overlaps.
    """
    stop = len(stream.text) if end is None else end
    if not 0 <= start <= stop <= len(stream.text):
        raise ValueError(f"union range [{start}, {stop}) is not a range of {len(stream.text)}")
    pieces: list[str] = []
    runs: list[Run] = []
    cursor = 0
    # The spans tile the union in order, so the first one that overlaps the range is found
    # by search and the scan stops at the range's end: projecting a paragraph costs that
    # paragraph, not the whole part (a range per paragraph would otherwise be quadratic).
    first = bisect_right(stream.spans, start, key=lambda span: span.end)
    for span in stream.spans[first:]:
        if span.start >= stop:
            break
        low = max(span.start, start)
        high = min(span.end, stop)
        if low >= high or not _retained(stream, span, view):
            continue
        text = stream.text[low:high]
        runs.append(Run(view_start=cursor, view_end=cursor + len(text), union_start=low))
        pieces.append(text)
        cursor += len(text)
    return Projection(view=view, part_id=stream.part_id, text="".join(pieces), runs=tuple(runs))


def _retained(stream: UnionStream, span: ElementarySpan, view: View) -> bool:
    """Whether ``view`` keeps ``span``: its mask, plus the paragraph boundary.

    A terminator is kept by every mask, ``superseded`` included -- it is the boundary
    gap-closing never crosses (section 4), not text a view has an opinion about. The
    union text is consulted for that fact alone; a literal ``"\\n"`` never reaches a
    span otherwise, since the walker maps every other break to ``U+000B``.
    """
    if not span.stack and stream.text[span.start : span.end] == TERMINATOR:
        return True
    return keeps(span.stack, view)
