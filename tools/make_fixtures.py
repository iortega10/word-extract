"""Generate synthetic insurance-flavored .docx fixtures exercising the hard cases:
comments (with replies + ranges spanning runs), tracked changes, merged-cell tables,
multi-level numbering, headers/footers, content controls, and near-synonym term variants.

Also emits one ``<name>.expected.json`` sidecar per fixture (generator ground truth:
comment ranges/authors, revision spans, section outline, table shapes). Sidecars are
read straight from the OOXML with ``zipfile`` + ``lxml`` -- never python-docx, which
drops text inside ``w:ins``.

``program_review_v2.docx`` is the v3 review minus two edits and gets no sidecar of its
own: what separates the two revisions is recorded, hand-typed, in
``program_review_pair.json`` instead (build spec, Phase 2, 0e).
"""
from pathlib import Path
import argparse
import json
import sys
import zipfile
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from lxml import etree

OUT = Path("fixtures")

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W14 = "http://schemas.microsoft.com/office/word/2010/wordml"

FIXTURES = [
    "program_review_v2.docx",
    "program_review_v3.docx",
    "binder_summary.docx",
    "edge_cases.docx",
]

# Regeneration is byte-reproducible: python-docx stamps comment dates and zip members
# with "now", so both are pinned after build.
FIXED_DATE = "2026-01-01T00:00:00Z"
ZIP_WHEN = (2026, 1, 1, 0, 0, 0)

# The v2 / v3 pair's edit set (build spec, Phase 2, 0e). v2 is v3 minus exactly these two
# edits and nothing else -- one body paragraph before a tracked change, one comment before
# it was rewritten -- so the pair isolates them, and `PAIR_SIDECAR` below records which
# chunks that moves. The third string is the control: a paragraph no edit touches.
PAIR_V2_POLLUTION = "excluded in all cases"
PAIR_V3_POLLUTION = "excluded except for hostile-fire release"
PAIR_V2_COMMENT = "Confirm limit against the binder."
PAIR_V3_COMMENT = "Confirm limit vs. the binder - binder says $500k occurrence."
PAIR_UNTOUCHED = (
    "Cancellation requires 30 days notice, 10 days for non-payment. The insured may not "
    "assign the policy without consent."
)

#: The pair's sidecar, typed by hand and written to ``program_review_pair.json`` (build spec,
#: Phase 2, 0e): which chunks the edit set moves and which is the control. Every text in it
#: comes from the constants above, so the sidecar and the documents cannot disagree about
#: *what* the edits are -- only about where they land, which is what it exists to record.
PAIR_SIDECAR = {
    "fixture_pair": ["program_review_v2.docx", "program_review_v3.docx"],
    "labels_provenance": "spec",
    "note": (
        "Hand-typed from the edit set build_endorsement_review defines, never derived from a "
        "diff. program_review_v2.docx is program_review_v3.docx minus exactly the edits under "
        "changed -- one body paragraph before its tracked change, one comment before it was "
        "rewritten -- and everything else is carried over unchanged: every other block and "
        "comment, and the header, the footer and the core properties, the header's banner "
        "still reading v3 included, since the banner is not one of the edits either. A chunk "
        "is named by its 0-based ordinal in the accepted view's chunk list, in document "
        "order, plus its section_path, never by its chunk id, its content_hash or a node id: "
        "the two documents chunk one-for-one -- the same boundary count, the same section "
        "paths, the same ordinals -- while those keys move with the revision that produced "
        "them, so an ordinal and a path are the only names the two documents share. changed "
        "is every chunk whose summary key the edits move: content_hash, and so the chunk id, "
        "which is its hash, for a body edit, or context_hash alone when a comment the chunk "
        "carries is rewritten -- the chunk is the same chunk, re-summarized, which is the "
        "case a summary cache is meant to make cheap. unchanged is the control: a paragraph "
        "no edit touches and no comment starts in, so both hashes are equal in the two "
        "revisions and its summary key must stay a hit."
    ),
    "changed": [
        {
            "chunk": 0,
            "change": "comment-text",
            "comment": {"id": "0", "v2": PAIR_V2_COMMENT, "v3": PAIR_V3_COMMENT},
            "section_path": [
                "Program Review: Contractors General Liability",
                "1. Coverage Terms",
            ],
            "note": (
                "comment 0 is anchored on this chunk's first block, and a comment's own text "
                "is a context_hash input, so rewriting it re-summarizes the chunk it "
                "annotates. The chunk's own text is untouched, so its content_hash and its id "
                "are the same in the two revisions: this is the pair's cheaper half."
            ),
        },
        {
            "chunk": 2,
            "change": "body-paragraph",
            "paragraph": {
                "v2": "Pollution: " + PAIR_V2_POLLUTION,
                "v3": "Pollution: " + PAIR_V3_POLLUTION,
            },
            "section_path": [
                "Program Review: Contractors General Liability",
                "2. Exclusions",
            ],
            "note": (
                "the pair's tracked change: R. Alvarez deletes one wording and inserts the "
                "other, so v3 holds the paragraph revision-marked and its accepted text is "
                "the inserted one, while v2 holds the deleted one as ordinary text. The "
                "chunk's own text moves, so its content_hash and therefore its id move with "
                "it, and the comment 4 anchored in the chunk is the same comment in both "
                "revisions."
            ),
        },
    ],
    "unchanged": [
        {
            "chunk": 4,
            "change": "none",
            "paragraph": {"v2": PAIR_UNTOUCHED, "v3": PAIR_UNTOUCHED},
            "section_path": [
                "Program Review: Contractors General Liability",
                "4. Notes",
            ],
            "note": (
                "the control: no edit in the pair touches this paragraph and no comment starts "
                "in its chunk, so both hashes are equal in the two revisions and its summary "
                "key has to stay a cache hit."
            ),
        }
    ],
}


def tracked(par, old, new, author, date="2026-09-01T10:00:00Z", rid=(900, 901)):
    """Append a w:del(old) + w:ins(new) pair to a paragraph."""
    d = OxmlElement("w:del"); d.set(qn("w:id"), str(rid[0])); d.set(qn("w:author"), author); d.set(qn("w:date"), date)
    r = OxmlElement("w:r"); t = OxmlElement("w:delText"); t.text = old; t.set(qn("xml:space"), "preserve"); r.append(t); d.append(r)
    i = OxmlElement("w:ins"); i.set(qn("w:id"), str(rid[1])); i.set(qn("w:author"), author); i.set(qn("w:date"), date)
    r2 = OxmlElement("w:r"); t2 = OxmlElement("w:t"); t2.text = new; t2.set(qn("xml:space"), "preserve"); r2.append(t2); i.append(r2)
    par._p.append(d); par._p.append(i)


def numbered(doc, text, level=0):
    p = doc.add_paragraph(text, style="List Number" if level == 0 else "List Number 2")
    return p


def build_endorsement_review(name="program_review_v3.docx", *, revision=True, binder_comment=PAIR_V3_COMMENT):
    """The program review, v3 by default and v2 -- the pair's earlier revision, 0e -- when
    ``revision`` is off: one tracked change and one comment rewritten are the whole of what
    separates them, so both revisions come out of one builder and cannot drift apart."""
    d = Document()
    d.core_properties.author = "Underwriting"; d.core_properties.title = "MGU Program Review - Contractors GL"
    sec = d.sections[0]
    sec.header.paragraphs[0].text = "CONFIDENTIAL - Carrier Program Review v3"
    sec.footer.paragraphs[0].text = "Page footer: ref PR-2026-0142"

    d.add_heading("Program Review: Contractors General Liability", 0)
    d.add_heading("1. Coverage Terms", 1)
    p1 = d.add_paragraph("The policy provides a Limit of Liability of $1,000,000 per occurrence and $2,000,000 aggregate. ")
    r = p1.add_run("Additional insured status is granted by blanket endorsement where required by written contract.")
    p2 = d.add_paragraph("A waiver of subrogation applies in favor of any party where required by written contract. ")
    p2.add_run("Carrier may waive subrogation only with prior written consent.")
    d.add_heading("2. Exclusions", 1)
    numbered(d, "Coverage is excluded for damage arising from professional services.")
    numbered(d, "This policy does not apply to bodily injury to employees of the insured (employer's liability carve-out).")
    numbered(d, "Residential new-construction is not covered above three stories.", level=1)
    p3 = d.add_paragraph("Pollution: ")
    if revision:
        tracked(p3, PAIR_V2_POLLUTION, PAIR_V3_POLLUTION, "R. Alvarez")
    else:
        p3.add_run(PAIR_V2_POLLUTION)

    d.add_heading("3. Fee and Rate Schedule", 1)
    t = d.add_table(rows=4, cols=3); t.style = "Table Grid"
    hdr = t.rows[0].cells
    hdr[0].text, hdr[1].text, hdr[2].text = "Class", "Rate", "Minimum Premium"
    rows = [("Carpentry", "12.50", "2,500"), ("Roofing", "28.00", "5,000")]
    for i, row in enumerate(rows, 1):
        for j, v in enumerate(row):
            t.rows[i].cells[j].text = v
    m = t.rows[3].cells[0].merge(t.rows[3].cells[2]); m.text = "Rates subject to loss-control survey; see Fees tab."

    d.add_heading("4. Notes", 1)
    d.add_paragraph(PAIR_UNTOUCHED)

    # comments: (paragraph, run range, text, author, initials) + a reply
    c1 = d.add_comment(runs=p1.runs[0], text=binder_comment, author="R. Alvarez", initials="RA")
    d.add_comment(runs=p1.runs[0], text="Binder was superseded; $1M is right. Updating binder.", author="M. Chen", initials="MC")
    d.add_comment(runs=r, text="Is blanket AI ongoing-ops only, or completed-ops too? Carrier form CG 20 10 vs 20 37.", author="R. Alvarez", initials="RA")
    d.add_comment(runs=p2.runs[1], text="Consent language conflicts with section 1 (blanket waiver). Needs legal review.", author="Legal", initials="LG")
    d.add_comment(runs=p3.runs[0], text="Pollution exclusion wording changed - flag for carrier sign-off.", author="M. Chen", initials="MC")
    d.save(OUT / name)


def build_endorsement_review_v2():
    """The pair's earlier revision: :func:`build_endorsement_review` before its two edits."""
    build_endorsement_review("program_review_v2.docx", revision=False, binder_comment=PAIR_V2_COMMENT)


def build_variant():
    """Second doc, different wording for same concepts -> exercises term-group flagging."""
    d = Document()
    d.core_properties.title = "Excess Liability Binder Summary"
    d.add_heading("Binder Summary", 0)
    d.add_heading("Insuring Agreement", 1)
    d.add_paragraph("Maximum limits of insurance: $5,000,000 each occurrence. Insured parties may be added as additional insureds by endorsement.")
    d.add_heading("Conditions", 1)
    d.add_paragraph("The insurer waives its right of recovery (subrogation) against parties named in a written contract.")
    d.add_heading("Not Covered", 1)
    d.add_paragraph("Claims resulting from pollutants, asbestos, or professional errors are excluded. Employee injury claims are outside coverage.")
    d.add_paragraph("Fees: policy fee 250; broker fee 10% of premium. Cancelation: 60 days written notice.")
    d.add_comment(runs=d.paragraphs[-1].runs[0], text="Cancelation period differs from program review (30 days).", author="M. Chen", initials="MC")
    d.save(OUT / "binder_summary.docx")


def build_torture():
    """Edge cases: empty comment, comment on table cell, nested list, comment anchored across paragraphs."""
    d = Document()
    d.add_heading("Edge Cases", 1)
    p = d.add_paragraph("Alpha ")
    p.add_run("bravo ").bold = True
    p.add_run("charlie.")
    d.add_comment(runs=[p.runs[0], p.runs[2]], text="Range comment spanning runs incl. a bold run in between.", author="A", initials="A")
    t = d.add_table(rows=2, cols=2); t.style = "Table Grid"
    t.cell(0, 0).text = "Header A"; t.cell(0, 1).text = "Header B"
    t.cell(1, 0).text = "cell"; t.cell(1, 1).text = "cell with comment"
    d.add_comment(runs=t.cell(1, 1).paragraphs[0].runs[0], text="Comment inside a table cell.", author="B", initials="B")
    numbered(d, "Level 0 item"); numbered(d, "Level 1 item", level=1); numbered(d, "Level 0 again")
    d.save(OUT / "edge_cases.docx")


# --- sidecar extraction (zipfile + lxml; never python-docx) -------------------


def _w(tag):
    return f"{{{W}}}{tag}"


def _w14(tag):
    return f"{{{W14}}}{tag}"


def _union_text(el):
    """Text of an element's runs in document order, including w:delText (i.e. w:ins/w:del)."""
    out = []
    for node in el.iter():
        if node.tag in (_w("t"), _w("delText")) and node.text:
            out.append(node.text)
    return "".join(out)


def _sections(body):
    out = []
    for p in body.findall(_w("p")):
        style = p.find(f"{_w('pPr')}/{_w('pStyle')}")
        if style is None:
            continue
        val = style.get(_w("val"), "")
        if val == "Title":
            level = 0
        elif val.startswith("Heading") and val[len("Heading"):].isdigit():
            level = int(val[len("Heading"):])
        else:
            continue
        out.append({"text": _union_text(p), "level": level, "order": len(out)})
    return out


def _revisions(body):
    kinds = {"ins": "ins", "del": "del", "moveFrom": "moveFrom", "moveTo": "moveTo"}
    out = []
    for el in body.iter():
        local = el.tag.split("}")[-1] if isinstance(el.tag, str) else ""
        if local in kinds:
            out.append({"kind": kinds[local], "author": el.get(_w("author"), ""), "text": _union_text(el)})
    return out


def _table_shape(tbl):
    grid = tbl.find(_w("tblGrid"))
    declared = len(grid.findall(_w("gridCol"))) if grid is not None else 0
    trs = tbl.findall(_w("tr"))
    rows = len(trs)
    cellinfo = []
    for tr in trs:
        row = []
        for tc in tr.findall(_w("tc")):
            span, vmerge = 1, None
            tcPr = tc.find(_w("tcPr"))
            if tcPr is not None:
                gs = tcPr.find(_w("gridSpan"))
                if gs is not None and gs.get(_w("val")):
                    span = int(gs.get(_w("val")))
                vm = tcPr.find(_w("vMerge"))
                if vm is not None:
                    vmerge = vm.get(_w("val")) or "continue"
            row.append((span, vmerge))
        cellinfo.append(row)
    cols = max([declared] + [sum(s for s, _ in r) for r in cellinfo])

    gridc = [[None] * cols for _ in range(rows)]
    rects: dict = {}
    for r, row in enumerate(cellinfo):
        c = 0
        for span, vm in row:
            key = (r, c)
            rects[key] = [r, r, c, c + span - 1, vm]
            for cc in range(c, min(c + span, cols)):
                gridc[r][cc] = key
            c += span
    for r in range(rows):
        for c in range(cols):
            key = gridc[r][c]
            if key is None or rects.get(key, [None, None, None, None, None])[4] != "continue" or r == 0:
                continue
            above = gridc[r - 1][c]
            if above is None or above == key:
                continue
            ar0, ar1, ac0, ac1, avm = rects[above]
            kr0, kr1, kc0, kc1, _ = rects[key]
            rects[above] = [ar0, max(ar1, kr1), min(ac0, kc0), max(ac1, kc1), avm]
            for rr in range(kr0, kr1 + 1):
                for cc in range(kc0, kc1 + 1):
                    if gridc[rr][cc] == key:
                        gridc[rr][cc] = above
            rects.pop(key, None)

    merges = sorted(
        [r0, c0, r1, c1] for r0, r1, c0, c1, _ in rects.values() if r1 > r0 or c1 > c0
    )
    return {"rows": rows, "columns": cols, "merges": merges}


def _tables(body):
    return [_table_shape(tbl) for tbl in body.findall(_w("tbl"))]


def _comments(docx_path, body):
    active: list = []
    anchors: dict = {}
    for node in body.iter():
        if node.tag == _w("commentRangeStart"):
            active.append({"id": node.get(_w("id")), "parts": []})
        elif node.tag == _w("commentRangeEnd"):
            cid = node.get(_w("id"))
            for a in list(active):
                if a["id"] == cid:
                    anchors[cid] = "".join(a["parts"])
                    active.remove(a)
        elif node.tag in (_w("t"), _w("delText")) and node.text:
            for a in active:
                a["parts"].append(node.text)

    with zipfile.ZipFile(docx_path) as z:
        if "word/comments.xml" not in z.namelist():
            return []
        croot = etree.fromstring(z.read("word/comments.xml"))
    out = []
    for c in croot.findall(_w("comment")):
        cid = c.get(_w("id"))
        ps = c.findall(_w("p"))
        out.append({
            "id": cid,
            "para_id": ps[-1].get(_w14("paraId")) if ps else None,
            "author": c.get(_w("author"), ""),
            "initials": c.get(_w("initials"), ""),
            "text": _union_text(c),
            "anchor_text": anchors.get(cid, ""),
        })
    return out


def sidecar_for(docx_path, provenance="generator"):
    with zipfile.ZipFile(docx_path) as z:
        body = etree.fromstring(z.read("word/document.xml")).find(_w("body"))
    return {
        "fixture": Path(docx_path).name,
        "labels_provenance": provenance,
        "comments": _comments(docx_path, body),
        "revisions": _revisions(body),
        "sections": _sections(body),
        "tables": _tables(body),
    }


def normalize_package(path):
    """Pin comment dates and zip member timestamps so regeneration is byte-stable."""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        parts = {name: z.read(name) for name in names}
    if "word/comments.xml" in parts:
        root = etree.fromstring(parts["word/comments.xml"])
        for comment in root.findall(_w("comment")):
            comment.set(_w("date"), FIXED_DATE)
        parts["word/comments.xml"] = etree.tostring(
            root, xml_declaration=True, encoding="UTF-8", standalone=True
        )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in names:
            info = zipfile.ZipInfo(name, date_time=ZIP_WHEN)
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, parts[name])


def emit_sidecars(out, names, provenance="generator"):
    out = Path(out)
    written = []
    for name in names:
        docx = out / name
        sidecar = sidecar_for(docx, provenance=provenance)
        path = docx.with_suffix(".expected.json")
        path.write_text(
            json.dumps(sidecar, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8"
        )
        written.append(path.name)
    return written


def emit_pair_sidecar(out):
    """Write the v2 / v3 pair's hand-typed sidecar (:data:`PAIR_SIDECAR`) into ``out``.

    It is deliberately not ``<name>.expected.json``: it labels a *pair* of fixtures rather
    than one, so nothing globs it as a single fixture's sidecar.
    """
    path = Path(out) / "program_review_pair.json"
    path.write_text(
        json.dumps(PAIR_SIDECAR, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8"
    )
    return path.name


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", nargs="?", default="fixtures", help="fixtures directory")
    parser.add_argument("--sidecars-only", action="store_true", help="do not rebuild the .docx files")
    args = parser.parse_args(argv)

    global OUT
    OUT = Path(args.out)
    OUT.mkdir(exist_ok=True)

    if not args.sidecars_only:
        for f in (build_endorsement_review_v2, build_endorsement_review, build_variant, build_torture):
            f()
        for name in FIXTURES:
            normalize_package(OUT / name)
    written = emit_sidecars(OUT, FIXTURES)
    written.append(emit_pair_sidecar(OUT))
    print(sorted(x.name for x in OUT.iterdir()))
    print("sidecars:", written)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
