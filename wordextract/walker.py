"""Turns 2a-2c: the per-part union stream, the node tree and the revision facts.

2a: this is the production replacement for the ``tools/opc_spike.py`` scaffold. It
builds one :class:`~wordextract.model.UnionStream` per part that holds at least one
``w:p``: the literal union text plus the elementary spans that tile it exactly, each
span a maximal run of equal ancestor stack (outermost first).

The rules below are fixed by ``text-model-spec.md`` (and its "Text-model decisions"
in the build spec). Nothing here guesses at them:

* one ``"\\n"`` terminator span per paragraph, empty stack, always its own span and
  never coalesced with the empty-stack content around it, so the spans tile the
  union exactly;
* a span is a maximal run of equal ancestor stack, so ``w:tab``, every ``w:br``,
  ``w:cr``, ``w:sym``, ``w:softHyphen``, ``w:noBreakHyphen`` and a literal ``\\n`` /
  ``\\r`` inside ``w:t`` are content **inside** a span, never boundaries;
* ``w:t`` and ``w:delText`` are content, ``w:instrText`` never is, and field text is
  emitted only once **every** open field has passed its ``separate`` (a ``w:fldChar``
  depth counter, so nested fields resolve; ``w:fldSimple``'s instruction is its
  attribute and is never read);
* revision ancestry is the ancestor stack, ``<kind>:<w:id>``;
* ``w:txbxContent`` is never entered: text-box text belongs to its own fragment and
  is not part of any part's union stream (a later slice, hence no fragment stream);
  the omission is recorded as the ``textbox`` known gap so it is never silent;
* a nested field's result inside an outer field's instruction is still instruction;
* descent is an explicit allow-list and every drawing is skipped. An unknown ``w:``
  container contributes nothing rather than something invented, **but the loss is
  recorded**: a skipped element that holds text is the ``unrecognized_container``
  known gap. A revision with no ``w:id`` gets a deterministic ``<kind>:noid<n>`` id
  (per-part document order) and the ``revision_missing_id`` gap.

The union is only an address space: view text and the view/union offset maps are
derived on demand from it (Turn 3), and the union itself is never matched or
summarized.

2b adds the nodes over that address space. A paragraph is a **heading** when its own
``w:pStyle`` is ``heading`` plus a level 1-9 (case-insensitive) -- a numbered heading is
still a heading, and carries its label -- else a **list item** when it is numbered, by
its own ``w:numPr`` or by the numbering its style defines (``styles.py``: ``basedOn``
chain, nearest definition wins). ``style`` is the paragraph's own ``w:pStyle`` and
``level`` its own ``w:outlineLvl``, else the one its style chain defines (never a
resolved font or spacing cascade). The numbering label comes from a small counter over
``numbering.xml``. ``table``, ``row`` and a block ``w:sdt`` are containers: their text
belongs to the nodes under them, so they carry no span and list ``child_ids``.
``header``, ``footer`` and ``footnote`` are the same kind of container -- a part's own
node, whose text is addressed by the paragraph nodes under it -- and every
``w:footnote`` / ``w:endnote`` element is one, Word's ``w:type="separator"`` plumbing
included (skipping them would be an unstated rule; their empty paragraphs carry a
zero-width span and a positional id). An **inline** ``w:sdt`` is transparent: no
node, no boundary, no ancestor -- and since its content control-ness is dropped, it
is reported as ``inline_sdt_transparent``.

Node ids (2b): Word's own ``w14:paraId`` when the element carries one and the package
has not already spent it; else a content hash of what the node is and says -- part,
kind, own union text, style, level, deliberately never the numbering label -- with the
ordinal counting equal-content nodes in document order; else, for a node with **no
text at all**, its position (``path:``), which renumbers on insertion and is flagged
``IdStability.PATH``. Ids are reserved package-wide, in part order, so a repeat
``w14:paraId`` falls through to the hash.

2c adds the raw revision facts. Every ``w:ins`` / ``w:del`` / ``w:moveFrom`` /
``w:moveTo`` met is one :class:`~wordextract.model.Revision` -- ``<kind>:<w:id>``, or
``<kind>:noid<n>`` when the mark carries no ``w:id`` -- with the mark's ``author``
(``""`` when absent), its ``date`` (``None`` when absent), the ancestor stack it sat
in (outermost first, itself excluded) and, for a move, the ``move_group_id``: the
``w:name`` shared by the ``moveFromRangeStart`` / ``moveToRangeStart`` markers that
wrap the mark, never the ``w:id`` of the mark or of the markers. The same id met twice
is **one** record, the first in document order; the mark still opens an ancestor frame
for the text under it. Nothing is synthesized: a ``w:del`` is never paired with its
``w:ins``. A deleted or inserted **paragraph mark** (``w:pPr/w:rPr/w:del|ins``) is a
fact like any other, and because its effect is not honored -- the break is retained in
every view -- the omission is recorded as the loud ``paragraph_mark_revision`` gap
(``text-model-spec.md`` section 7).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from lxml import etree

from docextract_core import sha256_json

from .model import (
    ElementarySpan,
    IdStability,
    Node,
    NodeKind,
    ParseResult,
    Revision,
    RevisionKind,
    Span,
    UnionStream,
)
from .opc import W14_NS, Package, Part, is_w, local_name, wattr
from .styles import StyleFacts, Styles

#: One per paragraph, empty stack: the paragraph boundary in the union coordinate
#: space (``text-model-spec.md`` section 1).
TERMINATOR = "\n"

_VERTICAL_TAB = "\u000b"
_SOFT_HYPHEN = "\u00ad"
_NO_BREAK_HYPHEN = "\u2011"

_REVISION_KINDS = {
    "ins": RevisionKind.INS,
    "del": RevisionKind.DEL,
    "moveFrom": RevisionKind.MOVE_FROM,
    "moveTo": RevisionKind.MOVE_TO,
}

#: The four revision marks, in document order.
_REVISION_LOCALS = tuple(_REVISION_KINDS)

#: The move-range markers that wrap a move: a ``...RangeStart`` opens a range and
#: carries the shared ``w:name``, its ``...RangeEnd`` carries only the marker's
#: ``w:id`` and closes it. A marker is never itself a revision record.
_MOVE_RANGE_START = {"moveFromRangeStart": "moveFrom", "moveToRangeStart": "moveTo"}
_MOVE_RANGE_END = frozenset({"moveFromRangeEnd", "moveToRangeEnd"})

#: The revision marks a range wraps, outermost run first.
_MOVE_MARKS = frozenset(_MOVE_RANGE_START.values())

#: Known-gap ids this module can report (``docs/design/phase1-gaps.md``).
GAP_TEXTBOX = "textbox"
GAP_UNRECOGNIZED_CONTAINER = "unrecognized_container"
GAP_REVISION_MISSING_ID = "revision_missing_id"
GAP_PARAGRAPH_MARK_REVISION = "paragraph_mark_revision"
GAP_INLINE_SDT_TRANSPARENT = "inline_sdt_transparent"
GAP_DUPLICATE_CONTENT_ID = "duplicate_content_id_churn"
GAP_STYLE_CHAIN_CYCLE = "style_chain_cycle"

#: A paragraph's own ``w:pStyle`` is a heading style when it is ``heading`` plus one
#: level 1-9; the style name is matched, never a resolved style definition.
_HEADING_STYLE = re.compile(r"heading\s*([1-9])", re.IGNORECASE)

#: ``w:lvlText``'s ``%N`` placeholder: the counter of level ``N - 1``.
_PLACEHOLDER = re.compile(r"%([1-9]|1[0-9])")

#: The block-level element locals that open a node. A paragraph opens its own kind
#: (``_paragraph_facts``), so it is not listed here.
_KINDS_BY_LOCAL = {
    "tbl": NodeKind.TABLE,
    "tr": NodeKind.ROW,
    "tc": NodeKind.CELL,
    "hdr": NodeKind.HEADER,
    "ftr": NodeKind.FOOTER,
    "footnote": NodeKind.FOOTNOTE,
    "endnote": NodeKind.FOOTNOTE,
    "sdt": NodeKind.SDT,
}

#: The node kinds whose text is their own: exactly one contiguous span, zero-width
#: when the node has no text. Every other kind is a container whose text belongs to
#: the nodes under it, so it addresses nothing itself and lists ``child_ids``.
_TEXT_NODE_KINDS = frozenset(
    {NodeKind.PARA, NodeKind.HEADING, NodeKind.LIST_ITEM, NodeKind.CELL}
)

#: Block-level containers the walk descends through. ``w:sdtContent`` carries both a
#: block sdt's paragraphs and, reached from a paragraph, an inline sdt's runs.
_BLOCK_CONTAINERS = frozenset(
    {
        "body",
        "tbl",
        "tr",
        "tc",
        "sdtContent",
        "hdr",
        "ftr",
        "footnotes",
        "endnotes",
        "footnote",
        "endnote",
        "comment",
        "customXml",
    }
)

#: Inline containers whose children are ordinary content at their document position.
_INLINE_CONTAINERS = frozenset(
    {"r", "hyperlink", "fldSimple", "smartTag", "customXml", "dir", "bdo"}
)

#: Block-level ``w:`` elements that carry no paragraph text and are skipped silently.
_SKIP_BLOCK_LOCALS = frozenset(
    {
        "sectPr",
        "tblPr",
        "tblGrid",
        "trPr",
        "tcPr",
        "sdtPr",
        "sdtEndPr",
        "bookmarkStart",
        "bookmarkEnd",
    }
)

#: Elements that never contribute text and are never descended into.
_SKIP_LOCALS = frozenset(
    {
        "pPr",
        "rPr",
        "proofErr",
        "bookmarkStart",
        "bookmarkEnd",
        "commentRangeStart",
        "commentRangeEnd",
        "commentReference",
        "lastRenderedPageBreak",
        "instrText",
    }
)


def _has_text(element: etree._Element) -> bool:
    """True when ``element`` holds any ``w:t`` / ``w:delText`` with text."""
    return any(
        (is_w(node, "t") or is_w(node, "delText")) and (node.text or "").strip()
        for node in element.iter()
        if isinstance(node.tag, str)
    )


def _holds_textbox(element: etree._Element) -> bool:
    return any(is_w(node, "txbxContent") for node in element.iter() if isinstance(node.tag, str))


def _text_of(element: etree._Element) -> str:
    """A ``w:t`` / ``w:delText`` literal: a newline inside one is content, not a break."""
    return (element.text or "").replace("\r", _VERTICAL_TAB).replace("\n", _VERTICAL_TAB)


def _sym_text(element: etree._Element) -> str:
    char = wattr(element, "char")
    if not char:
        return ""
    try:
        return chr(int(char, 16))
    except ValueError:
        return ""


def _first_w(element: etree._Element, local: str) -> etree._Element | None:
    for child in element:
        if is_w(child, local):
            return child
    return None


def _child_val(element: etree._Element | None, local: str) -> str | None:
    """The ``w:val`` of the first ``local`` child of ``element``, or ``None``."""
    if element is None:
        return None
    child = _first_w(element, local)
    return wattr(child, "val") if child is not None else None


def _int_or(value: str | None, default: int | None) -> int | None:
    """``value`` as an int, or ``default`` when it is missing or not a number."""
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _para_id(element: etree._Element) -> str | None:
    """The element's ``w14:paraId`` -- Word's own identity for it -- or ``None``.

    Matched by namespace and local name, never by a loose ``paraId`` search: a foreign
    or plain attribute is not the identity Word wrote, and adopting one would let an
    unrelated attribute decide a node's id.
    """
    return element.get(f"{{{W14_NS}}}paraId") or None


def _element_index(element: etree._Element) -> int:
    """``element``'s index among its element siblings, counted in document order.

    Nothing is skipped: a skipped ``w:tbl`` still shifts the ``w:p`` after it, so an
    index is a position in the *part's* XML, exactly as ``body[0]/p[5]`` reads.
    """
    parent = element.getparent()
    if parent is None:
        return 0
    index = 0
    for sibling in parent:
        if sibling is element:
            return index
        if isinstance(sibling.tag, str):
            index += 1
    return index


def _descent(element: etree._Element) -> list[tuple[str, int]]:
    """``element``'s position: every ancestor's local name and index, root first."""
    record: list[tuple[str, int]] = []
    node: etree._Element | None = element
    while node is not None:
        record.append((local_name(node), _element_index(node)))
        node = node.getparent()
    record.reverse()
    return record


class _PartStream:
    """One part's literal text with the elementary spans tiling it exactly."""

    def __init__(self) -> None:
        self._pieces: list[str] = []
        self._spans: list[ElementarySpan] = []
        self._length = 0
        self.paragraphs = 0
        # The stack an open span was emitted with, or None when the next emission
        # must start a new span (after a terminator, and at the start of a part).
        self._open_stack: tuple[str, ...] | None = None

    def emit(self, text: str, stack: tuple[str, ...]) -> None:
        if not text:
            return
        end = self._length + len(text)
        if self._open_stack == stack:
            previous = self._spans[-1]
            self._spans[-1] = ElementarySpan(start=previous.start, end=end, stack=list(stack))
        else:
            self._spans.append(ElementarySpan(start=self._length, end=end, stack=list(stack)))
        self._pieces.append(text)
        self._length = end
        self._open_stack = stack

    def terminate(self) -> None:
        self._spans.append(ElementarySpan(start=self._length, end=self._length + 1, stack=[]))
        self._pieces.append(TERMINATOR)
        self._length += 1
        self._open_stack = None
        self.paragraphs += 1

    @property
    def length(self) -> int:
        return self._length

    @property
    def text(self) -> str:
        return "".join(self._pieces)

    @property
    def spans(self) -> list[ElementarySpan]:
        return list(self._spans)


@dataclass(frozen=True)
class _Level:
    """One ``w:lvl``: its number format, its label template and its start value."""

    format: str
    text: str
    start: int | None


class _Numbering:
    """``numbering.xml``: the label template of a ``(numId, ilvl)`` pair (2b).

    Word's numbering is two lookups: ``w:num`` names a ``w:abstractNum``, which defines
    one ``w:lvl`` per ``w:ilvl``; a ``w:lvlOverride`` on the ``w:num`` replaces a level
    or only its start value. A ``w:lvl`` may also name the paragraph style it belongs to
    (``w:pStyle``), which is how a style whose ``w:numPr`` gives only a ``w:numId`` gets
    its level. Which numbering a *paragraph* has is decided by ``_resolve_numbering``.
    """

    def __init__(self, part: Part | None = None) -> None:
        self._abstracts: dict[str, dict[int, _Level]] = {}
        self._style_levels: dict[tuple[str, str], int] = {}
        self._numbers: dict[str, str] = {}
        self._levels: dict[tuple[str, int], _Level] = {}
        self._starts: dict[tuple[str, int], int] = {}
        if part is not None and part.tree is not None:
            self._read(part.tree)

    def level(self, num_id: str, ilvl: int) -> _Level | None:
        """The level a ``(numId, ilvl)`` pair labels with, or ``None`` when unknown."""
        abstract = self._numbers.get(num_id)
        if abstract is None:
            return None
        level = self._levels.get((num_id, ilvl)) or self._abstracts.get(abstract, {}).get(ilvl)
        if level is None:
            return None
        return _Level(level.format, level.text, self._starts.get((num_id, ilvl), level.start))

    def style_level(self, num_id: str, chain: tuple[str, ...]) -> int | None:
        """The level a style in ``chain`` is linked to by a ``w:lvl/w:pStyle``, if any."""
        abstract = self._numbers.get(num_id)
        if abstract is None:
            return None
        for style_id in chain:
            ilvl = self._style_levels.get((abstract, style_id))
            if ilvl is not None:
                return ilvl
        return None

    def _read(self, root: etree._Element) -> None:
        for child in root:
            if not isinstance(child.tag, str):
                continue
            if is_w(child, "abstractNum"):
                ident = wattr(child, "abstractNumId")
                if ident is not None:
                    self._abstracts[ident] = _levels_of(child)
                    for lvl in child:
                        style_id = _child_val(lvl, "pStyle") if is_w(lvl, "lvl") else None
                        ilvl = _int_or(wattr(lvl, "ilvl"), None) if style_id is not None else None
                        if style_id is not None and ilvl is not None:
                            self._style_levels[(ident, style_id)] = ilvl
            elif is_w(child, "num"):
                self._read_num(child)

    def _read_num(self, element: etree._Element) -> None:
        num_id = wattr(element, "numId")
        abstract = _child_val(element, "abstractNumId")
        if num_id is None or abstract is None:
            return
        self._numbers[num_id] = abstract
        for override in element:
            if not is_w(override, "lvlOverride"):
                continue
            ilvl = _int_or(wattr(override, "ilvl"), None)
            if ilvl is None:
                continue
            level = _level_of(_first_w(override, "lvl"))
            if level is not None:
                self._levels[(num_id, ilvl)] = level
            start = _int_or(_child_val(override, "startOverride"), None)
            if start is not None:
                self._starts[(num_id, ilvl)] = start


def _levels_of(element: etree._Element) -> dict[int, _Level]:
    """An ``w:abstractNum``'s ``w:lvl`` elements, keyed by ``w:ilvl``."""
    levels: dict[int, _Level] = {}
    for child in element:
        level = _level_of(child) if is_w(child, "lvl") else None
        ilvl = _int_or(wattr(child, "ilvl"), None)
        if level is not None and ilvl is not None:
            levels[ilvl] = level
    return levels


def _level_of(element: etree._Element | None) -> _Level | None:
    """One ``w:lvl`` as a label template; ``None`` when it declares no ``w:lvlText``."""
    if element is None:
        return None
    text = _child_val(element, "lvlText")
    if text is None:
        return None
    return _Level(
        format=_child_val(element, "numFmt") or "decimal",
        text=text,
        start=_int_or(_child_val(element, "start"), None),
    )


class _LabelCounter:
    """The display label of a numbered paragraph, counted over ``numbering.xml`` (2b).

    One counter per ``(numId, level)``, per part: a part is its own story, so its lists
    number from their start values. Using a *shallower* level clears the deeper ones, so
    a level restarts at its ``w:start`` when its list is picked up again; that is the
    only reset a document can express without Word's own numbering state (Word restarts
    a list by giving it a new ``w:numId``).
    """

    def __init__(self, numbering: _Numbering | None) -> None:
        self._numbering = numbering
        self._counters: dict[str, dict[int, int]] = {}
        self._starts: dict[tuple[str, int], int] = {}

    def label(self, num_id: str, ilvl: int) -> str | None:
        """The label of the next item at this level, or ``None`` when it has none.

        A ``%N`` placeholder reads level ``N - 1``'s counter, and a level that has not
        been used yet renders as ``0``: Word would render the start value of the levels
        it did use, which a document alone does not say.
        """
        numbering = self._numbering
        level = numbering.level(num_id, ilvl) if numbering is not None else None
        if level is None or level.format == "none":
            return None
        key = (num_id, ilvl)
        if level.start is not None:
            self._starts[key] = level.start
        values = self._counters.setdefault(num_id, {})
        value = values.get(ilvl)
        values[ilvl] = self._starts.get(key, 1) if value is None else value + 1
        for deeper in [open_level for open_level in values if open_level > ilvl]:
            del values[deeper]
        if level.format == "bullet":
            return level.text
        return _PLACEHOLDER.sub(
            lambda match: _format_number(values.get(int(match.group(1)) - 1, 0), level.format),
            level.text,
        )


_ROMAN = (
    (1000, "m"),
    (900, "cm"),
    (500, "d"),
    (400, "cd"),
    (100, "c"),
    (90, "xc"),
    (50, "l"),
    (40, "xl"),
    (10, "x"),
    (9, "ix"),
    (5, "v"),
    (4, "iv"),
    (1, "i"),
)


def _letters(value: int, *, upper: bool) -> str:
    """``1`` -> ``a`` (``A``), ``27`` -> ``aa``, the way Word's letter numbering counts."""
    if value < 1:
        return "0"
    first = ord("A" if upper else "a")
    out = ""
    while value > 0:
        value, remainder = divmod(value - 1, 26)
        out = chr(first + remainder) + out
    return out


def _roman(value: int, *, upper: bool) -> str:
    if value < 1:
        return "0"
    out = ""
    for amount, glyph in _ROMAN:
        while value >= amount:
            out += glyph
            value -= amount
    return out.upper() if upper else out


def _format_number(value: int, fmt: str) -> str:
    """One counter value in a level's ``w:numFmt``.

    Only the formats a Word list really uses are spelled out, because the label is
    display text and nothing downstream matches on it; an unknown format renders as its
    decimal value rather than as a guess at its glyphs.
    """
    if fmt == "decimalZero":
        return f"{value:02d}"
    if fmt == "lowerLetter":
        return _letters(value, upper=False)
    if fmt == "upperLetter":
        return _letters(value, upper=True)
    if fmt == "lowerRoman":
        return _roman(value, upper=False)
    if fmt == "upperRoman":
        return _roman(value, upper=True)
    return str(value)


def _resolve_numbering(
    p_pr: etree._Element | None,
    facts: StyleFacts,
    numbering: _Numbering | None,
) -> tuple[str, int] | None:
    """The paragraph's ``(numId, ilvl)``, or ``None`` when it is not numbered.

    Each half resolves on its own, paragraph before style: ``w:numId`` from the
    paragraph's ``w:numPr`` else its style chain, then ``w:ilvl`` from the paragraph
    else the style else the level a ``w:lvl/w:pStyle`` links the style to, else 0.
    ``w:numId 0`` is Word's "no numbering" -- it clears numbering inherited from a
    style -- so it is not a list.
    """
    num_pr = _first_w(p_pr, "numPr") if p_pr is not None else None
    num_id = _child_val(num_pr, "numId") if num_pr is not None else None
    if num_id is None:
        num_id = facts.num_id
    if num_id is None or num_id == "0":
        return None
    ilvl = _int_or(_child_val(num_pr, "ilvl"), None) if num_pr is not None else None
    if ilvl is None:
        ilvl = facts.ilvl
    if ilvl is None and numbering is not None:
        ilvl = numbering.style_level(num_id, facts.chain)
    return num_id, ilvl or 0


def _paragraph_facts(
    paragraph: etree._Element,
    styles: Styles | None = None,
    numbering: _Numbering | None = None,
) -> tuple[NodeKind, str | None, int | None, tuple[str, int] | None, bool]:
    """A paragraph's kind, style, level, numbering and whether its style chain has a cycle."""
    p_pr = _first_w(paragraph, "pPr")
    style = _child_val(p_pr, "pStyle")
    facts = styles.resolve(style) if styles is not None else StyleFacts()
    level = _int_or(_child_val(p_pr, "outlineLvl"), None)
    if level is None:
        level = facts.outline_level
    numbered = _resolve_numbering(p_pr, facts, numbering)
    if style is not None and _HEADING_STYLE.fullmatch(style):
        kind = NodeKind.HEADING
    elif numbered is not None:
        kind = NodeKind.LIST_ITEM
    else:
        kind = NodeKind.PARA
    return kind, style, level, numbered, facts.broken


@dataclass
class _Frame:
    """One node of the walk, before the walk can name it (2b).

    A node's id cannot be settled while it is open: a content-hash id needs the node's
    whole text, and its occurrence ordinal counts *document* order, not the order nodes
    happen to close in. So the walk collects frames -- text, spans, children -- and a
    second pass materializes the records, in document order, container before children.
    """

    kind: NodeKind
    element: etree._Element
    start: int
    pieces: list[str] = field(default_factory=list)
    children: list[_Frame] = field(default_factory=list)
    style: str | None = None
    level: int | None = None
    numbering_label: str | None = None
    end: int = 0
    ident: str = ""
    stability: IdStability = IdStability.PATH
    occurrence: int = 0

    @property
    def own_text(self) -> str:
        """Everything emitted while the node was open, child terminators included."""
        return "".join(self.pieces)


def _spans_of(kind: NodeKind, start: int, end: int, part_id: str) -> list[Span]:
    """A node's addresses: one contiguous span, or none for a container (2b)."""
    if kind not in _TEXT_NODE_KINDS:
        return []
    return [Span(part_id=part_id, start=start, end=end)]


class _Walker:
    """Walks one part's tree into a :class:`_PartStream`, its node tree and its revision
    facts (2a + 2b + 2c)."""

    def __init__(
        self,
        stream: _PartStream,
        part: Part,
        numbering: _Numbering | None = None,
        used_ids: set[str] | None = None,
        styles: Styles | None = None,
        seen_revisions: set[str] | None = None,
    ) -> None:
        self._stream = stream
        self._part = part
        self._numbering = numbering
        self._styles = styles
        self._labels = _LabelCounter(numbering)
        self._used_ids = used_ids if used_ids is not None else set()
        self._seen_revisions = seen_revisions if seen_revisions is not None else set()
        self._hashes: dict[str, int] = {}
        self._open: list[_Frame] = []
        self._roots: list[_Frame] = []
        self.nodes: list[Node] = []
        self.revisions: list[Revision] = []
        self.gaps: set[str] = set()
        self._noid = 0
        self._stack: list[str] = []
        # The open move ranges, outermost first: (marker w:id, shared w:name, mark local).
        self._move_ranges: list[tuple[str | None, str | None, str]] = []
        # One flag per open field: has that field passed its ``separate``?
        self._fields: list[bool] = []

    def walk(self, root: etree._Element) -> None:
        local = local_name(root)
        if is_w(root, local) and local in _KINDS_BY_LOCAL:
            self._walk_container(root)
        else:
            self._walk_block(root)
        self.nodes = self._materialize()

    def _walk_block(self, element: etree._Element) -> None:
        for child in element:
            if not isinstance(child.tag, str):
                continue
            local = local_name(child)
            if is_w(child, "p"):
                self._walk_paragraph(child)
            elif is_w(child, local) and local in _KINDS_BY_LOCAL:
                self._walk_container(child)
            elif is_w(child, local) and local in _BLOCK_CONTAINERS:
                self._walk_block(child)
            elif is_w(child, local) and (local in _MOVE_RANGE_START or local in _MOVE_RANGE_END):
                # A range can bracket whole paragraphs; it carries no text of its own.
                self._move_range(local, child)
            elif not (is_w(child, local) and local in _SKIP_BLOCK_LOCALS):
                self._note_skipped(child)

    def _walk_container(self, element: etree._Element) -> None:
        """Open the node an element makes, walk its content, close it (2b)."""
        self._open_node(_KINDS_BY_LOCAL[local_name(element)], element)
        self._walk_block(element)
        self._close_node()

    def _walk_paragraph(self, paragraph: etree._Element) -> None:
        kind, style, level, numbered, broken = _paragraph_facts(
            paragraph, self._styles, self._numbering
        )
        if broken:
            self.gaps.add(GAP_STYLE_CHAIN_CYCLE)
        label = self._labels.label(*numbered) if numbered is not None else None
        self._stack = []
        self._fields = []
        self._paragraph_mark_revisions(paragraph)
        self._open_node(kind, paragraph, style=style, level=level, label=label)
        self._walk_inline(paragraph)
        self._close_node()
        self._stack = []
        self._fields = []
        self._stream.terminate()
        # The paragraph is closed, so its terminator reaches its ancestors only: a
        # paragraph's own text excludes its own terminator, a cell's includes it.
        self._feed(TERMINATOR)

    def _walk_inline(self, element: etree._Element) -> None:
        for child in element:
            if not isinstance(child.tag, str):
                continue
            local = local_name(child)
            if not is_w(child, local):
                self._note_skipped(child)
                continue
            if local in _SKIP_LOCALS:
                continue
            if local in _REVISION_LOCALS:
                ident = self._revision_id(child, local)
                self._record_revision(child, local, ident)
                self._stack.append(ident)
                self._walk_inline(child)
                self._stack.pop()
            elif local in _MOVE_RANGE_START or local in _MOVE_RANGE_END:
                self._move_range(local, child)
            elif local in ("t", "delText"):
                if all(self._fields):
                    self._emit(_text_of(child))
            elif local == "fldChar":
                self._fld_char(wattr(child, "fldCharType"))
            elif local in ("tab", "ptab"):
                self._emit("\t")
            elif local in ("br", "cr"):
                self._emit(_VERTICAL_TAB)
            elif local == "softHyphen":
                self._emit(_SOFT_HYPHEN)
            elif local == "noBreakHyphen":
                self._emit(_NO_BREAK_HYPHEN)
            elif local == "sym":
                self._emit(_sym_text(child))
            elif local in _INLINE_CONTAINERS:
                self._walk_inline(child)
            elif local == "sdt":
                # Transparent: no node, no boundary, no ancestor. What the content
                # control *was* is not in the union, so the loss is reported.
                self.gaps.add(GAP_INLINE_SDT_TRANSPARENT)
                content = _first_w(child, "sdtContent")
                if content is not None:
                    self._walk_inline(content)
            else:
                # A drawing, w:txbxContent or an unknown w: container is skipped,
                # never guessed at -- and never lost silently.
                self._note_skipped(child)

    def _emit(self, text: str) -> None:
        if not text:
            return
        self._stream.emit(text, tuple(self._stack))
        self._feed(text)

    def _feed(self, text: str) -> None:
        """Give ``text`` to every open node: a container's text is the text under it."""
        for frame in self._open:
            frame.pieces.append(text)

    def _open_node(
        self,
        kind: NodeKind,
        element: etree._Element,
        *,
        style: str | None = None,
        level: int | None = None,
        label: str | None = None,
    ) -> None:
        frame = _Frame(
            kind=kind,
            element=element,
            start=self._stream.length,
            style=style,
            level=level,
            numbering_label=label,
        )
        if self._open:
            self._open[-1].children.append(frame)
        else:
            self._roots.append(frame)
        self._open.append(frame)

    def _close_node(self) -> None:
        self._open.pop().end = self._stream.length

    def _materialize(self) -> list[Node]:
        """Settle every frame's id -- in document order -- and read the records off."""
        for frame in self._roots:
            self._identify(frame)
        nodes: list[Node] = []
        for frame in self._roots:
            self._record(frame, nodes)
        return nodes

    def _identify(self, frame: _Frame) -> None:
        frame.ident, frame.stability, frame.occurrence = self._node_id(frame)
        for child in frame.children:
            self._identify(child)

    def _record(self, frame: _Frame, nodes: list[Node]) -> None:
        nodes.append(
            Node(
                id=frame.ident,
                kind=frame.kind,
                part_id=self._part.part_id,
                source_ref=self._source_ref(frame.element),
                spans=_spans_of(frame.kind, frame.start, frame.end, self._part.part_id),
                style=frame.style,
                level=frame.level,
                numbering_label=frame.numbering_label,
                child_ids=[child.ident for child in frame.children],
                id_stability=frame.stability,
                occurrence_index=frame.occurrence,
            )
        )
        for child in frame.children:
            self._record(child, nodes)

    def _node_id(self, frame: _Frame) -> tuple[str, IdStability, int]:
        """A node's id, the fallback that produced it and its occurrence ordinal (2b)."""
        para_id = _para_id(frame.element)
        if para_id is not None and para_id not in self._used_ids:
            ident, stability, occurrence = para_id, IdStability.PARAID, 0
        elif frame.own_text:
            base = sha256_json(
                [self._part.part_id, frame.kind.value, frame.own_text, frame.style, frame.level]
            )
            occurrence = self._hashes.get(base, 0)
            self._hashes[base] = occurrence + 1
            if occurrence:
                self.gaps.add(GAP_DUPLICATE_CONTENT_ID)
            ident, stability = f"hash:{base}:{occurrence}", IdStability.CONTENT_HASH
        else:
            ident, stability, occurrence = (
                f"path:{self._source_ref(frame.element)}",
                IdStability.PATH,
                0,
            )
        self._used_ids.add(ident)
        return ident, stability, occurrence

    def _source_ref(self, element: etree._Element) -> str:
        """Where a node came from: its part, then its position in that part's XML.

        The part's XML root *is* the part, so the pointer starts below it: a header's
        own node is just ``word/header1.xml`` and its first paragraph is
        ``word/header1.xml#p[0]``.
        """
        pointer = "/".join(
            f"{local}[{index}]" for local, index in _descent(element)[1:]
        )
        return f"{self._part.name}#{pointer}" if pointer else self._part.name

    def _revision_id(self, element: etree._Element, local: str) -> str:
        ident = wattr(element, "id")
        if ident is None:
            ident = f"noid{self._noid}"
            self._noid += 1
            self.gaps.add(GAP_REVISION_MISSING_ID)
        return f"{local}:{ident}"

    def _record_revision(self, element: etree._Element, local: str, ident: str) -> None:
        """One raw revision fact: kind, author, date, ancestry and move group (2c).

        The same ``w:id`` recurs whenever a revision spans several runs, so the first
        occurrence in document order is the record -- the mark still opens its ancestor
        frame either way.
        """
        if ident in self._seen_revisions:
            return
        self._seen_revisions.add(ident)
        self.revisions.append(
            Revision(
                id=ident,
                kind=_REVISION_KINDS[local],
                author=wattr(element, "author") or "",
                date=wattr(element, "date"),
                move_group_id=self._move_group(local),
                ancestors=list(self._stack),
            )
        )

    def _paragraph_mark_revisions(self, paragraph: etree._Element) -> None:
        """A ``w:pPr/w:rPr`` revision is a fact; its effect is not honored (2c).

        A deleted or inserted paragraph mark would add, remove or merge a terminator.
        The break is retained in every view, so *deciding* to retain it is the loud
        ``paragraph_mark_revision`` gap (``text-model-spec.md`` section 7), recorded
        here whether or not the mark's own id was already seen.
        """
        p_pr = _first_w(paragraph, "pPr")
        r_pr = _first_w(p_pr, "rPr") if p_pr is not None else None
        if r_pr is None:
            return
        for child in r_pr:
            if not isinstance(child.tag, str):
                continue
            local = local_name(child)
            if is_w(child, local) and local in _REVISION_LOCALS:
                self.gaps.add(GAP_PARAGRAPH_MARK_REVISION)
                self._record_revision(child, local, self._revision_id(child, local))

    def _move_range(self, local: str, element: etree._Element) -> None:
        """Track the markers that wrap a move: the group name lives on the start (2c).

        A ``...RangeStart`` opens a range carrying the shared ``w:name``; its
        ``...RangeEnd`` carries only the marker's ``w:id`` and closes the innermost open
        range with it. Markers are not revision records: the ``w:id`` of a range is not
        the ``w:id`` of the mark inside it.
        """
        if local in _MOVE_RANGE_START:
            self._move_ranges.append(
                (wattr(element, "id"), wattr(element, "name"), _MOVE_RANGE_START[local])
            )
            return
        marker = wattr(element, "id")
        for index in reversed(range(len(self._move_ranges))):
            if self._move_ranges[index][0] == marker:
                del self._move_ranges[index]
                return

    def _move_group(self, local: str) -> str | None:
        """The ``w:name`` of the innermost open range marker of the mark's own kind."""
        if local not in _MOVE_MARKS:
            return None
        for _marker, name, kind in reversed(self._move_ranges):
            if kind == local:
                return name
        return None

    def _note_skipped(self, element: etree._Element) -> None:
        """Record what skipping ``element`` loses, if anything."""
        if _holds_textbox(element):
            self.gaps.add(GAP_TEXTBOX)
        elif _has_text(element):
            self.gaps.add(GAP_UNRECOGNIZED_CONTAINER)

    def _fld_char(self, kind: str | None) -> None:
        if kind == "begin":
            self._fields.append(False)
        elif kind == "separate":
            if self._fields:
                self._fields[-1] = True
        elif kind == "end":
            if self._fields:
                self._fields.pop()


def _walk(
    part: Part | None,
    numbering: _Numbering | None = None,
    used_ids: set[str] | None = None,
    styles: Styles | None = None,
    seen_revisions: set[str] | None = None,
) -> tuple[UnionStream | None, set[str], list[Node], list[Revision]]:
    """One part's union stream, known-gap ids, node tree and revision facts (2a-2c)."""
    if part is None or part.tree is None:
        return None, set(), [], []
    stream = _PartStream()
    walker = _Walker(stream, part, numbering, used_ids, styles, seen_revisions)
    walker.walk(part.tree)
    if not stream.paragraphs:
        # No ``w:p`` is no address space: neither a stream nor nodes that would address
        # one. Their ids were still spent, so the next part's ids are the same either
        # way.
        return None, walker.gaps, [], walker.revisions
    return (
        UnionStream(part_id=part.part_id, text=stream.text, spans=stream.spans),
        walker.gaps,
        walker.nodes,
        walker.revisions,
    )


def walk_part(
    part: Part | None,
    numbering: _Numbering | None = None,
    used_ids: set[str] | None = None,
    styles: Styles | None = None,
) -> tuple[UnionStream | None, set[str]]:
    """``part``'s union stream (or ``None``) and the known-gap ids met while walking it.

    "Part exists" is not "part has content": a separator-only footnotes part is
    present with ``has_text`` False and still yields its terminator-only stream,
    while a part with no ``w:p`` at all contributes no address space and neither
    does ``None``.

    Nodes are not returned per part: a part alone cannot resolve ``numbering.xml``, so
    the node tree is a package-level product (:func:`walk_document`). Revision facts are
    package-level too (a ``w:id`` is one revision wherever its parts are met), so they
    are not returned here either. Numbering labels never reach the union, so a per-part
    walk without ``numbering`` streams the same text.
    """
    stream, gaps, _nodes, _revisions = _walk(part, numbering, used_ids, styles)
    return stream, gaps


def union_stream(part: Part | None) -> UnionStream | None:
    return walk_part(part)[0]


def _parts(package: Package) -> list[Part]:
    """The parts a walk reads, in the order their streams are addressed (D2).

    The document, then headers, footers, footnotes, endnotes and comments as the
    package resolves them; a part reached twice appears once.
    """
    parts: list[Part] = [package.document]
    parts.extend(package.headers())
    parts.extend(package.footers())
    parts.extend(
        part
        for part in (package.footnotes, package.endnotes, package.comments)
        if part is not None
    )
    unique: list[Part] = []
    seen: set[str] = set()
    for part in parts:
        if part.name not in seen:
            seen.add(part.name)
            unique.append(part)
    return unique


def walk_package(package: Package) -> tuple[list[UnionStream], list[str]]:
    """Every streamed part of ``package`` (document first) and the sorted known-gap ids.

    A part's stream is its own address space (D2), so nothing here merges parts or
    projects them into one coordinate space. Parts are addressed in a fixed order --
    the document, then headers, footers, footnotes, endnotes and comments as the
    package resolves them -- and a part reached twice is streamed once.
    """
    streams: list[UnionStream] = []
    gaps: set[str] = set()
    for part in _parts(package):
        stream, part_gaps = walk_part(part)
        gaps |= part_gaps
        if stream is not None:
            streams.append(stream)
    return streams, sorted(gaps)


def walk_document(package: Package) -> ParseResult:
    """``package``'s streams, node tree, revision facts and known-gap ids (2b-2c).

    Parts are read in the fixed order of :func:`walk_package`, and node ids and
    revision ids are spent globally across them, so a ``w14:paraId`` is package-unique
    and a revision met in two parts is one record. Comments (2d) and headings (Turn 4)
    are not read here yet.
    """
    numbering = _Numbering(package.numbering)
    styles = Styles(package.styles)
    used_ids: set[str] = set()
    seen_revisions: set[str] = set()
    streams: list[UnionStream] = []
    nodes: list[Node] = []
    revisions: list[Revision] = []
    gaps: set[str] = set()
    for part in _parts(package):
        stream, part_gaps, part_nodes, part_revisions = _walk(
            part, numbering, used_ids, styles, seen_revisions
        )
        gaps |= part_gaps
        if stream is not None:
            streams.append(stream)
        nodes.extend(part_nodes)
        revisions.extend(part_revisions)
    return ParseResult(
        union_streams=streams,
        nodes=nodes,
        revisions=revisions,
        known_gaps=sorted(gaps),
    )


def union_streams(package: Package) -> list[UnionStream]:
    return walk_package(package)[0]
