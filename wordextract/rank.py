"""Retrieval and ranking over the stored records (Phase 2, Turn 3; design D12).

A query is the **union of three sources**, each result labeled with the source it came
from: the term tier (the query resolved through the registry to a group's stored hits),
the text tier (normalized token containment over a chunk's leaves, each read in a view),
and the summary tier (the same containment over a chunk's summary text and topics).
Ordering is a **fixed source-priority tiering** -- term, then text, then summary -- never
a fused score: within a tier, by document id and then node order. A chunk matched by
several sources is returned **once** with all its sources listed, comment hits dedupe into
their anchor chunk, and the two hits of a move collapse into one result's two locations
(the query-time fold :func:`~wordextract.terms.dedupe_moves`, in the order
:func:`~wordextract.terms.match_document` returned the hits).

Everything here is a pure function over records the caller already holds -- parsed
documents, chunks, stored hits, summaries and one term registry -- so the ledger can
fingerprint it without a store and Turn 5's ``query.py`` can wrap the same core over the
read-only store. No result carries a numeric relevance, and no query path guesses: a
query that does not *equal* a group's canonical or synonym form (both sides through
:func:`~wordextract.terms.normalize`) has no term tier at all.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .model import Chunk, LocationKind, ParseResult, Span, TermHit, View
from .render import chunk_leaves, leaf_text, part_stream
from .terms import (
    TermGroup,
    TermRegistry,
    dedupe_hits,
    dedupe_moves,
    normalize,
    token_offsets,
    tokenize,
)
from .views import project

#: The sources in their fixed tier order: a result's first source is the highest tier it
#: matched, and that index is what orders the final list. A future embedding index slots
#: in as a fourth, separately labeled source (Phase 3) -- not here.
SOURCES = ("term", "text", "summary")
_TIER = {source: index for index, source in enumerate(SOURCES)}

#: The views the text tier scans and the results name when the caller does not choose:
#: both body readings. No default may collapse to accepted-only (D5).
DEFAULT_VIEWS = (View.ACCEPTED, View.ORIGINAL)


@dataclass(frozen=True)
class Location:
    """One place inside a chunk the query matched: a node, and where within it.

    ``node_id`` is a parse node id or ``comment:<para_id>`` -- the same names
    :class:`~wordextract.model.TermHit` carries, so a caller reaches the record from the
    citation. ``spans`` are the union addresses of the matched text (D6): the citation is
    view-independent because the address is, while the *view(s)* a result matched in are
    recorded on the result itself. Two hits of one move at one node are two Locations --
    their spans differ -- which is how a moved term keeps both of its addresses without
    ever becoming "one hit with two locations" (D4).
    """

    node_id: str
    spans: tuple[Span, ...] = ()


@dataclass(frozen=True)
class SummaryRef:
    """What the summary tier reads of one chunk's summary: its text and its topics.

    The summary tier cites the chunk (a summary has no node of its own), so only the
    words it was written with and the topics it carries matter here;
    :class:`~wordextract.model.SummaryArtifact` maps onto this record.
    """

    chunk_id: str
    text: str
    topics: tuple[str, ...] = ()


@dataclass(frozen=True)
class DocumentInput:
    """One document's stored records, as retrieval sees them.

    ``hits`` is the record :func:`~wordextract.terms.match_document` returned for **one**
    term list (the caller pairs them; a hit from another registry resolves nothing), in
    match order -- the move fold's ordinals depend on that order, so callers must not
    reorder. ``chunks`` are the document's chunks in node order: they are both the
    retrieval units and the tie-break within a tier.
    """

    document_id: str
    parsed: ParseResult
    chunks: tuple[Chunk, ...]
    hits: tuple[TermHit, ...] = ()
    summaries: tuple[SummaryRef, ...] = ()


@dataclass(frozen=True)
class RankedResult:
    """One chunk's answer: which sources matched, in which views, and where (D12).

    ``sources`` is every source that matched this chunk, already in tier order -- a chunk
    in two tiers appears once, not twice. ``views`` are the views the matched text was
    found in (term hits' ``present_in`` and the views the text tier scanned); a summary
    was written from union markup (D5) and so contributes no view. ``term_group`` is set
    exactly when the term tier matched, named by the group's canonical form. No numeric
    relevance exists anywhere on the record.
    """

    document_id: str
    chunk_id: str
    sources: tuple[str, ...]
    views: tuple[View, ...]
    locations: tuple[Location, ...]
    term_group: str | None = None

    @property
    def tier(self) -> int:
        """The index of the highest (best) tier this chunk matched."""
        return _TIER[self.sources[0]]


def pick_term_list(requested: str | None, available: Sequence[str]) -> str:
    """The term list to search under: the explicit one, or the store's only one (decision 3).

    An explicit ``requested`` must be stored -- an unknown id is an error naming what is
    there rather than a silent fallback. Without one, exactly one stored list is used and
    zero or several are errors that name the candidates: searching *some* list on
    accident would answer a different question than the caller asked.
    """
    candidates = sorted(set(available))
    if requested is not None:
        if requested not in set(available):
            raise ValueError(
                f"term list {requested!r} is not stored; the store holds {candidates}"
            )
        return requested
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ValueError("no term list is stored; pass one explicitly")
    raise ValueError(
        f"several term lists are stored, so none can be chosen for you: {candidates}; "
        f"pass term_list explicitly"
    )


def resolve_group(query: str, registry: TermRegistry) -> TermGroup | None:
    """The group whose canonical or synonym form the query *equals*, or None.

    Both sides go through :func:`~wordextract.terms.normalize` (the matcher's own), so
    ``Subrogation`` resolves like ``subrogation`` -- and nothing else does: no stem, no
    fuzzy guess, no substring. No match means no term tier (the text and summary tiers
    still run). A query equal to forms of two *different* groups is an error naming all
    of them: the registry validates one entry per canonical form, but a synonym of one
    group may still equal another group's form, and picking one would be a guess.
    """
    wanted = normalize(query)
    if not wanted:
        return None
    found = [
        group
        for group in registry.groups
        if any(normalize(form) == wanted for form in (group.canonical, *group.synonyms))
    ]
    if len(found) > 1:
        raise ValueError(
            f"query {query!r} is ambiguous: it equals forms of several term groups: "
            f"{sorted(group.canonical for group in found)}"
        )
    return found[0] if found else None


def rank(
    query: str,
    documents: Iterable[DocumentInput],
    *,
    registry: TermRegistry,
    views: Sequence[View] = DEFAULT_VIEWS,
) -> list[RankedResult]:
    """Every chunk that matches ``query``, in the fixed tier order (D12).

    The three tiers run in order over each document -- term hits (the query resolved to
    one group through :func:`resolve_group`, then that group's stored hits folded with
    ``dedupe_hits`` and ``dedupe_moves``), text match over each chunk's view text by
    normalized token containment, and summary match over each chunk's summary text and
    topics -- and the per-chunk contributions merge into one result per chunk with all its
    sources. Results sort by (tier, document id, node order, chunk id); the sort is total,
    so the same inputs give the same order every time. ``views`` are echoed in the results
    that matched view-held text; they are what the text tier scanned and what a term hit
    must be present in to count (view-less comment hits always count -- a comment belongs
    to no view, D6).
    """
    wanted = tokenize(query)
    group = resolve_group(query, registry)
    query_views = frozenset(views)
    contributions: dict[tuple[str, str], _ChunkMatch] = {}
    for document in documents:
        position = {
            chunk.id: index for index, chunk in enumerate(document.chunks)
        }
        owner = _chunk_owner(document.chunks)
        if group is not None and document.hits:
            _term_tier(contributions, document, group, query_views, owner, position)
        if wanted:
            _text_tier(contributions, document, wanted, query_views, position)
            _summary_tier(contributions, document, wanted, position)
    ordered = sorted(
        contributions.values(),
        key=lambda match: (match.tier, match.document_id, match.position, match.chunk_id),
    )
    return [
        RankedResult(
            document_id=match.document_id,
            chunk_id=match.chunk_id,
            sources=tuple(sorted(match.sources, key=lambda source: _TIER[source])),
            views=tuple(sorted(match.views, key=lambda view: view.value)),
            locations=tuple(match.locations),
            term_group=match.term_group,
        )
        for match in ordered
    ]


# --- the tiers -------------------------------------------------------------------------


@dataclass
class _ChunkMatch:
    """One chunk's accumulating contribution: sources, views, locations, group."""

    document_id: str
    chunk_id: str
    position: int
    sources: set[str] = field(default_factory=set)
    views: set[View] = field(default_factory=set)
    locations: list[Location] = field(default_factory=list)
    term_group: str | None = None

    @property
    def tier(self) -> int:
        return min(_TIER[source] for source in self.sources)

    def add(
        self,
        source: str,
        *,
        views: Iterable[View] = (),
        locations: Iterable[Location] = (),
        term_group: str | None = None,
    ) -> None:
        self.sources.add(source)
        self.views.update(views)
        self.term_group = term_group if term_group is not None else self.term_group
        for location in locations:
            if location not in self.locations:
                self.locations.append(location)


def _chunk_owner(chunks: Sequence[Chunk]) -> dict[str, str]:
    """The chunk each node id belongs to: node ids are the address a hit cites."""
    owner: dict[str, str] = {}
    for chunk in chunks:
        for node_id in chunk.node_ids:
            owner.setdefault(node_id, chunk.id)
    return owner


def _term_tier(
    contributions: dict[tuple[str, str], _ChunkMatch],
    document: DocumentInput,
    group: TermGroup,
    views: frozenset[View],
    owner: dict[str, str],
    position: dict[str, int],
) -> None:
    """The resolved group's stored hits, folded, into their chunks.

    Membership is decided by the **folded** hits (``dedupe_hits`` then ``dedupe_moves``,
    in the caller's stored order -- never reordered first), while the locations a member
    chunk cites come from every folded-or-paired hit at that chunk: both ends of a move
    in one chunk are that one result's two locations, and a pair whose ends sit in
    different chunks is one result at the first end (the fold's kept hit), never two.
    A hit whose node no chunk holds cannot be cited to one and is left out.

    A move whose two ends sit in **different** chunks is still one result: it sits at the kept
    end's chunk and its locations carry both ends' addresses (the other end's node lives in
    another chunk, and the address still names it). Dropping the far end would lose a place
    the term appears whenever only the term tier -- a synonym, say -- finds it.
    """
    hits = [
        hit
        for hit in document.hits
        if hit.group == group.canonical
        and (not hit.present_in or hit.present_in & views)
    ]
    strict = dedupe_hits(hits)
    mapped = [(hit, _hit_chunk(hit, owner)) for hit in strict]
    kept = dedupe_moves(strict, document.parsed.union_streams)
    members: dict[str, list[TermHit]] = {}
    for hit in kept:
        chunk_id = _hit_chunk(hit, owner)
        if chunk_id not in position:
            continue
        members.setdefault(chunk_id, [])
    for chunk_id in members:
        groups = {
            hit.move_group_id
            for hit in kept
            if _hit_chunk(hit, owner) == chunk_id and hit.move_group_id is not None
        }
        at_chunk = [
            hit
            for hit, where in mapped
            if where == chunk_id or (hit.move_group_id is not None and hit.move_group_id in groups)
        ]
        contributions.setdefault(
            (document.document_id, chunk_id),
            _ChunkMatch(
                document_id=document.document_id,
                chunk_id=chunk_id,
                position=position[chunk_id],
            ),
        ).add(
            "term",
            views=[view for hit in at_chunk for view in hit.present_in & views],
            locations=(
                Location(node_id=hit.node_id, spans=tuple(hit.spans)) for hit in at_chunk
            ),
            term_group=group.canonical,
        )


def _hit_chunk(hit: TermHit, owner: dict[str, str]) -> str | None:
    """The chunk this hit is cited to: its node, or for a comment its anchor's node.

    A comment's own words live in the comments part and address no document node, so the
    hit cites ``comment:<id>`` while the chunk it dedupes into is the anchor chunk -- the
    node ``match_comments`` recorded as ``context_node_id``. A comment with no anchor (or
    a hit whose node the chunks do not hold) has no chunk to cite and maps to None.
    """
    if hit.location == LocationKind.COMMENT:
        return owner.get(hit.context_node_id or "")
    return owner.get(hit.node_id)


def _text_tier(
    contributions: dict[tuple[str, str], _ChunkMatch],
    document: DocumentInput,
    wanted: tuple[str, ...],
    views: frozenset[View],
    position: dict[str, int],
) -> None:
    """Normalized token containment over each leaf's view text, per view (Turn 0b).

    Containment is **per leaf** (a paragraph, heading or list item), never across the line
    break between two: the matcher's own rule is that a phrase does not span a paragraph, and
    a retrieval hit that did would cite two paragraphs for words neither contains. Every
    occurrence counts, in every requested view, and its location is the union address of the
    occurrence inside the leaf it sits in, projected through the same view the text was read
    in. Two views agreeing on one occurrence are one location -- the union address does not
    depend on the view that found it.
    """
    for chunk in document.chunks:
        leaves = chunk_leaves(document.parsed, chunk)
        if not leaves:
            continue
        stream = part_stream(document.parsed, leaves[0].part_id)
        matched: set[View] = set()
        locations: list[Location] = []
        for view in views:
            for leaf in leaves:
                if not leaf.spans:
                    continue
                text = leaf_text(stream, leaf, view)
                found = _occurrences(token_offsets(text), wanted)
                if not found:
                    continue
                matched.add(view)
                projection = project(stream, view, leaf.spans[0].start, leaf.spans[0].end)
                for start, end in found:
                    spans = tuple(projection.union_spans(start, end))
                    location = Location(node_id=leaf.id, spans=spans)
                    if spans and location not in locations:
                        locations.append(location)
        if matched:
            contributions.setdefault(
                (document.document_id, chunk.id),
                _ChunkMatch(
                    document_id=document.document_id,
                    chunk_id=chunk.id,
                    position=position[chunk.id],
                ),
            ).add("text", views=matched, locations=locations)


def _summary_tier(
    contributions: dict[tuple[str, str], _ChunkMatch],
    document: DocumentInput,
    wanted: tuple[str, ...],
    position: dict[str, int],
) -> None:
    """The discovery path: the query contained in a chunk's summary text or a topic.

    Text and topics are each checked on their own -- the query must occur inside one of
    them, never strung across two topics. The summary contributes no view (it was written
    from union markup, D5) and no node (a summary cites its chunk, which is the citation).
    """
    for summary in document.summaries:
        if summary.chunk_id not in position:
            continue
        if _occurrences(token_offsets(summary.text), wanted) or any(
            _occurrences(token_offsets(topic), wanted) for topic in summary.topics
        ):
            contributions.setdefault(
                (document.document_id, summary.chunk_id),
                _ChunkMatch(
                    document_id=document.document_id,
                    chunk_id=summary.chunk_id,
                    position=position[summary.chunk_id],
                ),
            ).add("summary")


# --- containment and addresses ----------------------------------------------------------


def _occurrences(
    offsets: Sequence[tuple[str, int, int]], wanted: Sequence[str]
) -> list[tuple[int, int]]:
    """Every ``[start, end)`` in the source text where ``wanted`` occurs as tokens.

    Containment is token-wise after the shared normalization (the default hyphen reading
    included), so punctuation, case and spacing are never compared -- only the sequence
    of normalized tokens. Callers pass one leaf's offsets at a time, so a match never spans a
    paragraph break.
    """
    if not wanted:
        return []
    size = len(wanted)
    found: list[tuple[int, int]] = []
    for index in range(len(offsets) - size + 1):
        if tuple(offsets[index + step][0] for step in range(size)) == tuple(wanted):
            found.append((offsets[index][1], offsets[index + size - 1][2]))
    return found
