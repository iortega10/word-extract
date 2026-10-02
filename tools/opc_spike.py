"""Turn 0b scaffold: the smallest OPC reader + document walker that can answer the
size question for Phase 1.

This is **not** production code and deliberately not the Phase 1 module layout.
Phase 1 Turn 1 (opc.py) and Turn 2a (walker.py) rewrite it against the contracts
Turn 0.5 revises; it exists to (a) prove the 0a fixture literals are reachable from
raw OOXML without a full model and (b) put a number on the work the real slices owe.

What it does:
  * resolves the document / comments / commentsExtended parts by relationship type
    (so a renamed part is still found),
  * normalises strict and transitional ``w`` namespaces to one,
  * walks each ``w:p`` into a per-paragraph *union* stream of ``Span(text, stack)``
    with distinct revision ids (``<kind>:<w:id>``) and computes the accepted /
    original / superseded view masks,
  * anchors comment ranges onto that stream,
  * prints every paragraph, then diffs the whole thing against the 0a sidecars.

Run:  python tools/opc_spike.py
"""
from pathlib import Path
import json
import sys
import zipfile
from dataclasses import dataclass, field

from lxml import etree

W_TRANS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W_STRICT = "http://purl.oclc.org/ooxml/wordprocessingml/main"
W_URIS = {W_TRANS, W_STRICT}
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
RT_OFFICE_DOC = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
RT_COMMENTS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
RT_COMMENTS_EXT = "http://schemas.microsoft.com/office/2011/relationships/commentsExtended"

# ISO strict writes the *relationship* URIs under purl.oclc.org too: one more place strict
# differs from transitional, found by running this spike over the strict fixture.
REL_STRICT = {
    "http://purl.oclc.org/ooxml/officeDocument/relationships/officeDocument": RT_OFFICE_DOC,
    "http://purl.oclc.org/ooxml/officeDocument/relationships/comments": RT_COMMENTS,
}


def canon_rel(rtype):
    return REL_STRICT.get(rtype, rtype)

DEL_FAMILY = ("del", "moveFrom")
INS_FAMILY = ("ins", "moveTo")
REVISIONS = ("ins", "del", "moveFrom", "moveTo")


@dataclass
class Span:
    text: str
    stack: tuple = ()


@dataclass
class Para:
    index: int
    spans: list = field(default_factory=list)
    para_id: str | None = None
    mark_revisions: list = field(default_factory=list)
    anchors: dict = field(default_factory=dict)      # comment id -> (start, end)
    move_groups: dict = field(default_factory=dict)  # span index -> group id
    boxes: list = field(default_factory=list)

    @property
    def union(self):
        return "".join(s.text for s in self.spans)

    def views(self):
        def fam(s):
            return {r.split(":")[0] for r in s.stack}

        accepted = "".join(s.text for s in self.spans if not fam(s) & set(DEL_FAMILY))
        original = "".join(s.text for s in self.spans if not fam(s) & set(INS_FAMILY))
        superseded = "".join(
            s.text for s in self.spans
            if fam(s) & set(DEL_FAMILY) and fam(s) & set(INS_FAMILY)
        )
        return accepted, original, superseded


# --- namespace helpers --------------------------------------------------------
def name(el):
    return etree.QName(el).localname


def is_w(el, local):
    return name(el) == local and etree.QName(el).namespace in W_URIS


def kids(el, local=None):
    for child in el:
        if not isinstance(child.tag, str):
            continue
        if local is None or name(child) == local:
            yield child


def wattr(el, local):
    """Value of the first attribute named ``local`` in a transitional or strict w namespace."""
    for key, value in el.attrib.items():
        q = etree.QName(key)
        if q.localname == local and q.namespace in W_URIS:
            return value
    for key, value in el.attrib.items():
        if etree.QName(key).localname == local:
            return value
    return None


def first_w(el, local):
    for child in kids(el, local):
        if is_w(child, local):
            return child
    return None


# --- OPC ----------------------------------------------------------------------
class Package:
    def __init__(self, path):
        self.path = Path(path)
        with zipfile.ZipFile(self.path) as z:
            self.parts = {n: z.read(n) for n in z.namelist() if not n.endswith("/")}
        self.trees = {n: etree.fromstring(b) for n, b in self.parts.items() if n.endswith(".xml")
                      or n.endswith(".rels")}

    def rels(self, source):
        base = "" if source is None else source.rsplit("/", 1)[0] + "/" if "/" in source else ""
        rel_path = f"{base}_rels/{source.rsplit('/', 1)[-1]}.rels" if source else "_rels/.rels"
        tree = self.trees.get(rel_path)
        out = []
        if tree is not None:
            for rel in kids(tree, "Relationship"):
                out.append((rel.get("Type"), rel.get("Target"), rel.get("TargetMode")))
        return out

    def by_type(self, source, rtype):
        base = (source.rsplit("/", 1)[0] + "/") if source and "/" in source else ""
        for rel_type, target, mode in self.rels(source):
            if canon_rel(rel_type) == rtype and (mode is None or mode == "Internal"):
                return self.trees.get(base + target.lstrip("/"))
        return None

    def document(self):
        return self.by_type(None, RT_OFFICE_DOC)


# --- run + inline text --------------------------------------------------------
def _sym(child):
    ch = child.get(f"{{{W_TRANS}}}char") or child.get(f"{{{W_STRICT}}}char")
    return chr(int(ch, 16)) if ch else ""


def run_text(r):
    out = []
    for child in r:
        ln = name(child)
        if ln == "rPr":
            continue
        if ln in ("t", "delText"):
            out.append((child.text or "").replace("\r", "\u000b").replace("\n", "\u000b"))
        elif ln == "tab":
            out.append("\t")
        elif ln in ("br", "cr"):
            out.append("\u000b")
        elif ln == "softHyphen":
            out.append("\u00ad")
        elif ln == "noBreakHyphen":
            out.append("\u2011")
        elif ln == "sym":
            out.append(_sym(child))
    return "".join(out)


def box_text(el):
    out = []
    for alt in _iter_local(el, "AlternateContent"):
        for branch in ("Choice", "Fallback"):
            for b in kids(alt, branch):
                for box in _iter_local(b, "txbxContent"):
                    for p in _iter_w(box):
                        out.append("".join(run_text(r) for r in kids(p, "r")))
            if out:
                break
    return out


def _iter_local(el, local):
    if name(el) == local:
        yield el
    for child in kids(el):
        yield from _iter_local(child, local)


def _iter_w(el, local=None):
    for child in kids(el):
        if is_w(child, "p"):
            yield child
        yield from _iter_w(child, local)


# --- document walk ------------------------------------------------------------
def walk_paragraph(p, index):
    para = Para(index=index, para_id=_para_id(p))
    stack = []
    move_names = []
    open_ranges = {}
    mark = first_w(p, "pPr")
    if mark is not None:
        rpr = first_w(mark, "rPr")
        if rpr is not None:
            for d in kids(rpr, "del"):
                if is_w(d, "del"):
                    para.mark_revisions.append(f"del:{wattr(d, 'id')}")

    def pos():
        return len(para.union)

    def emit(text):
        if not text:
            return
        if para.spans and para.spans[-1].stack == tuple(stack):
            para.spans[-1].text += text
        else:
            para.spans.append(Span(text, tuple(stack)))

    def walk(el):
        for child in el:
            if not isinstance(child.tag, str):
                continue
            ln = name(child)
            if ln == "pPr" or ln in ("rPr", "proofErr", "bookmarkStart", "bookmarkEnd",
                                     "lastRenderedPageBreak", "commentReference"):
                continue
            if ln in REVISIONS and is_w(child, ln):
                group = move_names[-1] if (ln.startswith("move") and move_names) else None
                ident = f"{ln}:{wattr(child, 'id')}"
                stack.append(ident)
                if group:
                    para.move_groups[len(para.spans)] = group
                walk(child)
                stack.pop()
            elif ln in ("moveFromRangeStart", "moveToRangeStart"):
                move_names.append(wattr(child, "name"))
            elif ln in ("moveFromRangeEnd", "moveToRangeEnd"):
                if move_names:
                    move_names.pop()
            elif ln == "commentRangeStart":
                open_ranges[wattr(child, "id")] = pos()
            elif ln == "commentRangeEnd":
                cid = wattr(child, "id")
                para.anchors[cid] = (open_ranges.get(cid, pos()), pos())
            elif ln == "r" and is_w(child, "r"):
                emit(run_text(child))
                para.boxes.extend(box_text(child))
            elif ln in ("hyperlink", "fldSimple", "smartTag"):
                walk(child)
            elif ln == "sdt":
                content = first_w(child, "sdtContent")
                if content is not None:
                    walk(content)
            elif ln == "AlternateContent":
                para.boxes.extend(box_text(p))
    walk(p)
    return para


def _para_id(p):
    for key, value in p.attrib.items():
        if etree.QName(key).localname == "paraId":
            return value
    return None


def walk_document(tree):
    body = first_w(tree, "body")
    paras = []

    def visit(el):
        for child in el:
            if not isinstance(child.tag, str):
                continue
            if is_w(child, "p"):
                paras.append(walk_paragraph(child, len(paras)))
            elif is_w(child, "sdt"):
                content = first_w(child, "sdtContent")
                if content is not None:
                    visit(content)
            elif is_w(child, "tbl"):
                visit(child)

    visit(body)
    return paras


# --- comments -----------------------------------------------------------------
def comments(pkg):
    out = []
    tree = pkg.by_type("word/document.xml", RT_COMMENTS)
    if tree is None:
        return out
    for c in kids(tree, "comment"):
        text = []
        para_id = None
        for p in _iter_w(c):
            if para_id is None:
                para_id = _para_id(p)
            text.append("".join(run_text(r) for r in kids(p, "r") if is_w(r, "r")))
        out.append({"id": wattr(c, "id"), "author": wattr(c, "author"),
                    "initials": wattr(c, "initials"), "para_id": para_id,
                    "text": "".join(text)})
    return out


def comments_extended(pkg):
    out = {}
    tree = pkg.by_type("word/document.xml", RT_COMMENTS_EXT)
    if tree is None:
        return out
    for c in kids(tree, "commentEx"):
        pid = c.get("{http://schemas.microsoft.com/office/word/2012/wordml}paraId")
        out[pid] = {"done": c.get("{http://schemas.microsoft.com/office/word/2012/wordml}done"),
                    "parent": c.get("{http://schemas.microsoft.com/office/word/2012/wordml}paraIdParent")}
    return out


# --- reporting ----------------------------------------------------------------
def show(pkg, label):
    print(f"\n=== {label} ({pkg.path.name}) ===")
    paras = walk_document(pkg.document())
    for para in paras:
        spans = " ".join(f"{s.text!r}{{{'+'.join(s.stack)}}}" for s in para.spans)
        acc, orig, sup = para.views()
        print(f"  [{para.index}] union={para.union!r}")
        print(f"       spans: {spans}")
        print(f"       accepted={acc!r} original={orig!r} superseded={sup!r}")
        if para.anchors:
            print(f"       comment anchors: {para.anchors}")
        if para.mark_revisions:
            print(f"       paragraph-mark revisions (not honored): {para.mark_revisions}")
        if para.boxes:
            print(f"       text-box fragments (preferred branch): {para.boxes}")
    cs = comments(pkg)
    if cs:
        ext = comments_extended(pkg)
        for c in cs:
            print(f"  comment {c['id']} paraId={c['para_id']} threaded={c['para_id'] in ext} {c['text']!r}")
    return paras


def compare(paras, sidecar):
    problems = []
    want = sidecar.get("paragraphs", [])
    if len(paras) != len(want):
        problems.append(f"paragraph count {len(paras)} != {len(want)}")
    for got, exp in zip(paras, want):
        if got.union != exp["union"]:
            problems.append(f"p{exp['index']} union {got.union!r} != {exp['union']!r}")
        got_spans = [(s.text, list(s.stack)) for s in got.spans]
        exp_spans = [(s["text"], s["stack"]) for s in exp["spans"]]
        if got_spans != exp_spans:
            problems.append(f"p{exp['index']} spans {got_spans} != {exp_spans}")
        if tuple(got.views()) != (exp["accepted"], exp["original"], exp["superseded"]):
            problems.append(f"p{exp['index']} views {got.views()} != "
                            f"{(exp['accepted'], exp['original'], exp['superseded'])}")
    return problems


def main(argv=None):
    argv = argv or sys.argv[1:]
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    fixtures = sorted(Path("fixtures/model").glob("*.docx")) if not argv else [Path(a) for a in argv]
    agreement = []
    for path in fixtures:
        pkg = Package(path)
        paras = show(pkg, "model fixture")
        sidecar_path = path.with_suffix(".expected.json")
        if sidecar_path.exists():
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
            problems = compare(paras, sidecar)
            agreement.append((path.name, problems))
    others = [Path("fixtures/spec_threaded.docx"), Path("fixtures/ledger_summary.docx"),
              Path("fixtures/edge_cases.docx"), Path("fixtures/program_review_v3.docx"),
              Path("local-private/review_sample.docx")]
    for path in others:
        if path.exists():
            show(Package(path), "corpus")
    print("\n=== agreement with the 0a literals ===")
    for name, problems in agreement:
        print(f"  {name:38s} {'OK' if not problems else 'MISMATCH'}")
        for problem in problems:
            print(f"      - {problem}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
