"""Generate one extra fixture whose ground-truth labels come from a written spec
rather than from the generator, hence ``labels_provenance: "spec"``.

The package is assembled from raw OOXML parts with ``zipfile`` (deliberately *not*
python-docx). It carries threaded comments -- ``word/commentsExtended.xml`` with a
reply (``w15:paraIdParent``) and a resolved flag (``w15:done="1"``) -- plus a tracked
change, wired through the package relationships and content types. Output is
byte-reproducible: comment dates and zip member timestamps are pinned.

Run:  python tools/make_spec_fixtures.py [fixtures]
"""
from pathlib import Path
import argparse
import json
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_fixtures import sidecar_for  # noqa: E402

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "w14": "http://schemas.microsoft.com/office/word/2010/wordml",
    "w15": "http://schemas.microsoft.com/office/word/2012/wordml",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}
W = NS["w"]

NAME = "spec_threaded.docx"
DATE = "2026-01-01T00:00:00Z"
WHEN = (2026, 1, 1, 0, 0, 0)

RM_COMMENTS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
RM_COMMENTS_EXT = "http://schemas.microsoft.com/office/2011/relationships/commentsExtended"
RM_OFFICE_DOC = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
RM_CORE_PROPS = "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties"

XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'


def _nsdecl(*prefixes):
    return " ".join(f'xmlns:{p}="{NS[p]}"' for p in prefixes)


def _run(text, *props):
    return f'<w:r>{"".join(props)}<w:t xml:space="preserve">{text}</w:t></w:r>'


def _content_types():
    return XML_DECL + (
        f'<Types {_nsdecl("ct")}>'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/comments.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"/>'
        '<Override PartName="/word/commentsExtended.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.commentsExtended+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        "</Types>"
    )


def _package_rels():
    return XML_DECL + (
        f'<Relationships {_nsdecl("pr")}>'
        f'<Relationship Id="rId1" Type="{RM_OFFICE_DOC}" Target="word/document.xml"/>'
        f'<Relationship Id="rId2" Type="{RM_CORE_PROPS}" Target="docProps/core.xml"/>'
        "</Relationships>"
    )


def _document_rels():
    return XML_DECL + (
        f'<Relationships {_nsdecl("pr")}>'
        f'<Relationship Id="rId1" Type="{RM_COMMENTS}" Target="comments.xml"/>'
        f'<Relationship Id="rId2" Type="{RM_COMMENTS_EXT}" Target="commentsExtended.xml"/>'
        "</Relationships>"
    )


def _core_props():
    return XML_DECL + (
        f'<cp:coreProperties {_nsdecl("cp", "dc", "dcterms", "xsi")}>'
        "<dc:title>Spec-Annotated Threaded Comments</dc:title>"
        "<dc:creator>word-extract fixtures</dc:creator>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{DATE}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{DATE}</dcterms:modified>'
        "</cp:coreProperties>"
    )


def _heading(text, style):
    return f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr>{_run(text)}</w:p>'


def _document():
    comment_ref = '<w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr><w:commentReference w:id="{cid}"/></w:r>'
    body = "".join([
        _heading("Spec-Annotated Threaded Comments", "Title"),
        _heading("Threaded Review", "Heading1"),
        "<w:p>"
        '<w:commentRangeStart w:id="1"/>'
        + _run("The endorsement ")
        + '<w:commentRangeEnd w:id="1"/>'
        + comment_ref.format(cid=1)
        + _run("extends coverage to completed operations for ")
        + f'<w:del w:id="10" w:author="D. Okafor" w:date="{DATE}"><w:r><w:delText>three years</w:delText></w:r></w:del>'
        + f'<w:ins w:id="11" w:author="D. Okafor" w:date="{DATE}"><w:r><w:t>thirty-six months</w:t></w:r></w:ins>'
        + _run(".")
        + "</w:p>",
        "<w:p>"
        '<w:commentRangeStart w:id="2"/>'
        + _run("Post-completion tail")
        + '<w:commentRangeEnd w:id="2"/>'
        + comment_ref.format(cid=2)
        + "</w:p>",
    ])
    return XML_DECL + f'<w:document {_nsdecl("w", "w14", "w15", "r", "wp")}><w:body>{body}</w:body></w:document>'


def _comments():
    def comment(cid, author, initials, para_id, text):
        return (
            f'<w:comment w:id="{cid}" w:author="{author}" w:initials="{initials}" w:date="{DATE}">'
            f'<w:p w14:paraId="{para_id}" w15:paraId="{para_id}">'
            '<w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr><w:annotationRef/></w:r>'
            f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>'
            "</w:p></w:comment>"
        )

    body = (
        comment(1, "D. Okafor", "DO", "00000001", "Does this include the tail after project completion?")
        + comment(2, "S. Ruiz", "SR", "00000002", "Yes - three-year completed-ops tail, now confirmed resolved.")
    )
    return XML_DECL + f'<w:comments {_nsdecl("w", "w14", "w15")}>{body}</w:comments>'


def _comments_extended():
    return XML_DECL + (
        f'<w15:commentsEx {_nsdecl("w15")}>'
        '<w15:commentEx w15:paraId="00000001" w15:done="0"/>'
        '<w15:commentEx w15:paraId="00000002" w15:done="1" w15:paraIdParent="00000001"/>'
        "</w15:commentsEx>"
    )


def _package():
    return {
        "[Content_Types].xml": _content_types(),
        "_rels/.rels": _package_rels(),
        "docProps/core.xml": _core_props(),
        "word/document.xml": _document(),
        "word/_rels/document.xml.rels": _document_rels(),
        "word/comments.xml": _comments(),
        "word/commentsExtended.xml": _comments_extended(),
    }


def build(out):
    out = Path(out)
    out.mkdir(exist_ok=True)
    path = out / NAME
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, xml in _package().items():
            info = zipfile.ZipInfo(name, date_time=WHEN)
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, xml.encode("utf-8"))

    sidecar = sidecar_for(path, provenance="spec")
    sidecar_path = out / f"{Path(NAME).stem}.expected.json"
    sidecar_path.write_text(
        json.dumps(sidecar, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8"
    )
    return path, sidecar_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", nargs="?", default="fixtures")
    args = parser.parse_args(argv)
    path, sidecar = build(args.out)
    print("wrote", path.name, "and", sidecar.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
