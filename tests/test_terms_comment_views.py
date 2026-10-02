"""Turn 6c validation: a comment body is matched through the views, never the raw union.

A comment body is union text like any part's. A tracked change inside it holds the deleted
*and* the inserted words, so the union reads ``right of transfertermination``: an adjacency no
reading of the comment asserts. The first 6c matched ``Comment.text`` (the union) directly,
and reported a hit on the fused ``transfertermination``. These tests pin the fix: the comment's
range is matched in each view's text, the hit's spans skip the elided text, and the hit stays
view-less.

The offsets are hand-typed from the comment's text ``This is a right of transfertermination``:
``This is a `` is 10 characters, ``right of `` 9 more, ``transfer`` 8 more, ``termination`` 11 more.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

from docextract_core import from_json, to_json

from wordextract import opc
from wordextract.model import Comment, LocationKind, Span, TermGroup
from wordextract.terms import TermRegistry, compile_registry, match_comments, match_document
from wordextract.walker import walk_document

NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"'
)
RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"

BODY = (
    '<w:p><w:commentRangeStart w:id="1"/><w:r><w:t>text</w:t></w:r><w:commentRangeEnd w:id="1"/>'
    '<w:r><w:commentReference w:id="1"/></w:r></w:p>'
)
TRACKED_COMMENT = (
    '<w:comment w:id="1" w:author="A" w:date="d" w:initials="X"><w:p w14:paraId="0000000A">'
    '<w:r><w:t xml:space="preserve">This is a right of </w:t></w:r>'
    '<w:del w:id="9" w:author="A" w:date="d"><w:r><w:delText>transfer</w:delText></w:r></w:del>'
    '<w:ins w:id="10" w:author="A" w:date="d"><w:r><w:t>termination</w:t></w:r></w:ins>'
    "</w:p></w:comment>"
)
PLAIN_COMMENT = (
    '<w:comment w:id="1" w:author="A" w:date="d" w:initials="X"><w:p w14:paraId="0000000A">'
    "<w:r><w:t>This is a right of termination</w:t></w:r></w:p></w:comment>"
)


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
        for name, data in members.items():
            archive.writestr(name, data)
    return opc.Package(path)


def _index():
    return compile_registry(
        TermRegistry(
            groups=[
                TermGroup(
                    canonical="termination",
                    synonyms=["right of termination", "right of transfer"],
                ),
                TermGroup(canonical="transfertermination"),
            ]
        )
    )


def test_the_comment_text_is_the_union_which_is_why_it_cannot_be_matched_directly(tmp_path):
    parsed = walk_document(_package(tmp_path, TRACKED_COMMENT))
    assert parsed.comments[0].text == "This is a right of transfertermination"


def test_a_tracked_comment_never_matches_the_fused_union_adjacency(tmp_path):
    parsed = walk_document(_package(tmp_path, TRACKED_COMMENT))
    groups = [hit.group for hit in match_comments(_index(), parsed)]
    assert "transfertermination" not in groups
    assert groups == ["termination", "termination"]


def test_each_reading_of_a_tracked_comment_is_its_own_hit_with_spans_that_skip_elided_text(tmp_path):
    """Original reads 'right of transfer'; accepted reads 'right of termination'. The accepted
    hit is two spans -- never the deleted 'transfer' between them."""
    parsed = walk_document(_package(tmp_path, TRACKED_COMMENT))
    part = parsed.comments[0].text_span.part_id
    hits = match_comments(_index(), parsed)
    assert sorted(hit.spans[0].start for hit in hits) == [10, 10]
    by_spans = {tuple(hit.spans): hit for hit in hits}
    assert by_spans[(Span(part, 10, 27),)].match_type.value == "synonym"  # right of transfer
    assert by_spans[(Span(part, 10, 19), Span(part, 27, 38))].match_type.value == "synonym"


def test_a_tracked_comment_hit_is_still_view_less_and_reaches_its_comment(tmp_path):
    parsed = walk_document(_package(tmp_path, TRACKED_COMMENT))
    hits = match_comments(_index(), parsed)
    assert hits
    for hit in hits:
        assert hit.location is LocationKind.COMMENT
        assert hit.node_id == "comment:0000000A"
        assert hit.present_in == set() and hit.view_spans == []


def test_a_comment_without_revisions_is_one_hit_over_one_contiguous_span(tmp_path):
    """No revisions: both readings are the same text, so they fold into one hit."""
    parsed = walk_document(_package(tmp_path, PLAIN_COMMENT))
    part = parsed.comments[0].text_span.part_id
    (hit,) = match_comments(_index(), parsed)
    assert hit.spans == [Span(part, 10, 30)]  # "right of termination": 9 + 11 = 20 characters
    assert match_document(_index(), parsed)[-1] == hit


def test_a_schema_four_comment_blob_without_text_span_is_rejected_not_defaulted():
    """Comment.text_span changed the contract (schema 5): an older blob would otherwise
    decode with the new field silently empty."""
    import json

    comment = Comment(para_id="p", author="a", initials="x")
    blob = json.loads(to_json(comment))
    blob["schema_version"] = "4"
    del blob["record"]["text_span"]
    try:
        from_json(Comment, json.dumps(blob))
    except Exception as exc:  # CodecError
        assert "older" in str(exc)
    else:
        raise AssertionError("a schema-4 blob must be rejected")
