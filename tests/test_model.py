"""Every frozen contract round-trips through the strict codec."""
from __future__ import annotations

import json

import pytest

from docextract_core import CodecError, from_json, to_json
from wordextract import (
    ArtifactCache,
    CacheStatus,
    Chunk,
    Comment,
    ElementarySpan,
    HashedInputs,
    HeadingDecision,
    HeadingDetection,
    IdStability,
    LabelsProvenance,
    LocationKind,
    MatchType,
    Node,
    NodeKind,
    ParseResult,
    Revision,
    RevisionKind,
    RunRecord,
    Section,
    Span,
    TermGroup,
    TermHit,
    ThreadingStatus,
    UnionStream,
    View,
    ViewSpan,
)

HASHED = HashedInputs(
    source_content_hash="src",
    spec_parser_version="1",
    textmodel_version="1",
    view_id="accepted",
    heading_ruleset_version="1",
    chunker_version="1",
    chunker_params_hash="cp",
    term_list_hash="tl",
    matcher_version="1",
    summarizer_version="1",
    model_id="m",
    model_params_hash="mp",
    prompt_hash="pp",
    output_schema_version="1",
)

SAMPLES = [
    Span(part_id="word/document.xml", start=0, end=42),
    Node(
        id="n1",
        kind=NodeKind.HEADING,
        part_id="word/document.xml",
        source_ref="s2/p3",
        spans=[Span("word/document.xml", 0, 10)],
        style="Heading1",
        level=1,
        numbering_label="2.1(a)",
        child_ids=["n2", "n3"],
        id_stability=IdStability.PARAID,
    ),
    Revision(
        id="r1",
        kind=RevisionKind.MOVE_TO,
        author="R. Alvarez",
        date=None,
        move_group_id="mg1",
        ancestors=["r0", "r9"],
        labels_provenance=LabelsProvenance.SPEC,
    ),
    Comment(
        para_id="1A2B3C4D",
        author="M. Chen",
        initials="MC",
        id_stability=IdStability.PARAID,
        resolved_identity="mchen",
        date="2026-09-01T10:00:00Z",
        anchor=Span("word/document.xml", 5, 20),
        anchor_text="transfer",
        parent_id="0F0E0D0C",
        threading_status=ThreadingStatus.VERIFIED,
        resolved=False,
        labels_provenance=LabelsProvenance.HUMAN,
    ),
    Chunk(
        id="c1",
        content_hash="ch1",
        context_hash="ctx",
        occurrence_index=1,
        section_path=["Program Review", "Coverage Terms"],
        node_ids=["n1", "n2"],
        view_id="accepted",
        textmodel_version="1",
        size=128,
        heading_detection=HeadingDetection.DEGRADED,
        fired_rules=["style", "outlineLvl"],
        disputed_rules=["bold"],
    ),
    TermGroup(
        canonical="termination",
        synonyms=["waiver of transfer", "waiver of termination"],
        stemming="porter",
        rules={"case_sensitive": False},
        tags={"category": "coverage"},
    ),
    TermHit(
        group="termination",
        present_in={View.ACCEPTED, View.ORIGINAL},
        spans=[Span("word/document.xml", 30, 46)],
        view_spans=[
            ViewSpan(view=View.ACCEPTED, start=30, end=46),
            ViewSpan(view=View.ORIGINAL, start=30, end=44),
        ],
        node_id="n2",
        location=LocationKind.TABLE_CELL,
        match_type=MatchType.SYNONYM,
        move_group_id="mg1",
        context_node_id="n1",
    ),
    RunRecord(
        run_id="run1",
        hashed_inputs=HASHED,
        recorded_inputs={"core_git_rev": "deadbeef", "doc_git_rev": "cafef00d"},
        artifact_cache=[
            ArtifactCache(artifact_id="c1", status=CacheStatus.HIT, recomputed_key="k"),
            ArtifactCache(artifact_id="c2", status=CacheStatus.MISS, recomputed_key="k2"),
        ],
    ),
    Span(part_id="word/document.xml", start=10, end=16, fragment_id="n1/0"),
    Node(
        id="n4",
        kind=NodeKind.PARA,
        part_id="word/document.xml",
        source_ref="s3/p1",
        spans=[Span("word/document.xml", 10, 16, "n1/0")],
        id_stability=IdStability.CONTENT_HASH,
        occurrence_index=2,
        host_node_id="n1",
        section_path=["Program Review", "Coverage Terms"],
    ),
    Comment(
        para_id="hash:9f2c1b",
        author="L. Duarte",
        initials="LD",
        id_stability=IdStability.CONTENT_HASH,
    ),
    UnionStream(
        part_id="word/document.xml",
        text="right of transfertermination\n",
        spans=[
            ElementarySpan(start=0, end=9),
            ElementarySpan(start=9, end=17, stack=["r1"]),
            ElementarySpan(start=17, end=28, stack=["r2"]),
            ElementarySpan(start=28, end=29),
        ],
    ),
    HeadingDecision(
        node_id="n1",
        fired_rules=["style", "outlineLvl"],
        winner="style",
        disputed_rules=["outlineLvl"],
    ),
    ParseResult(
        union_streams=[
            UnionStream(
                part_id="word/document.xml",
                text="Coverage applies worldwide.\n",
                spans=[ElementarySpan(start=0, end=28)],
            )
        ],
        nodes=[
            Node(
                id="n1",
                kind=NodeKind.PARA,
                part_id="word/document.xml",
                source_ref="s1/p1",
                spans=[Span("word/document.xml", 0, 27)],
            )
        ],
        sections=[
            Section(
                heading_id="n1",
                title="Coverage Terms",
                level=2,
                node_ids=["n2"],
                children=[Section(heading_id="n3", title="", level=3, node_ids=["n4"])],
            )
        ],
        revisions=[Revision(id="r1", kind=RevisionKind.DEL, author="A. Ito")],
        comments=[
            Comment(
                para_id="0000001A",
                author="D. Okafor",
                initials="DO",
                anchor=Span("word/document.xml", 17, 38),
                anchor_text="excluded in all cases",
            )
        ],
        heading_decisions=[HeadingDecision(node_id="n1")],
        heading_detection=HeadingDetection.DEGRADED,
        known_gaps=["paragraph_mark_revision", "textbox"],
    ),
    Section(heading_id="n1", title="Coverage Terms", level=2, node_ids=["n2", "n4"]),
]


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda s: type(s).__name__)
def test_contract_round_trips_through_strict_codec(sample):
    assert from_json(type(sample), to_json(sample)) == sample


def test_fuzzy_is_not_a_valid_match_type():
    with pytest.raises(ValueError):
        MatchType("fuzzy")
    assert {m.value for m in MatchType} == {"exact", "synonym", "stem"}


def test_labels_provenance_values():
    assert {p.value for p in LabelsProvenance} == {"human", "generator", "spec"}


def test_explicit_none_dates_survive_round_trip():
    revision = from_json(Revision, to_json(SAMPLES[2]))
    assert revision.date is None
    assert revision.ancestors == ["r0", "r9"]


def test_location_kinds_cover_every_hit_location():
    assert {k.value for k in LocationKind} == {
        "body",
        "table_cell",
        "header",
        "footer",
        "footnote",
        "endnote",
        "textbox",
        "comment",
    }


def test_paragraph_terminator_is_an_elementary_span_with_an_empty_stack():
    stream = next(s for s in SAMPLES if isinstance(s, UnionStream))
    assert [s.start for s in stream.spans] == sorted(s.start for s in stream.spans)
    assert all(a.end == b.start for a, b in zip(stream.spans, stream.spans[1:])), "spans must tile"
    assert stream.spans[0].start == 0 and stream.spans[-1].end == len(stream.text)
    terminator = stream.spans[-1]
    assert terminator.stack == [] and terminator.end - terminator.start == 1
    assert stream.text[terminator.start : terminator.end] == "\n"


def test_term_hit_view_spans_are_the_views_the_hit_is_present_in():
    hit = next(s for s in SAMPLES if isinstance(s, TermHit))
    assert {vs.view for vs in hit.view_spans} == hit.present_in
    assert [vs.view.value for vs in hit.view_spans] == sorted(vs.view.value for vs in hit.view_spans)
    assert all(vs.start < vs.end for vs in hit.view_spans)


def test_a_pre_half_blob_with_the_removed_view_span_key_is_rejected():
    """Pre-0.5 blobs carried one ``view_span``. Version 1 is now rejected outright;
    at the current version the removed key is still rejected by strict decoding, and
    lenient decoding would lose the hit's views, which is why strict is the default."""
    hit = next(s for s in SAMPLES if isinstance(s, TermHit))
    with pytest.raises(CodecError, match="older"):
        from_json(TermHit, to_json(hit, schema_version="1"))
    blob = json.loads(to_json(hit))
    blob["record"]["view_span"] = blob["record"].pop("view_spans")[0]
    with pytest.raises(CodecError):
        from_json(TermHit, json.dumps(blob))


def test_term_hit_rejects_present_in_that_disagrees_with_view_spans():
    with pytest.raises(ValueError, match="disagrees"):
        TermHit(group="g", present_in={View.ACCEPTED}, view_spans=[ViewSpan(View.ORIGINAL, 0, 3)])
    with pytest.raises(ValueError, match="disagrees"):
        TermHit(group="g", present_in={View.ACCEPTED, View.ORIGINAL}, view_spans=[ViewSpan(View.ACCEPTED, 0, 3)])


def test_term_hit_requires_view_spans_sorted_and_unique_per_view():
    with pytest.raises(ValueError, match="sorted"):
        TermHit(
            group="g",
            present_in={View.ACCEPTED, View.ORIGINAL},
            view_spans=[ViewSpan(View.ORIGINAL, 0, 3), ViewSpan(View.ACCEPTED, 0, 3)],
        )
    with pytest.raises(ValueError, match="one entry per view"):
        TermHit(
            group="g",
            present_in={View.ACCEPTED},
            view_spans=[ViewSpan(View.ACCEPTED, 0, 3), ViewSpan(View.ACCEPTED, 5, 8)],
        )


def test_comment_hit_has_no_views():
    """A comment hit resolves via the Comment record: no present_in, no view_spans."""
    hit = TermHit(group="g", location=LocationKind.COMMENT, node_id="comment:x")
    assert hit.present_in == set() and hit.view_spans == []


def test_decoding_a_disagreeing_hit_raises():
    hit = next(s for s in SAMPLES if isinstance(s, TermHit))
    blob = json.loads(to_json(hit))
    blob["record"]["present_in"] = ["superseded"]
    with pytest.raises(ValueError, match="disagrees"):
        from_json(TermHit, json.dumps(blob))


def test_a_schema_two_node_blob_without_section_path_is_rejected_not_defaulted():
    """Node.section_path and ParseResult.sections changed the contract (schema 3): a
    schema-2 blob would otherwise decode with the new fields silently empty."""
    import json

    node = Node(id="n", kind=NodeKind.PARA, part_id="p", source_ref="p#0")
    blob = json.loads(to_json(node))
    blob["schema_version"] = "2"
    del blob["record"]["section_path"]
    with pytest.raises(CodecError, match="older"):
        from_json(Node, json.dumps(blob))
