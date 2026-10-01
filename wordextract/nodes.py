"""Turn 0c: the node-tree helpers the matcher, the query API and the catalog share.

The walker's node tree is a flat list of :class:`~wordextract.model.Node`, each with the ids
of its children (``child_ids``); a consumer that needs the tree back -- the matcher asking
where a comment sits, the query API grouping hits by section -- has to rebuild the edges
itself. These are the rebuild, once:

* :func:`parents` -- the ``child_ids`` edges read backwards, child id to parent id;
* :func:`ancestor_of_kind` -- the innermost ancestor of one node of one kind, which is how a
  paragraph learns it is inside a table cell;
* :func:`anchor_node` -- the node a comment is anchored to: the innermost node whose span
  holds the anchor's **start**, the containment the chunker attaches a comment by;
* :func:`section_of` -- the section path the walk assigned a node (Turn 4b), which is what a
  hit is located by.

Node ids are package-unique, so a map keyed by id is a map keyed by node. Nothing here is
stored: all four are derived from a :class:`~wordextract.model.ParseResult`, and the walk
stays the only producer of the facts they read. ``parents`` is a map, not a tree -- a
malformed package could point a node back into itself -- so every walk here carries the ids
it has seen and stops rather than looping.
"""
from __future__ import annotations

from .model import Node, NodeKind, ParseResult, Span


def parents(parsed: ParseResult) -> dict[str, str]:
    """Each node's parent, from the ``child_ids`` edges. Node ids are package-unique."""
    parent_of: dict[str, str] = {}
    for node in parsed.nodes:
        for child in node.child_ids:
            parent_of[child] = node.id
    return parent_of


def _depth(node_id: str, parent_of: dict[str, str]) -> int:
    """How deep ``node_id`` sits in the ``child_ids`` tree; a root is 0.

    ``parent_of`` is a map, not a tree -- a malformed one could point back into itself -- so
    the walk carries the ids it has seen and stops rather than looping.
    """
    seen = {node_id}
    depth = 0
    current = parent_of.get(node_id)
    while current is not None and current not in seen:
        seen.add(current)
        depth += 1
        current = parent_of.get(current)
    return depth


def ancestor_of_kind(
    node_id: str,
    by_id: dict[str, Node],
    parent_of: dict[str, str],
    kind: NodeKind,
) -> Node | None:
    """The innermost ancestor of ``node_id`` of ``kind``, or None. Cycle-guarded.

    ``node_id`` itself is not its own ancestor: a ``CELL`` node's own ancestor of kind
    ``CELL`` is the cell around it, which is what makes "different cells" a question about
    the text's place rather than about the node that starts it.
    """
    seen = {node_id}
    current = parent_of.get(node_id)
    while current is not None and current not in seen:
        seen.add(current)
        node = by_id.get(current)
        if node is None:
            break
        if node.kind is kind:
            return node
        current = parent_of.get(current)
    return None


def anchor_node(
    parsed: ParseResult,
    anchor: Span | None,
    parent_of: dict[str, str] | None = None,
) -> str | None:
    """The node a comment is anchored to: the innermost one whose span holds its start.

    A range comment belongs to the place it **starts** -- the containment the chunker
    attaches the comment to a chunk by -- so a range over three paragraphs has the node it
    opens in as its context. None where no node holds that offset: a comment anchored on a
    paragraph terminator, or one whose part (or the anchor's) did not stream, has no node to
    point at, and says so.

    ``parent_of`` is an optional precomputed :func:`parents` for a caller resolving many
    anchors (the matcher resolves one per comment); it is derived here when omitted.
    """
    if anchor is None:
        return None
    if parent_of is None:
        parent_of = parents(parsed)
    best: Node | None = None
    best_depth = -1
    for node in parsed.nodes:
        if not any(
            span.part_id == anchor.part_id
            and span.fragment_id == anchor.fragment_id
            and span.start <= anchor.start < span.end
            for span in node.spans
        ):
            continue
        depth = _depth(node.id, parent_of)
        if depth > best_depth:
            best, best_depth = node, depth
    return None if best is None else best.id


def section_of(parsed: ParseResult, node_id: str) -> list[str]:
    """The section path the walk assigned ``node_id``, outermost title first (Turn 4b).

    ``section_path`` is derived once, by the walk, and stored on the node -- so this is a
    lookup, not a re-derivation. ``[]`` is the honest answer for a node outside every
    section (a preamble before the first heading, anything in a part the outline does not
    cover) and for an id no node has, which is why a caller never has to ask whether the
    node exists first.
    """
    for node in parsed.nodes:
        if node.id == node_id:
            return list(node.section_path)
    return []
