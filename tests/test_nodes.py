"""Turn 0c: the node-tree helpers the matcher, the query API and the catalog share.

The walker hands back a **flat** node list with ``child_ids`` edges; everything that needs
the tree back -- the matcher asking where a comment sits, the query API grouping a hit by
section -- rebuilds it in :mod:`wordextract.nodes`. These tests pin that rebuild, and none of
them asks the helper to check itself:

* the parent map is checked against the ``child_ids`` edges restated as pairs (one parent per
  child, no id that is no node's) **and** against the walker's containment invariant
  (``tests/test_walker.py``): a child's text lies inside its parent's spans, a container that
  carries no span of its own -- a table, a row -- saying nothing;
* ``ancestor_of_kind`` is checked against an ancestor chain rebuilt by climbing the parent map
  a step at a time: the answer is the nearest chain member of the kind, a node is never its
  own ancestor, and nothing between the node and the answer is of the kind (the *innermost*
  claim, which is what makes a paragraph in a cell a table cell);
* ``anchor_node`` is checked by rebuilding the depth of every node whose span holds the
  anchor's start -- the deepest one is the answer, and the corpus is decisive (one deepest
  node per anchor, so the check cannot pass on a tie);
* ``section_of`` is checked against the section tree and its membership read off the walk
  (``parsed.sections`` and ``section.node_ids``), which is a different fact from the
  ``section_path`` the helper returns, plus the accepted-view text a heading titles its own
  path with.

Hand-built trees carry the cases the corpus does not have -- a text-box anchor, an anchor in
a part that did not stream, a cyclic or dangling parent map -- because a helper that hangs or
raises on a malformed package is worse than one that answers ``None``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from wordextract import nodes as nodes_mod
from wordextract import opc
from wordextract import terms as terms_mod
from wordextract.model import Node, NodeKind, ParseResult, Span, View
from wordextract.views import project
from wordextract.walker import walk_document

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ALL_DOCX = sorted(FIXTURES.rglob("*.docx"))
DOCX_BY_NAME = {path.name: path for path in ALL_DOCX}

BODY = "officeDocument:0"
#: The kinds a container chain is made of, plus the two text kinds, so ``ancestor_of_kind`` is
#: asked every question it is ever asked.
CHAIN_KINDS = (NodeKind.TABLE, NodeKind.ROW, NodeKind.CELL, NodeKind.HEADING, NodeKind.PARA)


def _walk(path) -> ParseResult:
    return walk_document(opc.Package(path))


def _by_id(parsed: ParseResult) -> dict[str, Node]:
    return {node.id: node for node in parsed.nodes}


def _chain(node_id: str, parent_of: dict[str, str]) -> list[str]:
    """``node_id``'s ancestors, nearest first, rebuilt one map lookup at a time.

    Cycle-guarded the way the helper is, so a cyclic map gives a finite chain here too and
    the comparison stays meaningful.
    """
    chain: list[str] = []
    current = parent_of.get(node_id)
    while current is not None and current not in chain:
        chain.append(current)
        current = parent_of.get(current)
    return chain


def _holds(node: Node, anchor: Span) -> bool:
    """``node`` has a span, in the anchor's own address space, holding the anchor's start."""
    return any(
        span.part_id == anchor.part_id
        and span.fragment_id == anchor.fragment_id
        and span.start <= anchor.start < span.end
        for span in node.spans
    )


def _node(node_id: str, kind: NodeKind, spans=(), children=(), section_path=()) -> Node:
    return Node(
        id=node_id,
        kind=kind,
        part_id=BODY,
        source_ref=f"word/document.xml:{node_id}",
        spans=list(spans),
        child_ids=list(children),
        section_path=list(section_path),
    )


def _paths_from_forest(sections, prefix=()) -> dict[str, list[str]]:
    """The tree and its membership as paths: the second source ``section_of`` must agree with.

    Written here rather than imported from :mod:`wordextract.sections`: what is checked is
    that a path looked up by node id is the one the *forest* implies for that node, so the
    rebuild must not be the code that assigned the field.
    """
    paths: dict[str, list[str]] = {}
    for section in sections:
        path = [*prefix, section.title]
        paths[section.heading_id] = path
        for node_id in section.node_ids:
            paths[node_id] = path
        paths.update(_paths_from_forest(section.children, tuple(path)))
    return paths


# --- parents -------------------------------------------------------------------------


@pytest.mark.parametrize("path", ALL_DOCX, ids=lambda path: path.name)
def test_the_parent_map_is_the_child_ids_edges_read_backwards(path):
    """One parent per child, every id real, and no node its own parent."""
    parsed = _walk(path)
    edges = [(child, node.id) for node in parsed.nodes for child in node.child_ids]
    assert len(edges) == len({child for child, _ in edges}), "a node has one parent"
    assert nodes_mod.parents(parsed) == dict(edges)
    ids = set(_by_id(parsed))
    assert {child for child, _ in edges} <= ids
    assert {parent for _, parent in edges} <= ids
    parent_of = nodes_mod.parents(parsed)
    assert all(parent_of.get(node.id) != node.id for node in parsed.nodes)


@pytest.mark.parametrize("path", ALL_DOCX, ids=lambda path: path.name)
def test_a_childs_text_lies_inside_its_parents_and_in_the_same_part(path):
    """The walker's containment invariant, read through the parent map.

    A container that carries no span of its own (a table, a row) and a child's zero-width
    span (an empty paragraph) say nothing about containment and are skipped rather than
    asserted; a parent and child are never in different parts.
    """
    parsed = _walk(path)
    by_id = _by_id(parsed)
    parent_of = nodes_mod.parents(parsed)
    for child_id, parent_id in parent_of.items():
        child, parent = by_id[child_id], by_id[parent_id]
        assert child.part_id == parent.part_id, "an edge never crosses an address space"
        if not parent.spans:
            continue  # a container of containers: a table, a row -- its own text is its cells'
        for span in child.spans:
            if span.end == span.start:
                continue
            assert any(
                outer.part_id == span.part_id
                and outer.fragment_id == span.fragment_id
                and outer.start <= span.start
                and span.end <= outer.end
                for outer in parent.spans
            ), f"{child.kind} {span} is not inside its {parent.kind}"


@pytest.mark.parametrize("path", ALL_DOCX, ids=lambda path: path.name)
def test_a_parent_map_chain_never_repeats_a_node(path):
    """The tree is a forest: every ancestor chain terminates without meeting itself."""
    parsed = _walk(path)
    parent_of = nodes_mod.parents(parsed)
    for node in parsed.nodes:
        chain = _chain(node.id, parent_of)
        assert node.id not in chain
        assert len(chain) == len(set(chain))


# --- ancestor_of_kind ----------------------------------------------------------------


@pytest.mark.parametrize("path", ALL_DOCX, ids=lambda path: path.name)
def test_the_innermost_ancestor_of_kind_is_the_nearest_chain_member_of_that_kind(path):
    parsed = _walk(path)
    by_id = _by_id(parsed)
    parent_of = nodes_mod.parents(parsed)
    for node in parsed.nodes:
        chain = _chain(node.id, parent_of)
        for kind in CHAIN_KINDS:
            answer = nodes_mod.ancestor_of_kind(node.id, by_id, parent_of, kind)
            assert answer == next((by_id[i] for i in chain if by_id[i].kind is kind), None)
            if answer is None:
                continue
            assert answer.id in chain and answer.kind is kind
            assert answer.id != node.id, "a node is not its own ancestor"
            assert all(by_id[i].kind is not kind for i in chain[: chain.index(answer.id)])


def test_edge_cases_nests_a_paragraph_in_a_cell_in_a_row_in_a_table():
    """The corpus' one real container chain, hand-typed: 2 rows of 2 cells of 1 paragraph."""
    parsed = _walk(DOCX_BY_NAME["edge_cases.docx"])
    by_id = _by_id(parsed)
    parent_of = nodes_mod.parents(parsed)
    (table,) = [node for node in parsed.nodes if node.kind is NodeKind.TABLE]
    rows = [by_id[child] for child in table.child_ids]
    assert [row.kind for row in rows] == [NodeKind.ROW, NodeKind.ROW]
    cells = [by_id[child] for row in rows for child in row.child_ids]
    assert [cell.kind for cell in cells] == [NodeKind.CELL] * 4
    (body_para,) = [
        node
        for node in parsed.nodes
        if node.kind is NodeKind.PARA and node.part_id == BODY and not _chain(node.id, parent_of)
    ]
    for cell in cells:
        (para,) = [by_id[child] for child in cell.child_ids]
        assert para.kind is NodeKind.PARA
        assert nodes_mod.ancestor_of_kind(para.id, by_id, parent_of, NodeKind.CELL) is cell
        assert nodes_mod.ancestor_of_kind(para.id, by_id, parent_of, NodeKind.TABLE) is table
        assert nodes_mod.ancestor_of_kind(cell.id, by_id, parent_of, NodeKind.CELL) is None
        assert nodes_mod.ancestor_of_kind(cell.id, by_id, parent_of, NodeKind.TABLE) is table
    for node in (table, body_para):
        for kind in (NodeKind.TABLE, NodeKind.ROW, NodeKind.CELL):
            assert nodes_mod.ancestor_of_kind(node.id, by_id, parent_of, kind) is None
    for row in rows:
        assert nodes_mod.ancestor_of_kind(row.id, by_id, parent_of, NodeKind.TABLE) is table
        for kind in (NodeKind.ROW, NodeKind.CELL):
            assert nodes_mod.ancestor_of_kind(row.id, by_id, parent_of, kind) is None


def test_a_cyclic_parent_map_stops_the_ancestor_walk_without_hanging():
    """``a`` is inside ``b`` is inside ``a``: the walk leaves the cycle rather than looping."""
    parsed = ParseResult(
        nodes=[
            _node("a", NodeKind.PARA, children=["b"]),
            _node("b", NodeKind.TABLE, children=["a"]),
        ]
    )
    by_id, parent_of = _by_id(parsed), nodes_mod.parents(parsed)
    assert parent_of == {"b": "a", "a": "b"}
    assert nodes_mod.ancestor_of_kind("a", by_id, parent_of, NodeKind.TABLE) is by_id["b"]
    assert nodes_mod.ancestor_of_kind("b", by_id, parent_of, NodeKind.TABLE) is None


def test_a_parent_that_is_no_node_ends_the_ancestor_walk():
    by_id = {"a": _node("a", NodeKind.PARA)}
    assert nodes_mod.ancestor_of_kind("a", by_id, {"a": "gone"}, NodeKind.TABLE) is None


# --- anchor_node ---------------------------------------------------------------------


@pytest.mark.parametrize("path", ALL_DOCX, ids=lambda path: path.name)
def test_a_comment_anchors_on_the_deepest_node_holding_its_start(path):
    """The chunker's containment rule, re-derived by depth rather than trusted.

    The corpus is decisive here: every anchor has exactly one deepest holder, so this cannot
    pass on a tie. A comment with no anchor, and one no node holds (a paragraph terminator),
    answer ``None``.
    """
    parsed = _walk(path)
    by_id = _by_id(parsed)
    parent_of = nodes_mod.parents(parsed)
    for comment in parsed.comments:
        answer = nodes_mod.anchor_node(parsed, comment.anchor, parent_of)
        if comment.anchor is None:
            assert answer is None
            continue
        holders = [node for node in parsed.nodes if _holds(node, comment.anchor)]
        if not holders:
            assert answer is None
            continue
        depths = {node.id: len(_chain(node.id, parent_of)) for node in holders}
        deepest = [node_id for node_id, depth in depths.items() if depth == max(depths.values())]
        assert len(deepest) == 1, "one node is the innermost holder of an anchor"
        assert answer == deepest[0]


@pytest.mark.parametrize("path", ALL_DOCX, ids=lambda path: path.name)
def test_the_optional_parent_map_answers_what_the_derived_one_does(path):
    parsed = _walk(path)
    parent_of = nodes_mod.parents(parsed)
    for comment in parsed.comments:
        assert nodes_mod.anchor_node(parsed, comment.anchor) == nodes_mod.anchor_node(
            parsed, comment.anchor, parent_of
        )


def test_the_binder_summary_comment_anchors_on_the_paragraph_that_holds_it():
    parsed = _walk(DOCX_BY_NAME["binder_summary.docx"])
    (comment,) = parsed.comments
    node = _by_id(parsed)[nodes_mod.anchor_node(parsed, comment.anchor)]
    assert node.kind is NodeKind.PARA and node.part_id == BODY
    assert node.spans[0].start <= comment.anchor.start < node.spans[0].end


def test_the_unanchored_comment_fixture_points_at_no_node():
    parsed = _walk(DOCX_BY_NAME["unanchored_comment.docx"])
    assert any(comment.anchor is None for comment in parsed.comments)
    parent_of = nodes_mod.parents(parsed)
    for comment in parsed.comments:
        if comment.anchor is None:
            assert nodes_mod.anchor_node(parsed, comment.anchor, parent_of) is None
    assert nodes_mod.anchor_node(parsed, None) is None


def test_the_anchor_belongs_to_the_innermost_node_that_holds_it():
    parsed = ParseResult(
        nodes=[
            _node("outer", NodeKind.CELL, [Span(BODY, 0, 20)], children=["inner"]),
            _node("inner", NodeKind.PARA, [Span(BODY, 5, 10)]),
        ]
    )
    assert nodes_mod.anchor_node(parsed, Span(BODY, 6, 9)) == "inner"
    assert nodes_mod.anchor_node(parsed, Span(BODY, 15, 20)) == "outer"
    assert nodes_mod.anchor_node(parsed, Span(BODY, 20, 20)) is None, "the end is excluded"


def test_an_anchor_in_another_part_or_fragment_is_no_nodes_context():
    """A text box lives in its own fragment and a header in its own part: neither is a node's."""
    parsed = ParseResult(nodes=[_node("p", NodeKind.PARA, [Span(BODY, 0, 10)])])
    assert nodes_mod.anchor_node(parsed, Span("header:0", 0, 5)) is None
    assert nodes_mod.anchor_node(parsed, Span(BODY, 0, 5, fragment_id="p:0")) is None
    assert nodes_mod.anchor_node(parsed, Span(BODY, 0, 5)) == "p"


def test_a_cyclic_parent_map_stops_the_anchor_depth_without_hanging():
    parsed = ParseResult(
        nodes=[
            _node("a", NodeKind.PARA, [Span(BODY, 0, 10)], children=["b"]),
            _node("b", NodeKind.PARA, [], children=["a"]),
        ]
    )
    assert nodes_mod.parents(parsed) == {"b": "a", "a": "b"}
    assert nodes_mod.anchor_node(parsed, Span(BODY, 0, 5)) == "a"


# --- section_of ----------------------------------------------------------------------


@pytest.mark.parametrize("path", ALL_DOCX, ids=lambda path: path.name)
def test_section_of_agrees_with_the_section_tree_and_its_membership(path):
    """The field is checked against the forest: two derivations of the same path."""
    parsed = _walk(path)
    rebuilt = _paths_from_forest(parsed.sections)
    assert {node.id: nodes_mod.section_of(parsed, node.id) for node in parsed.nodes} == {
        node.id: rebuilt.get(node.id, []) for node in parsed.nodes
    }


@pytest.mark.parametrize("path", ALL_DOCX, ids=lambda path: path.name)
def test_section_of_is_the_path_the_walk_put_on_the_node(path):
    parsed = _walk(path)
    for node in parsed.nodes:
        assert nodes_mod.section_of(parsed, node.id) == node.section_path
    assert nodes_mod.section_of(parsed, "<<no such node>>") == []
    assert nodes_mod.section_of(parsed, "") == []


@pytest.mark.parametrize("path", ALL_DOCX, ids=lambda path: path.name)
def test_a_headings_own_path_ends_with_its_accepted_view_text(path):
    """A path names the section by what the *reader* sees: the accepted view's heading text."""
    parsed = _walk(path)
    streams = {stream.part_id: stream for stream in parsed.union_streams}
    for node in parsed.nodes:
        if node.kind is not NodeKind.HEADING or not node.spans:
            continue
        path_ = nodes_mod.section_of(parsed, node.id)
        if not path_:
            continue
        span = node.spans[0]
        assert path_[-1] == project(streams[node.part_id], View.ACCEPTED, span.start, span.end).text


def test_a_part_outside_the_body_is_outside_every_section():
    """The outline is the body's (4b): a header, a footer and a comment carry no path."""
    parsed = _walk(DOCX_BY_NAME["program_review_v3.docx"])
    outside = [node for node in parsed.nodes if node.part_id != BODY]
    assert outside and all(nodes_mod.section_of(parsed, node.id) == [] for node in outside)
    body = [node for node in parsed.nodes if node.part_id == BODY]
    assert body and any(nodes_mod.section_of(parsed, node.id) for node in body)


def test_a_node_the_forest_does_not_cover_gets_the_empty_path():
    """``spec_threaded``'s two comment paragraphs are nodes the body's outline does not cover."""
    parsed = _walk(DOCX_BY_NAME["spec_threaded.docx"])
    covered = set(_paths_from_forest(parsed.sections))
    outside = [node for node in parsed.nodes if node.id not in covered]
    assert outside and all(nodes_mod.section_of(parsed, node.id) == [] for node in outside)


def test_the_answer_is_a_copy_a_caller_cannot_use_to_edit_the_node():
    parsed = _walk(DOCX_BY_NAME["binder_summary.docx"])
    node = next(node for node in parsed.nodes if node.section_path)
    answer = nodes_mod.section_of(parsed, node.id)
    assert answer == node.section_path and answer is not node.section_path
    answer.append("junk")
    assert nodes_mod.section_of(parsed, node.id) == node.section_path


# --- the move ------------------------------------------------------------------------


def test_the_helpers_are_public_and_the_matcher_reads_the_same_ones():
    """0c's point: one public copy, no private one left behind for a consumer to reach into."""
    for name in ("parents", "ancestor_of_kind", "anchor_node", "section_of"):
        assert callable(getattr(nodes_mod, name))
    for private in ("_parents", "_ancestor_of_kind", "_anchor_node", "_depth"):
        assert not hasattr(terms_mod, private)
    assert terms_mod.parents is nodes_mod.parents
    assert terms_mod.ancestor_of_kind is nodes_mod.ancestor_of_kind
    assert terms_mod.anchor_node is nodes_mod.anchor_node
