"""The three paragraph-style facts the walker needs from ``styles.xml``.

Design D1 rules out reimplementing the style cascade (fonts, spacing, latent styles).
Three facts, though, are *structure*, not appearance, and live on the style rather than
the paragraph in a great many real Word documents:

* ``w:numPr`` -- a style such as ``List Number`` or an auto-numbered ``Heading 1``
  carries its numbering, so the paragraph itself says nothing;
* ``w:outlineLvl`` -- the only thing that identifies a heading style whose name is not
  English (``Titre 1``, ``Überschrift 1``);
* ``w:basedOn`` -- the chain those two are inherited along.

Nothing else is read. Each property resolves independently to the **nearest** style in
the chain that defines it (Word merges paragraph properties, it does not replace the
group), a **cycle** ends the chain and marks the result ``broken`` so the caller can
record it instead of guessing. A ``basedOn`` naming an undefined style ends the chain
silently, and so does a paragraph whose own style is undefined: there is nothing to
inherit, exactly as when Word treats the style as Normal. (Docx generators routinely
omit ``Normal``, so flagging that would fire on every generated document.)
"""
from __future__ import annotations

from dataclasses import dataclass

from lxml import etree

from .opc import Part, is_w, wattr


@dataclass(frozen=True)
class StyleFacts:
    """A style's resolved numbering and outline level, and the chain they came from.

    ``broken`` is True only for a ``basedOn`` cycle.
    """

    chain: tuple[str, ...] = ()
    num_id: str | None = None
    ilvl: int | None = None
    outline_level: int | None = None
    broken: bool = False


@dataclass(frozen=True)
class _Own:
    based_on: str | None
    num_id: str | None
    ilvl: int | None
    outline_level: int | None


def _child(element: etree._Element | None, local: str) -> etree._Element | None:
    if element is None:
        return None
    for child in element:
        if isinstance(child.tag, str) and is_w(child, local):
            return child
    return None


def _val(element: etree._Element | None, local: str) -> str | None:
    child = _child(element, local)
    return wattr(child, "val") if child is not None else None


def _int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


class Styles:
    """``styles.xml`` read for numbering, outline level and ``basedOn`` only."""

    def __init__(self, part: Part | None = None) -> None:
        self._own: dict[str, _Own] = {}
        if part is not None and part.tree is not None:
            for element in part.tree:
                if isinstance(element.tag, str) and is_w(element, "style"):
                    self._read(element)

    def _read(self, element: etree._Element) -> None:
        ident = wattr(element, "styleId")
        if ident is None:
            return
        p_pr = _child(element, "pPr")
        num_pr = _child(p_pr, "numPr")
        self._own[ident] = _Own(
            based_on=_val(element, "basedOn"),
            num_id=_val(num_pr, "numId"),
            ilvl=_int(_val(num_pr, "ilvl")),
            outline_level=_int(_val(p_pr, "outlineLvl")),
        )

    def resolve(self, style_id: str | None) -> StyleFacts:
        """``style_id``'s numbering and outline level, inherited along ``basedOn``."""
        if style_id is None or style_id not in self._own:
            return StyleFacts()
        chain: list[str] = []
        num_id = ilvl = outline = None
        broken = False
        current: str | None = style_id
        while current is not None:
            if current in chain:
                broken = True  # a cycle: stop, and say so
                break
            own = self._own.get(current)
            if own is None:
                break  # basedOn names an undefined style: nothing further to inherit
            chain.append(current)
            num_id = own.num_id if num_id is None else num_id
            ilvl = own.ilvl if ilvl is None else ilvl
            outline = own.outline_level if outline is None else outline
            current = own.based_on
        return StyleFacts(
            chain=tuple(chain),
            num_id=num_id,
            ilvl=ilvl,
            outline_level=outline,
            broken=broken,
        )
