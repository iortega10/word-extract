"""Frozen record contracts for word-extract (fields from design D2-D8).

Nothing here parses a document or reads a fixture; these are the shapes the
deterministic parser, chunker, matcher and store agree on. Encoded/decoded
through the strict core codec.

Address space: text addresses are ``Span(part_id, start, end)`` in a part's
union coordinate space (design D2). A "view" is a *mask* over that union, never
the content (D2/D5); the union is only an address space.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .versions import HashedInputs


class NodeKind(str, Enum):
    HEADING = "heading"
    PARA = "para"
    LIST_ITEM = "list_item"
    TABLE = "table"
    ROW = "row"
    CELL = "cell"
    HEADER = "header"
    FOOTER = "footer"
    FOOTNOTE = "footnote"
    SDT = "sdt"


class IdStability(str, Enum):
    """How a node id was derived; ``path`` ids renumber on insertion (D3)."""

    PARAID = "paraid"
    CONTENT_HASH = "content_hash"
    PATH = "path"


class RevisionKind(str, Enum):
    INS = "ins"
    DEL = "del"
    MOVE_FROM = "moveFrom"
    MOVE_TO = "moveTo"


class ThreadingStatus(str, Enum):
    VERIFIED = "verified"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class View(str, Enum):
    ACCEPTED = "accepted"
    ORIGINAL = "original"
    SUPERSEDED = "superseded"


class MatchType(str, Enum):
    EXACT = "exact"
    SYNONYM = "synonym"
    STEM = "stem"


class LocationKind(str, Enum):
    BODY = "body"
    TABLE_CELL = "table_cell"
    HEADER = "header"
    FOOTER = "footer"
    FOOTNOTE = "footnote"


class HeadingDetection(str, Enum):
    NORMAL = "normal"
    DEGRADED = "degraded"


class CacheStatus(str, Enum):
    HIT = "hit"
    MISS = "miss"


class LabelsProvenance(str, Enum):
    """Where a label-carrying record's labels came from (design D11)."""

    HUMAN = "human"
    GENERATOR = "generator"
    SPEC = "spec"


@dataclass(frozen=True)
class Span:
    """A half-open range of a part's union stream. ``part_id`` lives here (D2)."""

    part_id: str
    start: int
    end: int


@dataclass(frozen=True)
class Node:
    id: str
    kind: NodeKind
    part_id: str
    source_ref: str
    spans: list[Span] = field(default_factory=list)
    style: str | None = None
    level: int | None = None
    numbering_label: str | None = None
    child_ids: list[str] = field(default_factory=list)
    id_stability: IdStability = IdStability.PATH


@dataclass(frozen=True)
class Revision:
    id: str
    kind: RevisionKind
    author: str
    date: str | None = None
    move_group_id: str | None = None
    ancestors: list[str] = field(default_factory=list)
    labels_provenance: LabelsProvenance = LabelsProvenance.GENERATOR


@dataclass(frozen=True)
class Comment:
    """Keyed on the ``w14:paraId`` of the comment's last paragraph, not ``w:id`` (D4)."""

    para_id: str
    author: str
    initials: str
    resolved_identity: str | None = None
    date: str | None = None
    anchor: Span | None = None
    anchor_text: str = ""
    parent_id: str | None = None
    threading_status: ThreadingStatus = ThreadingStatus.UNKNOWN
    resolved: bool | None = None
    labels_provenance: LabelsProvenance = LabelsProvenance.GENERATOR


@dataclass(frozen=True)
class Chunk:
    id: str
    context_hash: str
    section_path: list[str] = field(default_factory=list)
    node_ids: list[str] = field(default_factory=list)
    view_id: str = ""
    textmodel_version: str = ""
    size: int = 0
    heading_detection: HeadingDetection = HeadingDetection.NORMAL
    fired_rules: list[str] = field(default_factory=list)
    disputed_rules: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TermGroup:
    canonical: str
    synonyms: list[str] = field(default_factory=list)
    stemming: str | None = None
    rules: dict[str, Any] = field(default_factory=dict)
    tags: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class TermHit:
    group: str
    present_in: set[View] = field(default_factory=set)
    spans: list[Span] = field(default_factory=list)
    view_span: Span | None = None
    node_id: str = ""
    location: LocationKind = LocationKind.BODY
    match_type: MatchType = MatchType.EXACT


@dataclass(frozen=True)
class ArtifactCache:
    artifact_id: str
    status: CacheStatus
    recomputed_key: str


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    hashed_inputs: HashedInputs
    recorded_inputs: dict[str, str] = field(default_factory=dict)
    artifact_cache: list[ArtifactCache] = field(default_factory=list)
