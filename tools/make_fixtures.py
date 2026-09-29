"""Generate synthetic insurance-flavored .docx fixtures exercising the hard cases:
comments (with replies + ranges spanning runs), tracked changes, merged-cell tables,
multi-level numbering, headers/footers, content controls, and near-synonym term variants."""
from pathlib import Path
import copy, sys
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "fixtures")
OUT.mkdir(exist_ok=True)


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


def build_endorsement_review():
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
    tracked(p3, "excluded in all cases", "excluded except for hostile-fire release", "R. Alvarez")

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
    d.add_paragraph("Cancellation requires 30 days notice, 10 days for non-payment. The insured may not assign the policy without consent.")

    # comments: (paragraph, run range, text, author, initials) + a reply
    c1 = d.add_comment(runs=p1.runs[0], text="Confirm limit vs. the binder - binder says $500k occurrence.", author="R. Alvarez", initials="RA")
    d.add_comment(runs=p1.runs[0], text="Binder was superseded; $1M is right. Updating binder.", author="M. Chen", initials="MC")
    d.add_comment(runs=r, text="Is blanket AI ongoing-ops only, or completed-ops too? Carrier form CG 20 10 vs 20 37.", author="R. Alvarez", initials="RA")
    d.add_comment(runs=p2.runs[1], text="Consent language conflicts with section 1 (blanket waiver). Needs legal review.", author="Legal", initials="LG")
    d.add_comment(runs=p3.runs[0], text="Pollution exclusion wording changed - flag for carrier sign-off.", author="M. Chen", initials="MC")
    d.save(OUT / "program_review_v3.docx")


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


for f in (build_endorsement_review, build_variant, build_torture):
    f()
print(sorted(x.name for x in OUT.iterdir()))
