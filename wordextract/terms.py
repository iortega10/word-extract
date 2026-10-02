"""Turn 6a/6b: the term registry, normalization, and the exact/synonym/stem matcher.

``word-extraction-design.md`` D6 and the 6a and 6b slices of ``phase1-build-spec.md``'s Turn
6: a persistent, versioned registry of term groups and a matcher that reports where those
groups occur in a view's text -- deterministic, offline, with no threshold and no confidence
scalar anywhere. What is not deterministic is not a hit.

**What a hit is.** A group occurs where its normalized **token sequence** occurs: whole
tokens, in order, and never across a paragraph boundary. Occurrence is never tested on
characters, so a substring is never a hit -- ``subrogation`` inside ``subrogations`` is
reached by 6b's stemmer, and reported as ``stem``, never as a character substring.

**Normalization** (:func:`tokenize`) is identical for registry entries and corpus, which
is why it is part of ``matcher_version``: *any* change to the rules below must bump it.
Case is folded (``str.casefold``) and each token is NFC, so a precomposed and a
decomposed spelling of one word are one token. A token is a maximal run of letters,
digits and combining marks; everything else separates tokens -- punctuation, whitespace,
symbols. The hyphen family is the one character with two honest readings, and real
wording uses both (``non-compliance`` / ``noncompliance``, but ``hold-harmless`` /
``hold harmless``), so a term matches under **either**: read with hyphens *deleted* (they
join: ``non-compliance`` is the token ``noncompliance``) or with hyphens as *separators*
(``hold-harmless`` is the two tokens ``hold``, ``harmless``). A form and a text match
when their tokens are equal under one reading or the other, so the two spellings of a
compound are one term whichever way the registry or the document writes it. Nothing
fuzzy is involved: each reading is exact, and a hit is a hit under at least one of them.
A space is still a real separator everywhere else, so ``right ofsubrogation`` never
matches ``right of subrogation`` (no hyphen is involved), and a space in a registry
entry and a ``w:tab`` or ``w:br`` in the corpus match each other.

**Overlap precedence** is resolved **per group**, which is how "distinct groups report
distinct hits" is meant: two groups may report overlapping hits, but one group reports a
hit once. Within a group: the longest span -- ``span`` is a character range, so
``end - start``, not a token count -- then the leftmost, then ``exact > synonym > stem``
(:data:`MATCH_PRECEDENCE`). That ordering settles ``match_type`` *before* dedupe, so a
synonym that normalizes onto its own canonical form (``non-compliance`` vs
``noncompliance``) reports one hit, as ``exact``. Losing candidates are dropped and
disjoint matches are all reported.

**Speed.** An index is compiled once and matched against many texts. Matching looks up
each token of the text in a first-token table, so a position only tries the forms that
begin with that token: the cost follows the size of the text, not the size of the term
list (a 750-form list costs the same per position as a 10-form one).

**Stemming (6b).** A group whose ``stemming`` names a vendored algorithm (see
:mod:`wordextract.stem`) also matches where its **canonical form's stem** occurs: each
corpus token is stemmed and looked up, so ``including`` reaches the group ``included`` as a
``stem`` hit. Stemming is derived from the canonical alone, because stemming a synonym would
invent a form the term list never asked for, and it is per group -- the algorithm choice is
registry data (:attr:`wordextract.model.TermGroup.stemming`), which is why it travels in
``term_list_hash``. ``rules`` and ``tags`` stay opaque: domain data is registry data, not
matcher data (D6).

The form/precedence machinery is generic (:class:`TermIndex` holds ``(group, match_type,
tokens)`` entries and the precedence tuple ranks ``stem`` last), so a stem form slots in
beside the exact and synonym ones with no change to matching, overlap or hashing -- except
that a stem is a *derived* reading, so its forms live in their own tables
(:attr:`TermIndex.stem_forms`) and the exact/synonym tables stay exactly what the term list
wrote.

**Where a hit is (6c).** A whole document is matched part by part: the body, headers,
footers, footnotes, endnotes and the comment bodies, each hit saying which
:class:`~wordextract.model.LocationKind` it sits in -- the part it was walked in, the table
cell that encloses it, or the text-box fragment that addresses it. A part is matched
**paragraph by paragraph per view** (D6): each paragraph-like node is offered to the
whole-part projection as its own range, so a paragraph the view elided is no range and
yields nothing, a hit's ``view_spans`` are whole-part view offsets while its ``spans``
stay union addresses, and the same group at the same node in the same union span is **one**
hit present in every view that found it. Comments are the exception that proves the rule: a
comment's own words are text of their own part, held in no view and addressed by no node,
so a comment hit is **view-less** -- ``present_in`` empty, no ``view_spans`` at all -- and
is named ``comment:<para_id>``, which is how a caller reaches the ``Comment`` record. Text
boxes are the one location not matched: their text is its own fragment, and no fragment
stream exists yet, so their words are a known gap rather than a hit.

**Moves (6d).** A move is two revisions -- the ``w:moveFrom`` mark at the source and the
``w:moveTo`` mark at the destination -- that share a ``move_group_id``, and because those marks
are del-family and ins-family respectively the same wording is one hit in ``original`` and one
in ``accepted``: **two hits, one per location**, each carrying the shared ``move_group_id``
(D4; text-model-spec section 5). The group is read off the ancestor stack of the union text a
hit is made of -- the innermost move that text sits under, and only when every run of the hit
sits under that same move, so wording that merely neighbours a moved run is not a move hit.
*Where* the two ends are is not the matcher's business: the group is a property of each
location's own text, so a move whose ends are in different paragraphs yields its two hits for
free -- the paragraph-boundary gap rule (section 8) still governs matching *across* a boundary,
which never happens because a part is matched paragraph by paragraph either way. The matcher
never folds the pair: the two hits are one *group*, never one hit with two locations, so
:func:`match_document` returns both and the collapse is **query-time**
(:func:`dedupe_moves`), keyed on ``(group, move_group_id, normalized text, intra-group
ordinal)`` -- the two ends of one move are one row, while two distinct moved occurrences of
the same wording stay two.

The registry itself is a codec record, persisted as content-addressed JSON through the
core ``Collection`` with the record **id equal to** :func:`term_list_hash`: the term list
is the identity, so a re-save is a no-op and two runs of the same terms share one file.
"""
from __future__ import annotations

import unicodedata
from bisect import bisect_right
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from docextract_core import Collection, encode, from_json, sha256_json, to_json

from .model import (
    Comment,
    ElementarySpan,
    LocationKind,
    MatchType,
    Node,
    NodeKind,
    ParseResult,
    Span,
    TermGroup,
    TermHit,
    UnionStream,
    View,
    ViewSpan,
)
from .nodes import ancestor_of_kind, anchor_node, parents
from .stem import stemmer
from .views import Projection, project

#: The hyphen family: hyphen-minus, soft hyphen, hyphen, non-breaking hyphen. Deleted in
#: normalization, so a hyphen **joins** two character runs into one token.
HYPHENS = frozenset({"\u002d", "\u00ad", "\u2010", "\u2011"})

#: ``exact > synonym > stem``, strongest first: the overlap tie-break of D6, and the
#: reason 6b can add stem entries without touching :func:`_resolve`.
MATCH_PRECEDENCE: tuple[MatchType, ...] = (MatchType.EXACT, MatchType.SYNONYM, MatchType.STEM)
_RANK = {match_type: rank for rank, match_type in enumerate(MATCH_PRECEDENCE)}

#: The only paragraph boundary: a union stream's terminator is the empty-stack ``"\n"``.
PARAGRAPH_BOUNDARY = "\n"


# --- normalization -----------------------------------------------------------------

def tokenize(text: str, *, split_hyphens: bool = False) -> tuple[str, ...]:
    """``text``'s normalized tokens, in order (the corpus/registry-equal form).

    By default hyphens are deleted and so *join* (``non-compliance`` -> ``noncompliance``);
    with ``split_hyphens`` they are separators (``hold-harmless`` -> ``hold``, ``harmless``).
    The matcher tries both readings. A paragraph boundary is just another separator here:
    this is the lexical view of a string, and :func:`match_text` -- which never matches
    across a ``"\\n"`` -- owns the boundary.
    """
    return tuple(token for token, _, _ in _token_offsets(text, split_hyphens))


def normalize(text: str) -> str:
    """``text``'s tokens joined by one space: the canonical normalized string."""
    return " ".join(tokenize(text))


def _token_offsets(text: str, split_hyphens: bool = False) -> list[tuple[str, int, int]]:
    """``(token, start, end)`` per token of ``text``, in ``text``'s own coordinates.

    ``start``/``end`` straddle the **source** characters, which is what lets a match on a
    token sequence become a character range without ever comparing characters.
    """
    found: list[tuple[str, int, int]] = []
    start: int | None = None
    raw: list[str] = []
    for index, char in enumerate(text):
        if char in HYPHENS and not split_hyphens:
            continue
        if char.isalnum() or unicodedata.category(char).startswith("M"):
            if start is None:
                start = index
            raw.append(char)
            continue
        if start is not None:
            found.append((_fold("".join(raw)), start, index))
            start, raw = None, []
    if start is not None:
        found.append((_fold("".join(raw)), start, len(text)))
    return found


#: The public name of the tokenizer with source offsets (retrieval reuses it).
token_offsets = _token_offsets


def _fold(raw: str) -> str:
    return unicodedata.normalize("NFC", raw.casefold())


# --- the registry ------------------------------------------------------------------

@dataclass(frozen=True)
class TermRegistry:
    """A term list: one entry per canonical form, and the unit ``term_list_hash`` covers.

    A codec record, so it round-trips through the core ``Collection`` (``schema_version``
    envelope) with no bespoke serialization. It validates what would otherwise be a
    silent miss or a match everywhere: one entry per canonical form, every form
    non-empty after normalization, and no form containing a paragraph boundary, which
    matching never crosses and which therefore could never match.
    """

    groups: list[TermGroup] = field(default_factory=list)

    def __post_init__(self) -> None:
        canonical: set[str] = set()
        for group in self.groups:
            if group.canonical in canonical:
                raise ValueError(
                    f"duplicate term group {group.canonical!r}: a registry has one entry "
                    f"per canonical form"
                )
            canonical.add(group.canonical)
            for form in (group.canonical, *group.synonyms):
                if PARAGRAPH_BOUNDARY in form:
                    raise ValueError(
                        f"term form {form!r} contains a paragraph boundary; matching never "
                        f"crosses one, so it could never match"
                    )
                if not tokenize(form):
                    raise ValueError(
                        f"term form {form!r} normalizes to no tokens, which would match "
                        f"everywhere"
                    )


def term_list_hash(registry: TermRegistry) -> str:
    """The term list's identity: sha256 over its canonical serialization (D6).

    The codec's canonical JSON sorts keys recursively but not list elements, so the
    groups are sorted by canonical form and each group's synonyms sorted: one term list
    is one hash however its groups and synonyms were typed in. The hash is taken over
    ``encode``d groups rather than hand-picked fields, so a new ``TermGroup`` field joins
    the identity automatically, and one synonym change changes the hash.
    """
    return sha256_json({"groups": _canonical_groups(registry)})


def _canonical_groups(registry: TermRegistry) -> list[dict]:
    groups = encode(registry)["groups"]
    for group in groups:
        group["synonyms"] = sorted(group["synonyms"])
    return sorted(groups, key=lambda group: group["canonical"])


def registry_store(root: str | Path, *, read_only: bool = False) -> Collection[TermRegistry]:
    """A content-addressed store of term lists: one record per distinct term list.

    The record id **is** the term-list hash and so is the idempotency key, which is what
    makes a re-save a no-op and keeps a store free of duplicate copies of one term list.
    ``read_only`` is the store's own mode (Turn 0a), threaded through here so opening a
    store read-only opens its term lists read-only too.
    """
    return Collection(
        root, TermRegistry, id_of=term_list_hash, key_of=term_list_hash, read_only=read_only
    )


def save_registry(
    store: Collection[TermRegistry], registry: TermRegistry
) -> tuple[TermRegistry, bool]:
    """Store ``registry`` as ``(record, written)``; ``written`` False when already stored."""
    return store.save(registry)


def load_registry(store: Collection[TermRegistry], term_list: str) -> TermRegistry | None:
    """The stored registry whose term list hashes to ``term_list``, or None."""
    return store.find(term_list)


def registry_hashes(store: Collection[TermRegistry]) -> list[str]:
    """Every term list the store holds, sorted (the store's inventory)."""
    return store.list()


def dump_registry(registry: TermRegistry) -> str:
    """The registry as its canonical JSON document, which is what a registry file holds."""
    return to_json(registry)


def load_registry_text(text: str) -> TermRegistry:
    """Parse a registry document written by :func:`dump_registry`."""
    return from_json(TermRegistry, text)


# --- the matcher -------------------------------------------------------------------

#: The parts a package streams and the :class:`LocationKind` their text sits in, keyed by
#: the relationship type local name a ``part_id`` starts with (Turn 1's
#: ``<relationship type local name>:<ordinal>``). The *part* decides the kind, not the node:
#: a footnote and an endnote are both ``FOOTNOTE`` nodes, and only the parts tell them apart.
_PART_KINDS: dict[str, LocationKind] = {
    "officeDocument": LocationKind.BODY,
    "header": LocationKind.HEADER,
    "footer": LocationKind.FOOTER,
    "footnotes": LocationKind.FOOTNOTE,
    "endnotes": LocationKind.ENDNOTE,
    "comments": LocationKind.COMMENT,
}

#: The paragraph-like nodes -- the matching unit (D6). A container has no text of its own and
#: is not a location; a cell's paragraph is matched as a cell by :func:`_location`.
_MATCH_UNITS = frozenset({NodeKind.HEADING, NodeKind.PARA, NodeKind.LIST_ITEM})

#: The views a hit is *matched* in. D6's ``superseded`` is a mask and a fixture only: it is
#: never a matcher target, so text that was inserted and then deleted is in no hit.
_MATCH_VIEWS: tuple[View, ...] = (View.ACCEPTED, View.ORIGINAL)


@dataclass(frozen=True)
class TermMatch:
    """One accepted occurrence of one group's form, in one text's character coordinates.

    ``start``/``end`` address the text that was matched -- a view's text, not the union --
    and ``group`` is the matched group's canonical form.
    """

    group: str
    match_type: MatchType
    start: int
    end: int


@dataclass(frozen=True)
class _Form:
    group: str
    match_type: MatchType
    tokens: tuple[str, ...]
    #: Whether the form's own text contains a hyphen, i.e. whether its two readings can
    #: differ. Decides when the split-hyphen pass has to run (see :func:`_match_segment`).
    hyphenated: bool = False
    #: The algorithm that produced ``tokens`` when this is a ``stem`` form, else None: the
    #: corpus side has to be stemmed by the *same* algorithm to be comparable (per-group
    #: choice in the registry, 6b).
    stem: str | None = None


def _first_tokens(forms: Iterable[_Form]) -> dict[str, tuple[_Form, ...]]:
    """``forms`` grouped by their first token, each group in the forms' own order."""
    found: dict[str, list[_Form]] = {}
    for form in forms:
        found.setdefault(form.tokens[0], []).append(form)
    return {token: tuple(group) for token, group in found.items()}


def _first_tokens_by_algorithm(forms: Iterable[_Form]) -> dict[str, dict[str, tuple[_Form, ...]]]:
    """``forms`` grouped by algorithm, then by first token: the stem tables of the index.

    Nested rather than flat because the corpus side is stemmed one algorithm at a time: a
    word's ``porter`` stem is compared only against the ``porter`` forms, so two groups that
    name different algorithms never compare across them.
    """
    found: dict[str, dict[str, list[_Form]]] = {}
    for form in forms:
        assert form.stem is not None
        found.setdefault(form.stem, {}).setdefault(form.tokens[0], []).append(form)
    return {
        algorithm: {token: tuple(group) for token, group in by_first.items()}
        for algorithm, by_first in found.items()
    }


@dataclass(frozen=True)
class TermIndex:
    """A compiled registry: every form of every group as token sequences, per reading.

    ``forms`` is the hyphens-deleted reading and ``split_forms`` the hyphens-as-separators
    one; the matcher tries both (see the module docstring). ``stem_forms`` and
    ``stem_split_forms`` are the same two readings for the groups that stem -- kept apart so
    ``forms`` stays exactly what the term list wrote, and so a registry that stems nothing
    has empty stem tables and is matched by the exact/synonym pass alone. Compiled once and
    matched against many locations, with the forms looked up by their first token, which is
    what keeps the matcher linear in the corpus rather than in the corpus times the registry.
    Canonical in itself -- the forms are sorted -- so two registries with the same term list
    compile to an equal index whatever order they were built in.
    """

    forms: tuple[_Form, ...] = ()
    split_forms: tuple[_Form, ...] = ()
    stem_forms: tuple[_Form, ...] = ()
    stem_split_forms: tuple[_Form, ...] = ()
    _join_first: dict = field(init=False, repr=False, compare=False, default_factory=dict)
    _split_first: dict = field(init=False, repr=False, compare=False, default_factory=dict)
    _split_first_hyphenated: dict = field(
        init=False, repr=False, compare=False, default_factory=dict
    )
    _stem_join_first: dict = field(init=False, repr=False, compare=False, default_factory=dict)
    _stem_split_first: dict = field(init=False, repr=False, compare=False, default_factory=dict)
    _stem_split_first_hyphenated: dict = field(
        init=False, repr=False, compare=False, default_factory=dict
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "_join_first", _first_tokens(self.forms))
        object.__setattr__(self, "_split_first", _first_tokens(self.split_forms))
        object.__setattr__(
            self,
            "_split_first_hyphenated",
            _first_tokens(form for form in self.split_forms if form.hyphenated),
        )
        object.__setattr__(
            self, "_stem_join_first", _first_tokens_by_algorithm(self.stem_forms)
        )
        object.__setattr__(
            self, "_stem_split_first", _first_tokens_by_algorithm(self.stem_split_forms)
        )
        object.__setattr__(
            self,
            "_stem_split_first_hyphenated",
            _first_tokens_by_algorithm(form for form in self.stem_split_forms if form.hyphenated),
        )


def compile_registry(registry: TermRegistry) -> TermIndex:
    """Compile ``registry`` into the form table :func:`match_text` matches against.

    ``canonical`` forms are ``exact`` and ``synonyms`` are ``synonym``, both in every reading
    of the hyphens. A group whose ``stemming`` names a vendored algorithm also gets one
    ``stem`` form, derived from the canonical alone (stemming a synonym would invent a form
    the term list never asked for); those forms go into the index's own stem tables. A
    ``stemming`` that names no shipped algorithm is a registry the matcher cannot honour, so
    it fails here rather than matching nothing.
    """
    joined: list[_Form] = []
    split: list[_Form] = []
    stem_joined: list[_Form] = []
    stem_split: list[_Form] = []
    seen: set[tuple[bool, _Form]] = set()

    def emit(
        group: TermGroup,
        form: str,
        match_type: MatchType,
        targets: tuple[list[_Form], list[_Form]],
        algorithm: str | None,
    ) -> None:
        hyphenated = any(char in HYPHENS for char in form)
        for is_split, target in ((False, targets[0]), (True, targets[1])):
            tokens = tokenize(form, split_hyphens=is_split)
            if algorithm is not None:
                stem = stemmer(algorithm)
                tokens = tuple(stem(token) for token in tokens)
            candidate = _Form(
                group=group.canonical,
                match_type=match_type,
                tokens=tokens,
                hyphenated=hyphenated,
                stem=algorithm,
            )
            if candidate.tokens and (is_split, candidate) not in seen:
                seen.add((is_split, candidate))
                target.append(candidate)

    for group in registry.groups:
        emit(group, group.canonical, MatchType.EXACT, (joined, split), None)
        for synonym in group.synonyms:
            emit(group, synonym, MatchType.SYNONYM, (joined, split), None)
        if group.stemming is not None:
            stemmer(group.stemming)  # validate: an unknown algorithm is a loud failure
            emit(group, group.canonical, MatchType.STEM, (stem_joined, stem_split), group.stemming)

    def order(form: _Form) -> tuple:
        return (form.group, _RANK[form.match_type], form.tokens)

    return TermIndex(
        forms=tuple(sorted(joined, key=order)),
        split_forms=tuple(sorted(split, key=order)),
        stem_forms=tuple(sorted(stem_joined, key=order)),
        stem_split_forms=tuple(sorted(stem_split, key=order)),
    )


def match_text(index: TermIndex, text: str) -> list[TermMatch]:
    """Every hit of ``index`` in ``text``, in order, overlapping hits resolved.

    ``text`` may be a whole part's view text: a ``"\\n"`` is a paragraph boundary, so a
    term can never span two paragraphs (it is never even offered the chance -- each
    boundary-free segment is matched on its own).
    """
    matches: list[TermMatch] = []
    for base, segment in _segments(text):
        matches.extend(_match_segment(index, segment, base))
    return sorted(matches, key=lambda m: (m.start, m.end, m.group, _RANK[m.match_type]))


def match_projection(
    index: TermIndex,
    projection: Projection,
    *,
    node_id: str,
    start: int = 0,
    end: int | None = None,
    location: LocationKind = LocationKind.BODY,
) -> list[TermHit]:
    """Every hit of ``index`` in one location's slice of ``projection``, as ``TermHit``\\ s.

    ``projection`` is a **whole-part** projection of one view: it is the offset map the
    hits are addressed in, so ``view_spans`` are offsets in the whole-part view text with
    terminators kept, as D6 requires, and ``spans`` are the union addresses the hit is
    made of -- never the text the view elided between them. ``[start, end)`` is the
    location's range in that projection, one location per call; the default whole range
    suits a caller with no range to name, such as a text that is one range.

    One hit per group per occurrence: a group's overlapping forms are resolved per group
    and ``match_type`` is settled there, before any dedupe. ``location`` is the kind of
    place the range is, which 6c's :func:`match_part` derives per node -- as it also fills
    in a move's ``move_group_id``, which is a fact about the document rather than about one
    projection, so a bare call here leaves it ``None``.
    """
    stop = len(projection.text) if end is None else end
    hits: list[TermHit] = []
    for match in match_text(index, projection.text[start:stop]):
        hit_start = start + match.start
        hit_end = start + match.end
        hits.append(
            TermHit(
                group=match.group,
                present_in={projection.view},
                spans=projection.union_spans(hit_start, hit_end),
                view_spans=[ViewSpan(view=projection.view, start=hit_start, end=hit_end)],
                node_id=node_id,
                location=location,
                match_type=match.match_type,
            )
        )
    return hits


def match_part(
    index: TermIndex,
    parsed: ParseResult,
    part_id: str,
    *,
    view: View = View.ACCEPTED,
) -> list[TermHit]:
    """Every hit of ``index`` in one part of ``parsed`` in one view, in document order.

    A part is matched **paragraph by paragraph** (D6): one whole-part projection per call,
    and every paragraph-like node of the part offered to it as its own range. The range is
    the node's union span read *into* the view
    (:meth:`~wordextract.views.Projection.retained_before`), so a paragraph this view
    elided entirely is no range at all and its words are not matched -- the view's
    paragraphs are what matching sees, never the union's.

    Only real parts are matched. The comments part is not one: a comment's own words are
    read off the ``Comment`` record by :func:`match_comments`, never off the node tree
    (6c), so the part has no locations here. A text box is not matched either: its words
    are its own fragment's, and no fragment streams yet, so the gap is a gap rather than a
    hit addressed in the part it happens to sit in. A ``part_id`` that did not stream
    raises ``ValueError`` instead of matching nothing: there is no union text of it to
    address a hit in, which is a different thing from a part with no hits.
    """
    stream = _stream_of(parsed, part_id)
    if _part_kind(part_id) is LocationKind.COMMENT:
        return []
    projection = project(stream, view)
    by_id = {node.id: node for node in parsed.nodes}
    parent_of = parents(parsed)
    moves = _move_groups(parsed)
    hits: list[TermHit] = []
    for node in parsed.nodes:
        if node.part_id != part_id or node.kind not in _MATCH_UNITS:
            continue
        location = _location(node, by_id, parent_of)
        if location is LocationKind.TEXTBOX:
            continue  # a fragment is not this part's text (the ``textbox`` known gap)
        for span in node.spans:
            if span.part_id != part_id:
                continue
            start = projection.retained_before(span.start)
            end = projection.retained_before(span.end)
            if start == end:
                continue  # this view kept none of this paragraph
            for hit in match_projection(
                index,
                projection,
                node_id=node.id,
                start=start,
                end=end,
                location=location,
            ):
                hits.append(_with_move_group(hit, stream, moves))
    return hits


def match_document(index: TermIndex, parsed: ParseResult) -> list[TermHit]:
    """Every hit of ``index`` in a whole parsed document, parts in the walker's order.

    Every part that streamed is matched in every view a hit can be in (:data:`_MATCH_VIEWS`;
    D6 leaves ``superseded`` to masks and fixtures), and one group found at one node in the
    same union spans is **one** hit, whichever of the views hold it: that fold is what makes
    ``present_in`` a set and ``view_spans`` the hit's range per view. A hit's position is
    where it was first met, so it does not move when the other view stops holding it.

    The comment bodies follow the parts, being the one text a package streams that is in no
    view (see :func:`match_comments`).
    """
    hits: list[TermHit] = []
    for stream in parsed.union_streams:
        for view in _MATCH_VIEWS:
            hits.extend(match_part(index, parsed, stream.part_id, view=view))
    return _merge_views(hits) + match_comments(index, parsed)


def match_comments(index: TermIndex, parsed: ParseResult) -> list[TermHit]:
    """Every hit of ``index`` in a comment's own words, in comment order: view-less hits.

    A comment body is text like any other and matched like any other, but it belongs to no
    view and no node: the comment's own part streams on its own and the views are masks
    over the document's parts, so a comment hit carries ``present_in = set()`` and no
    ``view_spans`` at all (D6). Its ``spans`` are in the comments part's union stream --
    the range the group's tokens occupy there, which is the comment's
    :attr:`~wordextract.model.Comment.text_span` offset by where the tokens sit in the
    comment's ``text`` -- and ``node_id`` is ``comment:<para_id>``, so a caller reaches the
    ``Comment`` record from the hit and can fall back on the identity the walker resolved
    (``w14:paraId``, or its ``hash:`` fallback). ``context_node_id`` is the node the
    comment is anchored to, which is the "where" a comment hit has instead of a location of
    its own.

    A comment with no ``text_span`` is one whose part did not stream: its words are in no
    address space, so nothing about it is claimed.
    """
    parent_of = parents(parsed)
    streams = {stream.part_id: stream for stream in parsed.union_streams}
    moves = _move_groups(parsed)
    hits: list[TermHit] = []
    for comment in parsed.comments:
        address = comment.text_span
        if address is None:
            continue
        context = anchor_node(parsed, comment.anchor, parent_of)
        body = streams.get(address.part_id)
        for match_type, spans, group in _comment_matches(index, comment, address, streams):
            hit = TermHit(
                group=group,
                spans=spans,
                node_id=f"comment:{comment.para_id}",
                location=LocationKind.COMMENT,
                match_type=match_type,
                context_node_id=context,
            )
            hits.append(hit if body is None else _with_move_group(hit, body, moves))
    return hits


def _comment_matches(
    index: TermIndex,
    comment: Comment,
    address: Span,
    streams: dict[str, UnionStream],
) -> list[tuple[MatchType, list[Span], str]]:
    """One comment's matches as ``(match_type, union spans, group)``, never over the raw union.

    A comment body is union text like any part's: a tracked change inside it holds the
    deleted *and* the inserted words, which read together are an adjacency no reading of the
    comment asserts (``right of recovery`` deleted, ``subrogation`` inserted: the union is
    ``recoverysubrogation``). So when the comment's part streamed, its range is matched
    through each view's text -- gap-closed, and the hit's spans one per retained run, never
    the elided text between them -- and a hit found in both readings is one hit. The hit stays
    view-less (``present_in`` empty), as a comment's words are in no view of the document.
    A comment with no stream to project through has its plain text matched, which is the
    same thing for text with no revisions in it.
    """
    stream = streams.get(address.part_id)
    if stream is None:
        return [
            (
                match.match_type,
                [
                    Span(
                        part_id=address.part_id,
                        start=address.start + match.start,
                        end=address.start + match.end,
                        fragment_id=address.fragment_id,
                    )
                ],
                match.group,
            )
            for match in match_text(index, comment.text)
        ]
    found: dict[tuple[str, tuple[Span, ...]], tuple[MatchType, list[Span], str]] = {}
    for view in _MATCH_VIEWS:
        projection = project(stream, view, address.start, address.end)
        for match in match_text(index, projection.text):
            spans = projection.union_spans(match.start, match.end)
            key = (match.group, tuple(spans))
            known = found.get(key)
            if known is None or _RANK[match.match_type] < _RANK[known[0]]:
                found[key] = (match.match_type, spans, match.group)
    return list(found.values())


def dedupe_hits(hits: Iterable[TermHit]) -> list[TermHit]:
    """Drop repeats of one ``(group, node_id, view, start, end)``, keeping the first.

    Keeps the first occurrence -- and so the ``match_type`` already resolved for that
    span -- and the caller's order. A location re-offered by two passes is one hit, not two;
    the two hits of a move are two *locations* and are folded by :func:`dedupe_moves` instead.
    A hit with no view span at all (6c's comment hits, whose ``present_in`` is empty by
    design) is keyed by its union address instead, so the same address at the same node still
    collapses.
    """
    seen: set[tuple[str, str, str, int, int]] = set()
    kept: list[TermHit] = []
    for hit in hits:
        keys = {(hit.group, hit.node_id, vs.view.value, vs.start, vs.end) for vs in hit.view_spans}
        if not keys:
            keys = {(hit.group, hit.node_id, span.part_id, span.start, span.end) for span in hit.spans}
        if keys & seen:
            continue
        seen |= keys
        kept.append(hit)
    return kept


# --- moves (6d) ----------------------------------------------------------------------


def dedupe_moves(hits: Iterable[TermHit], streams: Iterable[UnionStream]) -> list[TermHit]:
    """Fold the two hits of one move into one, at query time (6d; D4, spec section 5).

    A move renders as two hits of the same wording at two locations; a query wants the term
    once, so the pair is folded here and **not** in :func:`match_document`, which is the
    record -- "the two hits are one *group*, never one hit with two locations". Only hits that
    carry a ``move_group_id`` are in play: everything else passes through untouched, so two
    ordinary occurrences of a group are still two hits. The key is ``(group, move_group_id,
    normalized text, intra-group ordinal)``, where the *normalized text* is the wording a hit
    is actually made of, read back out of ``streams`` and normalized (so a move whose two ends
    do not hold the same words does not fold), and the *ordinal* is which occurrence of that
    wording this is **within its own reading** -- the one stand-in for location the pair must
    not collide on, the way the ordinary key's ``node_id`` and ``view`` do. The two ends of a
    move sit in opposite readings (the source only in ``original``, the destination only in
    ``accepted``), so the wording moved once is ordinal 0 on both sides and folds, while two
    occurrences moved together are 0 and 1 on each. The kept hit is the first of the pair and
    the caller's order is kept. A move hit whose part is not among ``streams`` cannot have its
    wording read, so it is left alone rather than folded on a guess.
    """
    text_of = {stream.part_id: stream.text for stream in streams}
    counts: dict[tuple[str, str, str, frozenset[View]], int] = {}
    seen: set[tuple[str, str, str, int]] = set()
    kept: list[TermHit] = []
    for hit in hits:
        text = None if hit.move_group_id is None else _hit_text(hit, text_of)
        if text is None:
            kept.append(hit)
            continue
        here = (hit.group, hit.move_group_id, text, frozenset(hit.present_in))
        ordinal = counts.get(here, 0)
        counts[here] = ordinal + 1
        key = (hit.group, hit.move_group_id, text, ordinal)
        if key in seen:
            continue
        seen.add(key)
        kept.append(hit)
    return kept


def _hit_text(hit: TermHit, text_of: dict[str, str]) -> str | None:
    """The normalized wording a hit is made of, or None when a part of it is not in ``text_of``.

    A hit's ``spans`` are the union runs it is made of, so reading them straight back out --
    the text the view elided between two runs skipped, as the view skipped it -- is the wording
    the view matched.
    """
    pieces: list[str] = []
    for span in hit.spans:
        text = text_of.get(span.part_id)
        if text is None:
            return None
        pieces.append(text[span.start : span.end])
    if not pieces:
        return None
    return normalize("".join(pieces))


def _move_groups(parsed: ParseResult) -> dict[str, str]:
    """Every move's ``move_group_id``, by revision id (D4).

    Only a move carries a group, so a stack id absent here is not a move: this map is exactly
    the set of revisions a hit's wording has to sit under to be a move hit.
    """
    return {
        revision.id: revision.move_group_id
        for revision in parsed.revisions
        if revision.move_group_id is not None
    }


def _with_move_group(hit: TermHit, stream: UnionStream, moves: dict[str, str]) -> TermHit:
    """``hit`` with its move group filled in, or ``hit`` unchanged when it is not a move hit."""
    group = _move_group(stream, moves, hit.spans)
    return hit if group is None else replace(hit, move_group_id=group)


def _move_group(stream: UnionStream, moves: dict[str, str], spans: Sequence[Span]) -> str | None:
    """The move group a hit's wording was moved by, or None (6d).

    A hit is a move hit only when **every** elementary run its union spans are made of sits
    under a move and they name the same one: wording that only partly came from a move is not
    the moved text, so it claims no group and is never paired.
    """
    if not moves or not spans:
        return None
    found: str | None = None
    seen = False
    for span in spans:
        for elementary in _elementary_between(stream, span):
            seen = True
            group = _innermost_move(elementary.stack, moves)
            if group is None:
                return None  # a run with no move ancestor: not all of the hit was moved
            if found is None:
                found = group
            elif found != group:
                return None  # the hit's runs were moved by different groups
    return found if seen else None


def _elementary_between(stream: UnionStream, span: Span) -> Iterator[ElementarySpan]:
    """The stream's elementary spans that ``span`` overlaps, in order.

    The elementary spans tile the union, so the first that can overlap is found by search on
    their ends -- the same search :func:`~wordextract.views.project` makes -- and the scan
    stops at the span's own end.
    """
    first = bisect_right(stream.spans, span.start, key=lambda elementary: elementary.end)
    for elementary in stream.spans[first:]:
        if elementary.start >= span.end:
            break
        if elementary.end > span.start:
            yield elementary


def _innermost_move(stack: Sequence[str], moves: dict[str, str]) -> str | None:
    """The group of the innermost move in an ancestor stack, or None.

    Stacks are outermost first, so the last move a stack holds most directly placed the text.
    A stack with no move is not moved, which is what makes a hit that only partly came from a
    move claim no group.
    """
    for revision_id in reversed(stack):
        group = moves.get(revision_id)
        if group is not None:
            return group
    return None


def _resolve(candidates: list[TermMatch]) -> list[TermMatch]:
    """The accepted, non-overlapping subset of one group's candidates.

    Longest span (characters, not tokens), then leftmost, then ``exact > synonym > stem``:
    the first candidate in that order is kept and every candidate overlapping it is
    dropped -- which is where a form that normalizes onto its own canonical form loses to
    it, and where ``match_type`` is settled before any dedupe. Disjoint candidates all
    survive.
    """
    taken: list[TermMatch] = []
    for candidate in sorted(
        candidates, key=lambda m: (-(m.end - m.start), m.start, _RANK[m.match_type])
    ):
        if all(candidate.end <= winner.start or candidate.start >= winner.end for winner in taken):
            taken.append(candidate)
    return sorted(taken, key=lambda m: (m.start, m.end))


def _match_segment(index: TermIndex, segment: str, base: int) -> list[TermMatch]:
    """Every group's hits in one boundary-free ``segment``, addressed as ``base`` + offsets.

    Two readings, unioned: the hyphens-deleted one over every form, and the
    hyphens-as-separators one wherever it can differ from it -- over every form when the
    segment itself contains a hyphen, and over the hyphenated forms otherwise (a form
    without a hyphen reads the same either way, and so does a segment without one). That
    makes a hit depend only on whether the tokens match under *some* reading, never on an
    unrelated hyphen elsewhere in the paragraph.

    Each reading is scanned twice: once as written, and once with every token stemmed, per
    algorithm the index holds stem forms for. Both scans are whole-token sequence matches on
    the same offsets, so a ``stem`` hit is a hit for the same reason an ``exact`` one is --
    it just had to stem the text first. A registry that stems nothing has no stem tables and
    skips the second scan entirely.
    """
    by_group: dict[str, list[TermMatch]] = {}
    has_hyphen = any(char in HYPHENS for char in segment)
    passes = [(False, index._join_first, index._stem_join_first)]
    if has_hyphen:
        passes.append((True, index._split_first, index._stem_split_first))
    elif index._split_first_hyphenated or index._stem_split_first_hyphenated:
        passes.append((True, index._split_first_hyphenated, index._stem_split_first_hyphenated))
    for split_hyphens, first, stem_first in passes:
        tokens = _token_offsets(segment, split_hyphens)
        words = [token for token, _, _ in tokens]
        _scan(words, first, tokens, base, by_group)
        for algorithm, table in stem_first.items():
            stem = stemmer(algorithm)
            _scan([stem(word) for word in words], table, tokens, base, by_group)
    matches: list[TermMatch] = []
    for group in sorted(by_group):
        matches.extend(_resolve(by_group[group]))
    return matches


def _scan(
    words: list[str],
    first: dict[str, tuple[_Form, ...]],
    tokens: list[tuple[str, int, int]],
    base: int,
    by_group: dict[str, list[TermMatch]],
) -> None:
    """Record every form the first-token table ``first`` reaches from ``words``.

    ``words`` and ``first`` must already agree on the reading (both raw, or both stemmed by
    ``first``'s algorithm); ``tokens`` is the raw ``(token, start, end)`` list the hit's
    addresses come from, so a stem hit still points at the corpus characters that produced
    it.
    """
    for position, word in enumerate(words):
        for form in first.get(word, ()):
            width = len(form.tokens)
            if tuple(words[position : position + width]) != form.tokens:
                continue
            by_group.setdefault(form.group, []).append(
                TermMatch(
                    group=form.group,
                    match_type=form.match_type,
                    start=base + tokens[position][1],
                    end=base + tokens[position + width - 1][2],
                )
            )


def _segments(text: str) -> Iterator[tuple[int, str]]:
    """``(base, segment)`` per paragraph-boundary-free run of ``text``.

    Splitting on the terminator is what makes "matching never crosses a paragraph
    boundary" structural rather than a promise: no term is ever offered two segments at
    once.
    """
    base = 0
    for segment in text.split(PARAGRAPH_BOUNDARY):
        yield base, segment
        base += len(segment) + 1


# --- where a hit is (6c) --------------------------------------------------------------


def _merge_views(hits: Iterable[TermHit]) -> list[TermHit]:
    """Fold the hits of one group at one node in one set of spans into one hit.

    Two views are two texts, so the same group at the same node can be found in either,
    both, or neither -- and a hit is that group at that node **in those union spans**, with
    ``present_in`` the views that hold it (D6). The keys are the same three facts
    :func:`dedupe_hits` keys a view-less hit by, so two hits that address the same text are
    one hit here rather than two. A hit keeps the position it was first met at, and takes
    the strongest of the ``match_type``\\ s -- the precedence the forms were resolved by, so
    a word that is a ``stem`` hit in one view and an ``exact`` hit in the other is one
    ``exact`` hit.
    """
    order: list[tuple[str, str, tuple[Span, ...]]] = []
    folded: dict[tuple[str, str, tuple[Span, ...]], TermHit] = {}
    for hit in hits:
        key = (hit.group, hit.node_id, tuple(hit.spans))
        found = folded.get(key)
        if found is None:
            order.append(key)
            folded[key] = hit
            continue
        folded[key] = replace(
            found,
            present_in=found.present_in | hit.present_in,
            view_spans=sorted(
                [*found.view_spans, *hit.view_spans], key=lambda span: span.view.value
            ),
            match_type=MATCH_PRECEDENCE[min(_RANK[found.match_type], _RANK[hit.match_type])],
        )
    return [folded[key] for key in order]


def _stream_of(parsed: ParseResult, part_id: str) -> UnionStream:
    """The part's union stream, or a ``ValueError``: a part that did not stream has no text.

    Every part the walker streams is in ``parsed.union_streams``, so a caller naming a part
    that is not -- a header a document does not have, or a part the package holds but
    nothing reaches -- asked for a text that does not exist, which is not the same as a
    text with no hits in it.
    """
    for stream in parsed.union_streams:
        if stream.part_id == part_id:
            return stream
    raise ValueError(f"part {part_id!r} has no union stream in this document")


def _part_kind(part_id: str) -> LocationKind:
    """The kind of part a ``part_id`` names, from its relationship type local name."""
    kind = _PART_KINDS.get(part_id.split(":", 1)[0])
    if kind is None:
        raise ValueError(
            f"part {part_id!r} is not a part a package streams text in: a part id is "
            f"'<relationship type local name>:<ordinal>' (Turn 1)"
        )
    return kind


def _location(node: Node, by_id: dict[str, Node], parent_of: dict[str, str]) -> LocationKind:
    """Where ``node``'s text sits: a text box, a table cell, or the kind of part it is in.

    A fragment address is a text box before anything else -- its text is the fragment's,
    which is why a text box inside a cell is not a table cell. A paragraph inside a ``CELL``
    is a table cell rather than body text; everything else is its part's own kind, since
    only the part distinguishes the footnote nodes from the endnote ones.
    """
    if any(span.fragment_id is not None for span in node.spans):
        return LocationKind.TEXTBOX  # no fragment streams yet: text boxes are a known gap
    if ancestor_of_kind(node.id, by_id, parent_of, NodeKind.CELL) is not None:
        return LocationKind.TABLE_CELL
    return _part_kind(node.part_id)
