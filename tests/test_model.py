"""Every frozen contract round-trips through the strict codec."""
from __future__ import annotations

import pytest

from docextract_core import from_json, to_json
from wordextract import (
    ArtifactCache,
    CacheStatus,
    Chunk,
    Comment,
    HashedInputs,
    HeadingDetection,
    IdStability,
    LabelsProvenance,
    LocationKind,
    MatchType,
    Node,
    NodeKind,
    Revision,
    RevisionKind,
    RunRecord,
    Span,
    TermGroup,
    TermHit,
    ThreadingStatus,
    View,
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
        resolved_identity="mchen",
        date="2026-09-01T10:00:00Z",
        anchor=Span("word/document.xml", 5, 20),
        anchor_text="recovery",
        parent_id="0F0E0D0C",
        threading_status=ThreadingStatus.VERIFIED,
        resolved=False,
        labels_provenance=LabelsProvenance.HUMAN,
    ),
    Chunk(
        id="c1",
        context_hash="ctx",
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
        canonical="subrogation",
        synonyms=["waiver of recovery", "waiver of subrogation"],
        stemming="porter",
        rules={"case_sensitive": False},
        tags={"category": "coverage"},
    ),
    TermHit(
        group="subrogation",
        present_in={View.ACCEPTED, View.ORIGINAL},
        spans=[Span("word/document.xml", 30, 46)],
        view_span=Span("word/document.xml", 30, 46),
        node_id="n2",
        location=LocationKind.TABLE_CELL,
        match_type=MatchType.SYNONYM,
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
