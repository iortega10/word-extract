"""Turn 4b: the section tree the heading verdict defines, and every node's section_path.

Turn 4a decides *which* paragraphs are headings -- re-derived from the parts' XML in
``tests/test_walker.py``. What is re-derived **here** is the tree that verdict implies: the
nesting a level implies, what belongs to which section, the text a section is named after
and the path a node carries. No claim leans on :func:`wordextract.sections.build`: each is
either a hand-typed sidecar literal (the spec fixtures' ``sections``) or an independent
rebuild from the walk's own facts -- the kind 4a decided, the level the paragraph states,
the style it names, and document order.

The level a section sits at is the one thing 4b adds to 4a's facts, and its fallbacks are
pinned one package each: Word's outline level *n* is level *n + 1*, a style that only
*names* a level gives that level (a ``Heading3`` whose definition is missing, which is the
``spec_threaded`` fixture's case), and a heading that names no level at all is level 0 --
above every numbered heading, never silently nested under one.

``section_path`` is a **field**, never a key: it is derived, so it is read here out of the
walk and nothing a consumer keys on moves. The outline is the body's part; a heading in a
header, a footnote or a comment opens no section.
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

import pytest

from wordextract import opc
from wordextract import sections as sections_mod
from wordextract.evals import labels
from wordextract.model import HeadingDetection, Node, NodeKind, Section, View
from wordextract.views import project
from wordextract.walker import walk_document

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ALL_DOCX = sorted(FIXTURES.rglob("*.docx"))
DOCX_BY_NAME = {path.name: path for path in ALL_DOCX}
SIDECARS = labels.iter_sidecars(FIXTURES)
#: The sidecars that label an outline: the four spec fixtures. The model sidecars pin
#: paragraph literals, span stacks and view strings instead, and say nothing about sections.
OUTLINED = sorted(name for name, sidecar in SIDECARS.items() if sidecar.sections)
RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
W_ATTRS = f'xmlns:w="{opc.W_NS}" xmlns:w14="{opc.W14_NS}" xmlns:r="{opc.R_NS}"'


def _walk(path):
    """``path``'s parse and the part id of its body -- the only part an outline covers."""
    package = opc.Package(path)
    return walk_document(package), package.document.part_id


def _all_sections(forest):
    """Every section of the forest in document order: a parent before its children."""
    for section in forest:
        yield section
        yield from _all_sections(section.children)


def _flatten(forest) -> list[tuple[str, int]]:
    """The outline as a labelled list holds it: depth first, ``(title, level)``."""
    return [(section.title, section.level) for section in _all_sections(forest)]


def _by_text(parsed) -> dict[str, str]:
    """The body's text nodes by their union text, so a package built here is readable."""
    stream = parsed.union_streams[0]
    return {
        stream.text[node.spans[0].start : node.spans[0].end]: node.id
        for node in parsed.nodes
        if node.spans and node.part_id == stream.part_id
    }


def _level(node: Node) -> int | None:
    """The level a heading states, restated: outline 0-8 plus one, else the styled level,
    else 0 for a Title, else ``None`` (unlevelled). Outline level 9 is body text."""
    if node.level is not None and 0 <= node.level <= 8:
        return node.level + 1
    match = re.fullmatch(r"heading\s*([1-9])", node.style or "", re.IGNORECASE)
    if match:
        return int(match.group(1))
    return 0 if (node.style or "").casefold() == "title" else None


def _rebuild(nodes, part_id) -> list:
    """The forest rebuilt from the walk's facts alone, as ``[level, node_ids, children]``.

    A second implementation of the same stack, deliberately: what it shares with
    ``sections.build`` is the *rule*, not any code -- so a tree that nests wrongly fails
    here even though both sides read the same nodes.
    """
    roots: list = []
    open_sections: list = []
    for node in nodes:
        if node.part_id != part_id:
            continue
        if node.kind is not NodeKind.HEADING:
            if open_sections:
                open_sections[-1][1].append(node.id)
            continue
        stated = _level(node)
        if stated is None:
            above = [s for s in open_sections if s[3]]
            level = above[-1][0] + 1 if above else 1
        else:
            level = stated
        while open_sections and open_sections[-1][0] >= level:
            open_sections.pop()
        section = [level, [], [], stated is not None]
        (open_sections[-1][2] if open_sections else roots).append(section)
        open_sections.append(section)
    return roots


def _shape(section: Section) -> tuple:
    return (
        section.level,
        tuple(section.node_ids),
        tuple(_shape(child) for child in section.children),
    )


def _rebuilt_shape(section: list) -> tuple:
    return (section[0], tuple(section[1]), tuple(_rebuilt_shape(child) for child in section[2]))


def _is_subsequence(text: str, of: str) -> bool:
    """``text`` is ``of`` with characters deleted -- all a view can ever do to a range."""
    rest = iter(of)
    return all(character in rest for character in text)


# --- the hand-typed outlines -------------------------------------------------------


@pytest.mark.parametrize("name", OUTLINED, ids=str)
def test_every_sidecar_outline_is_the_walks_section_tree(name):
    """The walk's outline, flattened depth first, is the labels' ``(text, level)`` in order.

    The spec fixtures' outlines are hand-typed ground truth: not only that every heading is
    found, but that it sits at the level Word's own navigation puts it at and that the
    document order survives.
    """
    sidecar = SIDECARS[name]
    parsed, _ = _walk(DOCX_BY_NAME[sidecar.fixture])
    labelled = sorted(sidecar.sections, key=lambda entry: entry.order)
    assert [entry.order for entry in labelled] == list(range(len(labelled)))
    assert _flatten(parsed.sections) == [(entry.text, entry.level) for entry in labelled]


@pytest.mark.parametrize("name", OUTLINED, ids=str)
def test_a_spec_outline_is_one_section_per_body_heading_and_no_other(name):
    """A section per body heading node, named after it, opening or joining nothing else."""
    sidecar = SIDECARS[name]
    parsed, body = _walk(DOCX_BY_NAME[sidecar.fixture])
    headings = [
        node.id for node in parsed.nodes if node.part_id == body and node.kind is NodeKind.HEADING
    ]
    assert [section.heading_id for section in _all_sections(parsed.sections)] == headings
    assert len(headings) == len(sidecar.sections)


def test_the_program_review_outline_nests_four_level_ones_under_its_title():
    """The one spec fixture with a real tree: a level-0 title with four level-1 sections."""
    parsed, _ = _walk(DOCX_BY_NAME["program_review_v3.docx"])
    (title,) = parsed.sections
    assert (title.title, title.level) == ("Program Review: Consultants Service Agreement", 0)
    assert [(child.title, child.level) for child in title.children] == [
        ("1. Coverage Terms", 1),
        ("2. Exclusions", 1),
        ("3. Fee and Rate Schedule", 1),
        ("4. Notes", 1),
    ]
    assert all(child.children == [] for child in title.children)


def test_a_level_one_heading_with_no_title_above_it_is_the_root():
    """``edge_cases`` states a level-1 heading and no level-0 one: no root is invented and
    the section is the forest's own root, its level untouched."""
    parsed, _ = _walk(DOCX_BY_NAME["edge_cases.docx"])
    assert [(section.title, section.level) for section in parsed.sections] == [("Edge Cases", 1)]


# --- the tree over the corpus ------------------------------------------------------


@pytest.mark.parametrize("path", ALL_DOCX, ids=lambda path: path.name)
def test_the_tree_is_the_one_the_nodes_facts_imply(path):
    """Nesting, membership and levels, rebuilt from kinds, levels, styles and order alone."""
    parsed, body = _walk(path)
    assert [_shape(section) for section in parsed.sections] == [
        _rebuilt_shape(section) for section in _rebuild(parsed.nodes, body)
    ]


@pytest.mark.parametrize("path", ALL_DOCX, ids=lambda path: path.name)
def test_every_body_heading_opens_a_section_and_only_a_heading_does(path):
    """4b reads 4a's verdict, it never re-decides it: the sections are the body's heading
    nodes in order, and a heading is never a member of the section it opens."""
    parsed, body = _walk(path)
    headings = [
        node.id for node in parsed.nodes if node.part_id == body and node.kind is NodeKind.HEADING
    ]
    assert [section.heading_id for section in _all_sections(parsed.sections)] == headings
    by_id = {node.id: node for node in parsed.nodes}
    assert all(by_id[heading_id].kind is NodeKind.HEADING for heading_id in headings)
    for section in _all_sections(parsed.sections):
        assert all(by_id[node_id].kind is not NodeKind.HEADING for node_id in section.node_ids)


@pytest.mark.parametrize("path", ALL_DOCX, ids=lambda path: path.name)
def test_the_sections_partition_the_body_from_the_first_heading_on(path):
    """Every body node opens a section, belongs to exactly one, or stands before the first
    heading -- which belongs to none (a preamble names no section of its own)."""
    parsed, body = _walk(path)
    order = {node.id: index for index, node in enumerate(parsed.nodes)}
    members: list[str] = []
    for section in _all_sections(parsed.sections):
        assert [order[node_id] for node_id in section.node_ids] == sorted(
            order[node_id] for node_id in section.node_ids
        ), "a section's members are in document order"
        assert section.heading_id not in section.node_ids
        members.extend(section.node_ids)
    assert len(members) == len(set(members)), "a node belongs to one section only"
    opened = {section.heading_id for section in _all_sections(parsed.sections)}
    assert not opened & set(members)
    covered = opened | set(members)
    assert all(node.part_id == body for node in parsed.nodes if node.id in covered)
    if not parsed.sections:
        assert members == []
        assert all(node.part_id != body or not node.section_path for node in parsed.nodes)
        return
    free = [node.id for node in parsed.nodes if node.part_id == body and node.id not in covered]
    assert all(order[node_id] < order[parsed.sections[0].heading_id] for node_id in free)


@pytest.mark.parametrize("path", ALL_DOCX, ids=lambda path: path.name)
def test_section_paths_are_the_chain_of_titles_down_to_the_nodes_own_section(path):
    """A path is every enclosing title, outermost first, ending with the node's own -- so a
    child section's path is its parent's plus the child's own title, and a node outside the
    body has no path at all."""
    parsed, body = _walk(path)
    paths: dict[str, list[str]] = {}

    def walk_tree(forest, prefix):
        for section in forest:
            path = [*prefix, section.title]
            paths[section.heading_id] = path
            for node_id in section.node_ids:
                paths[node_id] = path
            walk_tree(section.children, path)

    walk_tree(parsed.sections, [])
    by_id = {node.id: node for node in parsed.nodes}
    for node in parsed.nodes:
        assert node.section_path == paths.get(node.id, []), node.source_ref
        if node.part_id != body:
            assert node.section_path == [], node.source_ref
    for section in _all_sections(parsed.sections):
        assert by_id[section.heading_id].section_path[-1:] == [section.title]
        for child in section.children:
            assert by_id[child.heading_id].section_path == [
                *by_id[section.heading_id].section_path,
                child.title,
            ]


@pytest.mark.parametrize("path", ALL_DOCX, ids=lambda path: path.name)
def test_a_sections_title_is_its_headings_own_text_in_the_accepted_view(path):
    """What the reader calls the section -- and nothing a view cannot do to a range: the
    title is the heading's own span projected through ``accepted``."""
    parsed, _ = _walk(path)
    streams = {stream.part_id: stream for stream in parsed.union_streams}
    by_id = {node.id: node for node in parsed.nodes}
    for section in _all_sections(parsed.sections):
        node = by_id[section.heading_id]
        (span,) = node.spans
        stream = streams[node.part_id]
        accepted = project(stream, View.ACCEPTED, span.start, span.end).text
        assert section.title == accepted, node.source_ref
        assert _is_subsequence(section.title, stream.text[span.start : span.end]), node.source_ref


@pytest.mark.parametrize("path", ALL_DOCX, ids=lambda path: path.name)
def test_walking_twice_builds_the_same_tree_and_the_same_paths(path):
    first, _ = _walk(path)
    second, _ = _walk(path)
    assert first.sections == second.sections
    assert [(node.id, node.section_path) for node in first.nodes] == [
        (node.id, node.section_path) for node in second.nodes
    ]


def test_the_section_path_is_a_field_of_no_key():
    """Derived, so it is stored on the node and nowhere else: the path is not a hashed
    input, and the section record's own fields are fixed."""
    from dataclasses import fields

    from wordextract.versions import HashedInputs

    assert "section_path" in {field.name for field in fields(Node)}
    assert not any("section" in field.name for field in fields(HashedInputs))
    assert [field.name for field in fields(Section)] == [
        "heading_id",
        "title",
        "level",
        "node_ids",
        "children",
    ]


# --- packages built here, so a fallback is testable without a Word file -------------


def _p(
    text: str,
    *,
    style: str | None = None,
    outline: int | None = None,
    num_id: str | None = None,
    bold: bool = False,
) -> str:
    """One ``w:p``: optionally a ``pStyle``, an own ``outlineLvl``, a numPr, or a bold run."""
    properties = ""
    if style is not None:
        properties += f'<w:pStyle w:val="{style}"/>'
    if outline is not None:
        properties += f'<w:outlineLvl w:val="{outline}"/>'
    if num_id is not None:
        properties += f'<w:numPr><w:ilvl w:val="0"/><w:numId w:val="{num_id}"/></w:numPr>'
    pre = f"<w:pPr>{properties}</w:pPr>" if properties else ""
    bold_run = f'<w:r><w:rPr><w:b/></w:rPr><w:t>{text}</w:t></w:r>'
    plain_run = f"<w:r><w:t>{text}</w:t></w:r>"
    return f"<w:p>{pre}{bold_run if bold else plain_run}</w:p>"


def _style(style_id: str, *, outline: int | None = None) -> str:
    """A one-style ``styles.xml``: the style's id as its name, plus an outline level."""
    p_pr = f'<w:pPr><w:outlineLvl w:val="{outline}"/></w:pPr>' if outline is not None else ""
    return (
        f'<w:style w:type="paragraph" w:styleId="{style_id}"><w:name w:val="{style_id}"/>'
        f"{p_pr}</w:style>"
    )


def _docx(
    tmp_path: Path,
    body: str,
    *,
    styles: str | None = None,
    numbering: str | None = None,
    header: str | None = None,
):
    """A minimal package: the body, optionally a styles, numbering and header part."""
    document_rels = ""
    if numbering is not None:
        document_rels += (
            f'<Relationship Id="rId2" Type="{opc.RT_NUMBERING}" Target="numbering.xml"/>'
        )
    if styles is not None:
        document_rels += f'<Relationship Id="rId3" Type="{opc.RT_STYLES}" Target="styles.xml"/>'
    if header is not None:
        document_rels += f'<Relationship Id="rId5" Type="{opc.RT_HEADER}" Target="header1.xml"/>'
    members = {
        "[Content_Types].xml": (
            f'<Types xmlns="{TYPES_NS}">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            f'<Relationships xmlns="{RELS_NS}">'
            f'<Relationship Id="rId1" Type="{opc.RT_OFFICE_DOCUMENT}" '
            'Target="word/document.xml"/></Relationships>'
        ),
        "word/document.xml": f"<w:document {W_ATTRS}><w:body>{body}</w:body></w:document>",
    }
    if document_rels:
        members["word/_rels/document.xml.rels"] = (
            f'<Relationships xmlns="{RELS_NS}">{document_rels}</Relationships>'
        )
    if numbering is not None:
        members["word/numbering.xml"] = f"<w:numbering {W_ATTRS}>{numbering}</w:numbering>"
    if styles is not None:
        members["word/styles.xml"] = f"<w:styles {W_ATTRS}>{styles}</w:styles>"
    if header is not None:
        members["word/header1.xml"] = f"<w:hdr {W_ATTRS}>{header}</w:hdr>"
    path = tmp_path / "synth.docx"
    with zipfile.ZipFile(path, "w") as archive:
        for member, data in members.items():
            archive.writestr(member, data.encode("utf-8"))
    return path


def _synth(tmp_path, body: str, **parts):
    """``body``'s parse and the part id of its body."""
    return _walk(_docx(tmp_path, body, **parts))


def test_title_is_level_zero_and_a_lone_unlevelled_heading_is_level_one(tmp_path):
    """``Title`` names level 0 -- above every numbered heading. The all-bold rule states no
    level at all: with nothing levelled open it takes level 1, so a document of bold
    headings is a flat list of siblings rather than a chain."""
    parsed, _ = _synth(tmp_path, _p("The title", style="Title") + _p("Body text"))
    assert _flatten(parsed.sections) == [("The title", 0)]
    parsed, _ = _synth(tmp_path, _p("Bold heading", bold=True) + _p("Body text"))
    assert _flatten(parsed.sections) == [("Bold heading", 1)]


def test_a_heading_style_that_names_a_level_gives_the_section_that_level(tmp_path):
    """A ``Heading3`` with no styles part states no outline level, but it names one: the
    name is what the section is built on, which is the ``spec_threaded`` fixture's case."""
    parsed, _ = _synth(tmp_path, _p("Named", style="Heading3") + _p("Body text"))
    assert _flatten(parsed.sections) == [("Named", 3)]


def test_the_stated_outline_level_wins_over_the_level_the_style_names(tmp_path):
    """A ``Heading3`` set to outline level 0 is Word's own first-level heading: the level
    the paragraph states is the one its navigation and its numbering use."""
    parsed, _ = _synth(tmp_path, _p("Stated", style="Heading3", outline=0) + _p("Body text"))
    assert _flatten(parsed.sections) == [("Stated", 1)]
    parsed, _ = _synth(
        tmp_path,
        _p("Both", style="Heading1") + _p("Body"),
        styles=_style("Heading1", outline=2),
    )
    assert _flatten(parsed.sections) == [("Both", 3)]


def test_a_localized_heading_style_states_the_only_level_it_has(tmp_path):
    """``Titre 2`` names nothing 4b can read, so its own ``outlineLvl`` is its level: 1 -> 2."""
    parsed, _ = _synth(
        tmp_path,
        _p("Titre", style="Localized2") + _p("Body"),
        styles=_style("Localized2", outline=1),
    )
    assert _flatten(parsed.sections) == [("Titre", 2)]


def test_a_level_jump_nests_under_the_last_shallower_heading(tmp_path):
    """No intermediate section is invented: a level-3 heading under a level-1 one is its
    child, and the next level-1 heading closes both."""
    body = (
        _p("One", style="Heading1")
        + _p("under one")
        + _p("Deep", style="Heading3")
        + _p("under deep")
        + _p("Two", style="Heading1")
        + _p("under two")
    )
    parsed, _ = _synth(tmp_path, body)
    ids = _by_text(parsed)
    one, two = parsed.sections
    (deep,) = one.children
    assert (one.title, one.level, one.node_ids) == ("One", 1, [ids["under one"]])
    assert (deep.title, deep.level, deep.node_ids, deep.children) == (
        "Deep",
        3,
        [ids["under deep"]],
        [],
    )
    assert (two.title, two.level, two.node_ids) == ("Two", 1, [ids["under two"]])
    assert ids["One"] not in one.node_ids


def test_a_section_holds_the_containers_between_its_headings(tmp_path):
    """A table is a member of its section -- rows, cells and their paragraphs with it, in
    document order -- and the heading that opens the section is not one of its members."""
    table = "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
    parsed, _ = _synth(tmp_path, _p("Section", style="Heading1") + table + _p("After"))
    (section,) = parsed.sections
    assert section.node_ids == [
        node.id for node in parsed.nodes if node.kind is not NodeKind.HEADING
    ]
    assert {node.kind for node in parsed.nodes if node.id in section.node_ids} == {
        NodeKind.TABLE,
        NodeKind.ROW,
        NodeKind.CELL,
        NodeKind.PARA,
    }
    assert all(node.section_path == ["Section"] for node in parsed.nodes)


def test_a_list_item_under_a_heading_is_a_member_and_opens_nothing(tmp_path):
    """A numbered list inside a section is the section's, item by item: a ``LIST_ITEM`` is
    never a heading, so it never opens a section of its own."""
    numbering = (
        '<w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:start w:val="1"/>'
        '<w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl></w:abstractNum>'
        '<w:num w:numId="5"><w:abstractNumId w:val="0"/></w:num>'
    )
    body = _p("Section", style="Heading1") + _p("One", num_id="5") + _p("Two", num_id="5")
    parsed, _ = _synth(tmp_path, body, numbering=numbering)
    (section,) = parsed.sections
    assert [node.kind for node in parsed.nodes if node.spans] == [
        NodeKind.HEADING,
        NodeKind.LIST_ITEM,
        NodeKind.LIST_ITEM,
    ]
    assert section.node_ids == [node.id for node in parsed.nodes if node.kind is NodeKind.LIST_ITEM]
    assert all(node.section_path == ["Section"] for node in parsed.nodes)


def test_a_heading_outside_the_body_opens_no_section(tmp_path):
    """The outline is the body's: a header's heading is decided like any other, and still
    opens nothing -- it is in no section's members and carries no path."""
    body = _p("Body heading", style="Heading1") + _p("Body text")
    header = _p("Header heading", style="Heading1") + _p("Header text")
    parsed, body_part = _synth(tmp_path, body, header=header)
    assert _flatten(parsed.sections) == [("Body heading", 1)]
    elsewhere = [node for node in parsed.nodes if node.part_id != body_part]
    assert {node.part_id for node in elsewhere} == {"header:0"}
    assert {node.kind for node in elsewhere} == {NodeKind.HEADER, NodeKind.HEADING, NodeKind.PARA}
    assert all(node.section_path == [] for node in elsewhere)
    assert not {node.id for node in elsewhere} & {
        node_id for section in parsed.sections for node_id in section.node_ids
    }
    assert all(
        node.section_path == ["Body heading"]
        for node in parsed.nodes
        if node.part_id == body_part and node.kind is not NodeKind.HEADING
    )


def test_a_document_with_no_heading_degrades_to_no_sections_at_all(tmp_path):
    """Fail open: no heading, no forest and no paths -- one flat root of fully walked nodes,
    so the chunker size-chunks instead of section-chunking."""
    body = (
        _p("Just text")
        + "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
        + _p("More text")
    )
    parsed, _ = _synth(tmp_path, body)
    assert parsed.sections == []
    assert parsed.heading_detection is HeadingDetection.DEGRADED
    assert all(node.section_path == [] for node in parsed.nodes)
    assert [node.kind for node in parsed.nodes] == [
        NodeKind.PARA,
        NodeKind.TABLE,
        NodeKind.ROW,
        NodeKind.CELL,
        NodeKind.PARA,
        NodeKind.PARA,
    ]
    assert all(node.spans for node in parsed.nodes if node.kind in (NodeKind.PARA, NodeKind.CELL))


def test_an_edited_heading_renames_its_section_in_the_accepted_view(tmp_path):
    """The title is a view projection, never the union text: what the reader sees names the
    section, and a heading the view elides entirely names its section with the empty
    string -- no placeholder, and the section still holds what follows it."""
    deleted = (
        '<w:del w:id="2" w:author="A" w:date="2020-01-01T00:00:00Z">'
        "<w:r><w:t>Old terms</w:t></w:r></w:del>"
    )
    inserted = (
        '<w:ins w:id="1" w:author="A" w:date="2020-01-01T00:00:00Z">'
        "<w:r><w:t>Coverage</w:t></w:r></w:ins>"
    )
    heading = f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>{deleted}{inserted}</w:p>'
    parsed, _ = _synth(tmp_path, heading + _p("Body"))
    (section,) = parsed.sections
    assert section.title == "Coverage"
    assert parsed.union_streams[0].text.startswith("Old termsCoverage")
    assert [
        node.section_path for node in parsed.nodes if node.kind is NodeKind.HEADING
    ] == [["Coverage"]]

    gone = f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>{deleted}</w:p>' + _p("Body")
    elided_parsed, _ = _synth(tmp_path, gone)
    (elided,) = elided_parsed.sections
    assert elided.title == ""
    assert [node.section_path for node in elided_parsed.nodes if node.kind is NodeKind.HEADING] == [
        [""]
    ]
    assert elided.node_ids == [
        node.id for node in elided_parsed.nodes if node.kind is not NodeKind.HEADING
    ]


# --- sections.assign: the walk's entry point ---------------------------------------


def test_assign_returns_the_forest_and_copies_the_nodes_it_was_given():
    """Assign is a re-derivation over a frozen tree: the walk's own node list and streams are
    left exactly as they were, and every node it returns is a new object equal to the walk's
    own -- the walk assigns once (``walker.walk_document`` calls this), so the tree it built
    is the tree it assigned."""
    parsed, body = _walk(DOCX_BY_NAME["program_review_v3.docx"])
    before, streams = list(parsed.nodes), list(parsed.union_streams)
    nodes, forest = sections_mod.assign(parsed.nodes, parsed.union_streams, body)
    assert forest == parsed.sections
    assert list(parsed.nodes) == before
    assert parsed.union_streams == streams
    assert [node.id for node in nodes] == [node.id for node in before]
    assert all(new is not old for new, old in zip(nodes, parsed.nodes))
    assert nodes == before
    again, again_forest = sections_mod.assign(parsed.nodes, parsed.union_streams, body)
    assert again == nodes and again_forest == forest


def test_build_is_the_forest_assign_assigns_and_covers_the_body_only():
    parsed, body = _walk(DOCX_BY_NAME["ledger_summary.docx"])
    assert sections_mod.build(parsed.nodes, parsed.union_streams, body) == parsed.sections
    assert sections_mod.build(parsed.nodes, parsed.union_streams, "header:0") == []
    assert sections_mod.build([], parsed.union_streams, body) == []


# --- an unlevelled heading nests where it stands; it never resets the outline ---------


def _paths(parsed) -> dict[str, list[str]]:
    stream = parsed.union_streams[0]
    return {
        stream.text[node.spans[0].start : node.spans[0].end]: node.section_path
        for node in parsed.nodes
        if node.spans and node.part_id == stream.part_id
    }


def test_a_bold_line_mid_section_nests_under_the_innermost_levelled_section(tmp_path):
    """The defect this pins: a bold 'Important notice' popped every open section and the
    headings after it landed under it. Hand-typed: Subquotas is Coverage's, Exclusions the
    Title's, and the notice sits inside Limits."""
    body = (
        _p("Permit", style="Title")
        + _p("Coverage", style="Heading1")
        + _p("Limits", style="Heading2")
        + _p("limits text")
        + _p("Important notice", bold=True)
        + _p("notice text")
        + _p("Subquotas", style="Heading2")
        + _p("sub text")
        + _p("Exclusions", style="Heading1")
    )
    parsed, _ = _synth(tmp_path, body)
    assert _flatten(parsed.sections) == [
        ("Permit", 0),
        ("Coverage", 1),
        ("Limits", 2),
        ("Important notice", 3),
        ("Subquotas", 2),
        ("Exclusions", 1),
    ]
    paths = _paths(parsed)
    assert paths["notice text"] == ["Permit", "Coverage", "Limits", "Important notice"]
    assert paths["sub text"] == ["Permit", "Coverage", "Subquotas"]
    assert paths["Exclusions"] == ["Permit", "Exclusions"]


def test_consecutive_unlevelled_headings_are_siblings_not_a_chain(tmp_path):
    body = (
        _p("Coverage", style="Heading1")
        + _p("First notice", bold=True)
        + _p("one")
        + _p("Second notice", bold=True)
        + _p("two")
    )
    parsed, _ = _synth(tmp_path, body)
    assert _flatten(parsed.sections) == [("Coverage", 1), ("First notice", 2), ("Second notice", 2)]
    (root,) = parsed.sections
    assert [child.title for child in root.children] == ["First notice", "Second notice"]


def test_a_document_of_unlevelled_headings_is_a_flat_list(tmp_path):
    body = _p("Alpha", bold=True) + _p("a") + _p("Beta", bold=True) + _p("b")
    parsed, _ = _synth(tmp_path, body)
    assert [(s.title, s.level) for s in parsed.sections] == [("Alpha", 1), ("Beta", 1)]
    assert all(not s.children for s in parsed.sections)


def test_an_unlevelled_heading_then_a_level_one_heading_are_siblings(tmp_path):
    body = _p("Preamble heading", bold=True) + _p("x") + _p("Coverage", style="Heading1") + _p("y")
    parsed, _ = _synth(tmp_path, body)
    assert [(s.title, s.level) for s in parsed.sections] == [("Preamble heading", 1), ("Coverage", 1)]


def test_an_unlevelled_heading_under_a_title_is_its_child(tmp_path):
    body = _p("Permit", style="Title") + _p("Notice", bold=True) + _p("x")
    parsed, _ = _synth(tmp_path, body)
    assert _flatten(parsed.sections) == [("Permit", 0), ("Notice", 1)]


def test_a_heading_one_paragraph_set_to_body_text_level_takes_the_level_its_style_names(tmp_path):
    """Outline level 9 is Word's body text, so it states no level: the style name decides."""
    parsed, _ = _synth(tmp_path, _p("Demoted", style="Heading1", outline=9) + _p("Body text"))
    assert _flatten(parsed.sections) == [("Demoted", 1)]
