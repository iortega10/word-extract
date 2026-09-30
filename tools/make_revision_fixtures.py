"""Turn 0a: text-model fixture package, authored as raw OOXML with ``zipfile``.

One fixture per construct the text model has to survive (nested revisions, a move,
hyperlinks and fields, content controls, breaks and special characters, a comment
whose range starts inside a deleted run, a deleted paragraph mark, mixed
``w14:paraId``, a strict-namespace variant, a renamed ``commentsExtended`` part,
empty parts, a text box). python-docx cannot author any of these, so the package is
assembled part by part and written with pinned zip timestamps (byte-reproducible).

Each fixture gets a ``<name>.expected.json`` sidecar whose ``labels_provenance`` is
``"spec"``: its text-model facts are **hand-typed literals in this file**, not read
back from the ``.docx`` by any extractor. Per span the literal is ``(text, ancestor
revision ids)``; the ``accepted`` / ``original`` / ``superseded`` view strings are
hand-typed too, and ``_para()`` asserts them against the stacks so a typo in either
representation is a hard error rather than silent ground truth. Offsets are the
cumulative lengths of the hand-typed span texts (arithmetic only). Every paragraph
carries the text-model-spec clause it encodes (worked examples A to E, or section 8).

The walker (Phase 1 Turn 2) is validated against these literals; ``labels.py`` learns
to read the model sidecars in Turn 0.5, so this file only writes them.

Run:  python tools/make_revision_fixtures.py [fixtures/model]
"""
from pathlib import Path
import argparse
import json
import zipfile
from xml.sax.saxutils import escape

XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'

# --- namespaces ---------------------------------------------------------------
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {
    "w": W,
    "w14": "http://schemas.microsoft.com/office/word/2010/wordml",
    "w15": "http://schemas.microsoft.com/office/word/2012/wordml",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "wps": "http://schemas.microsoft.com/office/word/2010/wordprocessingShape",
    "v": "urn:schemas-microsoft-com:vml",
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}
# ISO/IEC 29500 strict namespaces (same document content, different URIs).
STRICT_NS = dict(NS)
STRICT_NS.update({
    "w": "http://purl.oclc.org/ooxml/wordprocessingml/main",
    "r": "http://purl.oclc.org/ooxml/officeDocument/relationships",
    "wp": "http://purl.oclc.org/ooxml/drawingml/wordprocessingDrawing",
    "a": "http://purl.oclc.org/ooxml/drawingml/main",
})

RT = {
    "officeDocument": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument",
    "comments": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments",
    "commentsExtended": "http://schemas.microsoft.com/office/2011/relationships/commentsExtended",
    "footnotes": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes",
    "endnotes": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/endnotes",
    "hyperlink": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
    "core-properties": "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties",
}
STRICT_RT = dict(RT)
STRICT_RT["officeDocument"] = "http://purl.oclc.org/ooxml/officeDocument/relationships/officeDocument"
STRICT_RT["comments"] = "http://purl.oclc.org/ooxml/officeDocument/relationships/comments"
STRICT_RT["hyperlink"] = "http://purl.oclc.org/ooxml/officeDocument/relationships/hyperlink"

CT = {
    "document": "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    "comments": "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml",
    "commentsExtended": "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsExtended+xml",
    "footnotes": "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml",
    "endnotes": "application/vnd.openxmlformats-officedocument.wordprocessingml.endnotes+xml",
    "core": "application/vnd.openxmlformats-package.core-properties+xml",
}

AUTHOR = "A. Ito"
DATE = "2026-01-01T00:00:00Z"
WHEN = (2026, 1, 1, 0, 0, 0)

SPAN_RULE = ("spans are maximal runs of equal ancestor stack; the union is an address "
             "space only and is never matched directly")

DEL_FAMILY = ("del", "moveFrom")
INS_FAMILY = ("ins", "moveTo")


# --- sidecar literal helpers --------------------------------------------------
def _spans(items):
    """Offsets from the cumulative length of hand-typed ``(text, stack)`` pairs."""
    out, pos = [], 0
    for text, stack in items:
        out.append({"start": pos, "end": pos + len(text), "text": text, "stack": list(stack)})
        pos += len(text)
    return out


def _families(stack):
    return {rev.split(":")[0] for rev in stack}


def _masks(spans):
    def keep_without(prefixes):
        return "".join(s["text"] for s in spans if not _families(s["stack"]) & set(prefixes))

    superseded = "".join(
        s["text"]
        for s in spans
        if _families(s["stack"]) & set(DEL_FAMILY) and _families(s["stack"]) & set(INS_FAMILY)
    )
    return keep_without(DEL_FAMILY), keep_without(INS_FAMILY), superseded


def _para(index, items, accepted, original, superseded, clause, note=None):
    """One paragraph literal: hand-typed spans + hand-typed view strings, cross-checked."""
    spans = _spans(items)
    computed = _masks(spans)
    hand = (accepted, original, superseded)
    if computed != hand:
        raise AssertionError(f"p{index}: masks {computed!r} != hand-typed {hand!r}")
    para = {
        "index": index,
        "clause": clause,
        "union": "".join(text for text, _ in items),
        "spans": spans,
        "accepted": accepted,
        "original": original,
        "superseded": superseded,
    }
    if note:
        para["note"] = note
    return para


# --- OOXML fragments ----------------------------------------------------------
def _r(text):
    return f'<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r>'


def _dr(text):
    return f'<w:r><w:delText xml:space="preserve">{escape(text)}</w:delText></w:r>'


def _ins(rid, inner, author=AUTHOR):
    return f'<w:ins w:id="{rid}" w:author="{author}" w:date="{DATE}">{inner}</w:ins>'


def _del(rid, inner, author=AUTHOR):
    return f'<w:del w:id="{rid}" w:author="{author}" w:date="{DATE}">{inner}</w:del>'


def _p(inner, para_id=None, ppr=""):
    pid = f' w14:paraId="{para_id}"' if para_id else ""
    props = f"<w:pPr>{ppr}</w:pPr>" if ppr else ""
    return f"<w:p{pid}>{props}{inner}</w:p>"


def _comment_ref(cid):
    return (
        '<w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr>'
        f'<w:commentReference w:id="{cid}"/></w:r>'
    )


def _comment(cid, author, initials, para_id, text):
    pid = f' w14:paraId="{para_id}"' if para_id else ""
    return (
        f'<w:comment w:id="{cid}" w:author="{author}" w:initials="{initials}" w:date="{DATE}">'
        f"<w:p{pid}>"
        '<w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr><w:annotationRef/></w:r>'
        f"{_r(text)}</w:p></w:comment>"
    )


def _nsdecl(ns, prefixes):
    return " ".join(f'xmlns:{p}="{ns[p]}"' for p in prefixes)


def _document(body, ns=NS, prefixes=("w", "w14", "w15", "r")):
    sect = '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/></w:sectPr>'
    return XML_DECL + f'<w:document {_nsdecl(ns, prefixes)}><w:body>{body}{sect}</w:body></w:document>'


# --- package assembly ---------------------------------------------------------
def _content_types(overrides=()):
    rows = [
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
        '<Default Extension="xml" ContentType="application/xml"/>',
    ]
    for part, ctype in overrides:
        rows.append(f'<Override PartName="{part}" ContentType="{ctype}"/>')
    return XML_DECL + f'<Types {_nsdecl(NS, ("ct",))}>{"".join(rows)}</Types>'


def _rels(entries):
    rows = []
    for rid, rtype, target, mode in entries:
        extra = f' TargetMode="{mode}"' if mode else ""
        rows.append(f'<Relationship Id="{rid}" Type="{rtype}" Target="{target}"{extra}/>')
    return XML_DECL + f'<Relationships {_nsdecl(NS, ("pr",))}>{"".join(rows)}</Relationships>'


def _core_props(title):
    return XML_DECL + (
        f'<cp:coreProperties {_nsdecl(NS, ("cp", "dc", "dcterms", "xsi"))}>'
        f"<dc:title>{escape(title)}</dc:title>"
        "<dc:creator>word-extract fixtures</dc:creator>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{DATE}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{DATE}</dcterms:modified>'
        "</cp:coreProperties>"
    )


def package(name, body, *, sidecar, doc_rels=(), extra_parts=None, ct_overrides=(), ns=NS,
            prefixes=("w", "w14", "w15", "r"), title=None, rt=RT):
    """Assemble one fixture: filesystem path, bytes map and its model sidecar."""
    parts = {
        "[Content_Types].xml": _content_types(
            [("/word/document.xml", CT["document"]), ("/docProps/core.xml", CT["core"])]
            + list(ct_overrides)
        ),
        "_rels/.rels": _rels([
            ("rId1", rt["officeDocument"], "word/document.xml", None),
            ("rId2", rt["core-properties"], "docProps/core.xml", None),
        ]),
        "docProps/core.xml": _core_props(title or name),
        "word/document.xml": _document(body, ns=ns, prefixes=prefixes),
        "word/_rels/document.xml.rels": _rels(doc_rels),
    }
    parts.update(extra_parts or {})
    sidecar.setdefault("span_rule", SPAN_RULE)
    sidecar.setdefault("terminator", "\n")
    return name, parts, sidecar


def _write(out, name, parts, sidecar):
    path = Path(out) / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for member in sorted(parts):
            info = zipfile.ZipInfo(member, date_time=WHEN)
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, parts[member].encode("utf-8"))
    sidecar_path = path.with_suffix(".expected.json")
    sidecar_path.write_text(
        json.dumps(sidecar, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path, sidecar_path


# --- the fixtures -------------------------------------------------------------
def _nested_revisions():
    body = (
        _p(_ins(2, _r("Coverage ") + _del(3, _dr("is excluded")) + _r("applies worldwide.")))
        + _p(_del(4, _dr("Draft clause ") + _ins(5, _r("Final clause")) + _dr(" is void.")))
    )
    sidecar = {
        "fixture": "nested_revisions.docx",
        "labels_provenance": "spec",
        "clauses": ["2", "3", "B"],
        "known_gaps": [],
        "revisions": [
            {"id": "ins:2", "kind": "ins", "author": AUTHOR, "date": DATE},
            {"id": "del:3", "kind": "del", "author": AUTHOR, "date": DATE},
            {"id": "del:4", "kind": "del", "author": AUTHOR, "date": DATE},
            {"id": "ins:5", "kind": "ins", "author": AUTHOR, "date": DATE},
        ],
        "paragraphs": [
            _para(0, [("Coverage ", ["ins:2"]), ("is excluded", ["ins:2", "del:3"]),
                      ("applies worldwide.", ["ins:2"])],
                  accepted="Coverage applies worldwide.", original="", superseded="is excluded",
                  clause="B"),
            _para(1, [("Draft clause ", ["del:4"]), ("Final clause", ["del:4", "ins:5"]),
                      (" is void.", ["del:4"])],
                  accepted="", original="Draft clause  is void.", superseded="Final clause",
                  clause="2"),
        ],
    }
    return package("nested_revisions.docx", body, sidecar=sidecar,
                   title="Nested revisions")


def _move():
    body = (
        _p(
            '<w:moveFromRangeStart w:id="40" w:name="mg1" w:author="E. Nakamura" w:date="%s"/>' % DATE
            + f'<w:moveFrom w:id="5" w:author="E. Nakamura" w:date="{DATE}">{_dr("Section 4 ")}</w:moveFrom>'
            + '<w:moveFromRangeEnd w:id="40"/>',
            para_id="0000000A",
        )
        + _p(
            '<w:moveToRangeStart w:id="41" w:name="mg1" w:author="E. Nakamura" w:date="%s"/>' % DATE
            + f'<w:moveTo w:id="6" w:author="E. Nakamura" w:date="{DATE}">{_r("Section 4 ")}</w:moveTo>'
            + '<w:moveToRangeEnd w:id="41"/>',
            para_id="0000000B",
        )
    )
    sidecar = {
        "fixture": "move.docx",
        "labels_provenance": "spec",
        "clauses": ["3", "5", "C"],
        "known_gaps": [],
        "move_groups": [{"move_group_id": "mg1", "source_span": {"start": 0, "end": 10},
                         "dest_span": {"start": 0, "end": 10}}],
        "revisions": [
            {"id": "moveFrom:5", "kind": "moveFrom", "author": "E. Nakamura", "date": DATE,
             "move_group_id": "mg1"},
            {"id": "moveTo:6", "kind": "moveTo", "author": "E. Nakamura", "date": DATE,
             "move_group_id": "mg1"},
        ],
        "paragraphs": [
            _para(0, [("Section 4 ", ["moveFrom:5"])], accepted="", original="Section 4 ",
                  superseded="", clause="C",
                  note="move_group_id comes from the shared w:name on the range markers"),
            _para(1, [("Section 4 ", ["moveTo:6"])], accepted="Section 4 ", original="",
                  superseded="", clause="C"),
        ],
    }
    return package("move.docx", body, sidecar=sidecar, title="A move")


def _hyperlink_and_fields():
    hyperlink = (
        '<w:hyperlink r:id="rId10">'
        + _r("See the ")
        + _r("endorsement form")
        + "</w:hyperlink>"
    )
    nested_field = "".join([
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>',
        '<w:r><w:instrText xml:space="preserve"> IF </w:instrText></w:r>',
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>',
        '<w:r><w:instrText xml:space="preserve"> MERGEFIELD name </w:instrText></w:r>',
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>',
        _r("Acme"),
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>',
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>',
        _r("Endorsed"),
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>',
    ])
    simple = '<w:fldSimple w:instr=" DATE \\@ &quot;d&quot; ">' + _r("1/1/2026") + "</w:fldSimple>"
    body = _p(hyperlink, para_id="00000001") + _p(nested_field) + _p(simple)
    sidecar = {
        "fixture": "hyperlink_and_fields.docx",
        "labels_provenance": "spec",
        "clauses": ["E", "8"],
        "known_gaps": ["field_result_view_ancestry"],
        "doc_rels": [{"id": "rId10", "type": RT["hyperlink"], "target": "https://example.test/form",
                      "mode": "External"}],
        "paragraphs": [
            _para(0, [("See the endorsement form", [])], accepted="See the endorsement form",
                  original="See the endorsement form", superseded="", clause="E",
                  note="w:hyperlink runs are ordinary content at their document position"),
            _para(1, [("Endorsed", [])], accepted="Endorsed",
                  original="Endorsed", superseded="", clause="E",
                  note="complex field: the inner field's result 'Acme' lies between the outer "
                       "begin and separate, so it is instruction (section 6E) and excluded; only "
                       "the outer result is content"),
            _para(2, [("1/1/2026", [])], accepted="1/1/2026", original="1/1/2026", superseded="",
                  clause="E", note="w:fldSimple: w:instr excluded, child runs are the result"),
        ],
    }
    return package(
        "hyperlink_and_fields.docx", body, sidecar=sidecar, title="Hyperlink and fields",
        doc_rels=[("rId10", RT["hyperlink"], "https://example.test/form", "External")],
        prefixes=("w", "w14", "w15", "r"),
    )


def _content_controls():
    inline_sdt = (
        "<w:sdt><w:sdtPr><w:tag w:val=\"inline\"/></w:sdtPr>"
        f"<w:sdtContent>{_r('inline text')}</w:sdtContent></w:sdt>"
    )
    block_sdt = (
        "<w:sdt><w:sdtPr><w:tag w:val=\"block\"/></w:sdtPr>"
        f"<w:sdtContent>{_p(_r('Inside the content control.'))}</w:sdtContent></w:sdt>"
    )
    body = (
        _p(_r("Before ") + inline_sdt + _r(" after."))
        + block_sdt
    )
    sidecar = {
        "fixture": "content_controls.docx",
        "labels_provenance": "spec",
        "clauses": ["8"],
        "known_gaps": ["inline_sdt_transparent"],
        "paragraphs": [
            _para(0, [("Before inline text after.", [])], accepted="Before inline text after.",
                  original="Before inline text after.", superseded="", clause="8",
                  note="inline sdt is transparent: no node, no boundary, no ancestor"),
            _para(1, [("Inside the content control.", [])],
                  accepted="Inside the content control.", original="Inside the content control.",
                  superseded="", clause="8"),
        ],
        "block_nodes": [{"kind": "sdt", "child_paragraph": 1,
                         "note": "block sdt is a node with empty spans and child_ids"}],
    }
    return package("content_controls.docx", body, sidecar=sidecar, title="Content controls")


def _breaks_and_specials():
    p0 = _r("A") + "<w:r><w:tab/></w:r>" + _r("B") + '<w:r><w:br w:type="textWrapping"/></w:r>' + _r("C")
    p1 = (_r("D") + '<w:r><w:br w:type="page"/></w:r>' + _r("E")
          + '<w:r><w:br w:type="column"/></w:r>' + _r("F") + "<w:r><w:br/></w:r>" + _r("G"))
    p2 = "<w:r><w:t>soft</w:t><w:softHyphen/><w:t>hyphen</w:t></w:r>"
    p3 = "<w:r><w:t>no</w:t><w:noBreakHyphen/><w:t>break</w:t></w:r>"
    p4 = '<w:r><w:sym w:font="Wingdings" w:char="F0FC"/></w:r>'
    p5 = _r("line1\nline2")
    p6 = "<w:r><w:t>carriage</w:t><w:cr/><w:t>return</w:t></w:r>"
    body = "".join(_p(x) for x in (p0, p1, p2, p3, p4, p5, p6))
    sidecar = {
        "fixture": "breaks_and_specials.docx",
        "labels_provenance": "spec",
        "clauses": ["8"],
        "known_gaps": ["w_cr_unspecified"],
        "assumptions": ["w:cr is treated like w:br (U+000B); the spec names only w:br"],
        "paragraphs": [
            _para(0, [("A\tB\u000bC", [])],
                  accepted="A\tB\u000bC", original="A\tB\u000bC", superseded="", clause="8",
                  note="w:tab = '\\t'; every w:br type = U+000B; equal stacks coalesce, so the "
                       "tab/br boundaries do not survive as span boundaries"),
            _para(1, [("D\u000bE\u000bF\u000bG", [])],
                  accepted="D\u000bE\u000bF\u000bG", original="D\u000bE\u000bF\u000bG",
                  superseded="", clause="8",
                  note="page, column and bare w:br all = U+000B"),
            _para(2, [("soft\u00adhyphen", [])], accepted="soft\u00adhyphen",
                  original="soft\u00adhyphen", superseded="", clause="8",
                  note="w:softHyphen = U+00AD"),
            _para(3, [("no\u2011break", [])], accepted="no\u2011break", original="no\u2011break",
                  superseded="", clause="8", note="w:noBreakHyphen = U+2011"),
            _para(4, [("\uf0fc", [])], accepted="\uf0fc", original="\uf0fc", superseded="",
                  clause="8", note="w:sym = its character (chr of the hex w:char)"),
            _para(5, [("line1\u000bline2", [])], accepted="line1\u000bline2",
                  original="line1\u000bline2", superseded="", clause="8",
                  note="a literal \\n inside w:t is content, mapped to U+000B"),
            _para(6, [("carriage\u000breturn", [])], accepted="carriage\u000breturn",
                  original="carriage\u000breturn", superseded="", clause="8"),
        ],
    }
    return package("breaks_and_specials.docx", body, sidecar=sidecar,
                   title="Breaks and specials")


def _comment_in_deletion():
    body = _p(
        _r("The exclusion is ")
        + '<w:commentRangeStart w:id="7"/>'
        + _del(3, _dr("excluded in all cases"))
        + '<w:commentRangeEnd w:id="7"/>'
        + _comment_ref(7)
        + _r("."),
        para_id="00000001",
    )
    comments = XML_DECL + (
        f'<w:comments {_nsdecl(NS, ("w", "w14"))}>'
        + _comment(7, "D. Okafor", "DO", "0000001A", "Is this really excluded?")
        + "</w:comments>"
    )
    ext = XML_DECL + (
        f'<w15:commentsEx {_nsdecl(NS, ("w15",))}>'
        '<w15:commentEx w15:paraId="0000001A" w15:done="0"/>'
        "</w15:commentsEx>"
    )
    sidecar = {
        "fixture": "comment_in_deletion.docx",
        "labels_provenance": "spec",
        "clauses": ["D"],
        "known_gaps": [],
        "revisions": [{"id": "del:3", "kind": "del", "author": AUTHOR, "date": DATE}],
        "comments": [{
            "id": "7", "para_id": "0000001A", "author": "D. Okafor", "initials": "DO",
            "anchor": {"start": 17, "end": 38}, "anchor_text": "excluded in all cases",
            "clauses": ["D"],
            "note": "the range starts inside a deleted run: the union anchor is non-empty even "
                    "though the accepted view of that span is empty",
        }],
        "paragraphs": [
            _para(0, [("The exclusion is ", []), ("excluded in all cases", ["del:3"]), (".", [])],
                  accepted="The exclusion is .", original="The exclusion is excluded in all cases.",
                  superseded="", clause="D"),
        ],
    }
    return package(
        "comment_in_deletion.docx", body, sidecar=sidecar, title="Comment in a deletion",
        doc_rels=[("rId1", RT["comments"], "comments.xml", None),
                  ("rId2", RT["commentsExtended"], "commentsExtended.xml", None)],
        extra_parts={"word/comments.xml": comments, "word/commentsExtended.xml": ext},
        ct_overrides=[("/word/comments.xml", CT["comments"]),
                      ("/word/commentsExtended.xml", CT["commentsExtended"])],
        prefixes=("w", "w14", "w15", "r"),
    )


def _deleted_paragraph_mark():
    del_mark = f'<w:rPr><w:del w:id="9" w:author="{AUTHOR}" w:date="{DATE}"/></w:rPr>'
    body = _p(_r("First half,"), ppr=del_mark) + _p(_r("second half."))
    sidecar = {
        "fixture": "deleted_paragraph_mark.docx",
        "labels_provenance": "spec",
        "clauses": ["8"],
        "known_gaps": ["paragraph_mark_revision"],
        "revisions": [{"id": "del:9", "kind": "del", "author": AUTHOR, "date": DATE,
                       "note": "marks the paragraph mark deleted; not honored in Phase 1"}],
        "paragraphs": [
            _para(0, [("First half,", [])], accepted="First half,", original="First half,",
                  superseded="", clause="8",
                  note="the deleted paragraph mark is NOT honored: the break is retained in both views"),
            _para(1, [("second half.", [])], accepted="second half.", original="second half.",
                  superseded="", clause="8"),
        ],
    }
    return package("deleted_paragraph_mark.docx", body, sidecar=sidecar,
                   title="Deleted paragraph mark")


def _mixed_para_ids():
    body = (
        _p(_r("Has a paraId."), para_id="10000001")
        + _p(_r("No paraId here."))
        + _p(_r("Also has one."), para_id="10000003")
        + _p(_r("Neither does this one."))
    )
    sidecar = {
        "fixture": "mixed_para_ids.docx",
        "labels_provenance": "spec",
        "clauses": ["8"],
        "known_gaps": ["duplicate_content_id_churn"],
        "id_expectations": [
            {"index": 0, "para_id": "10000001", "fallback": "paraid"},
            {"index": 1, "para_id": None, "fallback": "content_hash"},
            {"index": 2, "para_id": "10000003", "fallback": "paraid"},
            {"index": 3, "para_id": None, "fallback": "content_hash"},
        ],
        "paragraphs": [
            _para(0, [("Has a paraId.", [])], accepted="Has a paraId.", original="Has a paraId.",
                  superseded="", clause="8"),
            _para(1, [("No paraId here.", [])], accepted="No paraId here.",
                  original="No paraId here.", superseded="", clause="8"),
            _para(2, [("Also has one.", [])], accepted="Also has one.", original="Also has one.",
                  superseded="", clause="8"),
            _para(3, [("Neither does this one.", [])], accepted="Neither does this one.",
                  original="Neither does this one.", superseded="", clause="8"),
        ],
    }
    return package("mixed_para_ids.docx", body, sidecar=sidecar, title="Mixed paraIds")


def _strict_namespaces():
    body = (
        _p('<w:pPr><w:pStyle w:val="Heading1"/></w:pPr>' + _r("Strict Clause"))
        + _p(_r("The limit is ") + _del(2, _dr("five million")) + _ins(3, _r("ten million")) + _r("."))
    )
    sidecar = {
        "fixture": "strict_namespaces.docx",
        "labels_provenance": "spec",
        "clauses": ["1", "7"],
        "known_gaps": ["strict_namespaces_unverified"],
        "namespaces": {"w": STRICT_NS["w"], "r": STRICT_NS["r"]},
        "parts": [
            {"rel_type": STRICT_RT["officeDocument"], "path": "word/document.xml",
             "content_type": CT["document"]},
        ],
        "revisions": [{"id": "del:2", "kind": "del", "author": AUTHOR, "date": DATE},
                      {"id": "ins:3", "kind": "ins", "author": AUTHOR, "date": DATE}],
        "paragraphs": [
            _para(0, [("Strict Clause", [])], accepted="Strict Clause", original="Strict Clause",
                  superseded="", clause="1"),
            _para(1, [("The limit is ", []), ("five million", ["del:2"]), ("ten million", ["ins:3"]),
                      (".", [])],
                  accepted="The limit is ten million.", original="The limit is five million.",
                  superseded="", clause="3"),
        ],
    }
    return package(
        "strict_namespaces.docx", body, sidecar=sidecar, title="Strict namespaces",
        ns=STRICT_NS, prefixes=("w", "r"), rt=STRICT_RT,
    )


def _renamed_comments_extended():
    body = (
        _p('<w:commentRangeStart w:id="1"/>' + _r("The carve-out ")
           + '<w:commentRangeEnd w:id="1"/>' + _comment_ref(1) + _r("is narrow."), para_id="30000001")
        + _p('<w:commentRangeStart w:id="2"/>' + _r("This reply sits on the last paragraph.")
           + '<w:commentRangeEnd w:id="2"/>' + _comment_ref(2), para_id="30000002")
    )
    comments = XML_DECL + (
        f'<w:comments {_nsdecl(NS, ("w", "w14"))}>'
        + _comment(1, "R. Vega", "RV", "00000021", "What does carve-out mean here?")
        + _comment(2, "L. Duarte", "LD", "00000022", "See the definition in schedule 2.")
        + "</w:comments>"
    )
    ext = XML_DECL + (
        f'<w15:commentsEx {_nsdecl(NS, ("w15",))}>'
        '<w15:commentEx w15:paraId="00000021" w15:done="0"/>'
        '<w15:commentEx w15:paraId="00000022" w15:done="1" w15:paraIdParent="00000021"/>'
        "</w15:commentsEx>"
    )
    sidecar = {
        "fixture": "renamed_comments_extended.docx",
        "labels_provenance": "spec",
        "clauses": ["8"],
        "known_gaps": ["renamed_part_unverified"],
        "parts": [
            {"rel_type": RT["comments"], "path": "word/comments.xml",
             "content_type": CT["comments"]},
            {"rel_type": RT["commentsExtended"], "path": "word/ext/commentsExt.xml",
             "content_type": CT["commentsExtended"],
             "note": "renamed part: resolved by relationship type, never by path"},
        ],
        "comments": [
            {"id": "1", "para_id": "00000021", "author": "R. Vega", "initials": "RV",
             "anchor_text": "The carve-out ", "threading_status": "verified",
             "resolved": False, "parent_id": None},
            {"id": "2", "para_id": "00000022", "author": "L. Duarte", "initials": "LD",
             "anchor_text": "This reply sits on the last paragraph.",
             "threading_status": "verified", "resolved": True, "parent_id": "00000021"},
        ],
        "paragraphs": [
            _para(0, [("The carve-out is narrow.", [])], accepted="The carve-out is narrow.",
                  original="The carve-out is narrow.", superseded="", clause="8"),
            _para(1, [("This reply sits on the last paragraph.", [])],
                  accepted="This reply sits on the last paragraph.",
                  original="This reply sits on the last paragraph.", superseded="", clause="8"),
        ],
    }
    return package(
        "renamed_comments_extended.docx", body, sidecar=sidecar,
        title="Renamed commentsExtended",
        doc_rels=[("rId1", RT["comments"], "comments.xml", None),
                  ("rId2", RT["commentsExtended"], "ext/commentsExt.xml", None)],
        extra_parts={"word/comments.xml": comments, "word/ext/commentsExt.xml": ext},
        ct_overrides=[("/word/comments.xml", CT["comments"]),
                      ("/word/ext/commentsExt.xml", CT["commentsExtended"])],
        prefixes=("w", "w14", "w15", "r"),
    )


def _empty_parts():
    body = _p(_r("A body with empty footnotes and endnotes parts."), para_id="40000001")
    empty_footnotes = XML_DECL + f'<w:footnotes {_nsdecl(NS, ("w",))}/>'
    empty_endnotes = XML_DECL + f'<w:endnotes {_nsdecl(NS, ("w",))}/>'
    sidecar = {
        "fixture": "empty_parts.docx",
        "labels_provenance": "spec",
        "clauses": ["1"],
        "known_gaps": ["empty_parts_unverified"],
        "parts": [
            {"rel_type": RT["footnotes"], "path": "word/footnotes.xml",
             "content_type": CT["footnotes"], "empty": True},
            {"rel_type": RT["endnotes"], "path": "word/endnotes.xml",
             "content_type": CT["endnotes"], "empty": True},
        ],
        "paragraphs": [
            _para(0, [("A body with empty footnotes and endnotes parts.", [])],
                  accepted="A body with empty footnotes and endnotes parts.",
                  original="A body with empty footnotes and endnotes parts.", superseded="",
                  clause="1"),
        ],
    }
    return package(
        "empty_parts.docx", body, sidecar=sidecar, title="Empty parts",
        doc_rels=[("rId1", RT["footnotes"], "footnotes.xml", None),
                  ("rId2", RT["endnotes"], "endnotes.xml", None)],
        extra_parts={"word/footnotes.xml": empty_footnotes, "word/endnotes.xml": empty_endnotes},
        ct_overrides=[("/word/footnotes.xml", CT["footnotes"]),
                      ("/word/endnotes.xml", CT["endnotes"])],
    )


def _text_box():
    choice = (
        '<mc:Choice Requires="wps">'
        '<w:drawing><wp:inline><wp:extent cx="2000000" cy="500000"/>'
        '<a:graphic><a:graphicData uri="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">'
        "<wps:wsp><wps:txbx><w:txbxContent>"
        + _p(_r("Choice box text"))
        + "</w:txbxContent></wps:txbx></wps:wsp>"
        "</a:graphicData></a:graphic></wp:inline></w:drawing>"
        "</mc:Choice>"
    )
    fallback = (
        "<mc:Fallback><w:pict><v:shape><v:textbox><w:txbxContent>"
        + _p(_r("Fallback box text"))
        + "</w:txbxContent></v:textbox></v:shape></w:pict></mc:Fallback>"
    )
    body = _p(
        _r("Before box.")
        + "<w:r><mc:AlternateContent>" + choice + fallback + "</mc:AlternateContent></w:r>"
        + _r("After box."),
        para_id="50000001",
    )
    sidecar = {
        "fixture": "text_box.docx",
        "labels_provenance": "spec",
        "clauses": ["8"],
        "known_gaps": ["textbox"],
        "paragraphs": [
            _para(0, [("Before box.After box.", [])], accepted="Before box.After box.",
                  original="Before box.After box.", superseded="", clause="8",
                  note="box text lives in its own fragment, so host document order is undisturbed"),
        ],
        "fragments": [
            {"ordinal": 0, "host_paragraph": 0, "preferred": "mc:Choice",
             "union": "Choice box text",
             "note": "exactly one body per drawing: mc:Choice > mc:Fallback > bare w:txbxContent"},
        ],
    }
    return package(
        "text_box.docx", body, sidecar=sidecar, title="Text box",
        prefixes=("w", "w14", "w15", "r", "mc", "wp", "a", "wps", "v"),
    )


def _fld(kind):
    return f'<w:r><w:fldChar w:fldCharType="{kind}"/></w:r>'


def _instr(text):
    return f'<w:r><w:instrText xml:space="preserve">{text}</w:instrText></w:r>'


def _nested_field_in_instruction():
    """Spec 6E: text is content only when EVERY open field has passed its separate."""
    body = (
        _p(_r("A ") + _fld("begin") + _instr(" IF ") + _fld("begin") + _instr(" PAGE ")
           + _fld("separate") + _r("INNER-RESULT-IN-OUTER-INSTRUCTION") + _fld("end")
           + _instr(" > 1 ") + _fld("separate") + _r("OUTER-RESULT") + _fld("end") + _r(" Z"))
        + _p(_fld("begin") + _instr(" REF a ") + _fld("separate") + _r("outer ")
             + _fld("begin") + _instr(" REF b ") + _fld("separate") + _r("inner")
             + _fld("end") + _r(" tail") + _fld("end"))
    )
    sidecar = {
        "fixture": "nested_field_in_instruction.docx",
        "labels_provenance": "spec",
        "clauses": ["E", "8"],
        "known_gaps": [],
        "paragraphs": [
            _para(0, [("A OUTER-RESULT Z", [])], accepted="A OUTER-RESULT Z",
                  original="A OUTER-RESULT Z", superseded="", clause="E",
                  note="a nested field's result inside the outer field's INSTRUCTION is "
                       "instruction, not content"),
            _para(1, [("outer inner tail", [])], accepted="outer inner tail",
                  original="outer inner tail", superseded="", clause="E",
                  note="a nested field inside the outer field's RESULT is content"),
        ],
    }
    return package("nested_field_in_instruction.docx", body, sidecar=sidecar,
                   title="Nested field in instruction")


def _transparent_containers():
    """customXml (inline and block), w:dir, w:bdo are transparent; w:ptab is a tab."""
    body = (
        _p(_r("before ") + '<w:customXml w:element="x">' + _r("inside-customXml") + "</w:customXml>"
           + _r(" after"))
        + '<w:customXml w:element="y">' + _p(_r("block customXml")) + "</w:customXml>"
        + _p(_r("a ") + '<w:dir w:val="rtl">' + _r("in-dir") + "</w:dir>"
             + '<w:bdo w:val="ltr">' + _r(" in-bdo") + "</w:bdo>" + _r(" b"))
        + _p(_r("a") + '<w:r><w:ptab w:relativeTo="margin" w:alignment="right" w:leader="none"/></w:r>'
             + _r("b"))
    )
    sidecar = {
        "fixture": "transparent_containers.docx",
        "labels_provenance": "spec",
        "clauses": ["8"],
        "known_gaps": [],
        "paragraphs": [
            _para(0, [("before inside-customXml after", [])],
                  accepted="before inside-customXml after",
                  original="before inside-customXml after", superseded="", clause="8",
                  note="inline w:customXml is transparent"),
            _para(1, [("block customXml", [])], accepted="block customXml",
                  original="block customXml", superseded="", clause="8",
                  note="block-level w:customXml is descended through"),
            _para(2, [("a in-dir in-bdo b", [])], accepted="a in-dir in-bdo b",
                  original="a in-dir in-bdo b", superseded="", clause="8",
                  note="w:dir and w:bdo are transparent"),
            _para(3, [("a\tb", [])], accepted="a\tb", original="a\tb", superseded="",
                  clause="8", note="w:ptab is a tab"),
        ],
    }
    return package("transparent_containers.docx", body, sidecar=sidecar,
                   title="Transparent containers")


def _unrecognized_container():
    """A w: element the walker does not know is skipped -- and the loss is recorded."""
    body = _p(_r("before ") + "<w:notARealContainer>" + _r("HIDDEN") + "</w:notARealContainer>"
              + _r(" after"))
    sidecar = {
        "fixture": "unrecognized_container.docx",
        "labels_provenance": "spec",
        "clauses": ["8"],
        "known_gaps": ["unrecognized_container"],
        "paragraphs": [
            _para(0, [("before  after", [])], accepted="before  after",
                  original="before  after", superseded="", clause="8",
                  note="unknown container is skipped (never guessed at); the text it held is "
                       "lost and recorded as the unrecognized_container gap"),
        ],
    }
    return package("unrecognized_container.docx", body, sidecar=sidecar,
                   title="Unrecognized container")


def _revision_missing_id():
    """A revision with no w:id gets a deterministic <kind>:noid<n> id and a recorded gap."""
    body = _p(
        _r("x")
        + f'<w:ins w:author="{AUTHOR}" w:date="{DATE}">{_r("y")}</w:ins>'
        + f'<w:del w:author="{AUTHOR}" w:date="{DATE}">{_dr("z")}</w:del>'
    )
    sidecar = {
        "fixture": "revision_missing_id.docx",
        "labels_provenance": "spec",
        "clauses": ["2"],
        "known_gaps": ["revision_missing_id"],
        "revisions": [
            {"id": "ins:noid0", "kind": "ins", "author": AUTHOR, "date": DATE},
            {"id": "del:noid1", "kind": "del", "author": AUTHOR, "date": DATE},
        ],
        "paragraphs": [
            _para(0, [("x", []), ("y", ["ins:noid0"]), ("z", ["del:noid1"])],
                  accepted="xy", original="xz", superseded="", clause="2",
                  note="ids for id-less revisions are <kind>:noid<n>, n counting id-less "
                       "revisions in per-part document order"),
        ],
    }
    return package("revision_missing_id.docx", body, sidecar=sidecar,
                   title="Revision missing id")


RT_STYLES = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles"
RT_NUMBERING = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering"
CT_STYLES = "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"
CT_NUMBERING = "application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"


def _part_xml(root, inner):
    return XML_DECL + f"<w:{root} {_nsdecl(NS, ('w',))}>{inner}</w:{root}>"


def _style(style_id, *, based_on=None, num_id=None, ilvl=None, outline=None, name=None):
    ppr = ""
    if num_id is not None or ilvl is not None:
        ppr += "<w:numPr>"
        if ilvl is not None:
            ppr += f'<w:ilvl w:val="{ilvl}"/>'
        if num_id is not None:
            ppr += f'<w:numId w:val="{num_id}"/>'
        ppr += "</w:numPr>"
    if outline is not None:
        ppr += f'<w:outlineLvl w:val="{outline}"/>'
    based = f'<w:basedOn w:val="{based_on}"/>' if based_on else ""
    return (f'<w:style w:type="paragraph" w:styleId="{style_id}"><w:name w:val="{name or style_id}"/>'
            f"{based}<w:pPr>{ppr}</w:pPr></w:style>")


def _sp(text, style):
    return _p(_r(text), ppr=f'<w:pStyle w:val="{style}"/>')


def _style_numbering():
    """Numbering and outline level defined on STYLES, not on the paragraphs."""
    styles = _part_xml("styles", "".join([
        _style("Heading1", num_id=1, outline=0, name="heading 1"),
        _style("ListNumber", num_id=2, name="List Number"),
        _style("ListNumber2", based_on="ListNumber", ilvl=1, name="List Number 2"),
        _style("Localized2", outline=1, name="Titre 2"),
    ]))
    lvl = lambda i, fmt, text, pstyle="": (
        f'<w:lvl w:ilvl="{i}"><w:start w:val="1"/><w:numFmt w:val="{fmt}"/>'
        + (f'<w:pStyle w:val="{pstyle}"/>' if pstyle else "")
        + f'<w:lvlText w:val="{text}"/></w:lvl>')
    numbering = _part_xml("numbering", "".join([
        '<w:abstractNum w:abstractNumId="0">' + lvl(0, "decimal", "%1.", "Heading1")
        + lvl(1, "decimal", "%1.%2") + "</w:abstractNum>",
        '<w:abstractNum w:abstractNumId="1">' + lvl(0, "decimal", "%1.")
        + lvl(1, "lowerLetter", "(%2)") + "</w:abstractNum>",
        '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>',
        '<w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>',
    ]))
    body = "".join([
        _sp("Coverage", "Heading1"),
        _sp("First", "ListNumber"),
        _sp("Second", "ListNumber"),
        _sp("Sub a", "ListNumber2"),
        _sp("Third", "ListNumber"),
        _sp("Exclusions", "Heading1"),
        _sp("Localized heading", "Localized2"),
    ])
    texts = ["Coverage", "First", "Second", "Sub a", "Third", "Exclusions", "Localized heading"]
    notes = {
        0: "Heading1 style carries numPr (numId 1) and outlineLvl 0; ilvl comes from the "
           "w:lvl/w:pStyle link. Heading style still wins over numbering: kind heading, label 1.",
        1: "ListNumber style carries numPr numId 2: a list item, label 1.",
        3: "ListNumber2 is basedOn ListNumber: numId inherited, ilvl 1 from its own numPr: label (a).",
        4: "level 0 resumes after a deeper item: label 3.",
        6: "a non-English heading style has no name to match; only its style's outlineLvl (1) "
           "identifies it. Kind is paragraph until Turn 4 decides headings.",
    }
    sidecar = {
        "fixture": "style_numbering.docx",
        "labels_provenance": "spec",
        "clauses": ["8"],
        "known_gaps": [],
        "paragraphs": [
            _para(i, [(t, [])], accepted=t, original=t, superseded="", clause="8", note=notes.get(i))
            for i, t in enumerate(texts)
        ],
        "node_facts": [
            {"paragraph": 0, "kind": "heading", "style": "Heading1", "level": 0, "label": "1."},
            {"paragraph": 1, "kind": "list_item", "style": "ListNumber", "level": None, "label": "1."},
            {"paragraph": 2, "kind": "list_item", "style": "ListNumber", "level": None, "label": "2."},
            {"paragraph": 3, "kind": "list_item", "style": "ListNumber2", "level": None, "label": "(a)"},
            {"paragraph": 4, "kind": "list_item", "style": "ListNumber", "level": None, "label": "3."},
            {"paragraph": 5, "kind": "heading", "style": "Heading1", "level": 0, "label": "2."},
            {"paragraph": 6, "kind": "para", "style": "Localized2", "level": 1, "label": None},
        ],
    }
    return package(
        "style_numbering.docx", body, sidecar=sidecar, title="Style numbering",
        doc_rels=[("rId20", RT_STYLES, "styles.xml", None),
                  ("rId21", RT_NUMBERING, "numbering.xml", None)],
        extra_parts={"word/styles.xml": styles, "word/numbering.xml": numbering},
        ct_overrides=[("/word/styles.xml", CT_STYLES), ("/word/numbering.xml", CT_NUMBERING)],
    )


def _style_chain_cycle():
    """A basedOn cycle is recorded, never guessed; an undefined basedOn / style is silent."""
    styles = _part_xml("styles", "".join([
        _style("CycleA", based_on="CycleB", num_id=9),
        _style("CycleB", based_on="CycleA"),
        _style("Orphan", based_on="NoSuchStyle"),
    ]))
    body = _sp("in a cycle", "CycleA") + _sp("orphaned", "Orphan") + _sp("undefined style", "NotDefined")
    sidecar = {
        "fixture": "style_chain_cycle.docx",
        "labels_provenance": "spec",
        "clauses": ["8"],
        "known_gaps": ["style_chain_cycle"],
        "paragraphs": [
            _para(0, [("in a cycle", [])], accepted="in a cycle", original="in a cycle",
                  superseded="", clause="8",
                  note="CycleA<->CycleB: the chain stops at the cycle and is recorded; numId 9 "
                       "has no numbering.xml, so the paragraph is numbered but has no label"),
            _para(1, [("orphaned", [])], accepted="orphaned", original="orphaned",
                  superseded="", clause="8",
                  note="basedOn names a style styles.xml does not define: nothing to inherit, silent"),
            _para(2, [("undefined style", [])], accepted="undefined style",
                  original="undefined style", superseded="", clause="8",
                  note="its own style is undefined: silent, Word treats it as Normal"),
        ],
    }
    return package(
        "style_chain_cycle.docx", body, sidecar=sidecar, title="Style chain cycle",
        doc_rels=[("rId20", RT_STYLES, "styles.xml", None)],
        extra_parts={"word/styles.xml": styles},
        ct_overrides=[("/word/styles.xml", CT_STYLES)],
    )


BUILDERS = [
    _nested_revisions,
    _move,
    _hyperlink_and_fields,
    _content_controls,
    _breaks_and_specials,
    _comment_in_deletion,
    _deleted_paragraph_mark,
    _mixed_para_ids,
    _strict_namespaces,
    _renamed_comments_extended,
    _empty_parts,
    _text_box,
    _nested_field_in_instruction,
    _transparent_containers,
    _unrecognized_container,
    _revision_missing_id,
    _style_numbering,
    _style_chain_cycle,
]


def build(out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for builder in BUILDERS:
        name, parts, sidecar = builder()
        path, sidecar_path = _write(out, name, parts, sidecar)
        written.append((path, sidecar_path))
    return written


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", nargs="?", default="fixtures/model")
    args = parser.parse_args(argv)
    written = build(args.out)
    for path, sidecar in written:
        print("wrote", path.name, "and", sidecar.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
