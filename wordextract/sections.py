"""Turn 4b: the section tree a document's headings define, and every node's path in it.

Turn 4a decided *which* paragraphs are headings; this turns that verdict into the
outline -- a forest of :class:`~wordextract.model.Section`, each opened by a heading and
holding the nodes that follow it until the next heading of the same or a shallower level
:

* a section is opened by a ``HEADING`` node, and the **level** it sits at is Word's own
  numbering (:func:`section_level`): a heading at outline level *n* (0-8) is level
  *n + 1*, a heading that only its style names a level for is that level, and ``Title``
  is level 0 -- above every numbered heading, which is where a title belongs;
* a heading that **states no level** and is not a ``Title`` -- the all-bold rule, an
  outline-numbered level with no style -- is *unlevelled*. It cannot be placed by a level
  it does not have, and placing it at the top would let a bold "Important notice" in the
  middle of a section pop every open section and orphan the headings after it. So it
  nests one level below the innermost **levelled** section open where it stands (level 1
  when none is), which makes consecutive unlevelled headings siblings and leaves a later
  ``Heading 2`` where it belongs;
* a heading deeper than the last one opens a section **inside** it, a heading at the
  same or a shallower level **closes** it, so the tree is built by one pass in document
  order and needs no backtracking;
* the section's ``title`` is the heading's own text in the **accepted** view -- what the
  reader calls the section. Like every view text this is gap-closed and never a
  placeholder: a heading the view elides entirely titles its section with the empty
  string, which is what that view reads as.

``section_path`` is the list of titles from the outermost section down to the node's own
-- ``["Program Review", "Coverage Terms"]`` -- and ``[]`` outside every section: a
preamble before the first heading, and a document with no heading at all, which is the
degraded flat root (no section can be named, and the chunker size-chunks instead). A
heading's own path ends with its own title.

Sections are read off the **body part only** (``part_id``). A heading in a header,
footer, footnote or comment is not a section of the document: parts are separate address
spaces (D2) and the outline is the body's, so those nodes carry the empty path. Nothing
here is stored: like a view, the tree is derived from the node tree and the union streams
and assigned to each node as it is built (the walk is the only thing that builds it, and
it builds it once).
"""
from __future__ import annotations

from dataclasses import replace
from typing import Sequence

from .headings import MAX_HEADING_OUTLINE_LEVEL, heading_style_level
from .model import Node, NodeKind, Section, UnionStream, View
from .views import project


def section_level(node: Node) -> int | None:
    """The level a heading *states* (Turn 4b), or ``None`` when it states none.

    Word's own numbering, so a level is comparable across a document: a stated outline
    level 0-8 is level *n + 1*, which is what makes ``Heading 1`` level 1. **9 is Word's
    "body text"** and states no level, so a ``Heading 1`` paragraph explicitly set to 9
    falls through to the level its style names. Nothing stated leaves the style's own
    name -- ``Heading3`` is level 3 -- and ``Title`` is level 0. Anything else (the
    all-bold rule, an outline-numbered level whose own style is not on the paragraph)
    states no level: ``None``, and the tree places it by where it stands.

    The outline level wins where the two disagree: it is the level Word's navigation pane
    builds on, and a style name is only a default for a paragraph that states none.
    """
    if node.level is not None and 0 <= node.level <= MAX_HEADING_OUTLINE_LEVEL:
        return node.level + 1
    named = heading_style_level(node.style)
    if named is not None:
        return named
    if (node.style or "").casefold() == "title":
        return 0
    return None


def heading_title(node: Node, stream: UnionStream) -> str:
    """A heading's own text in the accepted view: the section's title (Turn 4b)."""
    if not node.spans:
        return ""
    span = node.spans[0]
    return project(stream, View.ACCEPTED, span.start, span.end).text


def build(
    nodes: Sequence[Node], streams: Sequence[UnionStream], part_id: str
) -> list[Section]:
    """The outline forest ``part_id``'s heading nodes define, in document order (4b).

    Headings are the only nodes that can open a section -- 4a's rules are the only
    producer of a ``HEADING`` node -- and every other node belongs to the innermost
    section open where it stands, containers included: a table, its rows and its cells
    are the section's, in document order, and a consumer filters by ``kind``. Nodes under
    a deeper section belong to that one instead, and a node before the first heading
    belongs to no section and is in no section's ``node_ids``.

    A document whose walk found no heading at all has an empty forest -- the degraded
    flat root: nothing names a section, so nothing nests and no node gets a path.
    """
    by_part = {stream.part_id: stream for stream in streams}
    forest: list[Section] = []
    # (section, levelled): whether the section's level was stated, which decides where an
    # unlevelled heading below it nests.
    open_sections: list[tuple[Section, bool]] = []
    for node in nodes:
        if node.part_id != part_id:
            continue
        if node.kind is not NodeKind.HEADING:
            if open_sections:
                # A container and its descendants are all in this section, in order.
                open_sections[-1][0].node_ids.append(node.id)
            continue
        stated = section_level(node)
        if stated is None:
            levelled_above = [section for section, levelled in open_sections if levelled]
            level = levelled_above[-1].level + 1 if levelled_above else 1
        else:
            level = stated
        while open_sections and open_sections[-1][0].level >= level:
            open_sections.pop()
        section = Section(
            heading_id=node.id,
            title=heading_title(node, by_part[node.part_id]),
            level=level,
        )
        if open_sections:
            open_sections[-1][0].children.append(section)
        else:
            forest.append(section)
        open_sections.append((section, stated is not None))
    return forest


def assign(
    nodes: Sequence[Node], streams: Sequence[UnionStream], part_id: str
) -> tuple[list[Node], list[Section]]:
    """``nodes`` with their ``section_path`` set, and the outline forest (Turn 4b).

    The nodes are frozen, so a node with a path is a copy of it: the walk's node tree is
    replaced wholesale rather than edited. A node outside the body part, and every node
    of a part the outline does not cover, gets the empty path.
    """
    sections = build(nodes, streams, part_id)
    paths = _paths(sections)
    return [replace(node, section_path=paths.get(node.id, [])) for node in nodes], sections


def _paths(sections: Sequence[Section], prefix: tuple[str, ...] = ()) -> dict[str, list[str]]:
    """Every node id in the forest mapped to its section path, outermost title first."""
    paths: dict[str, list[str]] = {}
    for section in sections:
        path = [*prefix, section.title]
        paths[section.heading_id] = path
        for node_id in section.node_ids:
            paths[node_id] = path
        paths.update(_paths(section.children, tuple(path)))
    return paths
