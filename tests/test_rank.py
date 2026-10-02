"""Turn 3 validation: retrieval is the union of three sources under a fixed tiering (D12).

The build spec's six tests, over the committed fixtures and one synthetic package: the
same query gives the same order every time; a chunk hit by a term and by text is returned
once with both sources; a comment hit dedupes into its anchor chunk; the two hits of a
move collapse to one result with two locations; an ambiguous term list is an error naming
the candidates; and ordering follows the tiering -- not document order -- on a hand-built
mixed case.
"""
from __future__ import annotations

import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from wordextract import opc
from wordextract.chunker import DEFAULT_PARAMS, chunk
from wordextract.model import TermGroup, TermHit, View
from wordextract.rank import DocumentInput, SummaryRef, pick_term_list, rank
from wordextract.store import body_part_id
from wordextract.terms import TermRegistry, compile_registry, match_document
from wordextract.walker import walk_document

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"'
)
RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"

#: One paragraph, the comment anchored inside it: the comment body carries the query, the
#: anchor is the only chunk there is.
BODY = (
    '<w:p><w:commentRangeStart w:id="1"/><w:r><w:t>text</w:t></w:r>'
    '<w:commentRangeEnd w:id="1"/><w:r><w:commentReference w:id="1"/></w:r></w:p>'
)
PLAIN_COMMENT = (
    '<w:comment w:id="1" w:author="A" w:date="d" w:initials="X">'
    '<w:p w14:paraId="0000000A"><w:r><w:t>This is a right of termination</w:t></w:r></w:p>'
    "</w:comment>"
)


def _registry(*groups: TermGroup) -> TermRegistry:
    return TermRegistry(groups=list(groups))


def _document(name: str):
    parsed = walk_document(opc.Package(FIXTURES / name))
    chunks = tuple(chunk(parsed, body_part_id(parsed), params=DEFAULT_PARAMS))
    return parsed, chunks


def _package(tmp_path: Path, comment_xml: str) -> opc.Package:
    members = {
        "[Content_Types].xml": (
            f'<Types xmlns="{TYPES}"><Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/></Types>'
        ),
        "_rels/.rels": (
            f'<Relationships xmlns="{RELS}"><Relationship Id="rId1" '
            f'Type="{opc.RT_OFFICE_DOCUMENT}" Target="word/document.xml"/></Relationships>'
        ),
        "word/_rels/document.xml.rels": (
            f'<Relationships xmlns="{RELS}"><Relationship Id="rId2" '
            f'Type="{opc.RT_COMMENTS}" Target="comments.xml"/></Relationships>'
        ),
        "word/document.xml": f"<w:document {NS}><w:body>{BODY}</w:body></w:document>",
        "word/comments.xml": f"<w:comments {NS}>{comment_xml}</w:comments>",
    }
    path = tmp_path / "comment.docx"
    with zipfile.ZipFile(path, "w") as archive:
        for member, data in members.items():
            archive.writestr(member, data)
    return opc.Package(path)


def test_the_same_query_gives_the_same_results_in_the_same_order_every_time():
    """The sort is total (tier, document id, node order, chunk id), so neither the order
    the documents arrive in nor a second run may move a single result."""
    ledger_parsed, ledger_chunks = _document("ledger_summary.docx")
    edge_parsed, edge_chunks = _document("edge_cases.docx")
    registry = _registry(TermGroup(canonical="limits of liability"))
    hit = TermHit(group="limits of liability", node_id=ledger_chunks[2].node_ids[0])
    summaries = (
        SummaryRef(chunk_id=ledger_chunks[1].id, text="The limits of liability schedule applies."),
    )
    ledger = DocumentInput(
        document_id="ledger",
        parsed=ledger_parsed,
        chunks=ledger_chunks,
        hits=(hit,),
        summaries=summaries,
    )
    edge = DocumentInput(document_id="edge", parsed=edge_parsed, chunks=edge_chunks)

    first = rank("limits of liability", [ledger, edge], registry=registry)
    again = rank("limits of liability", [ledger, edge], registry=registry)
    reversed_in = rank("limits of liability", [edge, ledger], registry=registry)
    assert first == again == reversed_in
    assert [result.sources for result in first] == [("term",), ("text",), ("summary",)]


def test_a_chunk_hit_by_a_term_and_by_text_is_returned_once_with_both_sources():
    """``ledger_summary.docx`` names the group in its own words, so the term tier and the
    text tier both find the one chunk -- one result, sources merged, views recorded."""
    parsed, chunks = _document("ledger_summary.docx")
    registry = _registry(
        TermGroup(
            canonical="termination",
            synonyms=["right of termination", "right of transfer"],
        )
    )
    hits = match_document(compile_registry(registry), parsed)
    document = DocumentInput(
        document_id="ledger", parsed=parsed, chunks=chunks, hits=tuple(hits)
    )

    results = rank("termination", [document], registry=registry)
    (result,) = results
    assert result.sources == ("term", "text")
    assert result.term_group == "termination"
    assert result.views == (View.ACCEPTED, View.ORIGINAL)
    assert result.chunk_id == chunks[1].id
    assert len(result.locations) == 2  # the canonical hit and the synonym's


def test_a_comment_hit_dedupes_into_its_anchor_chunk(tmp_path):
    """A comment's words address no document node, so the hit cites ``comment:<id>`` and
    dedupes into the chunk the comment is anchored in -- the body's only chunk."""
    parsed = walk_document(_package(tmp_path, PLAIN_COMMENT))
    chunks = tuple(chunk(parsed, body_part_id(parsed), params=DEFAULT_PARAMS))
    registry = _registry(
        TermGroup(canonical="termination", synonyms=["right of termination"])
    )
    hits = match_document(compile_registry(registry), parsed)
    document = DocumentInput(
        document_id="comment", parsed=parsed, chunks=chunks, hits=tuple(hits)
    )

    results = rank("termination", [document], registry=registry)
    (result,) = results
    assert result.chunk_id == chunks[0].id
    assert result.sources == ("term",)
    assert [location.node_id for location in result.locations] == ["comment:0000000A"]


def test_move_hits_collapse_to_one_result_with_two_locations():
    """``model/move.docx`` holds one move (``mg1``) whose two ends sit in the one body
    chunk: the fold makes them one result, and the result keeps both addresses."""
    parsed, chunks = _document("model/move.docx")
    registry = _registry(TermGroup(canonical="Section 4"))
    hits = match_document(compile_registry(registry), parsed)
    assert [hit.move_group_id for hit in hits] == ["mg1", "mg1"]
    document = DocumentInput(
        document_id="move", parsed=parsed, chunks=chunks, hits=tuple(hits)
    )

    results = rank("Section 4", [document], registry=registry)
    (result,) = results
    assert result.sources == ("term", "text")  # the chunk's own text says it too
    assert result.term_group == "Section 4"
    assert len(result.locations) == 2
    assert {location.node_id for location in result.locations} == {
        hit.node_id for hit in hits
    }


def test_a_move_split_across_chunks_is_still_one_result_with_both_addresses():
    """With a small size cap the two ends of ``mg1`` land in different chunks. The query
    resolves to the group through its canonical form, while the words in the document are
    the synonym -- so the text tier finds nothing and the term tier is the only way to either
    end. One result, at the kept end's chunk, citing both ends."""
    parsed = walk_document(opc.Package(FIXTURES / "model/move.docx"))
    chunks = tuple(
        chunk(parsed, body_part_id(parsed), params=replace(DEFAULT_PARAMS, size_cap=8))
    )
    assert len(chunks) == 2
    registry = _registry(TermGroup(canonical="clause four", synonyms=["Section 4"]))
    hits = match_document(compile_registry(registry), parsed)
    assert [hit.move_group_id for hit in hits] == ["mg1", "mg1"]
    document = DocumentInput(
        document_id="move", parsed=parsed, chunks=chunks, hits=tuple(hits)
    )

    (result,) = rank("clause four", [document], registry=registry)

    assert result.sources == ("term",)
    assert result.term_group == "clause four"
    assert {location.node_id for location in result.locations} == {
        hit.node_id for hit in hits
    }
    assert len(result.locations) == 2


def test_a_text_match_never_spans_the_break_between_two_paragraphs():
    """``program_review_v3``: one paragraph ends "...required by written contract." and the
    next begins "A waiver of termination...". Their boundary words are not a phrase."""
    parsed, chunks = _document("program_review_v3.docx")
    document = DocumentInput(document_id="v3", parsed=parsed, chunks=chunks)
    registry = _registry()

    assert rank("contract A", [document], registry=registry) == []
    inside = rank("waiver of termination", [document], registry=registry)
    assert inside and all(result.sources == ("text",) for result in inside)


def test_an_ambiguous_term_list_is_an_error_naming_the_candidates():
    """Zero or several stored lists is never a silent pick: the error names what is
    there and asks for the explicit choice."""
    with pytest.raises(ValueError) as failure:
        pick_term_list(None, ["checklist-2019", "checklist-2021"])
    message = str(failure.value)
    assert "checklist-2019" in message and "checklist-2021" in message
    assert "pass term_list explicitly" in message


def test_ordering_follows_the_tiering_not_document_order_on_a_hand_built_mixed_case():
    """One hand-built case over ``ledger_summary.docx``: a term hit planted in the *last*
    chunk, a text match in the first, a summary match in the middle. The tiering wins --
    term, text, summary -- over document order, and each result carries only its own
    source. No relevance number exists to reorder them."""
    parsed, chunks = _document("ledger_summary.docx")
    registry = _registry(TermGroup(canonical="limits of liability"))
    hit = TermHit(group="limits of liability", node_id=chunks[2].node_ids[0])
    document = DocumentInput(
        document_id="ledger",
        parsed=parsed,
        chunks=chunks,
        hits=(hit,),
        summaries=(
            SummaryRef(
                chunk_id=chunks[1].id, text="The limits of liability schedule applies."
            ),
        ),
    )

    results = rank("limits of liability", [document], registry=registry)
    assert [result.chunk_id for result in results] == [
        chunks[2].id,
        chunks[0].id,
        chunks[1].id,
    ]
    assert [result.sources for result in results] == [("term",), ("text",), ("summary",)]
    assert results[0].term_group == "limits of liability"
    assert results[1].views == (View.ACCEPTED, View.ORIGINAL)
    assert results[2].locations == ()
