"""Load fixture sidecars (ground-truth labels) with no dependency on the implementation.

Nothing here may import parser/implementation code. The labels are read straight from the
JSON the fixture generators (or a human) wrote, so a parser bug can never be shared with
the checker: if the labels and the output agree, it is because both are right, not because
both are the same code. Labels are never edited to match output (design D11).

Two sidecar formats share this loader:

- the root fixture sidecars (``fixtures/*.expected.json``): ``comments`` / ``revisions`` /
  ``sections`` / ``tables``, written by ``tools/make_fixtures.py`` and
  ``tools/make_spec_fixtures.py``;
- the hand-typed model sidecars (``fixtures/model/*.expected.json``): ``paragraphs`` with
  the 0a literal union text, elementary-span offsets/stacks and the three view strings,
  plus the annotations that pin one construct each.

Required keys are required in both, an unknown key is an error (never a silent drop), and
anything the loader does not model structurally is kept verbatim in
:attr:`Sidecar.annotations`.

Deliberately stdlib-only (json/dataclasses/pathlib) so it can be loaded in isolation.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
import json

SIDECAR_SUFFIX = ".expected.json"
PROVENANCES = ("human", "generator", "spec")


@dataclass(frozen=True)
class SpanLabel:
    """One labelled elementary span: union offsets, ancestor stack (outer first),
    and the literal union text it covers."""

    start: int
    end: int
    stack: tuple[str, ...] = ()
    text: str = ""


@dataclass(frozen=True)
class CommentLabel:
    id: str
    para_id: str | None
    author: str
    initials: str
    anchor_text: str
    text: str | None = None
    anchor: SpanLabel | None = None
    clauses: tuple[str, ...] = ()
    parent_id: str | None = None
    resolved: bool | None = None
    threading_status: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class RevisionLabel:
    kind: str
    author: str
    text: str | None = None
    id: str | None = None
    date: str | None = None
    move_group_id: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class SectionLabel:
    text: str
    level: int
    order: int


@dataclass(frozen=True)
class TableLabel:
    rows: int
    columns: int
    merges: tuple[tuple[int, int, int, int], ...]


@dataclass(frozen=True)
class ParagraphLabel:
    """One paragraph's labelled union text, tiling spans, and view strings."""

    index: int
    union: str
    spans: tuple[SpanLabel, ...]
    accepted: str
    original: str
    superseded: str
    clause: str
    note: str | None = None


# Model-sidecar keys the loader keeps verbatim: they pin one construct each and are
# read by the tests that care, not by the loader.
ANNOTATION_KEYS = (
    "assumptions",
    "block_nodes",
    "doc_rels",
    "fragments",
    "id_expectations",
    "move_groups",
    "namespaces",
    "node_facts",
    "parts",
)


@dataclass(frozen=True)
class Sidecar:
    """One ``<name>.expected.json`` and the fixture it labels.

    Fields are optional where a sidecar format legitimately omits them (a model
    sidecar has no ``sections``; a root sidecar has no ``paragraphs``).
    """

    fixture: str
    labels_provenance: str
    comments: tuple[CommentLabel, ...] = ()
    revisions: tuple[RevisionLabel, ...] = ()
    sections: tuple[SectionLabel, ...] = ()
    tables: tuple[TableLabel, ...] = ()
    paragraphs: tuple[ParagraphLabel, ...] = ()
    clauses: tuple[str, ...] = ()
    span_rule: str | None = None
    terminator: str | None = None
    known_gaps: tuple[str, ...] = ()
    annotations: dict[str, object] = field(default_factory=dict)
    path: Path | None = None

    @property
    def name(self) -> str:
        return self.fixture[: -len(".docx")] if self.fixture.endswith(".docx") else self.fixture


def _require(data: dict, key: str) -> object:
    if key not in data:
        raise ValueError(f"sidecar is missing {key!r}")
    return data[key]


def _record(tp: type, data: dict, **convert):
    """Build one label record from a sidecar object; an unknown key is an error."""
    unknown = sorted(set(data) - {f.name for f in fields(tp)})
    if unknown:
        raise ValueError(f"unknown keys for {tp.__name__}: {unknown}")
    return tp(**{k: convert[k](v) if k in convert else v for k, v in data.items()})


def _span(data: dict) -> SpanLabel:
    return _record(SpanLabel, data, stack=tuple)


def _comment(data: dict) -> CommentLabel:
    return _record(CommentLabel, data, anchor=_span, clauses=tuple)


def _revision(data: dict) -> RevisionLabel:
    return _record(RevisionLabel, data)


def _table(data: dict) -> TableLabel:
    return _record(TableLabel, data, merges=lambda ms: tuple(tuple(m) for m in ms))


def _paragraph(data: dict) -> ParagraphLabel:
    return _record(
        ParagraphLabel, data, spans=lambda ss: tuple(_span(s) for s in ss)
    )


def sidecar_from_dict(data: dict, path: Path | None = None) -> Sidecar:
    provenance = _require(data, "labels_provenance")
    if provenance not in PROVENANCES:
        raise ValueError(f"unknown labels_provenance: {provenance!r}")
    unknown = sorted(
        set(data)
        - {
            "fixture",
            "labels_provenance",
            "comments",
            "revisions",
            "sections",
            "tables",
            "paragraphs",
            "clauses",
            "span_rule",
            "terminator",
            "known_gaps",
            *ANNOTATION_KEYS,
        }
    )
    if unknown:
        raise ValueError(f"unknown sidecar keys: {unknown}")
    return Sidecar(
        fixture=str(_require(data, "fixture")),
        labels_provenance=str(provenance),
        comments=tuple(_comment(c) for c in data.get("comments", ())),
        revisions=tuple(_revision(r) for r in data.get("revisions", ())),
        sections=tuple(_record(SectionLabel, s) for s in data.get("sections", ())),
        tables=tuple(_table(t) for t in data.get("tables", ())),
        paragraphs=tuple(_paragraph(p) for p in data.get("paragraphs", ())),
        clauses=tuple(data.get("clauses", ())),
        span_rule=data.get("span_rule"),
        terminator=data.get("terminator"),
        known_gaps=tuple(data.get("known_gaps", ())),
        annotations={k: data[k] for k in ANNOTATION_KEYS if k in data},
        path=path,
    )


def load_sidecar(path: str | Path) -> Sidecar:
    path = Path(path)
    return sidecar_from_dict(json.loads(path.read_text(encoding="utf-8")), path=path)


def iter_sidecars(fixtures_dir: str | Path) -> dict[str, Sidecar]:
    """Every sidecar under a fixtures directory (recursively), keyed by fixture
    stem, name-ordered. Two sidecars for one stem is an error, never a silent
    overwrite."""
    fixtures_dir = Path(fixtures_dir)
    found: dict[str, Sidecar] = {}
    for path in sorted(fixtures_dir.rglob(f"*{SIDECAR_SUFFIX}")):
        stem = path.name[: -len(SIDECAR_SUFFIX)]
        if stem in found:
            raise ValueError(f"two sidecars for {stem!r}: {found[stem].path} and {path}")
        found[stem] = load_sidecar(path)
    return found
