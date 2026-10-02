"""The query layer over a store: eight plain functions that answer questions (Turn 5, D12).

Every function takes the ``Store`` as given -- the caller decides ``read_only`` -- walks the
records a run already wrote, and returns dataclasses rather than a scalar, so an answer
always carries its evidence. Every answer carries a :class:`Citation`: the document, the
address (chunk id, node id, or ``comment:<para_id>``), the union spans, and the view its
text was read in, so a caller can check what it was told against the record it came from.
Nothing here writes: reads go through the catalog's helpers (``find_readable`` never
evicts, and :func:`~wordextract.catalog.runs_by_age` is the same run enumeration the
catalog uses), so a query over a read-only store is byte-for-byte what it is over a
writable one.

The rules these functions keep:

* **A view is never implied.** ``get_chunk`` renders the view it is given and echoes it
  on the row and its citation; ``search`` echoes the views it scanned -- both body
  readings (:data:`~wordextract.rank.DEFAULT_VIEWS`) unless told otherwise, never
  accepted-only. Section titles and document roll-ups are accepted-view facts (they are
  built from the parse's section tree and the stored roll-up) and are stated as such.
  ``find_terms`` citations are union addresses, which are view-independent (D6): their
  ``view`` is None and the views each hit occurs in are on the row itself.
* **Unknown stays unknown.** An explicit ``doc``, ``scope``, ``chunk_id`` or
  ``doc_a``/``doc_b`` lookup raises :class:`LookupError` naming the state when the store
  has no readable record for it; a listing over *every* document simply cannot include a
  document it cannot read (``catalog.list_documents`` reports those rows' unattributed
  fields as ``None``, and that is where the half-known states stay visible). A roll-up
  nothing has written reads as ``summary=None`` with ``has_pending=None`` -- never "" and
  never False. Tri-state flags are carried exactly as stored.
* **The term list is decision 3.** ``term_list=None`` means the store's only list; zero
  or several stored lists raise :class:`ValueError` naming the candidates, through
  :func:`~wordextract.rank.pick_term_list`.
* **A query that equals no term group is not an error**: :func:`~wordextract.rank.resolve_group`
  returns None, ``find_terms`` answers with no rows (a missing tier is an answer, like
  ``rank``'s), and ``search`` echoes ``resolved_group=None`` and still runs the text and
  summary tiers.
* **Absent is not zero.** A document whose stored hits belong to another term list
  contributes no term-tier rows -- which is not a claim that the group does not occur
  there (only a matcher run under this list can say that). ``compare`` raises when two
  documents share no stored view rather than reporting an empty difference: nothing to
  compare is a failure to compare (rule 10), not a finding.

Turn 5 touches no version in ``versions.py`` and no key: nothing here computes or writes a
stored artifact, so no behavior the ledger fingerprints moves -- only ``catalog`` and
``render`` grow public names for helpers query reads through them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from docextract_core import CodecError

from . import catalog
from .model import (
    Chunk,
    Comment,
    LocationKind,
    MatchType,
    NodeKind,
    ParseResult,
    RevisionKind,
    Section,
    Span,
    TermHit,
    ThreadingStatus,
    View,
)
from .nodes import anchor_node, parents, section_of
from .pending import pending_changes
from .rank import (
    DEFAULT_VIEWS,
    SOURCES,
    DocumentInput,
    RankedResult,
    SummaryRef,
    pick_term_list,
    rank,
    resolve_group,
)
from .render import chunk_text, comments_in, render_union_markup
from .store import Store
from .terms import TermRegistry, load_registry, registry_hashes

#: The address a hit (and a citation) uses for a comment: ``comment:<para_id>`` (D4/D12).
_COMMENT_PREFIX = "comment:"

#: Artifact id prefixes in a run's cache, as summarize writes them.
_SUMMARY_PREFIX = "summary:"
_ROLLUP_PREFIX = "rollup:"

#: The node kinds a chunk's text and union spans are made of (the chunker's leaves).
_LEAF_KINDS = frozenset({NodeKind.HEADING, NodeKind.PARA, NodeKind.LIST_ITEM})


# --- what an answer carries -------------------------------------------------------------


@dataclass(frozen=True)
class Citation:
    """Where an answer came from, in the store's own address space (D12).

    ``node_id`` is the walker's node id, ``chunk_id`` the chunker's, ``comment_id`` the
    ``comment:<para_id>`` reference -- whichever the cited thing is addressed by; the
    others are None. ``spans`` are union addresses (D2), one per retained run, and
    ``view`` names the view whose text was rendered for this answer: None on a
    union-address citation (a hit or a comment anchor), whose spans need no view.
    """

    document_id: str
    node_id: str | None = None
    chunk_id: str | None = None
    comment_id: str | None = None
    view: View | None = None
    spans: tuple[Span, ...] = ()


@dataclass(frozen=True)
class DocumentRow:
    """One document as ``list_documents`` reports it: the catalog row plus its roll-up.

    The catalog fields are the store's own (Turn 0d); ``views`` are the stored view ids
    this document was ingested and chunked in -- always named, never assumed. The last
    three are this turn's: ``comment_count`` is the parse's comment records, None when
    the parse was not readable (did not read, not "no comments"); ``summary`` is the
    document roll-up's text and ``has_pending`` its tri-state flag, both None when no
    run has written a readable roll-up for the document.
    """

    document_id: str
    title: str
    latest_run_id: str
    views: tuple[str, ...]
    comment_count: int | None
    unattributed_revisions: int | None
    unattributed_gaps: int | None
    summary: str | None
    has_pending: bool | None


@dataclass(frozen=True)
class OutlineSection:
    """One section of ``get_outline``'s tree: the heading it is named after, and its roll-up.

    ``summary`` and ``has_pending`` are the roll-up stored for this section's heading
    (``rollup:<heading_id>``) -- None when no run has written one, with ``has_pending``
    None alongside rather than a False that would read as "nothing pending" (rule 8).
    """

    heading_id: str
    title: str
    level: int
    summary: str | None
    has_pending: bool | None
    children: tuple["OutlineSection", ...] = ()


@dataclass(frozen=True)
class Outline:
    """A document's heading tree as the accepted view defines it (Turn 4b's section tree).

    ``view`` is :attr:`~View.ACCEPTED` because a section's title is the heading's text in
    the accepted view -- that is what the stored ``Section.title`` is -- so the outline
    names sections the way the document calls them. ``summary``/``has_pending`` are the
    document roll-up (``rollup:<document_id>``); each section carries its own beside it.
    """

    document_id: str
    title: str
    view: View
    summary: str | None
    has_pending: bool | None
    sections: tuple[OutlineSection, ...]


@dataclass(frozen=True)
class ChunkRow:
    """One chunk in one view: its bytes both ways, its members, and its citation.

    ``text`` is the view's rendering (:func:`~wordextract.render.chunk_text`) -- what
    ``Chunk.size`` and the content hash are over -- and ``markup`` is the union with
    revision and comment markup (:func:`~wordextract.render.render_union_markup`), the
    same bytes the summarizer reads. ``node_ids`` are the chunk's members (the chunker's
    node order) and ``comments`` are the chunk's by the chunker's containment rule,
    exactly the ones ``markup`` cites. ``pending_changes`` is the manifest
    :func:`~wordextract.pending.pending_changes` computes for this chunk -- the same
    entries a summary record carries -- empty when no revision touches the chunk, never
    a None that would read as "not evaluated" for a chunk the parse read fine. Both renderings come from the one stored parse, so a chunk whose
    view no longer holds the text still answers.
    """

    document_id: str
    chunk_id: str
    view: View
    section_path: tuple[str, ...]
    node_ids: tuple[str, ...]
    text: str
    markup: str
    comments: tuple[Comment, ...]
    pending_changes: tuple[dict[str, Any], ...]
    citation: Citation


@dataclass(frozen=True)
class CommentRow:
    """One comment record (D4): identity, threading, and where it sits.

    ``comment_id`` is the ``comment:<para_id>`` reference a citation and a hit use.
    ``threading_status`` and ``resolved`` are stored facts, never re-derived: absent
    threading is absent, unknown is unknown. ``section_path`` is () when no node holds
    the anchor (no anchor at all, a paragraph-terminator anchor, or text outside every
    section) -- the honest answer, not the first section.
    """

    document_id: str
    title: str
    para_id: str
    comment_id: str
    author: str
    initials: str
    date: str | None
    parent_id: str | None
    threading_status: ThreadingStatus
    resolved: bool | None
    anchor_text: str
    text: str
    section_path: tuple[str, ...]
    citation: Citation


@dataclass(frozen=True)
class RevisionRow:
    """One revision record (D9), with its own text and every section it touches.

    ``text`` is the union text under the revision's stack, joined raw across the tiling
    pieces exactly as Turn 2's manifest computes it -- the same bytes, one derivation.
    ``spans`` are the union ranges it covers, coalesced across adjacent pieces; a
    paragraph-mark revision nothing is stacked on has empty text and no spans (it is
    also what the document's ``unattributed_revisions`` counts). ``section_paths`` is
    every section a piece of its text stands in, in order, deduplicated -- and a
    revision of text outside every section contributes (), so a section filter excludes
    it rather than inventing a home. ``citation`` names the node its first piece anchors
    at, when one does.
    """

    revision_id: str
    kind: RevisionKind
    author: str
    date: str | None
    move_group_id: str | None
    text: str
    spans: tuple[Span, ...]
    section_paths: tuple[tuple[str, ...], ...]
    citation: Citation


@dataclass(frozen=True)
class DocumentRevisions:
    """One document's revisions, plus the pending counts the catalog reports for it.

    The counts are the catalog's fields verbatim -- None when the parse was not
    readable, which is why a listing that skips such a document never silently
    reports its revisions as none.
    """

    document_id: str
    title: str
    revisions: tuple[RevisionRow, ...]
    unattributed_revisions: int | None
    unattributed_gaps: int | None


@dataclass(frozen=True)
class RevisionsResult:
    """``get_revisions``' answer: what was asked for, and the documents it read."""

    doc: str | None
    documents: tuple[DocumentRevisions, ...]


@dataclass(frozen=True)
class HitRow:
    """One stored hit (D6), addressable and sectioned: union spans, never view offsets.

    ``node_id`` is the hit's own address -- a node id, or ``comment:<para_id>`` for a
    comment hit; ``comment_id`` mirrors it when it is a comment. ``views`` are the views
    the hit occurs in, sorted; for a comment hit that is (), because a comment belongs
    to no view (D6). ``chunk_id`` is a pointer for :func:`get_chunk`: the chunk the
    first stored view's chunk set holds for that node -- a comment hit through its
    anchor's node, rank's dedupe -- None when no chunk holds it (a header or footnote
    is outside the body's chunks -- honest None, not a fabricated body chunk).
    ``citation`` repeats the address with union spans and view None.
    """

    document_id: str
    node_id: str
    chunk_id: str | None
    location: LocationKind
    match_type: MatchType
    views: tuple[View, ...]
    move_group_id: str | None
    spans: tuple[Span, ...]
    comment_id: str | None
    citation: Citation


@dataclass(frozen=True)
class SectionHits:
    """The hits under one section path (outermost title first), in stored match order."""

    section_path: tuple[str, ...]
    hits: tuple[HitRow, ...]


@dataclass(frozen=True)
class DocumentHits:
    """One document's ``find_terms`` rows, grouped by section in first-appearance order."""

    document_id: str
    title: str
    sections: tuple[SectionHits, ...]


@dataclass(frozen=True)
class FindTermsResult:
    """``find_terms``' answer: the group as asked and as resolved, under its term list.

    ``term_list`` is the hash actually matched under (decision 3). ``resolved_group`` is
    the canonical form of the group the query equals, or None when it equals no stored
    group -- and then ``documents`` is empty: a query equal to no group is an answer
    with no rows, not an error. ``group`` echoes what was asked.
    """

    group: str
    resolved_group: str | None
    term_list: str
    documents: tuple[DocumentHits, ...]


@dataclass(frozen=True)
class SearchResult:
    """``search``' answer: :func:`~wordextract.rank.rank`'s results with the run's settings.

    ``resolved_group`` is None when the query equals no stored group (the text and
    summary tiers still ran); ``sources`` echoes the tier filter (all of
    :data:`~wordextract.rank.SOURCES` when none was asked); ``views`` echoes the views
    scanned -- the default both body readings unless told otherwise. ``scope`` echoes
    the document asked for (None = every readable document). No numeric relevance
    exists anywhere: the results are ``rank``'s tier order.
    """

    query: str
    term_list: str
    resolved_group: str | None
    scope: str | None
    sources: tuple[str, ...]
    views: tuple[View, ...]
    results: tuple[RankedResult, ...]


@dataclass(frozen=True)
class ChunkChange:
    """A chunk only one side of a ``compare`` has, in that side's document and view."""

    chunk_id: str
    section_path: tuple[str, ...]
    citation: Citation


@dataclass(frozen=True)
class ViewComparison:
    """The chunk difference between two documents in one shared stored view.

    ``unchanged_chunks`` counts the ids both sides hold -- chunks whose text is the same.
    ``comments_changed`` names those same-id chunks whose comments differ (``context_hash``:
    a comment edited, added, removed or resolved), because a chunk id is over the text alone
    and would otherwise report an edited comment as no change. ``hits_only_in_a`` and
    ``hits_only_in_b`` are chunk ids carrying at least one hit of the compared group
    **on that side alone**: the same chunk id carrying the group in both documents
    belongs to neither list (the same text holds the group's hits on both sides), while
    a chunk one side holds the group in and the other does not names that side alone.
    Both lists are () when no group was given, and also when the group
    resolved to nothing or the documents' stored hits are under another term list --
    which means "not compared", never "neither document has it".
    """

    view: View
    only_in_a: tuple[ChunkChange, ...]
    only_in_b: tuple[ChunkChange, ...]
    unchanged_chunks: int
    hits_only_in_a: tuple[str, ...]
    hits_only_in_b: tuple[str, ...]
    comments_changed: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompareResult:
    """``compare``' answer: two named documents, per shared view, over the chunks themselves.

    Chunk identity is the chunker's: an id is the hash of content (per view) and
    occurrence, so a chunk whose bytes changed under any view it was chunked in is on
    exactly one side. ``term_list`` is None unless a ``group`` was asked for.
    """

    document_a: str
    document_b: str
    group: str | None
    resolved_group: str | None
    term_list: str | None
    views: tuple[ViewComparison, ...]


__all__ = [
    "Citation",
    "ChunkChange",
    "ChunkRow",
    "CommentRow",
    "CompareResult",
    "DocumentHits",
    "DocumentRevisions",
    "DocumentRow",
    "FindTermsResult",
    "HitRow",
    "Outline",
    "OutlineSection",
    "RevisionsResult",
    "RevisionRow",
    "SearchResult",
    "SectionHits",
    "ViewComparison",
    "compare",
    "find_terms",
    "get_chunk",
    "get_comments",
    "get_outline",
    "get_revisions",
    "list_documents",
    "search",
]


# --- what the run log names (the catalog enumerates; query reads alongside) -------------


@dataclass(frozen=True)
class _Stored:
    """A catalog row plus the artifact keys this document's runs name (Turn 5's view).

    ``summary_keys`` maps chunk id to the latest summarize pass's entry, ``rollup_keys``
    maps target id (a heading id or the document id) to the latest pass that rolled it
    up, and ``hits_keys`` maps a term-list hash to the latest hits entry -- last write
    wins by run age, so an artifact a later run did not re-list keeps its older entry
    rather than being dropped. The catalog keeps these keys private, so this re-reads
    the run log through :func:`catalog.runs_by_age` -- the same enumeration, not a
    second opinion.
    """

    entry: catalog.DocumentEntry
    parse_key: str
    summary_keys: dict[str, str]
    rollup_keys: dict[str, str]
    hits_keys: dict[str, str]


def _stored(store: Store) -> list[_Stored]:
    """Every document the catalog holds, in catalog order, with its runs' artifact keys."""
    entries = {entry.document_id: entry for entry in catalog.list_documents(store)}
    keyed: dict[str, dict[str, Any]] = {
        document_id: {
            "parse_key": "",
            "summary_keys": {},
            "rollup_keys": {},
            "hits_keys": {},
        }
        for document_id in entries
    }
    for _written_at, _run_id, record in catalog.runs_by_age(store):
        state = keyed.get(record.hashed_inputs.source_content_hash)
        if state is None:
            continue
        artifacts = {item.artifact_id: item.recomputed_key for item in record.artifact_cache}
        parse_key = artifacts.get("parse")
        if parse_key is not None:
            state["parse_key"] = parse_key
        for artifact_id, key in artifacts.items():
            if artifact_id.startswith(_SUMMARY_PREFIX):
                state["summary_keys"][artifact_id[len(_SUMMARY_PREFIX) :]] = key
            elif artifact_id.startswith(_ROLLUP_PREFIX):
                state["rollup_keys"][artifact_id[len(_ROLLUP_PREFIX) :]] = key
        hits_key = artifacts.get("hits")
        if hits_key is not None:
            state["hits_keys"][record.hashed_inputs.term_list_hash] = hits_key
    return [
        _Stored(
            entry=entries[document_id],
            parse_key=str(state["parse_key"]),
            summary_keys=state["summary_keys"],
            rollup_keys=state["rollup_keys"],
            hits_keys=state["hits_keys"],
        )
        for document_id, state in keyed.items()
    ]


def _resolve(rows: Sequence[_Stored], wanted: str) -> _Stored:
    """The one document ``wanted`` names: its id first, else its exact title.

    The title is the filename -- a caller who knows the document knows that -- but an id
    always wins, a title naming several documents is an error listing them (picking one
    would be a guess), and an unknown name is an error naming what the store holds.
    """
    for stored in rows:
        if stored.entry.document_id == wanted:
            return stored
    titled = [stored for stored in rows if stored.entry.title == wanted]
    if len(titled) == 1:
        return titled[0]
    if len(titled) > 1:
        raise LookupError(
            f"title {wanted!r} names several documents: "
            f"{sorted(stored.entry.document_id for stored in titled)}"
        )
    raise LookupError(
        f"no document {wanted!r} in this store; it holds "
        f"{sorted((stored.entry.title, stored.entry.document_id) for stored in rows)}"
    )


def _require_parse(store: Store, stored: _Stored) -> ParseResult:
    """The document's parse, or a :class:`LookupError` naming the state.

    An explicit lookup is loud where a listing would skip: "never parsed by a run this
    code can read" and "parsed, but the blob is missing or undecodable" are different
    states and the message says which.
    """
    if not stored.parse_key:
        raise LookupError(
            f"document {stored.entry.document_id} ({stored.entry.title}) was never parsed "
            f"by a run this code can read; list_documents reports its unattributed fields "
            f"as None"
        )
    artifact = catalog.find_readable(store.parse, stored.parse_key)
    if artifact is None:
        raise LookupError(
            f"document {stored.entry.document_id} ({stored.entry.title})'s parse is "
            f"missing or unreadable under key {stored.parse_key}"
        )
    return artifact.parsed


def _optional_parse(store: Store, stored: _Stored) -> ParseResult | None:
    """The document's parse, or None when the store has none readable (then: skip)."""
    if not stored.parse_key:
        return None
    artifact = catalog.find_readable(store.parse, stored.parse_key)
    return None if artifact is None else artifact.parsed


def _chunks(store: Store, stored: _Stored, view_id: str) -> tuple[Chunk, ...]:
    """The document's chunks in one stored view, in document order."""
    view = next((item for item in stored.entry.views if item.view == view_id), None)
    if view is None:
        raise LookupError(
            f"document {stored.entry.document_id} holds no view {view_id!r}; its stored "
            f"views are {[item.view for item in stored.entry.views]}"
        )
    if not view.chunks:
        raise LookupError(
            f"document {stored.entry.document_id}, view {view_id!r}, names no chunk set"
        )
    artifact = catalog.find_readable(store.chunks, view.chunks)
    if artifact is None:
        raise LookupError(
            f"document {stored.entry.document_id}, view {view_id!r}: chunk set missing or "
            f"unreadable under key {view.chunks}"
        )
    return tuple(artifact.chunks)


def _hits(store: Store, stored: _Stored, term_list: str) -> tuple[TermHit, ...]:
    """The document's stored hits for one term list.

    () when no run matched this document under this list -- the document contributes no
    term-tier rows, which is absence of evidence, not evidence of absence. A hits record
    that is named but unreadable raises instead: that is corruption, and quietly
    comparing nothing is the one thing a query must not do.
    """
    key = stored.hits_keys.get(term_list)
    if key is None:
        return ()
    artifact = catalog.find_readable(store.hits, key)
    if artifact is None:
        raise LookupError(
            f"document {stored.entry.document_id}'s hits for term list {term_list} are "
            f"missing or unreadable under key {key}"
        )
    return tuple(artifact.hits)


def _registry(store: Store, term_list: str) -> TermRegistry:
    """The stored registry for ``term_list``; unreadable is an error, not an empty answer."""
    try:
        registry = load_registry(store.terms, term_list)
    except CodecError:
        raise LookupError(f"term list {term_list} is stored but unreadable") from None
    if registry is None:
        raise LookupError(f"term list {term_list} is not stored")
    return registry


def _in_section(section_path: Sequence[str], wanted: Sequence[str] | None) -> bool:
    """Whether a row's path stands in the ``wanted`` section: an outermost-first prefix.

    ``None`` and ``()`` pass everything (no filter). A prefix match means the row is in
    that section *or a section under it*; orphans and sectionless rows pass only the
    empty filter, never a named section.
    """
    if not wanted:
        return True
    return list(section_path)[: len(wanted)] == list(wanted)


def _owner(chunks: Sequence[Chunk]) -> dict[str, str]:
    """node id → chunk id, the first chunk in document order winning -- rank's own rule."""
    owner: dict[str, str] = {}
    for chunk in chunks:
        for node_id in chunk.node_ids:
            owner.setdefault(node_id, chunk.id)
    return owner


def _comment_id(para_id: str) -> str:
    return f"{_COMMENT_PREFIX}{para_id}"


def _node_section(parsed: ParseResult, node_id: str, parent_of: dict[str, str]) -> tuple[str, ...]:
    """The section of an address as hits name it: a node id, or ``comment:<para_id>``.

    A comment resolves through its anchor; no comment, no anchor, or no node holding
    that offset answers () -- in no section, which is what ``section_of`` says for an
    id no node has.
    """
    target = _anchor_target(parsed, node_id, parent_of)
    return tuple(section_of(parsed, target)) if target else ()


def _anchor_target(parsed: ParseResult, node_id: str, parent_of: dict[str, str]) -> str:
    """The node an address resolves through for chunk ownership: itself, or a comment's anchor's.

    Rank dedupes a comment hit into its anchor chunk, so a ``comment:<para_id>`` hit row
    carries that chunk; an address no node can hold answers "" (no chunk owns it).
    """
    if not node_id:
        return ""
    if node_id.startswith(_COMMENT_PREFIX):
        comment = next(
            (item for item in parsed.comments if item.para_id == node_id[len(_COMMENT_PREFIX) :]),
            None,
        )
        if comment is None or comment.anchor is None:
            return ""
        return anchor_node(parsed, comment.anchor, parent_of) or ""
    return node_id


def _comment_row(
    entry: catalog.DocumentEntry,
    parsed: ParseResult,
    parent_of: dict[str, str],
    comment: Comment,
) -> CommentRow:
    """One comment record as a row: its anchor's node and section, or () where there is none."""
    node_id = anchor_node(parsed, comment.anchor, parent_of)
    return CommentRow(
        document_id=entry.document_id,
        title=entry.title,
        para_id=comment.para_id,
        comment_id=_comment_id(comment.para_id),
        author=comment.author,
        initials=comment.initials,
        date=comment.date,
        parent_id=comment.parent_id,
        threading_status=comment.threading_status,
        resolved=comment.resolved,
        anchor_text=comment.anchor_text,
        text=comment.text,
        section_path=tuple(section_of(parsed, node_id)) if node_id else (),
        citation=Citation(
            document_id=entry.document_id,
            node_id=node_id,
            comment_id=_comment_id(comment.para_id),
            spans=() if comment.anchor is None else (comment.anchor,),
        ),
    )


def _chunk_spans(parsed: ParseResult, chunk: Chunk) -> tuple[Span, ...]:
    """The union spans of the chunk's text leaves, in the chunk's own node order.

    One span per retained run of a leaf (table, header and footnote containers hold no
    text of their own), so a citation covers what ``text`` renders -- never the
    chunk's elided range across views (rule 6: never ``union_range``).
    """
    by_id = {node.id: node for node in parsed.nodes}
    spans: list[Span] = []
    for node_id in chunk.node_ids:
        node = by_id.get(node_id)
        if node is not None and node.kind in _LEAF_KINDS:
            spans.extend(node.spans)
    return tuple(spans)


@dataclass
class _Place:
    """One revision's accumulated facts over the union streams (text, ranges, section)."""

    text: str = ""
    spans: list[Span] = field(default_factory=list)
    node_id: str | None = None
    sections: list[tuple[str, ...]] = field(default_factory=list)


def _revision_places(parsed: ParseResult, parent_of: dict[str, str]) -> dict[str, _Place]:
    """Walk the union streams in document order, accumulating per revision id on a stack.

    The text is each stacked piece's union text joined raw -- the same derivation Turn 2's
    manifest excerpt uses, so a summary's pending text and this row's text cannot drift.
    Ranges coalesce while adjacent; the first piece that anchors names the row's
    citation node; each piece's section joins the order-preserving ``sections`` list
    ((), for a piece outside every section, stays in the list so a section filter sees
    it). Ids on stacks without a record never become rows (pending's rule), and a record
    nothing stacks on keeps an empty place.
    """
    places: dict[str, _Place] = {}
    for stream in parsed.union_streams:
        for elementary in stream.spans:
            if not elementary.stack:
                continue
            for revision_id in elementary.stack:
                place = places.setdefault(revision_id, _Place())
                place.text += stream.text[elementary.start : elementary.end]
                span = Span(stream.part_id, elementary.start, elementary.end)
                if place.spans and place.spans[-1].part_id == span.part_id:
                    if place.spans[-1].end == span.start:
                        place.spans[-1] = Span(span.part_id, place.spans[-1].start, span.end)
                    else:
                        place.spans.append(span)
                else:
                    place.spans.append(span)
                node_id = anchor_node(parsed, span, parent_of)
                if place.node_id is None and node_id is not None:
                    place.node_id = node_id
                path = tuple(section_of(parsed, node_id)) if node_id else ()
                if path not in place.sections:
                    place.sections.append(path)
    return places


def _summaries(
    store: Store, stored: _Stored, chunk_ids: set[str]
) -> tuple[SummaryRef, ...]:
    """The stored summaries of ``chunk_ids``, as :class:`rank.SummaryRef` records.

    A summary of a chunk outside the set is skipped rather than left for ``rank`` to
    trip over: summaries are keyed by rendered input, and this document is being read
    through one chunk set. An entry named but unreadable is an error, like an unreadable
    hits record.
    """
    refs: list[SummaryRef] = []
    for key in stored.summary_keys.values():
        artifact = catalog.find_readable(store.summaries, key)
        if artifact is None:
            raise LookupError(
                f"document {stored.entry.document_id}: summary missing or unreadable "
                f"under key {key}"
            )
        if artifact.chunk_id not in chunk_ids:
            continue
        refs.append(
            SummaryRef(
                chunk_id=artifact.chunk_id,
                text=artifact.summary,
                topics=tuple(artifact.topics),
            )
        )
    return tuple(refs)


def _rollup(store: Store, stored: _Stored, target_id: str) -> tuple[str | None, bool | None]:
    """A stored roll-up's text and tri-state flag; (None, None) when nothing readable holds."""
    key = stored.rollup_keys.get(target_id)
    if key is None:
        return None, None
    artifact = catalog.find_readable(store.rollups, key)
    if artifact is None:
        return None, None
    return artifact.summary, artifact.has_pending


def _selected(store: Store, rows: Sequence[_Stored], doc: str | None) -> list[tuple[_Stored, ParseResult]]:
    """The documents a listing reads: every readable one, or the one explicit ``doc``.

    Explicit means loud -- an unreadable explicit document raises where the all-document
    listing skips it (documented in the module docstring).
    """
    chosen: list[tuple[_Stored, ParseResult]] = []
    for stored in rows if doc is None else [_resolve(rows, doc)]:
        parsed = _optional_parse(store, stored) if doc is None else _require_parse(store, stored)
        if parsed is not None:
            chosen.append((stored, parsed))
    return chosen


def _load(store: Store, term_list: str | None) -> tuple[str, TermRegistry]:
    """The term list to match under and its registry (decision 3)."""
    chosen = pick_term_list(term_list, registry_hashes(store.terms))
    return chosen, _registry(store, chosen)


# --- the eight functions ----------------------------------------------------------------


def list_documents(store: Store) -> list[DocumentRow]:
    """Every document the store holds, in catalog order (title, then id), with its roll-up.

    The catalog fields are unchanged from ``catalog.list_documents``; this adds the
    document roll-up (``rollup:<document_id>``) and a parse-derived comment count. A
    roll-up no run has written, or one that is named but unreadable, reads as
    ``summary=None`` with ``has_pending=None`` -- the difference between "not written"
    and "written: nothing pending" is the whole point of the tri-state (rule 8).
    """
    rows: list[DocumentRow] = []
    for stored in _stored(store):
        parsed = _optional_parse(store, stored)
        summary, has_pending = _rollup(store, stored, stored.entry.document_id)
        rows.append(
            DocumentRow(
                document_id=stored.entry.document_id,
                title=stored.entry.title,
                latest_run_id=stored.entry.latest_run_id,
                views=tuple(view.view for view in stored.entry.views),
                comment_count=None if parsed is None else len(parsed.comments),
                unattributed_revisions=stored.entry.unattributed_revisions,
                unattributed_gaps=stored.entry.unattributed_gaps,
                summary=summary,
                has_pending=has_pending,
            )
        )
    return rows


def get_outline(store: Store, doc: str) -> Outline:
    """One document's heading tree, each section with its stored roll-up (Turn 4b's tree).

    The tree is the parse's section tree -- titles are the headings' text in the accepted
    view, so ``view`` says so -- and each section carries the roll-up stored under its
    heading id; the outline's own summary/has_pending are the document roll-up. Both read
    None when nothing readable holds a roll-up. An unreadable or unknown document raises
    :class:`LookupError` naming the state.
    """
    rows = _stored(store)
    stored = _resolve(rows, doc)
    parsed = _require_parse(store, stored)

    def section_row(section: Section) -> OutlineSection:
        summary, has_pending = _rollup(store, stored, section.heading_id)
        return OutlineSection(
            heading_id=section.heading_id,
            title=section.title,
            level=section.level,
            summary=summary,
            has_pending=has_pending,
            children=tuple(section_row(child) for child in section.children),
        )

    summary, has_pending = _rollup(store, stored, stored.entry.document_id)
    return Outline(
        document_id=stored.entry.document_id,
        title=stored.entry.title,
        view=View.ACCEPTED,
        summary=summary,
        has_pending=has_pending,
        sections=tuple(section_row(section) for section in parsed.sections),
    )


def get_chunk(
    store: Store, chunk_id: str, view: str, *, doc: str | None = None
) -> ChunkRow:
    """One chunk, read in the view asked for: its text, its members, its comments.

    ``view`` is a stored view id (:class:`View`'s values); an unknown one raises naming
    the views the store holds. Only views a run ingested are readable: accepted and original
    chunk ids differ, so there is no mapping from one view's id to the other's. The deleted
    text of an accepted-view chunk is still visible in ``markup`` and ``pending_changes``. The lookup scans every document's chunks in that view --
    chunk ids are content hashes and cannot collide across views -- and an id that lands
    in two documents, or one found nowhere, raises :class:`LookupError` naming the
    candidates rather than picking. ``doc`` (a document id, or its exact title) is how a
    caller that already knows the side -- search results carry ``document_id`` -- picks
    one of those candidates: identical bytes in two documents are the same chunk id by
    construction, while their comments and pending manifests are still their own. Both
    renderings come from the one stored parse, ``comments`` are the same containment rule
    ``markup`` cites (the chunker's), and ``pending_changes`` is Turn 2's manifest over
    this chunk's own nodes.
    """
    rows = _stored(store)
    if doc is not None:
        rows = [_resolve(rows, doc)]
    held = sorted({item.view for row in rows for item in row.entry.views})
    try:
        wanted = View(view)
    except ValueError:
        raise LookupError(f"unknown view {view!r}; this store's views are {held}") from None
    found: list[tuple[_Stored, Chunk]] = []
    for stored in rows:
        for view_entry in stored.entry.views:
            if view_entry.view != wanted.value:
                continue
            if not view_entry.chunks:
                continue
            artifact = catalog.find_readable(store.chunks, view_entry.chunks)
            if artifact is None:
                raise LookupError(
                    f"document {stored.entry.document_id}, view {wanted.value!r}: chunk "
                    f"set missing or unreadable under key {view_entry.chunks}"
                )
            chunk = next((item for item in artifact.chunks if item.id == chunk_id), None)
            if chunk is not None:
                found.append((stored, chunk))
    if not found:
        raise LookupError(
            f"no chunk {chunk_id!r} in view {wanted.value!r}; this store's views are {held}; "
            f"its documents are {sorted({row.entry.document_id for row in rows})}"
        )
    documents = {stored.entry.document_id for stored, _chunk in found}
    if len(documents) > 1:
        raise LookupError(
            f"chunk {chunk_id!r} in view {wanted.value!r} belongs to several documents: "
            f"{sorted(documents)}"
        )
    stored, chunk = found[0]
    parsed = _require_parse(store, stored)
    return ChunkRow(
        document_id=stored.entry.document_id,
        chunk_id=chunk.id,
        view=wanted,
        section_path=tuple(chunk.section_path),
        node_ids=tuple(chunk.node_ids),
        text=chunk_text(parsed, chunk, wanted),
        markup=render_union_markup(parsed, chunk),
        comments=tuple(comments_in(parsed, chunk)),
        pending_changes=tuple(pending_changes(parsed, chunk)),
        citation=Citation(
            document_id=stored.entry.document_id,
            chunk_id=chunk.id,
            view=wanted,
            spans=_chunk_spans(parsed, chunk),
        ),
    )


def get_comments(
    store: Store,
    doc: str | None = None,
    *,
    section: Sequence[str] | None = None,
    author: str | None = None,
) -> list[CommentRow]:
    """The store's comment records (D4), optionally one document's, by section and author.

    ``doc`` is a document id, or its exact title when that names one document. ``section``
    is an outermost-first title path (the accepted-view titles ``get_outline`` shows):
    rows in that section or under it pass, and a comment with no node holding its anchor
    has section () so a named section excludes it honestly. ``author`` is exact
    equality on the record's author. Without ``doc``, every document the parse reads is
    listed in catalog order, comments in document order; a document whose parse is not
    readable cannot contribute and does not (``list_documents`` shows which).
    """
    rows = _stored(store)
    comments: list[CommentRow] = []
    for stored, parsed in _selected(store, rows, doc):
        parent_of = parents(parsed)
        for comment in parsed.comments:
            row = _comment_row(stored.entry, parsed, parent_of, comment)
            if author is not None and row.author != author:
                continue
            if not _in_section(row.section_path, section):
                continue
            comments.append(row)
    return comments


def get_revisions(
    store: Store,
    doc: str | None = None,
    *,
    section: Sequence[str] | None = None,
) -> RevisionsResult:
    """The store's revision records (D9), one document's or every readable document's.

    ``doc`` resolves like ``get_comments``; the result echoes it (None when everything
    readable was read). Each row carries the revision's own union text, its ranges, and
    every section its pieces stand in; ``section`` keeps the rows touching that section
    (a prefix path, outermost first), and a revision of text outside every section passes
    only when no section was asked for. The document counts are the catalog's, so
    ``unattributed_revisions`` stays None beside a parse that was not read rather than
    pretending the store saw no revisions.
    """
    rows = _stored(store)
    documents: list[DocumentRevisions] = []
    for stored, parsed in _selected(store, rows, doc):
        parent_of = parents(parsed)
        places = _revision_places(parsed, parent_of)
        revisions: list[RevisionRow] = []
        for revision in parsed.revisions:
            place = places.get(revision.id, _Place())
            spans = tuple(place.spans)
            section_paths = tuple(place.sections)
            if section and not any(_in_section(path, section) for path in section_paths):
                continue
            revisions.append(
                RevisionRow(
                    revision_id=revision.id,
                    kind=revision.kind,
                    author=revision.author,
                    date=revision.date,
                    move_group_id=revision.move_group_id,
                    text=place.text,
                    spans=spans,
                    section_paths=section_paths,
                    citation=Citation(
                        document_id=stored.entry.document_id,
                        node_id=place.node_id,
                        spans=spans,
                    ),
                )
            )
        documents.append(
            DocumentRevisions(
                document_id=stored.entry.document_id,
                title=stored.entry.title,
                revisions=tuple(revisions),
                unattributed_revisions=stored.entry.unattributed_revisions,
                unattributed_gaps=stored.entry.unattributed_gaps,
            )
        )
    return RevisionsResult(doc=doc, documents=tuple(documents))


def find_terms(
    store: Store,
    group: str,
    *,
    term_list: str | None = None,
    doc: str | None = None,
    section: Sequence[str] | None = None,
) -> FindTermsResult:
    """The stored hits of the term group ``group`` names, grouped by document and section.

    ``group`` goes through ``rank.resolve_group``: its canonical form or a synonym of one
    stored group, nothing else -- no stem, no substring, and an ambiguous query names the
    groups rather than choosing. Resolving to no group answers with no rows: a missing
    tier is an answer, not an error. The term list follows decision 3; a document matched
    under another list contributes no rows (absence of evidence, see the module
    docstring). Rows keep the matcher's stored order (the move fold's ordinals depend on
    it), grouped into sections in first-appearance order, and ``section`` filters by an
    outermost-first title path, prefix-matching.
    """
    rows = _stored(store)
    if doc is not None:
        rows = [_resolve(rows, doc)]
    chosen, registry = _load(store, term_list)
    resolved = resolve_group(group, registry)
    documents: list[DocumentHits] = []
    if resolved is not None:
        for stored in rows:
            parsed = _optional_parse(store, stored) if doc is None else _require_parse(store, stored)
            if parsed is None:
                continue
            hits = _hits(store, stored, chosen)
            if not hits:
                continue
            parent_of = parents(parsed)
            owner = (
                _owner(_chunks(store, stored, stored.entry.views[0].view))
                if stored.entry.views
                else {}
            )
            buckets: dict[tuple[str, ...], list[HitRow]] = {}
            for hit in hits:
                if hit.group != resolved.canonical:
                    continue
                path = _node_section(parsed, hit.node_id, parent_of)
                if not _in_section(path, section):
                    continue
                anchor = _anchor_target(parsed, hit.node_id, parent_of)
                chunk_id = owner.get(anchor) if anchor else None
                comment_id = hit.node_id if hit.node_id.startswith(_COMMENT_PREFIX) else None
                buckets.setdefault(path, []).append(
                    HitRow(
                        document_id=stored.entry.document_id,
                        node_id=hit.node_id,
                        chunk_id=chunk_id,
                        location=hit.location,
                        match_type=hit.match_type,
                        views=tuple(sorted(hit.present_in)),
                        move_group_id=hit.move_group_id,
                        spans=tuple(hit.spans),
                        comment_id=comment_id,
                        citation=Citation(
                            document_id=stored.entry.document_id,
                            node_id=hit.node_id,
                            chunk_id=chunk_id,
                            comment_id=comment_id,
                            spans=tuple(hit.spans),
                        ),
                    )
                )
            if not buckets:
                continue
            documents.append(
                DocumentHits(
                    document_id=stored.entry.document_id,
                    title=stored.entry.title,
                    sections=tuple(
                        SectionHits(section_path=path, hits=tuple(section_rows))
                        for path, section_rows in buckets.items()
                    ),
                )
            )
    return FindTermsResult(
        group=group,
        resolved_group=None if resolved is None else resolved.canonical,
        term_list=chosen,
        documents=tuple(documents),
    )


def search(
    store: Store,
    query: str,
    *,
    term_list: str | None = None,
    scope: str | None = None,
    sources: Sequence[str] | None = None,
    views: Sequence[View] | None = None,
) -> SearchResult:
    """``rank``'s tiered answer over the stored records, with the settings it ran under.

    Each in-scope document is reassembled as a :class:`rank.DocumentInput`: its parse,
    one chunk set (the first stored view's -- the retrieval units are the chunker's, and
    a summary or hit outside that set contributes nothing rather than tripping the
    tier), its hits for the term list, and its summaries. ``scope`` is one document (id
    or exact title); None reads every document whose parse the store can show, and an
    unreadable scoped document raises rather than searching nothing. ``sources`` filters
    to tiers (:data:`rank.SOURCES`); ``views`` names which view readings the text tier
    scans, defaulting to both body readings -- accepted and original, never
    accepted-only. ``term_list`` follows decision 3; the result echoes everything so the
    answer states the question it answered.
    """
    rows = _stored(store)
    if scope is not None:
        rows = [_resolve(rows, scope)]
    if sources is None:
        wanted_sources = tuple(SOURCES)
    else:
        unknown = [source for source in sources if source not in SOURCES]
        if unknown:
            raise ValueError(f"unknown sources {unknown}; the tiers are {list(SOURCES)}")
        wanted_sources = tuple(sources)
    scanned = tuple(
        View(view) for view in (DEFAULT_VIEWS if views is None else views)
    )
    chosen, registry = _load(store, term_list)
    resolved = resolve_group(query, registry)
    inputs: list[DocumentInput] = []
    for stored in rows:
        parsed = _optional_parse(store, stored) if scope is None else _require_parse(store, stored)
        if parsed is None or not stored.entry.views:
            continue
        chunks = _chunks(store, stored, stored.entry.views[0].view)
        inputs.append(
            DocumentInput(
                document_id=stored.entry.document_id,
                parsed=parsed,
                chunks=chunks,
                hits=_hits(store, stored, chosen),
                summaries=_summaries(store, stored, {chunk.id for chunk in chunks}),
            )
        )
    results = [
        result
        for result in rank(query, inputs, registry=registry, views=scanned)
        if sources is None or any(source in result.sources for source in wanted_sources)
    ]
    return SearchResult(
        query=query,
        term_list=chosen,
        resolved_group=None if resolved is None else resolved.canonical,
        scope=scope,
        sources=wanted_sources,
        views=scanned,
        results=tuple(results),
    )


def compare(
    store: Store,
    doc_a: str,
    doc_b: str,
    *,
    group: str | None = None,
    term_list: str | None = None,
) -> CompareResult:
    """Two documents' chunks (and optionally one term group's hits), side by side.

    The documents resolve like every other lookup (id first, else exact title) and must
    be different documents sharing a stored view -- no shared view raises, because "the
    difference is nothing" would report an unasked question as an answered one (rule 10).
    Chunk identity is :attr:`Chunk.id`: content per view plus occurrence, so a changed
    chunk is on exactly one side of each shared view and the same id never collides
    across documents. A chunk with the same id on both sides but different comments is in
    ``comments_changed``, not in ``only_in_a``/``only_in_b``. With ``group``, each side's
    hits under the term list (decision 3) name the chunks only that side carries the
    group in; hits that exist under another term list are not read, so an empty hit difference there means *not compared*, never
    *absent*.
    """
    rows = _stored(store)
    a = _resolve(rows, doc_a)
    b = _resolve(rows, doc_b)
    if a.entry.document_id == b.entry.document_id:
        raise ValueError(
            f"compare needs two different documents; both name "
            f"{a.entry.document_id}"
        )
    shared = [
        view.view
        for view in a.entry.views
        if any(other.view == view.view for other in b.entry.views)
    ]
    if not shared:
        raise LookupError(
            f"documents {a.entry.document_id} and {b.entry.document_id} share no stored "
            f"view: {[view.view for view in a.entry.views]} vs "
            f"{[view.view for view in b.entry.views]}"
        )
    resolved = None
    chosen: str | None = None
    compared = False
    hits_a: tuple[TermHit, ...] = ()
    hits_b: tuple[TermHit, ...] = ()
    if group is not None:
        chosen, registry = _load(store, term_list)
        resolved = resolve_group(group, registry)
        if resolved is not None:
            compared = (
                a.hits_keys.get(chosen) is not None
                and b.hits_keys.get(chosen) is not None
            )
            if compared:
                hits_a = tuple(
                    hit for hit in _hits(store, a, chosen) if hit.group == resolved.canonical
                )
                hits_b = tuple(
                    hit for hit in _hits(store, b, chosen) if hit.group == resolved.canonical
                )

    parsed_a = _require_parse(store, a)
    parsed_b = _require_parse(store, b)
    parents_a = parents(parsed_a)
    parents_b = parents(parsed_b)
    comparisons: list[ViewComparison] = []
    for view_id in shared:
        chunks_a = _chunks(store, a, view_id)
        chunks_b = _chunks(store, b, view_id)
        ids_a = {chunk.id for chunk in chunks_a}
        ids_b = {chunk.id for chunk in chunks_b}
        context_a = {chunk.id: chunk.context_hash for chunk in chunks_a}
        context_b = {chunk.id: chunk.context_hash for chunk in chunks_b}
        wanted = View(view_id)

        def changes_in(
            document_id: str,
            parsed: ParseResult,
            chunks: Sequence[Chunk],
            other_ids: set[str],
        ) -> tuple[ChunkChange, ...]:
            return tuple(
                ChunkChange(
                    chunk_id=chunk.id,
                    section_path=tuple(chunk.section_path),
                    citation=Citation(
                        document_id=document_id,
                        chunk_id=chunk.id,
                        view=wanted,
                        spans=_chunk_spans(parsed, chunk),
                    ),
                )
                for chunk in chunks
                if chunk.id not in other_ids
            )

        hit_ids_a = _hit_chunks(parsed_a, parents_a, chunks_a, hits_a)
        hit_ids_b = _hit_chunks(parsed_b, parents_b, chunks_b, hits_b)
        compared_a, compared_b = set(hit_ids_a), set(hit_ids_b)
        comparisons.append(
            ViewComparison(
                view=wanted,
                only_in_a=changes_in(a.entry.document_id, parsed_a, chunks_a, ids_b),
                only_in_b=changes_in(b.entry.document_id, parsed_b, chunks_b, ids_a),
                unchanged_chunks=len(ids_a & ids_b),
                hits_only_in_a=tuple(c for c in hit_ids_a if c not in compared_b),
                hits_only_in_b=tuple(c for c in hit_ids_b if c not in compared_a),
                comments_changed=tuple(
                    chunk.id
                    for chunk in chunks_a
                    if chunk.id in ids_b and context_a[chunk.id] != context_b[chunk.id]
                ),
            )
        )
    return CompareResult(
        document_a=a.entry.document_id,
        document_b=b.entry.document_id,
        group=group,
        resolved_group=None if resolved is None else resolved.canonical,
        term_list=chosen,
        views=tuple(comparisons),
    )


def _hit_chunks(
    parsed: ParseResult, parent_of: dict[str, str], chunks: Sequence[Chunk], hits: Sequence[TermHit]
) -> tuple[str, ...]:
    """The chunks carrying at least one of ``hits``, in document order, deduplicated.

    A ``comment:<para_id>`` hit counts against its anchor's chunk, the same dedupe rank
    applies; a hit no chunk holds (header, footnote, unanchored comment) is skipped.
    """
    if not hits:
        return ()
    owner = _owner(chunks)
    found: list[str] = []
    for hit in hits:
        anchor = _anchor_target(parsed, hit.node_id, parent_of)
        chunk_id = owner.get(anchor) if anchor else None
        if chunk_id is not None and chunk_id not in found:
            found.append(chunk_id)
    return tuple(found)

