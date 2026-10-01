"""Turn 6e: measure the cross-paragraph miss -- a report-only permissive matcher.

The matcher is paragraph-bounded by construction: a part is matched one paragraph per
view, and ``match_text`` splits on the terminator, so a term is never even *offered* two
paragraphs at once (``text-model-spec.md`` section 4; design D6). That is the right
default -- matching the raw union invents adjacencies no view asserts -- but it leaves a
question the design says to *measure* rather than assume: does real wording ever put a
term's words on both sides of a paragraph break, where the strict matcher is blind to
them? (design D6, "cross-paragraph matching: deferred, with the miss **measured**".)

This script answers it. It is a **permissive** cross-paragraph matcher: the same
normalization, the same registry, the same hyphen and stem readings, but each part's
whole view text is scanned as one buffer with the terminator read as an ordinary
separator, so a term's tokens *can* join across a break. Every hit that spans at least one
break is a phrase the strict matcher missed by construction; the script runs the strict
matcher too (``match_document``) and records, per finding, whether the strict matcher
really had **no** hit on that same union text -- so the claim "the real matcher missed it"
is checked against the real matcher, not assumed from the theory.

It runs **offline over the golden set only**: the fixtures that carry a sidecar (their
ground truth), plus any documents in ``fixtures/real``. It changes nothing -- it is *not*
package code, and a non-zero find is a finding to *promote cross-paragraph matching
deliberately*, never an edit made here. The report is the whole point; nothing in
``wordextract`` imports this module.

Run:  python tools/crossparagraph_report.py [fixtures] [--registry PATH] [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, replace
from pathlib import Path

# The tool is run from the repo root, where neither package is on the path outside pytest
# (the root ``pyproject`` supplies both via ``pythonpath``). Put them there, as the
# ``python -m`` entry points rely on an editable install of the core to do.
_ROOT = Path(__file__).resolve().parent.parent
for _path in (_ROOT, _ROOT / "docextract-core"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from wordextract import opc  # noqa: E402
from wordextract.evals.labels import iter_sidecars  # noqa: E402
from wordextract.model import NodeKind, ParseResult, Span, TermHit, View  # noqa: E402
from wordextract.terms import (  # noqa: E402
    PARAGRAPH_BOUNDARY,
    TermIndex,
    TermMatch,
    TermRegistry,
    _MATCH_VIEWS,
    _ancestor_of_kind,
    _parents,
    compile_registry,
    dedupe_hits,
    load_registry_text,
    match_document,
    match_text,
    term_list_hash,
)
from wordextract.versions import MATCHER_VERSION  # noqa: E402
from wordextract.views import project  # noqa: E402
from wordextract.walker import walk_document  # noqa: E402

#: Below this many strict hits a run is too thin to say anything about what the strict
#: matcher misses; the report says so rather than concluding from it.
MIN_STRICT_HITS = 20

#: How a terminator is shown in the human report: the phrase reads across a break, so the
#: break is drawn where it was rather than hidden. JSON keeps the bare ``"\\n"``.
BOUNDARY_MARK = "\u00b6"

#: The view a comment body is reported under. A comment's words are in no view (6c), but
#: the report still has to say where a finding was, and ``"comment"`` is not a ``View``.
COMMENT_VIEW = "comment"


@dataclass(frozen=True)
class Finding:
    """One cross-paragraph phrase: a term whose words straddle a paragraph break.

    ``views`` is every view holding the phrase, a single-entry tuple for the usual case and
    both entries when no revision markup separates them -- the same fold ``match_document``
    makes, so a phrase held in two views is one finding, not two. ``spans`` are union
    addresses in ``part_id`` (the space a ``TermHit``'s ``spans`` live in), ``phrase`` is the
    literal text the hit is made of -- the break still in it, so a caller can see exactly
    what joined -- and ``missed`` says the strict matcher reported no hit on that same
    union text. ``missed`` is true by construction (a phrase crossing a break can never be a
    strict hit's own spans), so two fields say what it cannot:

    * ``group_hit_nearby`` -- the strict matcher *did* report this group on some of the
      very text the phrase covers (a shorter synonym on one side of the break), so what it
      missed is a longer form, not the term;
    * ``boundary`` -- what kind of break the phrase crosses, because the three are not the
      same evidence: ``cell`` (the words sit in different table cells, which is a layout
      adjacency, almost never one phrase), ``heading`` (a heading and the text under it)
      and ``paragraph`` (two ordinary paragraphs -- the case this measurement is for,
      e.g. a document whose lines were hard-wrapped into paragraphs).
    """

    document: str
    part_id: str
    views: tuple[str, ...]
    group: str
    match_type: str
    phrase: str
    spans: tuple[Span, ...]
    missed: bool
    boundary: str = "paragraph"
    group_hit_nearby: bool = False

    def to_dict(self) -> dict:
        return {
            "part_id": self.part_id,
            "views": list(self.views),
            "group": self.group,
            "match_type": self.match_type,
            "phrase": self.phrase,
            "spans": [{"start": span.start, "end": span.end} for span in self.spans],
            "missed": self.missed,
            "boundary": self.boundary,
            "group_hit_nearby": self.group_hit_nearby,
        }


# --- the permissive matcher ----------------------------------------------------------

def flatten(text: str) -> str:
    """``text`` with each paragraph terminator read as an ordinary separator.

    The terminator is one character, so replacing it (rather than deleting it) keeps every
    offset the same: a hit's coordinates here are the projection's own, and the boundary it
    crossed is still at the offset it was. That is what lets
    :func:`cross_boundary_matches` tell a cross-paragraph find from an in-paragraph one by
    simply looking for the terminator under the hit.
    """
    return text.replace(PARAGRAPH_BOUNDARY, " ")


def permissive_matches(index: TermIndex, text: str) -> list[TermMatch]:
    """Every registry hit in ``text`` with paragraph boundaries flattened away.

    ``match_text`` splits on the terminator; given the flattened text there is none to
    split on, so the whole string is one boundary-free buffer and a form's tokens may start
    in one paragraph and end in the next.
    """
    return match_text(index, flatten(text))


def cross_boundary_matches(index: TermIndex, text: str) -> list[TermMatch]:
    """The permissive hits that actually span at least one paragraph boundary."""
    return [
        match
        for match in permissive_matches(index, text)
        if PARAGRAPH_BOUNDARY in text[match.start : match.end]
    ]


def _key(group: str, spans: tuple[Span, ...]) -> tuple:
    """The addressing two matchers share: a group at a set of union spans in one part."""
    return (group, tuple((span.part_id, span.start, span.end) for span in spans))


def _strict_keys(hits: list[TermHit]) -> set[tuple]:
    return {_key(hit.group, tuple(hit.spans)) for hit in hits}


# --- what kind of break a finding crosses --------------------------------------------

BOUNDARY_KINDS = ("cell", "heading", "paragraph")


def classify_boundary(parsed: ParseResult, spans: tuple[Span, ...]) -> str:
    """The kind of break a phrase crosses, read off the nodes at its two ends.

    ``cell`` when either end sits in a table cell and the two ends are not in the same one;
    otherwise ``heading`` when either end is a heading; otherwise ``paragraph``. Judged at the
    ends because a phrase that starts in one node and ends in another is what crossed; the
    nodes in between are the phrase's own words.
    """
    if not spans:
        return "paragraph"
    by_id = {node.id: node for node in parsed.nodes}
    parents = _parents(parsed)
    first, last = spans[0], spans[-1]

    def node_at(offset: int):
        best = None
        for node in parsed.nodes:
            if node.kind not in (NodeKind.PARA, NodeKind.HEADING, NodeKind.LIST_ITEM):
                continue
            for span in node.spans:
                if span.part_id == first.part_id and span.start <= offset < span.end:
                    best = node
        return best

    start = node_at(first.start)
    end = node_at(last.end - 1)
    if start is None or end is None:
        return "paragraph"
    start_cell = _ancestor_of_kind(start.id, by_id, parents, NodeKind.CELL)
    end_cell = _ancestor_of_kind(end.id, by_id, parents, NodeKind.CELL)
    if (start_cell is not None or end_cell is not None) and (
        start_cell is None or end_cell is None or start_cell.id != end_cell.id
    ):
        return "cell"
    if NodeKind.HEADING in (start.kind, end.kind):
        return "heading"
    return "paragraph"


def group_hit_nearby(strict_hits: list[TermHit], group: str, spans: tuple[Span, ...]) -> bool:
    """Whether the strict matcher reported ``group`` on any text the phrase covers."""
    if not spans:
        return False
    part_id = spans[0].part_id
    low = min(span.start for span in spans)
    high = max(span.end for span in spans)
    return any(
        hit.group == group
        and any(s.part_id == part_id and s.start < high and s.end > low for s in hit.spans)
        for hit in strict_hits
    )


# --- scanning one document -----------------------------------------------------------

def scan_document(
    index: TermIndex, parsed: ParseResult, strict_hits: list[TermHit], name: str
) -> list[Finding]:
    """Every cross-paragraph phrase of ``parsed``, checked against ``strict_hits``.

    Parts are scanned in the walker's order and each in the two views a hit can be in
    (:data:`wordextract.terms._MATCH_VIEWS`), then the comment bodies -- the one text a
    package streams that is in no view (6c). A phrase found in both views is **one**
    finding whose ``views`` names both; ``missed`` is True whenever no strict hit is keyed
    to the same union spans, which is the finding. A False would mean the strict matcher
    already reported that text, and the pair would need looking at.
    """
    strict = _strict_keys(strict_hits)
    merged: dict[tuple, Finding] = {}

    def record(
        part_id: str,
        view: str,
        match: TermMatch,
        phrase: str,
        spans: tuple[Span, ...],
    ) -> None:
        key = _key(match.group, spans)
        found = merged.get(key)
        if found is None:
            merged[key] = Finding(
                document=name,
                part_id=part_id,
                views=(view,),
                group=match.group,
                match_type=match.match_type.value,
                phrase=phrase,
                spans=spans,
                missed=key not in strict,
                boundary=classify_boundary(parsed, spans),
                group_hit_nearby=group_hit_nearby(strict_hits, match.group, spans),
            )
        else:
            merged[key] = replace(found, views=tuple(sorted({*found.views, view})))

    for stream in parsed.union_streams:
        for view in _MATCH_VIEWS:
            projection = project(stream, view)
            for match in cross_boundary_matches(index, projection.text):
                record(
                    stream.part_id,
                    view.value,
                    match,
                    projection.text[match.start : match.end],
                    tuple(projection.union_spans(match.start, match.end)),
                )
    for comment in parsed.comments:
        address = comment.text_span
        if address is None:
            continue
        for match in cross_boundary_matches(index, comment.text):
            record(
                address.part_id,
                COMMENT_VIEW,
                match,
                comment.text[match.start : match.end],
                (
                    Span(
                        part_id=address.part_id,
                        start=address.start + match.start,
                        end=address.start + match.end,
                    ),
                ),
            )
    return list(merged.values())


# --- the golden set and the report ---------------------------------------------------

def golden_documents(fixtures_dir: str | Path) -> list[Path]:
    """The documents to measure: every sidecar-backed fixture, plus any real document.

    A sidecar is what makes a fixture *golden* -- the generator or a spec wrote down its
    facts, so it names a document this tool can scan without guessing. ``fixtures/real``
    holds the documents the measurement is really for; they carry no sidecar (their labels
    are the blocked human input), and a document that simply exists is still worth scanning.
    """
    fixtures_dir = Path(fixtures_dir)
    documents: list[Path] = []
    for sidecar in iter_sidecars(fixtures_dir).values():
        if sidecar.path is None:
            continue
        document = sidecar.path.parent / sidecar.fixture
        if document.is_file():
            documents.append(document)
    # The samples carry no sidecar yet (their labels are the blocked human input) but they are
    # the most realistic documents here, so they are measured too; ``real`` is what the
    # measurement is ultimately for.
    for extra in ("samples", "real"):
        extra_dir = fixtures_dir / extra
        if extra_dir.is_dir():
            documents.extend(sorted(extra_dir.glob("*.docx")))
    return sorted(set(documents))


def build_report(fixtures_dir: str | Path, registry_path: str | Path) -> dict:
    """The whole report: per document, the strict hits and the cross-paragraph finds.

    Nothing here writes to disk or to the package; the return value is the report, and
    :func:`format_report` or ``json.dumps`` renders it. ``promote`` is the design's verdict
    line: a non-zero find is the trigger to promote cross-paragraph matching, so it is
    stated rather than left for the reader to compute.
    """
    registry_path = Path(registry_path)
    registry: TermRegistry = load_registry_text(registry_path.read_text(encoding="utf-8"))
    index = compile_registry(registry)
    documents: list[dict] = []
    strict_total = 0
    finds_total = 0
    for document in golden_documents(fixtures_dir):
        parsed = walk_document(opc.Package(document))
        strict = dedupe_hits(match_document(index, parsed))
        findings = scan_document(index, parsed, strict, str(document))
        strict_total += len(strict)
        finds_total += len(findings)
        documents.append(
            {
                "document": str(document),
                "strict_hits": len(strict),
                "cross_paragraph": [finding.to_dict() for finding in findings],
            }
        )
    all_findings = [f for d in documents for f in d["cross_paragraph"]]
    by_boundary = {kind: sum(1 for f in all_findings if f["boundary"] == kind) for kind in BOUNDARY_KINDS}
    real_documents = sum(1 for d in documents if Path(d["document"]).parent.name == "real")
    synthetic_registry = registry_path.name.startswith("synthetic")
    reasons = []
    if synthetic_registry:
        reasons.append("the registry is the synthetic example, not a real term list")
    if real_documents == 0:
        reasons.append("no real document is in the set")
    if strict_total < MIN_STRICT_HITS:
        reasons.append(f"only {strict_total} strict hits (fewer than {MIN_STRICT_HITS})")
    return {
        "matcher_version": MATCHER_VERSION,
        "term_list_hash": term_list_hash(registry),
        "registry": str(registry_path),
        "documents": documents,
        "totals": {
            "documents": len(documents),
            "real_documents": real_documents,
            "strict_hits": strict_total,
            "cross_paragraph_finds": finds_total,
            "by_boundary": by_boundary,
        },
        "evidence": {"adequate": not reasons, "limits": reasons},
        # Promotion is argued by phrases across paragraph or heading breaks; a phrase across
        # two table cells is a layout adjacency and is not evidence for it.
        "promote": by_boundary["paragraph"] + by_boundary["heading"] > 0,
    }


def format_report(report: dict) -> str:
    """The report as text: one line per finding, then the totals and the verdict."""
    totals = report["totals"]
    lines = [
        "cross-paragraph report (Turn 6e) -- report only, nothing promoted",
        f"matcher {report['matcher_version']}   term list {report['term_list_hash'][:16]}"
        f"   registry {report['registry']}",
        f"golden set: {totals['documents']} documents, {totals['strict_hits']} strict hits",
        "",
    ]
    for document in report["documents"]:
        if not document["cross_paragraph"]:
            continue
        lines.append(f"{document['document']}  (strict hits: {document['strict_hits']})")
        for finding in document["cross_paragraph"]:
            phrase = finding["phrase"].replace(PARAGRAPH_BOUNDARY, f" {BOUNDARY_MARK} ")
            spans = ", ".join(f"[{span['start']},{span['end']})" for span in finding["spans"])
            lines.append(
                f"  {finding['part_id']} {'/'.join(finding['views'])}  {finding['group']}"
                f" ({finding['match_type']})  \"{phrase}\"  union {spans}"
                f"  missed={finding['missed']}"
            )
        lines.append("")
    lines.append(
        f"totals: strict hits {totals['strict_hits']}, "
        f"cross-paragraph finds {totals['cross_paragraph_finds']} "
        f"(cell {totals['by_boundary']['cell']}, heading {totals['by_boundary']['heading']}, "
        f"paragraph {totals['by_boundary']['paragraph']})"
    )
    if report["promote"]:
        lines.append(
            "verdict: cross-paragraph phrases found across paragraph/heading breaks -- "
            "promote cross-paragraph matching"
        )
    elif totals["cross_paragraph_finds"]:
        lines.append(
            "verdict: only table-cell adjacencies found -- not evidence for promoting "
            "cross-paragraph matching"
        )
    elif report["evidence"]["adequate"]:
        lines.append(
            "verdict: no cross-paragraph phrase in the golden set -- matching stays"
            " paragraph-bounded"
        )
    else:
        lines.append(
            "verdict: INCONCLUSIVE -- nothing found, but this run cannot show that such "
            "phrases do not occur:"
        )
        lines.extend(f"  - {reason}" for reason in report["evidence"]["limits"])
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("fixtures", nargs="?", default="fixtures")
    parser.add_argument(
        "--registry",
        default=None,
        help="term registry to match with"
        " (default: <fixtures>/terms/synthetic.example.json)",
    )
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = parser.parse_args(argv)
    fixtures_dir = Path(args.fixtures)
    registry_path = (
        Path(args.registry)
        if args.registry is not None
        else fixtures_dir / "terms" / "synthetic.example.json"
    )
    report = build_report(fixtures_dir, registry_path)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
