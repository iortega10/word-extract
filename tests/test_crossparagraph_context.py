"""Turn 6e validation: what a cross-paragraph finding does and does not prove.

The first report called every phrase across a break a miss and concluded from a run of 27
tiny synthetic documents (5 strict hits, a synthetic registry) that "matching stays
paragraph-bounded". Three things were missing:

* ``missed`` is true by construction -- a phrase crossing a break can never equal a strict
  hit's spans -- so it cannot say whether the strict matcher already reported the *group*
  (a shorter synonym on one side). ``group_hit_nearby`` does.
* A phrase across two table cells is a layout adjacency, not a split phrase, and a heading
  followed by its body is a third thing. ``boundary`` says which break was crossed.
* A zero from a thin run is not evidence. The verdict says INCONCLUSIVE, with its reasons.
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import crossparagraph_report as report  # noqa: E402

from wordextract import opc  # noqa: E402
from wordextract.model import TermGroup  # noqa: E402
from wordextract.terms import (  # noqa: E402
    TermRegistry,
    compile_registry,
    dedupe_hits,
    match_document,
)
from wordextract.walker import walk_document  # noqa: E402

NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"


def _package(tmp_path: Path, body: str) -> opc.Package:
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
        "word/_rels/document.xml.rels": f'<Relationships xmlns="{RELS}"/>',
        "word/document.xml": f"<w:document {NS}><w:body>{body}</w:body></w:document>",
    }
    path = tmp_path / "doc.docx"
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return opc.Package(path)


def _p(text: str, style: str | None = None) -> str:
    props = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f'<w:p>{props}<w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


def _cell(text: str) -> str:
    return f"<w:tc>{_p(text)}</w:tc>"


INDEX = compile_registry(
    TermRegistry(
        groups=[
            TermGroup(canonical="waiver of subrogation"),
            TermGroup(canonical="named storm deductible"),
            TermGroup(canonical="aggregate limit", synonyms=["aggregate"]),
        ]
    )
)


def _findings(tmp_path: Path, body: str):
    parsed = walk_document(_package(tmp_path, body))
    strict = dedupe_hits(match_document(INDEX, parsed))
    return report.scan_document(INDEX, parsed, strict, "doc"), strict


def test_a_phrase_split_across_two_ordinary_paragraphs_is_a_paragraph_boundary(tmp_path):
    (finding,), _ = _findings(
        tmp_path, _p("The carrier grants a waiver") + _p("of subrogation where required.")
    )
    assert finding.group == "waiver of subrogation"
    assert finding.boundary == "paragraph"
    assert finding.group_hit_nearby is False


def test_a_phrase_across_two_table_cells_is_a_cell_boundary_not_a_split_phrase(tmp_path):
    body = "<w:tbl><w:tr>" + _cell("Named storm") + _cell("deductible applies") + "</w:tr></w:tbl>"
    (finding,), _ = _findings(tmp_path, body)
    assert finding.boundary == "cell"


def test_a_phrase_within_one_cell_across_two_paragraphs_is_not_a_cell_adjacency(tmp_path):
    cell = "<w:tc>" + _p("Named storm") + _p("deductible applies") + "</w:tc>"
    (finding,), _ = _findings(tmp_path, "<w:tbl><w:tr>" + cell + "</w:tr></w:tbl>")
    assert finding.boundary == "paragraph"  # same cell: an ordinary paragraph break inside it


def test_a_heading_and_the_text_under_it_is_a_heading_boundary(tmp_path):
    (finding,), _ = _findings(
        tmp_path, _p("Named Storm", style="Heading1") + _p("Deductible: 5% of TIV.")
    )
    assert finding.boundary == "heading"


def test_a_group_the_strict_matcher_already_reported_there_is_flagged(tmp_path):
    """Strict matched the shorter synonym ``aggregate``; what it missed is ``aggregate limit``."""
    (finding,), strict = _findings(tmp_path, _p("The aggregate") + _p("limit is $2M."))
    assert [hit.group for hit in strict] == ["aggregate limit"]  # the synonym, hit in-paragraph
    assert finding.group == "aggregate limit"
    assert finding.missed is True  # still true by construction ...
    assert finding.group_hit_nearby is True  # ... which is why this field exists


def test_the_new_fields_are_in_the_json_form(tmp_path):
    (finding,), _ = _findings(tmp_path, _p("a waiver") + _p("of subrogation"))
    payload = finding.to_dict()
    assert payload["boundary"] == "paragraph" and payload["group_hit_nearby"] is False


# --- the verdict states how strong its evidence is ----------------------------------------


def test_a_zero_over_the_synthetic_registry_is_inconclusive_and_says_why():
    built = report.build_report(ROOT / "fixtures", ROOT / "fixtures" / "terms" / "synthetic.example.json")
    assert built["totals"]["cross_paragraph_finds"] == 0
    assert built["evidence"]["adequate"] is False
    limits = " ".join(built["evidence"]["limits"])
    assert "synthetic" in limits and "no real document" in limits
    text = report.format_report(built)
    assert "INCONCLUSIVE" in text
    assert "stays paragraph-bounded" not in text


def test_the_golden_set_includes_the_machine_local_documents(tmp_path):
    (tmp_path / "real").mkdir()
    local = tmp_path / "real" / "mine.docx"
    local.write_bytes((ROOT / "fixtures" / "program_review_v3.docx").read_bytes())
    assert local in report.golden_documents(tmp_path)


def test_only_cell_adjacencies_are_not_evidence_for_promotion():
    built = {
        "matcher_version": "x",
        "term_list_hash": "0" * 16,
        "registry": "r",
        "documents": [],
        "totals": {
            "documents": 1,
            "real_documents": 1,
            "strict_hits": 99,
            "cross_paragraph_finds": 2,
            "by_boundary": {"cell": 2, "heading": 0, "paragraph": 0},
        },
        "evidence": {"adequate": True, "limits": []},
        "promote": False,
    }
    text = report.format_report(built)
    assert "only table-cell adjacencies" in text and "promote cross-paragraph matching" not in text.replace(
        "not evidence for promoting cross-paragraph matching", ""
    )


def test_paragraph_or_heading_finds_are_the_promotion_signal(tmp_path):
    built = report.build_report(ROOT / "fixtures", ROOT / "fixtures" / "terms" / "synthetic.example.json")
    assert built["promote"] == (
        built["totals"]["by_boundary"]["paragraph"] + built["totals"]["by_boundary"]["heading"] > 0
    )
