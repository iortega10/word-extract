"""Turn 6a: the term registry, normalization, and the exact/synonym matcher.

``word-extraction-design.md`` D6 and the 6a slice of ``phase1-build-spec.md``'s Turn 6:
a persistent, versioned registry of term groups and a matcher that reports where those
groups occur in a view's text -- deterministic, offline, with no threshold and no
confidence scalar anywhere. What is not deterministic is not a hit.

**What a hit is.** A group occurs where its normalized **token sequence** occurs: whole
tokens, in order, and never across a paragraph boundary. Occurrence is never tested on
characters, so a substring is never a hit -- ``subrogation`` does not match inside
``subrogations``, which is 6b's stemmer and a different ``match_type``.

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

**6a is exact and synonym only.** The form/precedence machinery is generic
(:class:`TermIndex` holds ``(group, match_type, tokens)`` entries and the precedence
tuple already ranks ``stem`` last), so 6b adds one entry per group whose ``stemming``
names a vendored algorithm, with no change to matching, overlap or hashing. Until then
``TermGroup.stemming``, ``rules`` and ``tags`` round-trip through the registry and are
read by nobody: ``rules``/``tags`` are opaque to this matcher by design (D6 -- domain
data is registry data, not matcher data).

The registry itself is a codec record, persisted as content-addressed JSON through the
core ``Collection`` with the record **id equal to** :func:`term_list_hash`: the term list
is the identity, so a re-save is a no-op and two runs of the same terms share one file.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from docextract_core import Collection, encode, from_json, sha256_json, to_json

from .model import LocationKind, MatchType, TermGroup, TermHit, ViewSpan
from .views import Projection

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


def registry_store(root: str | Path) -> Collection[TermRegistry]:
    """A content-addressed store of term lists: one record per distinct term list.

    The record id **is** the term-list hash and so is the idempotency key, which is what
    makes a re-save a no-op and keeps a store free of duplicate copies of one term list.
    """
    return Collection(root, TermRegistry, id_of=term_list_hash, key_of=term_list_hash)


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


def _first_tokens(forms: Iterable[_Form]) -> dict[str, tuple[_Form, ...]]:
    """``forms`` grouped by their first token, each group in the forms' own order."""
    found: dict[str, list[_Form]] = {}
    for form in forms:
        found.setdefault(form.tokens[0], []).append(form)
    return {token: tuple(group) for token, group in found.items()}


@dataclass(frozen=True)
class TermIndex:
    """A compiled registry: every form of every group as token sequences, per reading.

    ``forms`` is the hyphens-deleted reading and ``split_forms`` the hyphens-as-separators
    one; the matcher tries both (see the module docstring). Compiled once and matched
    against many locations, with the forms looked up by their first token, which is what
    keeps the matcher linear in the corpus rather than in the corpus times the registry.
    Canonical in itself -- the forms are sorted -- so two registries with the same term
    list compile to an equal index whatever order they were built in.
    """

    forms: tuple[_Form, ...] = ()
    split_forms: tuple[_Form, ...] = ()
    _join_first: dict = field(init=False, repr=False, compare=False, default_factory=dict)
    _split_first: dict = field(init=False, repr=False, compare=False, default_factory=dict)
    _split_first_hyphenated: dict = field(
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


def compile_registry(registry: TermRegistry) -> TermIndex:
    """Compile ``registry`` into the form table :func:`match_text` matches against.

    Canonical ``canonical`` forms are ``exact``, ``synonyms`` are ``synonym``: the one
    place 6b has to extend to add a ``stem`` form per group.
    """
    joined: list[_Form] = []
    split: list[_Form] = []
    seen: set[tuple[bool, _Form]] = set()
    for group in registry.groups:
        for match_type, form in (
            [(MatchType.EXACT, group.canonical)]
            + [(MatchType.SYNONYM, synonym) for synonym in group.synonyms]
        ):
            hyphenated = any(char in HYPHENS for char in form)
            for is_split, target in ((False, joined), (True, split)):
                candidate = _Form(
                    group=group.canonical,
                    match_type=match_type,
                    tokens=tokenize(form, split_hyphens=is_split),
                    hyphenated=hyphenated,
                )
                if candidate.tokens and (is_split, candidate) not in seen:
                    seen.add((is_split, candidate))
                    target.append(candidate)

    def order(form: _Form) -> tuple:
        return (form.group, _RANK[form.match_type], form.tokens)

    return TermIndex(forms=tuple(sorted(joined, key=order)), split_forms=tuple(sorted(split, key=order)))


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
    location's range in that projection, one node per call (6c loops over the tree); the
    default whole range suits a location that is the whole part, such as a comment body.

    One hit per group per occurrence: a group's overlapping forms are resolved per group
    and ``match_type`` is settled there, before any dedupe. ``location`` defaults to
    ``body``; 6c assigns the real kind (table cell, header, footnote, comment).
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


def dedupe_hits(hits: Iterable[TermHit]) -> list[TermHit]:
    """Drop repeats of one ``(group, node_id, view, start, end)``, keeping the first.

    Keeps the first occurrence -- and so the ``match_type`` already resolved for that
    span -- and the caller's order. A location visited twice (6d's moves, or a location
    re-offered by two passes) is one hit, not two. A hit with no view span at all (6c's
    comment hits, whose ``present_in`` is empty by design) is keyed by its union address
    instead, so the same address at the same node still collapses.
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
    """
    by_group: dict[str, list[TermMatch]] = {}
    has_hyphen = any(char in HYPHENS for char in segment)
    passes = [(False, index._join_first)]
    if has_hyphen:
        passes.append((True, index._split_first))
    elif index._split_first_hyphenated:
        passes.append((True, index._split_first_hyphenated))
    for split_hyphens, first in passes:
        tokens = _token_offsets(segment, split_hyphens)
        words = [token for token, _, _ in tokens]
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
    matches: list[TermMatch] = []
    for group in sorted(by_group):
        matches.extend(_resolve(by_group[group]))
    return matches


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
