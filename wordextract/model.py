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
    ENDNOTE = "endnote"
    TEXTBOX = "textbox"
    COMMENT = "comment"


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
    """A half-open range of a part's union stream. ``part_id`` lives here (D2).

    ``fragment_id`` is set only for text-box content, which lives in its own
    fragment (host node id + ordinal) so host document order is undisturbed.
    """

    part_id: str
    start: int
    end: int
    fragment_id: str | None = None


@dataclass(frozen=True)
class ElementarySpan:
    """One tiling piece of a part's union stream: an offset range plus the
    ancestor stack of the text it covers, outermost first (D2)."""

    start: int
    end: int
    stack: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class UnionStream:
    """A part's union stream (D2): the literal union text plus the elementary
    spans that tile it exactly.

    A paragraph terminator is an elementary span whose text is a single newline
    character and whose stack is empty, so the spans tile the union exactly while
    ``Node.spans`` excludes a paragraph's own terminator. View text and
    view/union offset maps are **derived on demand** from this record -- there is
    no stored ``ViewText`` record.
    """

    part_id: str
    text: str
    spans: list[ElementarySpan] = field(default_factory=list)


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
    occurrence_index: int = 0
    host_node_id: str | None = None
    section_path: list[str] = field(default_factory=list)


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
    """Identity is the ``w14:paraId`` of the comment's last paragraph, never
    ``w:id`` (D4).

    ``para_id`` carries that identity. When the last paragraph has no
    ``w14:paraId`` it carries a ``hash:`` fallback over canonical (author, date,
    text, ordinal among equal hashes in document order); ``id_stability`` says
    which kind it is, and the parser always writes ``PARAID`` or
    ``CONTENT_HASH`` here (``PATH`` is only the dataclass default). The anchor
    start is deliberately **not** folded into the id: it is a union offset, so it
    would churn with ``textmodel_version``.
    """

    para_id: str
    author: str
    initials: str
    id_stability: IdStability = IdStability.PATH
    resolved_identity: str | None = None
    date: str | None = None
    anchor: Span | None = None
    anchor_text: str = ""
    #: The comment's own words -- what the reviewer wrote, not the text it is about
    #: (``anchor_text``). The body's paragraphs joined by ``"\n"``, without the terminator
    #: after the last one.
    text: str = ""
    #: The union range ``text`` was read from, in the **comments** part's own stream -- so
    #: the comment's words are addressable like any other text, even though no document
    #: node addresses them and no view holds them. ``None`` when that part did not stream.
    text_span: Span | None = None
    parent_id: str | None = None
    threading_status: ThreadingStatus = ThreadingStatus.UNKNOWN
    resolved: bool | None = None
    labels_provenance: LabelsProvenance = LabelsProvenance.GENERATOR


@dataclass(frozen=True)
class Chunk:
    """``content_hash`` is the hash of the chunk's own view text plus the
    ``textmodel_version``/``view_id`` that produced it; ``id`` is the hash of the
    canonical ``(content_hash, occurrence_index)`` pair, computed by the chunker.

    ``section_path`` is a field, **never** part of any key. The store key is
    ``(source_content_hash, chunk_id)``. ``context_hash`` (content + comment
    texts) is a summary-key input only.
    """

    id: str
    content_hash: str
    context_hash: str
    occurrence_index: int = 0
    section_path: list[str] = field(default_factory=list)
    node_ids: list[str] = field(default_factory=list)
    view_id: str = ""
    textmodel_version: str = ""
    size: int = 0
    heading_detection: HeadingDetection = HeadingDetection.NORMAL
    fired_rules: list[str] = field(default_factory=list)
    disputed_rules: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class HeadingDecision:
    """Per-node heading decision: every rule that fired, the winner, and the
    rules that disagreed. ``Chunk`` carries the aggregate, this carries the node
    the aggregate is made of (D3: no confidence scalar)."""

    node_id: str
    fired_rules: list[str] = field(default_factory=list)
    winner: str | None = None
    disputed_rules: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ViewSpan:
    """A hit's range in one view's text, in the **whole-part** view projection
    with terminators kept (D6). Not a union address."""

    view: View
    start: int
    end: int


@dataclass(frozen=True)
class Section:
    """One section of a document's outline: the heading that opens it, and what it holds
    (Turn 4b).

    ``heading_id`` is the ``HEADING`` node the section is named after and ``title`` is
    that heading's own text **in the accepted view** -- what the section is *called*,
    never an identity: a renamed heading renames its section, which is why
    ``section_path`` is a field and part of no key. ``level`` is Word's own numbering,
    where a heading at outline level *n* is level *n + 1* and ``Title`` is level 0 -- so
    a level compares across the tree even where the tree nests a title over a heading. A
    heading that states no level (the all-bold rule) takes the level one below the
    innermost levelled section open where it stands (1 when none is), so it nests where
    it is written instead of resetting the outline.

    ``node_ids`` are the nodes the section holds directly, in document order;
    containers included, so a consumer filters by ``Node.kind``. Nodes under a
    descendant section are the descendant's, not this one's, and the heading itself is
    ``heading_id`` rather than a member. ``children`` are the sections nested under it.
    """

    heading_id: str
    title: str
    level: int
    node_ids: list[str] = field(default_factory=list)
    children: list[Section] = field(default_factory=list)


@dataclass(frozen=True)
class ParseResult:
    """Everything the deterministic walker produces for one document: the union
    streams (the address space), the resolved node tree, the section tree the
    heading tree defines (4b), raw revision and comment facts, the heading
    decisions, and ``known_gaps`` as **ids only** (the prose lives in
    ``docs/design/phase1-gaps.md``)."""

    union_streams: list[UnionStream] = field(default_factory=list)
    nodes: list[Node] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    revisions: list[Revision] = field(default_factory=list)
    comments: list[Comment] = field(default_factory=list)
    heading_decisions: list[HeadingDecision] = field(default_factory=list)
    heading_detection: HeadingDetection = HeadingDetection.NORMAL
    known_gaps: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TermGroup:
    canonical: str
    synonyms: list[str] = field(default_factory=list)
    stemming: str | None = None
    rules: dict[str, Any] = field(default_factory=dict)
    tags: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class TermHit:
    """One hit of one term group in one location (D6).

    ``spans`` are union addresses; ``view_spans`` are the same hit's ranges in
    each view it occurs in, **sorted by view**, in the whole-part view projection
    (terminators kept). ``present_in`` is derivable from ``view_spans`` and must
    not disagree with it. A move yields one hit per location, both sharing
    ``move_group_id``; "one hit" is realized by query-time dedupe.
    """

    group: str
    present_in: set[View] = field(default_factory=set)
    spans: list[Span] = field(default_factory=list)
    view_spans: list[ViewSpan] = field(default_factory=list)
    node_id: str = ""
    location: LocationKind = LocationKind.BODY
    match_type: MatchType = MatchType.EXACT
    move_group_id: str | None = None
    context_node_id: str | None = None

    def __post_init__(self) -> None:
        views = [vs.view for vs in self.view_spans]
        if set(views) != set(self.present_in):
            raise ValueError(
                f"present_in {sorted(v.value for v in self.present_in)} disagrees with "
                f"view_spans views {sorted(v.value for v in set(views))}"
            )
        if len(views) != len(set(views)) or [v.value for v in views] != sorted(v.value for v in views):
            raise ValueError("view_spans must hold one entry per view, sorted by view")


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
