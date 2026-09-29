"""Load fixture sidecars (ground-truth labels) with no dependency on the implementation.

Nothing here may import parser/implementation code. The labels are read straight from the
JSON the fixture generators wrote, so a parser bug can never be shared with the checker:
if the labels and the output agree, it is because both are right, not because both are the
same code. Labels are never edited to match output (design D11).

Deliberately stdlib-only (json/dataclasses/pathlib) so it can be loaded in isolation.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json

SIDECAR_SUFFIX = ".expected.json"
PROVENANCES = ("human", "generator", "spec")


@dataclass(frozen=True)
class CommentLabel:
    id: str
    para_id: str | None
    author: str
    initials: str
    text: str
    anchor_text: str


@dataclass(frozen=True)
class RevisionLabel:
    kind: str
    author: str
    text: str


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
class Sidecar:
    """One ``<name>.expected.json`` and the fixture it labels."""

    fixture: str
    labels_provenance: str
    comments: tuple[CommentLabel, ...]
    revisions: tuple[RevisionLabel, ...]
    sections: tuple[SectionLabel, ...]
    tables: tuple[TableLabel, ...]
    path: Path | None = None

    @property
    def name(self) -> str:
        return self.fixture[: -len(".docx")] if self.fixture.endswith(".docx") else self.fixture


def _require(data: dict, key: str) -> object:
    if key not in data:
        raise ValueError(f"sidecar is missing {key!r}")
    return data[key]


def sidecar_from_dict(data: dict, path: Path | None = None) -> Sidecar:
    provenance = _require(data, "labels_provenance")
    if provenance not in PROVENANCES:
        raise ValueError(f"unknown labels_provenance: {provenance!r}")
    return Sidecar(
        fixture=str(_require(data, "fixture")),
        labels_provenance=str(provenance),
        comments=tuple(CommentLabel(**c) for c in _require(data, "comments")),
        revisions=tuple(RevisionLabel(**r) for r in _require(data, "revisions")),
        sections=tuple(SectionLabel(**s) for s in _require(data, "sections")),
        tables=tuple(
            TableLabel(t["rows"], t["columns"], tuple(tuple(m) for m in t["merges"]))
            for t in _require(data, "tables")
        ),
        path=path,
    )


def load_sidecar(path: str | Path) -> Sidecar:
    path = Path(path)
    return sidecar_from_dict(json.loads(path.read_text(encoding="utf-8")), path=path)


def iter_sidecars(fixtures_dir: str | Path) -> dict[str, Sidecar]:
    """All sidecars in a fixtures directory, keyed by fixture stem, name-ordered."""
    fixtures_dir = Path(fixtures_dir)
    found = {}
    for path in sorted(fixtures_dir.glob(f"*{SIDECAR_SUFFIX}")):
        found[path.name[: -len(SIDECAR_SUFFIX)]] = load_sidecar(path)
    return found
