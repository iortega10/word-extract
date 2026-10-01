"""Turn 6a: normalization, the term registry, and the exact/synonym matcher.

A term list is *input*, so -- unlike every earlier turn -- there is no sidecar to derive
these expectations from. They are the build spec's 6a rules written out as literals:
normalization identical for registry entries and corpus, whole tokens and never
substrings, longest span then leftmost then exact > synonym > stem, disjoint matches all
reported, ``match_type`` resolved before dedupe, one synonym change changes
``term_list_hash`` -- plus ``text-model-spec.md``'s worked example A, whose union and
view strings a 0a sidecar already pins (§6; the hyphen family and the union alphabet at
§8, ``w:softHyphen`` = `U+00AD`, ``w:noBreakHyphen`` = `U+2011``).

The projection tests build their stream here the way ``test_views`` does -- a union text
tiled by elementary spans with ancestor stacks -- so a claim about a hit's union address
is a claim about the matcher and the offset map, not about the walker.

The 6c tests hand-build the ``ParseResult`` -- streams, node tree, comments -- for the same
reason at one level up: *where* a hit is (its part, its table cell, the comment it was
written in) is the thing under test, and a package fixture would make the assertions read
as fixture facts rather than as 6c's rules.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from docextract_core import from_json, to_json

from wordextract.model import (
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
from wordextract.terms import (
    HYPHENS,
    MATCH_PRECEDENCE,
    TermIndex,
    TermMatch,
    TermRegistry,
    compile_registry,
    dedupe_hits,
    dump_registry,
    load_registry,
    load_registry_text,
    match_comments,
    match_document,
    match_part,
    match_projection,
    match_text,
    normalize,
    registry_hashes,
    registry_store,
    save_registry,
    term_list_hash,
    tokenize,
)
from wordextract.views import Projection, project
from wordextract.walker import TERMINATOR

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
EXAMPLE_PATH = FIXTURES / "terms" / "synthetic.example.json"

# --- hand-typed term groups ----------------------------------------------------------

SUBROGATION = TermGroup(
    canonical="subrogation",
    synonyms=["right of subrogation", "right of recovery", "waiver of subrogation"],
    stemming="porter",
    tags={"category": "coverage"},
)
NONCOMPLIANCE = TermGroup(
    canonical="non-compliance",
    synonyms=["noncompliance"],
    tags={"category": "compliance"},
)
AGGREGATE = TermGroup(
    canonical="aggregate limit",
    synonyms=["policy aggregate"],
    tags={"category": "limits"},
)
INSURED = TermGroup(
    canonical="named insured",
    synonyms=["additional insured"],
    tags={"category": "parties"},
)

#: Typed out again rather than imported from the test fixture, so the shipped example
#: registry is checked against a second reading of the same literals.
EXAMPLE_GROUPS = [SUBROGATION, NONCOMPLIANCE, AGGREGATE, INSURED]


def _registry(*groups: TermGroup) -> TermRegistry:
    return TermRegistry(groups=list(groups))


def _index(*groups: TermGroup) -> TermIndex:
    return compile_registry(_registry(*groups))


def _stream(
    *pieces: tuple[list[str], str], part_id: str = "word/document.xml"
) -> UnionStream:
    """A union stream from ``(stack, text)`` pieces, offsets derived here.

    ``part_id`` defaults to the file name the older tests pour their union text under;
    the 6c tests pass a real part id, since that is what a hit's kind is read from.
    """
    text = ""
    spans: list[ElementarySpan] = []
    for stack, piece in pieces:
        spans.append(ElementarySpan(len(text), len(text) + len(piece), list(stack)))
        text += piece
    return UnionStream(part_id, text, spans)


def _paragraphs(*texts: str, part_id: str = "word/document.xml") -> UnionStream:
    """One part whose body is ``texts``, one terminated paragraph each."""
    pieces: list[tuple[list[str], str]] = []
    for text in texts:
        pieces += [([], text), ([], TERMINATOR)]
    return _stream(*pieces, part_id=part_id)


def _whole_part(*texts: str, view: View = View.ACCEPTED) -> Projection:
    return project(_paragraphs(*texts), view)


def _example_a_stream() -> UnionStream:
    """``text-model-spec.md`` §6 example A: a delete then an insert, no separator."""
    return _stream(
        ([], "right of "),
        (["del:1"], "recovery"),
        (["ins:2"], "subrogation"),
        ([], TERMINATOR),
    )


# --- normalization (§ build spec 6a) -------------------------------------------------

def test_case_punctuation_and_whitespace_normalize_alike():
    assert normalize("  Waiver  of,\tSUBROGATION!  ") == "waiver of subrogation"
    assert normalize("Waiver Of Subrogation.") == normalize("waiver  of subrogation")


def test_the_hyphen_family_joins_and_a_space_separates():
    assert HYPHENS == {"\u002d", "\u00ad", "\u2010", "\u2011"}
    for hyphen in sorted(HYPHENS):
        assert tokenize(f"non{hyphen}compliance") == ("noncompliance",), hyphen
    assert tokenize("non compliance") == ("non", "compliance")
    assert tokenize("right ofsubrogation") == ("right", "ofsubrogation")


def test_a_hyphen_after_a_token_fuses_what_follows_it():
    """The price of the rule: a hyphen *joins*, so it never separates a token from the
    text after it -- which is exactly what makes the corpus seam faithful."""
    assert tokenize("subrogation-clause") == ("subrogationclause",)
    assert tokenize("subrogation - clause") == ("subrogation", "clause")


def test_the_fused_seam_token_is_a_faithful_match_not_a_false_positive():
    index = _index(TermGroup(canonical="non-compliance"))
    assert match_text(index, "noncompliance") == [
        TermMatch(group="non-compliance", match_type=MatchType.EXACT, start=0, end=13)
    ]
    # the same document with the seams written as characters the union keeps as one run
    assert [m.start for m in match_text(index, "non\u00adcompliance")] == [0]
    # a hyphenated form also reads with its hyphen as a separator, so the spaced spelling of
    # the same compound is the same term (a space is still a real separator elsewhere)
    assert [m.end - m.start for m in match_text(index, "non compliance")] == [14]


def test_a_decomposed_spelling_folds_onto_the_precomposed_token():
    assert tokenize("cafe\u0301") == tokenize("caf\u00e9") == ("caf\u00e9",)
    assert normalize("Re\u0301sume\u0301") == normalize("R\u00e9sum\u00e9")


def test_punctuation_only_and_empty_text_have_no_tokens():
    assert tokenize("  --- ,, !! ") == ()
    assert tokenize("") == ()
    assert normalize("") == ""


def test_normalization_is_one_rule_for_registry_and_corpus():
    index = _index(TermGroup(canonical="Waiver of Subrogation"))
    assert [m.group for m in match_text(index, "the WAIVER  of,  subrogation!")] == [
        "Waiver of Subrogation"
    ]


# --- the registry and its hash -------------------------------------------------------

def test_a_registry_rejects_what_could_never_match_or_would_match_everywhere():
    with pytest.raises(ValueError, match="duplicate"):
        _registry(SUBROGATION, TermGroup(canonical="subrogation"))
    with pytest.raises(ValueError, match="paragraph boundary"):
        _registry(TermGroup(canonical="right of\nsubrogation"))
    with pytest.raises(ValueError, match="no tokens"):
        _registry(TermGroup(canonical="  ---  "))
    with pytest.raises(ValueError, match="no tokens"):
        _registry(TermGroup(canonical="ok", synonyms=["..."]))


def test_the_registry_round_trips_through_the_codec():
    registry = _registry(SUBROGATION, AGGREGATE)
    assert from_json(TermRegistry, to_json(registry)) == registry


def test_term_list_hash_ignores_the_order_the_term_list_was_typed_in():
    assert term_list_hash(_registry(SUBROGATION, AGGREGATE)) == term_list_hash(
        _registry(AGGREGATE, SUBROGATION)
    )
    reordered = TermGroup(
        canonical="subrogation",
        synonyms=list(reversed(SUBROGATION.synonyms)),
        stemming="porter",
        tags={"category": "coverage"},
    )
    assert term_list_hash(_registry(reordered, AGGREGATE)) == term_list_hash(
        _registry(SUBROGATION, AGGREGATE)
    )


def test_one_synonym_change_changes_the_term_list_hash():
    base = term_list_hash(_registry(SUBROGATION))
    assert term_list_hash(_registry(TermGroup(canonical="subrogation"))) != base
    assert term_list_hash(_registry(TermGroup(canonical="subrogation", synonyms=["waiver"]))) != base


def test_every_term_group_field_participates_in_the_term_list_hash():
    """flip-one-input for the term list: nothing on a group can change silently."""
    base = term_list_hash(_registry(SUBROGATION, AGGREGATE))
    for flipped in (
        TermGroup(canonical="subrogations", synonyms=SUBROGATION.synonyms, stemming="porter", tags=SUBROGATION.tags),
        TermGroup(canonical="subrogation", synonyms=["waiver of subrogation"], stemming="porter", tags=SUBROGATION.tags),
        TermGroup(canonical="subrogation", synonyms=SUBROGATION.synonyms, stemming=None, tags=SUBROGATION.tags),
        TermGroup(canonical="subrogation", synonyms=SUBROGATION.synonyms, stemming="snowball", tags=SUBROGATION.tags),
        TermGroup(canonical="subrogation", synonyms=SUBROGATION.synonyms, stemming="porter", rules={"allow": "x"}, tags=SUBROGATION.tags),
        TermGroup(canonical="subrogation", synonyms=SUBROGATION.synonyms, stemming="porter", tags={"category": "limits"}),
    ):
        assert term_list_hash(_registry(flipped, AGGREGATE)) != base, flipped


def test_the_store_is_content_addressed_by_the_term_list_hash(tmp_path):
    store = registry_store(tmp_path / "term_registry")
    registry = _registry(SUBROGATION, AGGREGATE)
    record, written = save_registry(store, registry)
    assert written is True and record == registry

    digest = term_list_hash(registry)
    stored = tmp_path / "term_registry" / f"{digest}.json"
    assert stored.read_text(encoding="utf-8") == dump_registry(registry)
    assert registry_hashes(store) == [digest]
    assert load_registry(store, digest) == registry
    assert load_registry(store, "0" * 64) is None

    # the same term list, typed in another order, is the same record -- not a second file
    again, written_again = save_registry(store, _registry(AGGREGATE, SUBROGATION))
    assert written_again is False and again == registry
    assert registry_hashes(store) == [digest]


def test_the_shipped_example_registry_is_the_canonical_registry_it_claims():
    registry = load_registry_text(EXAMPLE_PATH.read_text(encoding="utf-8"))
    assert registry == TermRegistry(groups=EXAMPLE_GROUPS)
    assert json.loads(EXAMPLE_PATH.read_text(encoding="utf-8")) == json.loads(dump_registry(registry))
    assert term_list_hash(registry) == term_list_hash(_registry(*EXAMPLE_GROUPS))


def test_the_example_registry_matches_its_own_terms():
    index = compile_registry(load_registry_text(EXAMPLE_PATH.read_text(encoding="utf-8")))
    hits = match_text(index, "this policy is subject to noncompliance and waiver of subrogation")
    assert [(hit.group, hit.match_type.value) for hit in hits] == [
        ("non-compliance", "exact"),
        ("subrogation", "synonym"),
    ]


# --- the matcher --------------------------------------------------------------------

def test_the_match_types_are_ranked_exact_synonym_stem():
    """The tie-break 6b has to slot into without touching overlap resolution."""
    assert MATCH_PRECEDENCE == (MatchType.EXACT, MatchType.SYNONYM, MatchType.STEM)


def test_an_exact_match_is_exact_and_a_synonym_match_is_synonym():
    index = _index(SUBROGATION)
    assert match_text(index, "The subrogation clause") == [
        TermMatch(group="subrogation", match_type=MatchType.EXACT, start=4, end=15)
    ]
    assert match_text(index, "any waiver of subrogation applies") == [
        TermMatch(group="subrogation", match_type=MatchType.SYNONYM, start=4, end=25)
    ]
    assert match_text(index, "the right of recovery") == [
        TermMatch(group="subrogation", match_type=MatchType.SYNONYM, start=4, end=21)
    ]


def test_matching_is_whole_tokens_never_substrings():
    # a group without a stemmer matches only the whole token: the substring is never a hit
    index = _index(TermGroup(canonical="subrogation"), TermGroup(canonical="of"))
    assert match_text(index, "subrogations") == []
    assert match_text(index, "subrogation") == [
        TermMatch(group="subrogation", match_type=MatchType.EXACT, start=0, end=11)
    ]
    # a one-token term is a token, not two characters
    assert [(m.group, m.start) for m in match_text(index, "office of the insured")] == [("of", 7)]


def test_a_stemmer_is_what_reaches_a_whole_token_variant_never_a_substring():
    """``subrogations`` is a hit of ``subrogation`` only because ``porter`` maps both to
    ``subrog`` -- reported as ``stem``, and only for a group that asked for stemming."""
    index = _index(SUBROGATION, TermGroup(canonical="of"))
    assert match_text(index, "subrogations") == [
        TermMatch(group="subrogation", match_type=MatchType.STEM, start=0, end=12)
    ]


def test_a_form_occurs_as_a_contiguous_token_sequence_or_not_at_all():
    index = _index(TermGroup(canonical="right of subrogation"))
    assert [m.start for m in match_text(index, "the right of subrogation clause")] == [4]
    assert match_text(index, "the right subrogation of clause") == []


def test_every_disjoint_match_is_reported():
    index = _index(SUBROGATION)
    assert [m.start for m in match_text(index, "subrogation and subrogation")] == [0, 16]


def test_the_longest_span_wins_over_the_match_type():
    index = _index(TermGroup(canonical="waiver", synonyms=["waiver of subrogation"]))
    assert match_text(index, "a waiver of subrogation clause") == [
        TermMatch(group="waiver", match_type=MatchType.SYNONYM, start=2, end=23)
    ]


def test_the_leftmost_span_wins_when_two_spans_are_the_same_length():
    """A tie on span length is reachable, so the second rule of the precedence gets its
    turn: both candidates are 14 characters, so the leftmost wins."""
    index = _index(TermGroup(canonical="of subrogation", synonyms=["subrogation of"]))
    assert match_text(index, "of subrogation of") == [
        TermMatch(group="of subrogation", match_type=MatchType.EXACT, start=0, end=14)
    ]


def test_match_type_is_resolved_before_dedupe():
    """A synonym that normalizes onto its own canonical form reports one hit, as exact."""
    index = _index(TermGroup(canonical="noncompliance", synonyms=["non-compliance"]))
    for text in ("noncompliance", "non-compliance", "NONcompliance"):
        assert match_text(index, text) == [
            TermMatch(group="noncompliance", match_type=MatchType.EXACT, start=0, end=len(text))
        ]


def test_distinct_groups_report_distinct_hits_on_the_same_span():
    """Two canonical forms that normalize alike are two groups, so two hits: the
    dedupe key carries the group."""
    index = _index(TermGroup(canonical="non-compliance"), TermGroup(canonical="noncompliance"))
    hits = match_text(index, "non-compliance")
    assert sorted(hit.group for hit in hits) == ["non-compliance", "noncompliance"]
    assert {(hit.start, hit.end) for hit in hits} == {(0, 14)}


def test_stemming_is_applied_per_group_and_the_portmanteau_is_honest():
    """6b: a group that names an algorithm matches its stem, in the canonical's own
    reading. ``included`` and ``including`` both stem to ``includ`` and unify; ``inclusion``
    stems to ``inclus`` and does not -- Porter's documented, genuine non-unification, kept
    rather than papered over."""
    registry = _registry(TermGroup(canonical="included", stemming="porter"))
    assert registry.groups[0].stemming == "porter"
    index = compile_registry(registry)
    assert match_text(index, "included") == [
        TermMatch(group="included", match_type=MatchType.EXACT, start=0, end=8)
    ]
    assert match_text(index, "including") == [
        TermMatch(group="included", match_type=MatchType.STEM, start=0, end=9)
    ]
    assert match_text(index, "inclusion") == []


def test_stem_forms_are_derived_from_the_canonical_only():
    """A synonym is matched exactly; it never widens the stem table, which is the
    canonical's alone."""
    index = _index(TermGroup(canonical="include", synonyms=["incorporate"], stemming="porter"))
    assert {form.tokens for form in index.stem_forms} == {("includ",)}
    assert index.stem_split_forms == index.stem_forms
    assert match_text(index, "incorporation") == []


def test_stem_forms_never_leak_into_the_forms_the_term_list_wrote():
    """``forms``/``split_forms`` are exactly what the registry wrote; the stem tables are the
    matcher's own, so a caller reading ``index.forms`` never sees an invented token."""
    index = _index(TermGroup(canonical="include", synonyms=["incorporate"], stemming="porter"))
    assert {form.tokens for form in index.forms} == {("include",), ("incorporate",)}
    assert {form.tokens for form in index.split_forms} == {("include",), ("incorporate",)}
    assert {form.stem for form in index.forms} == {None}
    assert {form.tokens for form in index.stem_forms} == {("includ",)}


def test_a_stem_hit_loses_to_an_exact_hit_on_the_same_span():
    """``exact > synonym > stem`` settles before dedupe: the canonical's own token is one
    hit, reported as exact, even though its stem form also matches there."""
    index = _index(TermGroup(canonical="subrogation", stemming="porter"))
    assert match_text(index, "subrogation") == [
        TermMatch(group="subrogation", match_type=MatchType.EXACT, start=0, end=11)
    ]


def test_an_unknown_stemming_algorithm_is_a_loud_failure():
    """A ``stemming`` naming no shipped algorithm is a registry the matcher cannot honour;
    it must fail at compile, not silently match nothing (the miss is the failure mode)."""
    registry = _registry(TermGroup(canonical="subrogation", stemming="snowball"))
    with pytest.raises(ValueError, match="snowball"):
        compile_registry(registry)


def test_stem_matching_reads_the_hyphen_the_same_way_exact_matching_does():
    index = _index(TermGroup(canonical="non-compliance", stemming="porter"))
    for text in ("non-compliance", "noncompliance", "non compliance"):
        assert [
            (m.group, m.match_type) for m in match_text(index, text)
        ] == [("non-compliance", MatchType.EXACT)], text
    assert match_text(index, "non-compliance") == match_text(
        _index(TermGroup(canonical="non-compliance")), "non-compliance"
    )
    # the split reading reaches a stem hit too: ``compliances`` stems to ``complianc``
    assert match_text(index, "non compliances") == [
        TermMatch(group="non-compliance", match_type=MatchType.STEM, start=0, end=15)
    ]


def test_rules_and_tags_cannot_steer_the_matcher():
    """D6: the matcher is generic, so a group's own config is registry data it does not
    read. Only ``canonical`` and ``synonyms`` widen what a group matches -- which is what
    makes "an unapproved candidate never affects hits" structural rather than a promise."""
    plain = _index(TermGroup(canonical="subrogation"))
    configured = _index(
        TermGroup(
            canonical="subrogation",
            rules={"fuzzy": {"threshold": 0.8}, "regex": "subrogat.*"},
            tags={"category": "coverage", "approved": "yes"},
        )
    )
    assert configured == plain
    for text in ("subrogation", "subrogations", "waiver of subrogation"):
        assert match_text(configured, text) == match_text(plain, text)


def test_matching_never_crosses_a_paragraph_boundary():
    index = _index(TermGroup(canonical="right of subrogation"))
    assert match_text(index, "right of" + TERMINATOR + "subrogation") == []
    # a w:br / w:tab is content inside the paragraph, so a phrase may cross it (§8)
    assert [m.start for m in match_text(index, "right of\u000b subrogation")] == [0]


def test_the_same_inputs_give_the_same_matches_every_time():
    text = "waiver of subrogation and noncompliance, twice: waiver of subrogation"
    index = _index(SUBROGATION, NONCOMPLIANCE)
    first = match_text(index, text)
    assert first == match_text(index, text)
    assert first == match_text(_index(NONCOMPLIANCE, SUBROGATION), text)
    assert [(m.group, m.start, m.end) for m in first] == [
        ("subrogation", 0, 21),
        ("non-compliance", 26, 39),
        ("subrogation", 48, 69),
    ]


def test_two_typing_orders_compile_to_the_same_index():
    assert _index(SUBROGATION, AGGREGATE) == _index(
        AGGREGATE,
        TermGroup(
            canonical="subrogation",
            synonyms=list(reversed(SUBROGATION.synonyms)),
            stemming="porter",
            tags={"category": "coverage"},
        ),
    )


# --- hits in a projection -----------------------------------------------------------

def test_example_a_is_two_hits_in_one_group():
    """The spec's headline case: the deleted text is a hit in ``original`` and the
    inserted text a hit in ``accepted``, both of one group."""
    index = _index(TermGroup(canonical="right of subrogation", synonyms=["right of recovery"]))
    stream = _example_a_stream()

    accepted = project(stream, View.ACCEPTED)
    original = project(stream, View.ORIGINAL)
    assert accepted.text == "right of subrogation" + TERMINATOR
    assert original.text == "right of recovery" + TERMINATOR

    (inserted,) = match_projection(index, accepted, node_id="n1")
    assert inserted.group == "right of subrogation"
    assert inserted.match_type is MatchType.EXACT
    assert inserted.present_in == {View.ACCEPTED}
    assert inserted.view_spans == [ViewSpan(View.ACCEPTED, 0, 20)]
    assert inserted.spans == [Span("word/document.xml", 0, 9), Span("word/document.xml", 17, 28)]

    (deleted,) = match_projection(index, original, node_id="n1")
    assert deleted.match_type is MatchType.SYNONYM
    assert deleted.present_in == {View.ORIGINAL}
    assert deleted.view_spans == [ViewSpan(View.ORIGINAL, 0, 17)]
    # one contiguous union span: the inserted text between them was elided, not skipped
    assert deleted.spans == [Span("word/document.xml", 0, 17)]


def test_the_raw_union_false_adjacency_is_never_a_hit():
    index = _index(TermGroup(canonical="right of subrogation", synonyms=["right of recovery"]))
    assert match_text(index, _example_a_stream().text) == []


def test_view_spans_are_whole_part_offsets_not_paragraph_relative():
    index = _index(SUBROGATION)
    projection = _whole_part("Coverage applies worldwide.", "The subrogation clause.")
    assert projection.text == (
        "Coverage applies worldwide." + TERMINATOR + "The subrogation clause." + TERMINATOR
    )

    (hit,) = match_projection(
        index, projection, node_id="n2", start=28, end=52, location=LocationKind.BODY
    )
    assert hit.node_id == "n2"
    assert hit.view_spans == [ViewSpan(View.ACCEPTED, 32, 43)]
    assert hit.spans == [Span("word/document.xml", 32, 43)]

    # the whole part in one call is still matched paragraph by paragraph, in order
    every = match_projection(index, _whole_part("a subrogation clause", "another subrogation"), node_id="n1")
    assert [h.match_type for h in every] == [MatchType.EXACT, MatchType.EXACT]
    assert [h.view_spans[0].start for h in every] == [2, 29]


def test_dedupe_hits_keeps_one_hit_per_group_node_view_and_span():
    index = _index(SUBROGATION)
    projection = _whole_part("a subrogation clause")
    hits = match_projection(index, projection, node_id="n1")
    assert dedupe_hits(hits + hits) == hits
    other = match_projection(index, projection, node_id="n2")
    assert dedupe_hits(hits + other) == hits + other


def test_dedupe_hits_addresses_a_view_less_hit_by_its_union_span():
    """6c's comment hits carry no view; the same address at the same node still folds."""
    hit = TermHit(
        group="subrogation",
        node_id="comment:0000001A",
        spans=[Span("word/comments.xml", 3, 14)],
        location=LocationKind.COMMENT,
    )
    assert dedupe_hits([hit, hit]) == [hit]


# --- where a hit is (6c) ---------------------------------------------------------------

#: 6c addresses parts by real part ids -- ``<relationship type local name>:<ordinal>`` --
#: because the *part* is what a hit's kind is read from, and ``word/document.xml`` is the
#: file name of the same thing.
DOCUMENT = "officeDocument:0"
COMMENTS_PART = "comments:0"


def _nodes(*specs: tuple[str, NodeKind, str, int | None, int | None, str | None]) -> list[Node]:
    """A node list from ``(id, kind, part_id, start, end, parent_id)``.

    ``start``/``end`` are ``None`` for a container, which has no text of its own -- the
    walker gives it no span. Parentage is written into the parent's ``child_ids`` here,
    because that is the edge the parent map is made of.
    """
    children: dict[str, list[str]] = {}
    for node_id, _kind, _part_id, _start, _end, parent in specs:
        if parent is not None:
            children.setdefault(parent, []).append(node_id)
    return [
        Node(
            id=node_id,
            kind=kind,
            part_id=part_id,
            # the package member the node was read from: not under test, so the id stands in
            source_ref=node_id,
            spans=[] if start is None else [Span(part_id, start, end)],
            child_ids=children.get(node_id, []),
        )
        for node_id, kind, part_id, start, end, _parent in specs
    ]


def _parsed(
    streams: list[UnionStream], nodes: list[Node], comments: list[Comment] | None = None
) -> ParseResult:
    return ParseResult(union_streams=streams, nodes=nodes, comments=comments or [])


def test_a_part_that_did_not_stream_is_a_loud_failure():
    """A part with no union stream has no text to address a hit in, which is not the same
    as a part with no hits in it: the miss is the failure mode, so it is a ``ValueError``."""
    index = _index(SUBROGATION)
    parsed = _parsed([_paragraphs("a subrogation clause", part_id=DOCUMENT)], [])
    with pytest.raises(ValueError, match="comments:0"):
        match_part(index, parsed, COMMENTS_PART)


def test_one_paragraph_is_matched_in_each_view_on_its_own():
    """Example A through the 6c API: one paragraph, and the words it holds differ per view,
    so the two views are two hits in one group -- each addressed in the whole part."""
    index = _index(TermGroup(canonical="right of subrogation", synonyms=["right of recovery"]))
    stream = _stream(
        ([], "right of "),
        (["del:1"], "recovery"),
        (["ins:2"], "subrogation"),
        ([], TERMINATOR),
        part_id=DOCUMENT,
    )
    parsed = _parsed([stream], _nodes(("p1", NodeKind.PARA, DOCUMENT, 0, 28, None)))

    (accepted,) = match_part(index, parsed, DOCUMENT)
    assert accepted.node_id == "p1"
    assert accepted.location is LocationKind.BODY
    assert accepted.match_type is MatchType.EXACT
    assert accepted.present_in == {View.ACCEPTED}
    assert accepted.view_spans == [ViewSpan(View.ACCEPTED, 0, 20)]
    assert accepted.spans == [Span(DOCUMENT, 0, 9), Span(DOCUMENT, 17, 28)]

    (original,) = match_part(index, parsed, DOCUMENT, view=View.ORIGINAL)
    assert original.match_type is MatchType.SYNONYM
    assert original.present_in == {View.ORIGINAL}
    assert original.view_spans == [ViewSpan(View.ORIGINAL, 0, 17)]
    # one contiguous union span: the text between them is elided here, not skipped
    assert original.spans == [Span(DOCUMENT, 0, 17)]

    # the views read one paragraph as different words, so they are two hits, not one
    assert [(h.match_type, h.present_in) for h in match_document(index, parsed)] == [
        (MatchType.EXACT, {View.ACCEPTED}),
        (MatchType.SYNONYM, {View.ORIGINAL}),
    ]


def test_one_group_at_one_node_in_one_union_span_is_one_hit_in_both_views():
    """Where what the views disagree about sits *outside* the hit, both views hold the same
    union range: that is one hit found in two views (D6's ``present_in`` set)."""
    index = _index(SUBROGATION)
    stream = _stream(
        (["del:1"], "draft "), ([], "a subrogation clause"), ([], TERMINATOR), part_id=DOCUMENT
    )
    parsed = _parsed([stream], _nodes(("p1", NodeKind.PARA, DOCUMENT, 6, 26, None)))

    assert [h.present_in for h in match_part(index, parsed, DOCUMENT)] == [{View.ACCEPTED}]
    assert [h.present_in for h in match_part(index, parsed, DOCUMENT, view=View.ORIGINAL)] == [
        {View.ORIGINAL}
    ]

    (hit,) = match_document(index, parsed)
    assert hit.node_id == "p1"
    assert hit.match_type is MatchType.EXACT  # one reading in both views, so one type
    assert hit.present_in == {View.ACCEPTED, View.ORIGINAL}
    # the hit is where it was first met, and its range per view is that view's own
    assert hit.view_spans == [ViewSpan(View.ACCEPTED, 2, 13), ViewSpan(View.ORIGINAL, 8, 19)]
    assert hit.spans == [Span(DOCUMENT, 8, 19)]


def test_a_paragraph_the_view_elided_is_not_matched():
    """Matching sees the view's paragraphs, never the union's: text this view deleted is in
    no paragraph of it, so it is no hit -- in ``original``, where it is a paragraph, it is."""
    index = _index(SUBROGATION)
    stream = _stream((["del:1"], "a subrogation clause"), ([], TERMINATOR), part_id=DOCUMENT)
    parsed = _parsed([stream], _nodes(("p1", NodeKind.PARA, DOCUMENT, 0, 22, None)))

    assert match_part(index, parsed, DOCUMENT) == []

    (deleted,) = match_part(index, parsed, DOCUMENT, view=View.ORIGINAL)
    assert deleted.present_in == {View.ORIGINAL}
    assert deleted.view_spans == [ViewSpan(View.ORIGINAL, 2, 13)]
    assert match_document(index, parsed) == [deleted]


def test_a_hit_says_which_kind_of_place_it_sits_in():
    """The kind is the *part*'s, except inside a table: a footnote and an endnote are both
    ``FOOTNOTE`` nodes, so only the parts tell those two hits apart. A container is not a
    matching unit, so the table whose text it holds is not a second hit."""
    index = _index(SUBROGATION)
    streams = [
        _paragraphs("body subrogation", "cell subrogation", part_id=DOCUMENT),
        _paragraphs("header subrogation", part_id="header:0"),
        _paragraphs("footer subrogation", part_id="footer:0"),
        _paragraphs("footnote subrogation", part_id="footnotes:0"),
        _paragraphs("endnote subrogation", part_id="endnotes:0"),
    ]
    nodes = _nodes(
        ("b1", NodeKind.PARA, DOCUMENT, 0, 16, None),
        # the table holds the cell paragraph's own span and is still not a location
        ("t1", NodeKind.TABLE, DOCUMENT, 17, 34, None),
        ("r1", NodeKind.ROW, DOCUMENT, None, None, "t1"),
        ("c1", NodeKind.CELL, DOCUMENT, 17, 34, "r1"),
        ("c1p", NodeKind.PARA, DOCUMENT, 17, 33, "c1"),
        ("h1", NodeKind.PARA, "header:0", 0, 18, None),
        ("f1", NodeKind.PARA, "footer:0", 0, 18, None),
        ("fn0", NodeKind.FOOTNOTE, "footnotes:0", None, None, None),
        ("fn1", NodeKind.PARA, "footnotes:0", 0, 20, "fn0"),
        ("en0", NodeKind.FOOTNOTE, "endnotes:0", None, None, None),
        ("en1", NodeKind.PARA, "endnotes:0", 0, 19, "en0"),
    )
    parsed = _parsed(streams, nodes)

    assert [(h.node_id, h.location) for h in match_document(index, parsed)] == [
        ("b1", LocationKind.BODY),
        ("c1p", LocationKind.TABLE_CELL),
        ("h1", LocationKind.HEADER),
        ("f1", LocationKind.FOOTER),
        ("fn1", LocationKind.FOOTNOTE),
        ("en1", LocationKind.ENDNOTE),
    ]


def test_a_comments_part_has_no_locations_of_its_own():
    """A comment's own words stream in a part of their own, but they are not in the node
    tree: they are read off the ``Comment`` record by :func:`match_comments`, never matched
    as text of the part they streamed from -- which is where a node would put them."""
    index = _index(SUBROGATION)
    stream = _paragraphs("a subrogation clause", part_id=COMMENTS_PART)
    parsed = _parsed([stream], _nodes(("c1", NodeKind.PARA, COMMENTS_PART, 0, 22, None)))

    assert stream.text.startswith("a subrogation clause")  # the words *are* there to match
    assert match_part(index, parsed, COMMENTS_PART) == []
    assert match_document(index, parsed) == []


def test_a_comment_body_is_a_hit_whose_location_is_the_comment():
    index = _index(SUBROGATION)
    document = _paragraphs("the subrogation clause", part_id=DOCUMENT)
    comments = _paragraphs("please check the subrogation wording", part_id=COMMENTS_PART)
    nodes = _nodes(("p1", NodeKind.PARA, DOCUMENT, 0, 22, None))
    comment = Comment(
        para_id="0000001A",
        author="Reviewer",
        initials="RE",
        anchor=Span(DOCUMENT, 4, 15),
        anchor_text="subrogation",
        text="please check the subrogation wording",
        text_span=Span(COMMENTS_PART, 0, 36),
    )
    parsed = _parsed([document, comments], nodes, [comment])

    body, hit = match_document(index, parsed)
    assert (body.node_id, body.location) == ("p1", LocationKind.BODY)

    assert hit.group == "subrogation"
    assert hit.match_type is MatchType.EXACT
    assert hit.node_id == "comment:0000001A"  # reaches the ``Comment`` the hit came from
    assert hit.location is LocationKind.COMMENT
    # no view holds a comment's words: the hit is in the address space, not in a text
    assert hit.present_in == set()
    assert hit.view_spans == []
    # the address is the comment's own range, offset by where the tokens sit in its ``text``
    assert hit.spans == [Span(COMMENTS_PART, 17, 28)]
    # ...and its "where" is the node the comment is anchored to
    assert hit.context_node_id == "p1"


def test_a_comment_with_no_anchor_is_a_hit_with_no_context_node():
    index = _index(SUBROGATION)
    comment = Comment(
        para_id="0000002B",
        author="Reviewer",
        initials="RE",
        text="subrogation",
        text_span=Span(COMMENTS_PART, 0, 11),
    )
    (hit,) = match_comments(index, _parsed([], [], [comment]))
    assert hit.spans == [Span(COMMENTS_PART, 0, 11)]
    assert hit.context_node_id is None


def test_a_comment_whose_part_did_not_stream_claims_nothing():
    """Its words are known, but the range they were read from is not -- so nothing at all."""
    index = _index(SUBROGATION)
    comment = Comment(
        para_id="0000001A",
        author="Reviewer",
        initials="RE",
        anchor=Span(DOCUMENT, 4, 15),
        anchor_text="subrogation",
        text="please check the subrogation wording",
    )
    parsed = _parsed(
        [_paragraphs("the subrogation clause", part_id=DOCUMENT)],
        _nodes(("p1", NodeKind.PARA, DOCUMENT, 0, 22, None)),
        [comment],
    )
    assert match_comments(index, parsed) == []
    assert len(match_document(index, parsed)) == 1  # the body hit, and none for the comment


def test_a_text_box_is_not_matched_as_the_part_it_sits_in():
    """The gap: a text box's words are its own fragment's, and no fragment streams yet. The
    offsets below *are* a range of the part, holding the same word, so reading them as the
    part's is exactly the mistake the fragment check is there to prevent."""
    index = _index(SUBROGATION)
    stream = _paragraphs("a subrogation clause", part_id=DOCUMENT)
    box = Node(
        id="b1",
        kind=NodeKind.PARA,
        part_id=DOCUMENT,
        source_ref="b1",
        spans=[Span(DOCUMENT, 2, 13, fragment_id="b1:0")],
    )
    parsed = _parsed([stream], [box])

    assert stream.text[2:13] == "subrogation"  # what a part-addressed reading would say
    assert match_part(index, parsed, DOCUMENT) == []
    assert match_document(index, parsed) == []


# --- option C: a term matches under either reading of the hyphen -----------------------


def _spans(index: TermIndex, text: str) -> list[tuple[str, int, int]]:
    return [(m.group, m.start, m.end) for m in match_text(index, text)]


def test_a_plain_form_matches_the_hyphenated_spelling_in_the_text():
    """``hold-harmless`` was silently missed by ``hold harmless`` before (hyphens joined)."""
    index = _index(TermGroup(canonical="hold harmless"))
    assert _spans(index, "Hold-harmless agreement") == [("hold harmless", 0, 13)]
    assert _spans(index, "hold\u2010harmless") == [("hold harmless", 0, 13)]  # U+2010
    assert _spans(index, "hold\u2011harmless") == [("hold harmless", 0, 13)]  # U+2011
    assert _spans(index, "hold harmless") == [("hold harmless", 0, 13)]


def test_a_hyphenated_form_matches_the_spaced_and_the_joined_spelling():
    index = _index(TermGroup(canonical="hold-harmless"))
    for text in ("hold-harmless", "hold harmless", "holdharmless", "HOLD  HARMLESS"):
        assert [g for g, _, _ in _spans(index, text)] == ["hold-harmless"], text


def test_the_joined_reading_still_matches_a_prefix_compound():
    index = _index(TermGroup(canonical="non-compliance", synonyms=["noncompliance"]))
    for text in ("non-compliance", "noncompliance", "non compliance", "NON\u00adCOMPLIANCE"):
        assert len(match_text(index, text)) == 1, text


def test_a_span_found_under_both_readings_is_one_hit():
    """``non-compliance`` reads the same as a form under both readings: one hit, exact."""
    index = _index(TermGroup(canonical="non-compliance", synonyms=["non compliance"]))
    hits = match_text(index, "non-compliance")
    assert [(h.start, h.end, h.match_type) for h in hits] == [(0, 14, MatchType.EXACT)]


def test_an_unrelated_hyphen_elsewhere_never_changes_a_hit():
    """The result is a property of the tokens that match, not of what else the paragraph
    contains: the split reading runs for hyphenated forms either way."""
    index = _index(
        TermGroup(canonical="non-compliance"),
        TermGroup(canonical="right of subrogation"),
    )
    plain = _spans(index, "a non compliance and a right of subrogation here")
    with_hyphen = _spans(index, "a non compliance and a right of subrogation here x-y")
    assert [g for g, _, _ in plain] == [g for g, _, _ in with_hyphen] == [
        "non-compliance",
        "right of subrogation",
    ]
    assert [(s, e) for _, s, e in plain] == [(s, e) for _, s, e in with_hyphen]


def test_a_hyphen_never_makes_a_fused_seam_a_hit():
    """``right ofsubrogation`` is not ``right of subrogation`` under any reading."""
    index = _index(TermGroup(canonical="right of subrogation"))
    assert match_text(index, "right ofsubrogation") == []
    assert match_text(index, "right ofsubrogation x-y") == []
    assert match_text(index, "right of-subrogation") != []  # a hyphen IS a separator there


def test_hyphenated_hits_address_the_source_characters():
    index = _index(TermGroup(canonical="hold harmless"))
    text = "see hold-harmless here"
    (hit,) = match_text(index, text)
    assert text[hit.start : hit.end] == "hold-harmless"


def test_split_forms_are_compiled_and_the_index_is_order_independent():
    one = _index(TermGroup(canonical="hold-harmless"), TermGroup(canonical="aggregate limit"))
    two = _index(TermGroup(canonical="aggregate limit"), TermGroup(canonical="hold-harmless"))
    assert one == two
    assert {form.tokens for form in one.split_forms} == {
        ("hold", "harmless"),
        ("aggregate", "limit"),
    }
    assert {form.tokens for form in one.forms} == {("holdharmless",), ("aggregate", "limit")}


def test_the_matcher_version_records_the_matcher_and_the_nested_stemmer():
    """``matcher_version`` is the reproducibility key's matcher half (D10): the matcher's own
    algorithm -- normalization never changed without it -- and, nested inside it, the
    vendored stemmer's own identity, so a new ``stem.py`` invalidates every key that stemmed
    anything even when no matcher line changed. 6c's change to what a hit addresses is the
    matcher's algorithm, so it counts here even though the old hits were unchanged."""
    from wordextract.stem import STEM_ALGORITHM_VERSION
    from wordextract.versions import MATCHER_VERSION

    assert MATCHER_VERSION == "4+" + STEM_ALGORITHM_VERSION
    assert MATCHER_VERSION == "4+porter-2"


# --- the first-token index finds exactly what the linear scan found ---------------------


def _reference_match_text(index: TermIndex, text: str) -> list[TermMatch]:
    """The pre-index algorithm, kept here as the reference: every form at every position
    under the hyphens-deleted reading. Equal to the index's answer on hyphen-free text."""
    from wordextract.terms import _RANK, _resolve, _segments, _token_offsets

    matches: list[TermMatch] = []
    for base, segment in _segments(text):
        tokens = _token_offsets(segment)
        words = [token for token, _, _ in tokens]
        by_group: dict[str, list[TermMatch]] = {}
        for form in index.forms:
            width = len(form.tokens)
            for position in range(len(words) - width + 1):
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
        for group in sorted(by_group):
            matches.extend(_resolve(by_group[group]))
    return sorted(matches, key=lambda m: (m.start, m.end, m.group, _RANK[m.match_type]))


def _corpus_registry() -> TermIndex:
    return _index(
        TermGroup(canonical="subrogation", synonyms=["right of subrogation", "right of recovery"]),
        TermGroup(canonical="aggregate limit", synonyms=["policy aggregate", "aggregate"]),
        TermGroup(canonical="named insured", synonyms=["additional insured", "insured"]),
        TermGroup(canonical="limit of liability", synonyms=["limit", "liability"]),
    )


def test_the_index_equals_the_linear_scan_on_every_fixture_view():
    from wordextract import opc
    from wordextract.model import View
    from wordextract.views import project
    from wordextract.walker import union_streams

    index = _corpus_registry()
    checked = 0
    for path in sorted(Path(__file__).resolve().parents[1].joinpath("fixtures").rglob("*.docx")):
        for stream in union_streams(opc.Package(path)):
            for view in (View.ACCEPTED, View.ORIGINAL):
                text = project(stream, view).text
                if "-" in text:
                    continue  # hyphenated text is option C's new ground, not the old rule's
                assert match_text(index, text) == _reference_match_text(index, text), (
                    path.name,
                    view,
                )
                checked += 1
    assert checked > 10


def test_the_index_equals_the_linear_scan_on_random_hyphen_free_text():
    import random

    rng = random.Random(7)
    words = "the insured limit aggregate of right subrogation recovery policy liability additional".split()
    index = _corpus_registry()
    for _ in range(300):
        text = "\n".join(
            " ".join(rng.choice(words) for _ in range(rng.randint(1, 12)))
            for _ in range(rng.randint(1, 4))
        )
        assert match_text(index, text) == _reference_match_text(index, text), text


def test_forms_sharing_a_first_token_are_all_tried():
    index = _index(
        TermGroup(canonical="limit"),
        TermGroup(canonical="limit of liability"),
        TermGroup(canonical="limit of indemnity"),
    )
    assert [g for g, _, _ in _spans(index, "limit of liability")] == ["limit", "limit of liability"]
    assert [g for g, _, _ in _spans(index, "limit of indemnity")] == ["limit", "limit of indemnity"]
