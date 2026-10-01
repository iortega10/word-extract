"""Turn 6e: the cross-paragraph report tool -- the permissive matcher and its report.

The golden set is *measured*, not asserted, so a 0-find result there would be vacuous on
its own. These tests build the case the measurement is about -- a term whose words sit on
both sides of a paragraph break -- by hand, so the tool's silence over the fixtures is a
fact about the fixtures and not about a matcher that cannot see across a break. The other
half is the claim the tool makes for every finding: that the strict matcher really missed
it, checked against ``match_document`` here rather than trusted.

The documents are hand-built (streams, nodes, comments) the way ``test_terms`` builds them,
because *where* a term breaks is the thing under test and a package fixture would only make
the assertion a fixture fact.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from wordextract.evals.labels import iter_sidecars
from wordextract.model import (
    Comment,
    ElementarySpan,
    MatchType,
    Node,
    NodeKind,
    ParseResult,
    Span,
    TermGroup,
    UnionStream,
)
from wordextract.terms import (
    TermRegistry,
    compile_registry,
    dedupe_hits,
    match_document,
)
from wordextract.walker import TERMINATOR

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import crossparagraph_report as report  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
REGISTRY = FIXTURES / "terms" / "synthetic.example.json"
DOCUMENT = "officeDocument:0"
COMMENTS_PART = "comments:0"

AGGREGATE = TermGroup(canonical="aggregate limit", synonyms=["policy aggregate"])
SUBROGATION = TermGroup(canonical="subrogation", synonyms=["right of subrogation"])


def _index(*groups: TermGroup):
    return compile_registry(TermRegistry(groups=list(groups)))


def _stream(*pieces: tuple[list[str], str], part_id: str = DOCUMENT) -> UnionStream:
    text = ""
    spans: list[ElementarySpan] = []
    for stack, piece in pieces:
        spans.append(ElementarySpan(len(text), len(text) + len(piece), list(stack)))
        text += piece
    return UnionStream(part_id, text, spans)


def _paragraphs(*texts: str, part_id: str = DOCUMENT) -> UnionStream:
    """One part whose body is ``texts``, one terminated paragraph each."""
    pieces: list[tuple[list[str], str]] = []
    for text in texts:
        pieces += [([], text), ([], TERMINATOR)]
    return _stream(*pieces, part_id=part_id)


def _nodes(*specs: tuple[str, NodeKind, str, int | None, int | None, str | None]) -> list[Node]:
    children: dict[str, list[str]] = {}
    for node_id, _kind, _part_id, _start, _end, parent in specs:
        if parent is not None:
            children.setdefault(parent, []).append(node_id)
    return [
        Node(
            id=node_id,
            kind=kind,
            part_id=part_id,
            source_ref=node_id,
            spans=[] if start is None else [Span(part_id, start, end)],
            child_ids=children.get(node_id, []),
        )
        for node_id, kind, part_id, start, end, _parent in specs
    ]


def _parsed(
    streams: list[UnionStream], nodes: list[Node], comments: list[Comment] | None = None
) -> ParseResult:
    return ParseResult(
        union_streams=streams, nodes=nodes, comments=comments or [], revisions=[]
    )


# --- the permissive matcher ----------------------------------------------------------

def test_flatten_reads_the_boundary_as_a_separator_and_keeps_every_offset():
    text = "annual aggregate" + TERMINATOR + "limit applies"
    flat = report.flatten(text)
    assert flat == "annual aggregate limit applies"
    assert len(flat) == len(text)


def test_a_term_spanning_a_break_is_found_and_the_strict_matcher_really_missed_it():
    stream = _paragraphs("annual aggregate", "limit applies")
    parsed = _parsed(
        [stream],
        _nodes(
            ("p1", NodeKind.PARA, DOCUMENT, 0, 16, None),
            ("p2", NodeKind.PARA, DOCUMENT, 17, 30, None),
        ),
    )
    index = _index(AGGREGATE)
    strict = dedupe_hits(match_document(index, parsed))
    assert strict == []  # neither paragraph holds the term on its own

    findings = report.scan_document(index, parsed, strict, "built.docx")

    assert len(findings) == 1
    finding = findings[0]
    assert finding.group == "aggregate limit"
    assert finding.match_type == MatchType.EXACT.value
    assert finding.phrase == TERMINATOR.join(["aggregate", "limit"])
    assert finding.part_id == DOCUMENT
    # The phrase lands in both views (no revision markup separates them): one finding.
    assert finding.views == ("accepted", "original")
    assert finding.missed is True
    assert [(span.start, span.end) for span in finding.spans] == [(7, 22)]


def test_a_term_inside_one_paragraph_is_not_a_cross_paragraph_finding():
    stream = _paragraphs("the aggregate limit", "applies")
    parsed = _parsed(
        [stream],
        _nodes(
            ("p1", NodeKind.PARA, DOCUMENT, 0, 19, None),
            ("p2", NodeKind.PARA, DOCUMENT, 20, 27, None),
        ),
    )
    index = _index(AGGREGATE)
    strict = dedupe_hits(match_document(index, parsed))
    assert [hit.match_type for hit in strict] == [MatchType.EXACT]

    assert report.scan_document(index, parsed, strict, "built.docx") == []


def test_a_synonym_across_a_break_is_reported_even_when_the_group_is_hit_elsewhere():
    stream = _paragraphs("right of", "subrogation now")
    parsed = _parsed(
        [stream],
        _nodes(
            ("p1", NodeKind.PARA, DOCUMENT, 0, 8, None),
            ("p2", NodeKind.PARA, DOCUMENT, 9, 24, None),
        ),
    )
    index = _index(SUBROGATION)
    strict = dedupe_hits(match_document(index, parsed))
    # "subrogation" alone opening the second paragraph is a strict hit ...
    assert [hit.match_type for hit in strict] == [MatchType.EXACT]

    findings = report.scan_document(index, parsed, strict, "built.docx")

    # ... but the strict matcher still missed the longer synonym the break hides, and it
    # is against that longer text -- not against "any hit at all" -- that missed is judged.
    assert len(findings) == 1
    assert findings[0].group == "subrogation"
    assert findings[0].match_type == MatchType.SYNONYM.value
    assert findings[0].missed is True


def test_a_comment_whose_words_span_a_break_is_reported_against_the_comments_part():
    text = "annual aggregate" + TERMINATOR + "limit applies"
    comment = Comment(
        para_id="1A2B3C",
        author="Reviewer",
        initials="R",
        text=text,
        text_span=Span(COMMENTS_PART, 0, len(text)),
    )
    parsed = _parsed([], [], comments=[comment])
    index = _index(AGGREGATE)
    strict = dedupe_hits(match_document(index, parsed))
    assert strict == []

    findings = report.scan_document(index, parsed, strict, "built.docx")

    assert len(findings) == 1
    finding = findings[0]
    assert finding.views == (report.COMMENT_VIEW,)
    assert finding.part_id == COMMENTS_PART
    assert finding.phrase == TERMINATOR.join(["aggregate", "limit"])
    assert [(span.start, span.end) for span in finding.spans] == [(7, 22)]
    assert finding.missed is True


# --- the golden set and the report ---------------------------------------------------

def test_the_golden_set_is_every_sidecar_backed_document():
    documents = report.golden_documents(FIXTURES)

    assert documents
    assert all(document.suffix == ".docx" and document.is_file() for document in documents)
    for sidecar in iter_sidecars(FIXTURES).values():
        if sidecar.path is None:
            continue
        assert (sidecar.path.parent / sidecar.fixture) in documents
    # the sidecar-less samples are measured too: they are the most realistic documents here
    for sample in sorted((FIXTURES / "samples").glob("*.docx")):
        assert sample in documents


def test_the_report_over_the_golden_set_is_deterministic_and_every_find_is_a_real_miss():
    first = report.build_report(FIXTURES, REGISTRY)
    second = report.build_report(FIXTURES, REGISTRY)

    assert first == second
    assert first["matcher_version"].endswith("porter-2")
    assert first["documents"]
    assert first["totals"]["documents"] == len(first["documents"])
    for document in first["documents"]:
        for finding in document["cross_paragraph"]:
            # The claim the tool makes of every finding: the strict matcher missed it, and
            # the phrase it names is one that really crosses a break.
            assert finding["missed"] is True
            assert TERMINATOR in finding["phrase"]
    assert first["promote"] == (first["totals"]["cross_paragraph_finds"] > 0)
    assert "verdict:" in report.format_report(first)


def test_the_cli_emits_json_and_text(capsys):
    assert report.main([str(FIXTURES), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["totals"]["documents"] == len(payload["documents"])

    assert report.main([str(FIXTURES)]) == 0
    assert "cross-paragraph report" in capsys.readouterr().out
