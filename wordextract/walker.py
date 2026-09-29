"""Turn 2a: the per-part union stream (text-model-spec sections 1 and 2).

This is the production replacement for the ``tools/opc_spike.py`` scaffold. It
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
  emitted only once the innermost open field has passed its ``separate`` (a
  ``w:fldChar`` depth counter, so nested fields resolve; ``w:fldSimple``'s
  instruction is its attribute and is never read);
* revision ancestry is the ancestor stack, ``<kind>:<w:id>`` (2c adds the records);
* block ``w:sdt`` is descended through (2b makes it a node), inline ``w:sdt`` is
  transparent;
* ``w:txbxContent`` is never entered: text-box text belongs to its own fragment and
  is not part of any part's union stream (a later slice, hence no fragment stream);
  the omission is recorded as the ``textbox`` known gap so it is never silent;
* field text is content only when **every** open field has passed its ``separate``:
  a nested field's result inside an outer field's instruction is still instruction;
* descent is an explicit allow-list and every drawing is skipped. An unknown ``w:``
  container contributes nothing rather than something invented, **but the loss is
  recorded**: a skipped element that holds text is the ``unrecognized_container``
  known gap. A revision with no ``w:id`` gets a deterministic ``<kind>:noid<n>`` id
  (per-part document order) and the ``revision_missing_id`` gap.

The union is only an address space: view text and the view/union offset maps are
derived on demand from it (Turn 3), and the union itself is never matched or
summarized.
"""
from __future__ import annotations

from lxml import etree

from .model import ElementarySpan, UnionStream
from .opc import Package, Part, is_w, local_name, wattr

#: One per paragraph, empty stack: the paragraph boundary in the union coordinate
#: space (``text-model-spec.md`` section 1).
TERMINATOR = "\n"

_VERTICAL_TAB = "\u000b"
_SOFT_HYPHEN = "\u00ad"
_NO_BREAK_HYPHEN = "\u2011"

_REVISION_LOCALS = ("ins", "del", "moveFrom", "moveTo")

#: Known-gap ids this module can report (``docs/design/phase1-gaps.md``).
GAP_TEXTBOX = "textbox"
GAP_UNRECOGNIZED_CONTAINER = "unrecognized_container"
GAP_REVISION_MISSING_ID = "revision_missing_id"

#: Block-level containers the walk descends through. ``w:sdtContent`` carries both a
#: block sdt's paragraphs and, reached from a paragraph, an inline sdt's runs.
_BLOCK_CONTAINERS = frozenset(
    {
        "body",
        "tbl",
        "tr",
        "tc",
        "sdt",
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
    {"sectPr", "tblPr", "tblGrid", "trPr", "tcPr", "sdtPr", "sdtEndPr", "bookmarkStart", "bookmarkEnd"}
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
    def text(self) -> str:
        return "".join(self._pieces)

    @property
    def spans(self) -> list[ElementarySpan]:
        return list(self._spans)


class _Walker:
    """Walks one part's tree into a :class:`_PartStream`."""

    def __init__(self, stream: _PartStream) -> None:
        self._stream = stream
        self.gaps: set[str] = set()
        self._noid = 0
        self._stack: list[str] = []
        # One flag per open field: has that field passed its ``separate``?
        self._fields: list[bool] = []

    def walk(self, root: etree._Element) -> None:
        self._walk_block(root)

    def _walk_block(self, element: etree._Element) -> None:
        for child in element:
            if not isinstance(child.tag, str):
                continue
            local = local_name(child)
            if is_w(child, "p"):
                self._walk_paragraph(child)
            elif is_w(child, local) and local in _BLOCK_CONTAINERS:
                self._walk_block(child)
            elif not (is_w(child, local) and local in _SKIP_BLOCK_LOCALS):
                self._note_skipped(child)

    def _walk_paragraph(self, paragraph: etree._Element) -> None:
        self._stack = []
        self._fields = []
        self._walk_inline(paragraph)
        self._stack = []
        self._fields = []
        self._stream.terminate()

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
                self._stack.append(self._revision_id(child, local))
                self._walk_inline(child)
                self._stack.pop()
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
                content = _first_w(child, "sdtContent")
                if content is not None:
                    self._walk_inline(content)
            else:
                # A drawing, w:txbxContent or an unknown w: container is skipped,
                # never guessed at -- and never lost silently.
                self._note_skipped(child)

    def _emit(self, text: str) -> None:
        self._stream.emit(text, tuple(self._stack))

    def _revision_id(self, element: etree._Element, local: str) -> str:
        ident = wattr(element, "id")
        if ident is None:
            ident = f"noid{self._noid}"
            self._noid += 1
            self.gaps.add(GAP_REVISION_MISSING_ID)
        return f"{local}:{ident}"

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


def walk_part(part: Part | None) -> tuple[UnionStream | None, set[str]]:
    """``part``'s union stream (or ``None``) and the known-gap ids met while walking it.

    "Part exists" is not "part has content": a separator-only footnotes part is
    present with ``has_text`` False and still yields its terminator-only stream,
    while a part with no ``w:p`` at all contributes no address space and neither
    does ``None``.
    """
    if part is None or part.tree is None:
        return None, set()
    stream = _PartStream()
    walker = _Walker(stream)
    walker.walk(part.tree)
    if not stream.paragraphs:
        return None, walker.gaps
    return UnionStream(part_id=part.part_id, text=stream.text, spans=stream.spans), walker.gaps


def union_stream(part: Part | None) -> UnionStream | None:
    return walk_part(part)[0]


def walk_package(package: Package) -> tuple[list[UnionStream], list[str]]:
    """Every streamed part of ``package`` (document first) and the sorted known-gap ids.

    A part's stream is its own address space (D2), so nothing here merges parts or
    projects them into one coordinate space. Parts are addressed in a fixed order --
    the document, then headers, footers, footnotes, endnotes and comments as the
    package resolves them -- and a part reached twice is streamed once.
    """
    parts: list[Part] = [package.document]
    parts.extend(package.headers())
    parts.extend(package.footers())
    parts.extend(
        part
        for part in (package.footnotes, package.endnotes, package.comments)
        if part is not None
    )
    streams: list[UnionStream] = []
    gaps: set[str] = set()
    seen: set[str] = set()
    for part in parts:
        if part.name in seen:
            continue
        seen.add(part.name)
        stream, part_gaps = walk_part(part)
        gaps |= part_gaps
        if stream is not None:
            streams.append(stream)
    return streams, sorted(gaps)


def union_streams(package: Package) -> list[UnionStream]:
    return walk_package(package)[0]
