"""Turn 5: chunker v1 -- the retrieval units the outline and a size cap define.

Chunking is the last derived shape before the store: a chunk is what a summary is keyed
to and what a term hit is reported against. v1 is deliberately structural -- one pass
over the body part's block sequence, no tokenizer and no model (D7) -- so one package
always yields the same chunks, and a reviewer can re-derive every boundary by hand:

* the **heading tree** (:mod:`wordextract.sections`, 4b) draws them: a section's own
  members, filtered to the block kinds, are packed into chunks, and a chunk never mixes
  two sections' blocks. The blocks before the first heading are the document's preamble
  and are packed the same way under the empty path, as is a document whose walk found no
  heading at all (the degraded flat root: nothing names a section, so the size cap is
  the only boundary there is);
* the **size cap** measures ``len(view_text)`` in Python code points -- the chunk's own
  view text, its member blocks joined by ``"\\n"`` -- and a section's blocks are packed
  in document order into chunks no longer than ``size_cap``. A block is never split, so
  a single paragraph longer than the cap is a chunk of its own, over cap and honest
  about it: the invariant is "no chunk is over the cap unless one block is";
* a section shorter than ``min_size`` is **merged up** into the parent section that has
  content of its own -- a one-line subsection under an established heading is not its
  own retrieval unit, it reads as part of what encloses it -- *except* a section holding
  a **numbered-list run** (>= ``list_run`` consecutive ``LIST_ITEM`` blocks), which keeps
  its own chunks: a numbered list is a unit of its own whether or not it is short, and a
  run is forced to a boundary **before and after**, so list items merge only with list
  items and never with the prose around them;
* a **table** is atomic: it is packed whole when it fits and never split across chunks
  unless it does not fit at all. An oversize table splits into consecutive row groups
  that each **repeat the table's first row** -- v1 reads no ``w:tblHeader``, so "the
  header row" is the first row, which is Word's own default for a header row and the
  only one the node tree records. A repeated header row is what makes identical row
  groups possible, which is the ``occurrence_index`` path: two chunks with the same
  ``content_hash`` are two chunks, told apart by their ordinal;
* a **range comment** belongs to the chunk its range **starts** in: the chunk whose
  covered block ranges contain ``anchor.start``. A comment anchored where no chunk holds
  text -- a heading's own text (a heading is a section's ``heading_id``, never one of its
  members), a paragraph terminator, or any part but the body the chunks are built from --
  belongs to no chunk and folds into no ``context_hash``; see
  ``docs/design/phase1-gaps.md``. A comment anchored in a **heading's own text** belongs
  to the first chunk of that heading's section -- the chunk a reader of the section meets
  first -- or, when the section merged up, to the chunk its heading text landed in;

Identity. ``content_hash`` is over the chunk's own view text plus the ``view_id`` and
``textmodel_version`` that produced it, so the same text in another view is another
chunk; ``id`` is the canonical hash of ``(content_hash, occurrence_index)``, and
``occurrence_index`` is the ordinal of this chunk among the *preceding chunks of the same
document with the same content hash* -- which is what keeps two identical paragraphs in
two sections apart (D3's fallback, one level up). Two identical row groups of one
oversize table collide the same way, which is why the split needs the ordinal at all.

``context_hash`` = ``content_hash`` plus the facts of the comments that start in the
chunk. It is a **summary**-key input only, never part of the chunk id or the store key
``(source_content_hash, chunk_id)``, so editing or resolving a comment re-summarizes
nothing (D8). The contract's :class:`~wordextract.model.Comment` carries no body text --
only its anchors and its facts -- so the facts are what is folded in (identity, author,
initials, date, anchored text, the comment's own text, resolution, threading,
parent), so a rewritten comment re-summarizes whatever it annotates. Raw anchor
offsets are deliberately excluded: they are union offsets, so they churn with
``textmodel_version`` and would re-summarize on any edit above the anchor.

Heading text is **not** chunk content for a section that opens its own chunks: 4b makes
a heading the section's ``heading_id`` and not one of its members, and ``section_path``
says where the chunk sits -- which also means retitling such a heading re-ids no chunk
beneath it. A section that **merges up** has no chunk of its own and no path entry left,
so its heading text becomes the leading line of what it merged into: a short clause
heading ("Waiver of Subrogation") is exactly what a reader searches for and must never
vanish from every chunk. A heading is never the last line of a chunk it does not end.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Iterable, Iterator, Sequence

from docextract_core import sha256_json

from .model import (
    Chunk,
    Comment,
    HeadingDecision,
    Node,
    NodeKind,
    ParseResult,
    Section,
    UnionStream,
    View,
)
from .versions import TEXTMODEL_VERSION
from .views import project

#: The kinds a chunk is made of: a top-level block of the body. A container is walked
#: through, never split, which is what makes a table atomic and a content control invisible.
#: A ``HEADING`` is deliberately not one: it opens a section instead of being a member of
#: one (4b), so it is a *boundary* -- its text is nobody's chunk content, which is why it
#: never appears in ``_blocks`` at all.
_BLOCK_KINDS = frozenset({NodeKind.PARA, NodeKind.LIST_ITEM, NodeKind.TABLE})

#: The kinds that carry text of their own (exactly one contiguous span, 2b). A ``CELL``
#: has a span too, but it *covers* its paragraphs, so a table's text is read off leaves.
_LEAF_KINDS = frozenset({NodeKind.HEADING, NodeKind.PARA, NodeKind.LIST_ITEM})


@dataclass(frozen=True)
class ChunkParams:
    """v1's tunables, hashed into ``chunker_params_hash`` (D10).

    ``size_cap`` is a little under a typical embedding window so one chunk is one
    retrieval unit, ``min_size`` is what "tiny" means to the merge-up rule, and
    ``list_run`` is N: the length a numbered-list run must reach to be a unit of its own
    (the build spec fixes N = 2). They are parameters rather than constants so that
    changing one is a change to ``chunker_params_hash`` and can never reuse a summary.
    """

    size_cap: int = 1000
    min_size: int = 250
    list_run: int = 2


DEFAULT_PARAMS = ChunkParams()


def params_hash(params: ChunkParams = DEFAULT_PARAMS) -> str:
    """``chunker_params_hash``: the canonical hash of the tunables (D10)."""
    return sha256_json(asdict(params))


def chunk(
    parsed: ParseResult,
    part_id: str,
    *,
    view: View = View.ACCEPTED,
    params: ChunkParams = DEFAULT_PARAMS,
) -> list[Chunk]:
    """``part_id``'s chunks in document order: the outline, capped (Turn 5).

    ``part_id`` is the body part -- the one part the section tree covers -- so the
    outline shapes the chunks. ``view`` picks which view's text the chunks *are*: their
    bytes, and therefore their ``content_hash``, differ per view while the boundaries do
    not (the block sequence is the union's, and a view only masks it).

    Chunks are returned in document order, which is the order the ordinals are handed
    out in: two chunks with the same ``content_hash`` are ordered as they stand.
    """
    stream = next((s for s in parsed.union_streams if s.part_id == part_id), None)
    if stream is None:
        return []
    by_id = {node.id: node for node in parsed.nodes}
    blocks = _blocks(parsed.nodes, part_id, by_id, stream, view)
    if not blocks:
        return []
    headings = _heading_blocks(parsed.sections, by_id, stream, view)
    drafts = _Groups(parsed.sections, blocks, headings).drafts(params, by_id)
    drafts.sort(key=lambda draft: (draft.first, draft.last))
    decisions = {decision.node_id: decision for decision in parsed.heading_decisions}
    comments = [c for c in parsed.comments if _anchored_in(c, part_id)]
    seen: dict[str, int] = {}
    chunks: list[Chunk] = []
    for draft in drafts:
        content_hash = sha256_json(
            {
                "text": draft.text,
                "view_id": view.value,
                "textmodel_version": TEXTMODEL_VERSION,
            }
        )
        occurrence = seen.get(content_hash, 0)
        seen[content_hash] = occurrence + 1
        fired, disputed = _rules(draft, decisions)
        chunks.append(
            Chunk(
                id=sha256_json({"content_hash": content_hash, "occurrence_index": occurrence}),
                content_hash=content_hash,
                context_hash=sha256_json(
                    {
                        "content_hash": content_hash,
                        "comments": _comment_facts(comments, draft),
                    }
                ),
                occurrence_index=occurrence,
                section_path=list(draft.path),
                node_ids=list(draft.node_ids),
                view_id=view.value,
                textmodel_version=TEXTMODEL_VERSION,
                size=len(draft.text),
                heading_detection=parsed.heading_detection,
                fired_rules=fired,
                disputed_rules=disputed,
            )
        )
    return chunks


# --- what a chunk is made of ---------------------------------------------------------


@dataclass(frozen=True)
class _Row:
    """One ``ROW``'s view text and where it is: the unit an oversize table splits on."""

    node: Node
    text: str
    start: int
    end: int


@dataclass(frozen=True)
class _Block:
    """One top-level block of the body: the atomic unit packing works in.

    ``index`` is its position in the body's block sequence, which is document order and
    survives a block with no text at all (an empty paragraph is no union range, so its
    offsets are not orderable) -- and doc order is what chunk order is checked against.
    """

    node: Node
    index: int
    text: str
    start: int
    end: int
    rows: tuple[_Row, ...] = ()

    @property
    def list_item(self) -> bool:
        return self.node.kind is NodeKind.LIST_ITEM


@dataclass
class _Draft:
    """One chunk before it is hashed: its text, what it covers and where it belongs."""

    path: list[str]
    text: str
    node_ids: list[str]
    ranges: list[tuple[int, int]]
    heading_ids: list[str]
    first: int
    last: int


def _blocks(
    nodes: Sequence[Node],
    part_id: str,
    by_id: dict[str, Node],
    stream: UnionStream,
    view: View,
) -> list[_Block]:
    """The body's blocks in document order, containers walked through.

    The walker records a node tree in document order with a parent before its children,
    so the blocks are the ``_BLOCK_KINDS`` nodes of the part reachable from a node that
    is nobody's child -- a table is one block, not its rows -- and their indices count
    the sequence they are found in. A heading is walked *through*, so it contributes no
    block: what the heading opens is a group (see :class:`_Groups`), not a member.
    """
    part = [node for node in nodes if node.part_id == part_id]
    children = {child_id for node in part for child_id in node.child_ids}
    blocks: list[_Block] = []

    def descend(node: Node) -> None:
        if node.kind in _BLOCK_KINDS:
            blocks.append(_block(node, len(blocks), by_id, stream, view))
            return
        for child_id in node.child_ids:
            child = by_id.get(child_id)
            if child is not None and child.part_id == part_id:
                descend(child)

    for root in part:
        if root.id not in children:
            descend(root)
    return blocks


def _block(
    node: Node, index: int, by_id: dict[str, Node], stream: UnionStream, view: View
) -> _Block:
    start, end = _extent(node, by_id)
    rows: list[_Row] = []
    if node.kind is NodeKind.TABLE:
        for child_id in node.child_ids:
            child = by_id.get(child_id)
            if child is not None and child.kind is NodeKind.ROW:
                low, high = _extent(child, by_id)
                rows.append(
                    _Row(
                        node=child,
                        text=_node_text(child, by_id, stream, view),
                        start=low,
                        end=high,
                    )
                )
    return _Block(
        node=node,
        index=index,
        text=_node_text(node, by_id, stream, view),
        start=start,
        end=end,
        rows=tuple(rows),
    )


def _heading_blocks(
    forest: Sequence[Section], by_id: dict[str, Node], stream: UnionStream, view: View
) -> dict[str, _Block]:
    """Every section's heading as a block of its own text, keyed by the heading's node id.

    A heading is not a member of its section's chunks, so these blocks are only used where
    a heading has to *become* chunk content: a section that merges up (its heading text
    leads what it merged into), and comment anchoring (a comment on a heading is held by
    the heading's own range). The index is a placeholder; a merge assigns the real one.
    """
    found: dict[str, _Block] = {}

    def visit(sections: Sequence[Section]) -> None:
        for section in sections:
            node = by_id.get(section.heading_id)
            if node is not None:
                found[section.heading_id] = _block(node, 0, by_id, stream, view)
            visit(section.children)

    visit(forest)
    return found


def _leaves(node: Node, by_id: dict[str, Node]) -> Iterator[Node]:
    """``node``'s text-bearing nodes in document order (a leaf is not descended)."""
    if node.kind in _LEAF_KINDS:
        yield node
        return
    for child_id in node.child_ids:
        child = by_id.get(child_id)
        if child is not None:
            yield from _leaves(child, by_id)


def _node_text(node: Node, by_id: dict[str, Node], stream: UnionStream, view: View) -> str:
    """``node``'s own view text: its leaves' view texts joined by ``"\\n"`` (Turn 5).

    The join is what the size cap measures, and what a chunk's members are joined by, so
    a paragraph's own text, a cell's paragraph and a table's rows all read the same way.
    A leaf with no span (an empty paragraph) contributes the empty string, so a cell with
    two paragraphs is two lines and an empty one is a line of its own.
    """
    pieces: list[str] = []
    for leaf in _leaves(node, by_id):
        if not leaf.spans:
            pieces.append("")
            continue
        span = leaf.spans[0]
        pieces.append(project(stream, view, span.start, span.end).text)
    return "\n".join(pieces)


def _extent(node: Node, by_id: dict[str, Node]) -> tuple[int, int]:
    """The union range ``node``'s text occupies: first leaf's start to last leaf's end."""
    spans = [leaf.spans[0] for leaf in _leaves(node, by_id) if leaf.spans]
    if not spans:
        return 0, 0
    return min(span.start for span in spans), max(span.end for span in spans)


def _subtree_ids(node: Node, by_id: dict[str, Node]) -> list[str]:
    """``node`` and every node under it, in document order: a chunk's ``node_ids``."""
    found = [node.id]
    for child_id in node.child_ids:
        child = by_id.get(child_id)
        if child is not None:
            found.extend(_subtree_ids(child, by_id))
    return found


# --- the outline as a tree of groups --------------------------------------------------


@dataclass
class _Group:
    """One section (or the preamble) as a set of blocks plus the sections under it."""

    path: list[str]
    section: Section | None
    heading_ids: list[str]
    parent: _Group | None = None
    blocks: list[_Block] = field(default_factory=list)
    children: list[_Group] = field(default_factory=list)
    position: int = 0
    stream: list[_Block] | None = None


class _Groups:
    """The 4b forest turned into the block streams chunks are packed from.

    A group's *stream* is its own blocks plus the blocks of every descendant section that
    dissolved into it (see :meth:`_dissolves`), in document order: the stream is what one
    chunking run sees, so a tiny section merged up is not "a chunk of the parent" -- its
    blocks are simply the parent's from then on.
    """

    def __init__(
        self,
        forest: Sequence[Section],
        blocks: Sequence[_Block],
        headings: dict[str, _Block] | None = None,
    ) -> None:
        self.headings = headings or {}
        self.root = _Group(path=[], section=None, heading_ids=[])
        by_section: dict[int, _Group] = {}
        owner: dict[str, _Group] = {}

        def build(sections: Sequence[Section], parent: _Group) -> None:
            for section in sections:
                group = _Group(
                    path=[*parent.path, section.title],
                    section=section,
                    heading_ids=[*parent.heading_ids, section.heading_id],
                    parent=parent,
                )
                by_section[id(section)] = group
                parent.children.append(group)
                build(section.children, group)

        build(forest, self.root)
        for group in self._walk(self.root):
            if group.section is not None:
                for node_id in group.section.node_ids:
                    owner[node_id] = group
        for block in blocks:
            owner.get(block.node.id, self.root).blocks.append(block)
        self._position(self.root, len(blocks))

    def _position(self, group: _Group, last: int) -> int:
        """Where ``group`` sits in the block sequence: its earliest block, or its child's.

        A section that holds no block of its own sits where its first block-bearing
        descendant sits -- which is what orders a parent's later blocks around a child's.
        A section with nothing under it at all (a heading with no content, ever) sorts
        last, deterministically.
        """
        found = [block.index for block in group.blocks]
        found.extend(self._position(child, last) for child in group.children)
        group.position = min(found) if found else last
        return group.position

    def _walk(self, group: _Group) -> Iterator[_Group]:
        for child in group.children:
            yield child
            yield from self._walk(child)

    def _order(self, group: _Group) -> list[_Block | _Group]:
        """``group``'s own blocks and children, interleaved in document order."""
        entries: list[tuple[int, int, _Block | _Group]] = []
        rank = 0
        for block in group.blocks:
            entries.append((block.index, rank, block))
            rank += 1
        for child in group.children:
            entries.append((child.position, rank, child))
            rank += 1
        entries.sort(key=lambda entry: (entry[0], entry[1]))
        return [item for _, _, item in entries]

    def _melt(self, group: _Group, params: ChunkParams) -> list[_Block]:
        """``group``'s stream: its blocks, with every dissolved child's spliced in."""
        if group.stream is None:
            stream: list[_Block] = []
            for item in self._order(group):
                if isinstance(item, _Group):
                    if self._dissolves(item, params):
                        # the merged section has no chunk and no path entry of its own:
                        # its heading text leads its content instead of vanishing
                        heading = self.headings.get(item.section.heading_id)
                        if heading is not None:
                            stream.append(replace(heading, index=item.position))
                        stream.extend(self._melt(item, params))
                else:
                    stream.append(item)
            group.stream = stream
        return group.stream

    def _dissolves(self, group: _Group, params: ChunkParams) -> bool:
        """Whether a tiny section merges **up** into the parent that has content (Turn 5).

        Four things have to hold: the section is tiny (under ``min_size``), it holds no
        numbered-list run (a list keeps its own chunks however short it is), its parent is
        a *section* -- the document root is where the preamble goes, and a section that
        dissolved into it would lose the outline the chunks are there to keep -- and that
        parent holds blocks of its own, because a merge exists to stop a stray one-liner
        being its own retrieval unit while merging siblings into an empty parent would
        fuse sections that are only short.
        """
        if (
            group.section is None
            or group.parent is None
            or group.parent.section is None
            or not group.parent.blocks
        ):
            return False
        stream = self._melt(group, params)
        return len(_join(stream)) < params.min_size and not _list_run(stream, params.list_run)

    def drafts(self, params: ChunkParams, by_id: dict[str, Node]) -> list[_Draft]:
        """Every surviving group's chunks, in pre-order (the caller puts them in order)."""
        found: list[_Draft] = []
        self._collect(self.root, params, by_id, found)
        return found

    def _collect(
        self,
        group: _Group,
        params: ChunkParams,
        by_id: dict[str, Node],
        found: list[_Draft],
    ) -> None:
        mark = len(found)
        _pack(self._melt(group, params), group, params, by_id, found)
        for child in group.children:
            if not self._dissolves(child, params):
                self._collect(child, params, by_id, found)
        heading = self.headings.get(group.section.heading_id) if group.section else None
        if heading is not None and len(found) > mark:
            # a comment anchored in the heading's own text is held by the first chunk the
            # section (or, with no content of its own, its first descendant) opens
            found[mark].ranges.append((heading.start, heading.end))


# --- packing -------------------------------------------------------------------------


def _join(blocks: Iterable[_Block]) -> str:
    """A stream's text: its blocks' own texts joined by ``"\\n"`` -- what the cap measures."""
    return "\n".join(block.text for block in blocks)


def _list_run(blocks: Sequence[_Block], length: int) -> bool:
    """Whether ``blocks`` holds a maximal run of >= ``length`` consecutive ``LIST_ITEM``s."""
    run = 0
    for block in blocks:
        run = run + 1 if block.list_item else 0
        if run >= length:
            return True
    return False


def _segments(blocks: Sequence[_Block], length: int) -> list[list[_Block]]:
    """``blocks`` split at every numbered-list run: a forced boundary before and after.

    A run is maximal -- it is exactly the consecutive ``LIST_ITEM`` blocks around it -- so
    packing it whole is what "list items merge only with list items" means: the prose
    before and after it can never land in the same chunk as its items.
    """
    segments: list[list[_Block]] = []
    pending: list[_Block] = []
    index = 0
    while index < len(blocks):
        if blocks[index].list_item:
            stop = index
            while stop < len(blocks) and blocks[stop].list_item:
                stop += 1
            if stop - index >= length:
                if pending:
                    segments.append(pending)
                    pending = []
                segments.append(list(blocks[index:stop]))
                index = stop
                continue
        pending.append(blocks[index])
        index += 1
    if pending:
        segments.append(pending)
    return segments


def _pack(
    blocks: Sequence[_Block],
    group: _Group,
    params: ChunkParams,
    by_id: dict[str, Node],
    found: list[_Draft],
) -> None:
    """Greedy packing of one group's stream: cap in document order, tables atomic."""
    for segment in _segments(blocks, params.list_run):
        current: list[_Block] = []
        size = 0
        for block in segment:
            if block.rows and len(block.text) > params.size_cap:
                _emit(current, group, by_id, found)
                current, size = [], 0
                _emit_rows(block, group, params, by_id, found)
                continue
            if current and size + 1 + len(block.text) > params.size_cap:
                carry: list[_Block] = []
                if len(current) > 1 and current[-1].node.kind is NodeKind.HEADING:
                    carry = [current.pop()]  # a heading leads its content, never ends a chunk
                _emit(current, group, by_id, found)
                current, size = carry, len(_join(carry)) if carry else 0
            size += len(block.text) + (1 if current else 0)
            current.append(block)
        _emit(current, group, by_id, found)


def _emit(
    blocks: Sequence[_Block], group: _Group, by_id: dict[str, Node], found: list[_Draft]
) -> None:
    """One chunk from consecutive blocks: its text, its nodes and what it covers."""
    if not blocks:
        return
    node_ids: list[str] = []
    for block in blocks:
        node_ids.extend(_subtree_ids(block.node, by_id))
    found.append(
        _Draft(
            path=list(group.path),
            text=_join(blocks),
            node_ids=node_ids,
            ranges=[(block.start, block.end) for block in blocks],
            heading_ids=list(group.heading_ids),
            first=blocks[0].index,
            last=blocks[-1].index,
        )
    )


def _emit_rows(
    block: _Block,
    group: _Group,
    params: ChunkParams,
    by_id: dict[str, Node],
    found: list[_Draft],
) -> None:
    """An oversize table's chunks: consecutive row groups, first row repeated (Turn 5).

    The first row is repeated in every group because a row group torn out of a table is
    unreadable without its header; the repetition is also what a repeated header row
    *is*, so it is content rather than metadata and counts against the cap. A single row
    with the header over the cap is still one group -- a row is never split.
    """
    groups: list[list[_Row]] = [[block.rows[0]]]
    for row in block.rows[1:]:
        size = len(_rows_join(groups[-1]))
        if len(groups[-1]) > 1 and size + 1 + len(row.text) > params.size_cap:
            groups.append([block.rows[0]])
        groups[-1].append(row)
    for rows in groups:
        node_ids = [block.node.id]
        for row in rows:
            node_ids.extend(_subtree_ids(row.node, by_id))
        found.append(
            _Draft(
                path=list(group.path),
                text=_rows_join(rows),
                node_ids=node_ids,
                ranges=[(row.start, row.end) for row in rows],
                heading_ids=list(group.heading_ids),
                first=block.index,
                last=block.index,
            )
        )


def _rows_join(rows: Sequence[_Row]) -> str:
    return "\n".join(row.text for row in rows)


# --- comments and heading decisions ---------------------------------------------------


def _anchored_in(comment: Comment, part_id: str) -> bool:
    """Whether a comment is anchored in ``part_id`` at all -- the chunks' part."""
    return comment.anchor is not None and comment.anchor.part_id == part_id


def _comment_facts(comments: Sequence[Comment], draft: _Draft) -> list[list[str]]:
    """The facts of the comments that **start** in ``draft``: its ``context_hash`` input.

    "Starts" is containment: the chunk whose covered ranges hold ``anchor.start`` is the
    one the range begins in, and a range spanning two chunks belongs to the first of them.
    The facts are the contract's own :class:`~wordextract.model.Comment` fields, sorted by
    their whole value so the list is a set -- one comment's edit changes exactly one entry.
    """
    facts = [
        [
            comment.para_id,
            comment.author,
            comment.initials,
            comment.date or "",
            comment.anchor_text,
            comment.text,
            comment.threading_status.value,
            comment.resolved_identity or "",
            comment.parent_id or "",
            "resolved" if comment.resolved else "open",
        ]
        for comment in comments
        if _holds(draft, comment)
    ]
    facts.sort()
    return facts


def _holds(draft: _Draft, comment: Comment) -> bool:
    assert comment.anchor is not None
    start = comment.anchor.start
    return any(low <= start < high for low, high in draft.ranges)


def _rules(draft: _Draft, decisions: dict[str, HeadingDecision]) -> tuple[list[str], list[str]]:
    """The heading rules the chunk's ``section_path`` was decided by (Turn 5).

    A chunk carries the aggregate of its position in the outline: every rule that fired
    on any heading that opens the chunk's section or an ancestor of it, and every rule
    that disagreed there. A chunk's *members* are not headings -- a heading opens a
    section instead of belonging to one (4b) -- so the path is the only place a decision
    can come from, and the degraded flat root has none of either.
    """
    fired: set[str] = set()
    disputed: set[str] = set()
    for heading_id in draft.heading_ids:
        decision = decisions.get(heading_id)
        if decision is not None:
            fired.update(decision.fired_rules)
            disputed.update(decision.disputed_rules)
    return sorted(fired), sorted(disputed)
