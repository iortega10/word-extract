"""Turn 4a: the ordered heading rules, over facts the walker reads off the XML.

A document is chunked by its heading tree (Turn 5), so the walk has to say which
paragraphs are headings -- and D3 fixes *how*: an **ordered deterministic rule list**,
**every** rule that fired recorded per paragraph, the winner, and the disagreements.
Never a confidence scalar, and never a probabilistic second guess.

    style -> outlineLvl -> outline-numbered ilvl -> all-bold short paragraph

The first rule that fires is the winner; the rules that fired behind it are the
disagreements. A paragraph is a heading exactly when a rule fired at all, so
:func:`decide` is the **only** producer of :attr:`~wordextract.model.NodeKind.HEADING`:
the walker's own paragraph fact is the *fallback* kind -- a numbered paragraph is a list
item, everything else a paragraph -- and a rule list that changes its mind changes the
node's kind without touching the walk.

The rules, in order:

* ``style`` -- the paragraph's own ``w:pStyle`` names a heading style: ``heading`` plus
  a level 1-9, or ``title``. The value is matched as an inert string, never resolved
  against a style definition (D1). Word names heading styles in the document's own
  language, so this rule only ever catches the styles whose id happens to be English --
  ``outlineLvl`` is what catches the others.
* ``outlineLvl`` -- the paragraph states an outline level: its own ``w:outlineLvl``,
  else the one its style chain defines. This is the *only* rule that identifies a
  heading style with a non-English id (``Titre 2``, ``Überschrift 1``), and the reason
  ``styles.py`` reads ``w:outlineLvl`` at all. A paragraph can state an outline level and
  still be a list item -- an auto-numbered ``Heading 1`` is numbered -- and the rule
  fires anyway: what that paragraph *is* is decided by the rule order, not by a special
  case here.
* ``outlineIlvl`` -- the paragraph is numbered by an **outline-numbered**
  ``w:abstractNum``: the ``w:lvl`` its own ``w:ilvl`` names links a heading style by
  ``w:pStyle``. That link is the whole guard, and it is structural on purpose: a list
  level names its own list style (``ListNumber``, ``List Bullet``, ...), never a heading
  style, so a list item is never numbered by an outline level. The build spec's "never to
  list-context paragraphs (``List Number``, ``ListParagraph``)" therefore holds without
  matching style names, which D1 rules out anyway. A ``w:pStyle``-less level is not an
  outline level either, so a plain numbered list stays a list.
* ``boldShort`` -- an all-bold short paragraph: every text-bearing run of the paragraph
  is **directly** bold (``w:rPr/w:b``, no style cascade) and its visible text is at most
  :data:`BOLD_MAX_CHARS` characters. Guards: a paragraph in a table cell is a label
  rather than a heading, and a paragraph whose nearest preceding sibling block is a list
  item is a continuation of that list.

Fail open: a document no rule fires on yields no heading at all, which is reported as
``heading_detection: degraded`` -- one flat root, and the chunker must fall back to size
chunks (:func:`summarize`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .model import HeadingDecision, HeadingDetection
from .versions import HEADING_RULESET_VERSION

#: The ruleset's version (D10), the same value the reproducibility key stamps. It is
#: recorded with every run, so changing a rule -- or the threshold below -- invalidates a
#: cached walk instead of silently re-chunking documents extracted under the old rules.
RULESET_VERSION = HEADING_RULESET_VERSION

#: A ``w:pStyle`` that names a heading style: ``heading`` plus one level 1-9.
_HEADING_STYLE = re.compile(r"heading\s*([1-9])", re.IGNORECASE)

#: ... and the one heading style that names no level.
_TITLE_STYLE = "title"

#: The longest paragraph the all-bold rule will call a heading, in characters of visible
#: text (ruleset version 1): a heading is a line, and a bold *sentence* is emphasis.
BOLD_MAX_CHARS = 120

RULE_STYLE = "style"
RULE_OUTLINE_LVL = "outlineLvl"
RULE_OUTLINE_ILVL = "outlineIlvl"
RULE_BOLD_SHORT = "boldShort"

#: The rule ids, in the order that decides which fired rule wins. Every id a record can
#: carry is one of these, which is what an eval reads against.
RULES: tuple[str, ...] = (RULE_STYLE, RULE_OUTLINE_LVL, RULE_OUTLINE_ILVL, RULE_BOLD_SHORT)


def is_heading_style(value: str | None) -> bool:
    """Whether a ``w:pStyle`` value (or a ``w:lvl/w:pStyle``) names a heading style.

    ``Title`` carries no level and ``HeadingN`` names one, but only the *kind* is
    decided here: the level a heading has is the paragraph's ``w:outlineLvl``
    (:attr:`~wordextract.model.Node.level`), which the walk reads from the same XML.
    """
    if value is None:
        return False
    return value.casefold() == _TITLE_STYLE or _HEADING_STYLE.fullmatch(value) is not None


@dataclass(frozen=True)
class ParagraphFacts:
    """What the rules read off one paragraph -- all of it, so that nothing is guessed.

    The walker fills every field from the XML: these are facts about the paragraph and
    its place in the tree, not about the rules. A caller that has no XML (a test) can
    state the facts directly and get the ruleset's verdict for them.
    """

    #: The paragraph's own ``w:pStyle``.
    style: str | None = None
    #: Its outline level: its own ``w:outlineLvl``, else its style chain's.
    outline_level: int | None = None
    #: The ``w:ilvl`` of the numbering that numbers it, ``None`` when it has none.
    numbering_level: int | None = None
    #: The heading style that ilvl's ``w:lvl`` links by ``w:pStyle``, if any.
    numbering_style: str | None = None
    #: Every text-bearing, visible run of the paragraph is directly bold.
    bold_all: bool = False
    #: The length of the paragraph's visible text, in characters.
    chars: int = 0
    #: The paragraph sits in a ``w:tc``.
    in_cell: bool = False
    #: The nearest preceding sibling block of the paragraph is a list item.
    list_continuation: bool = False


@dataclass(frozen=True)
class RuleFiring:
    """One paragraph's ruleset outcome: what fired, and which of them won.

    ``fired`` is in the ruleset's order, so ``fired[0]`` is the winner by construction
    and the rest are the disagreements -- the two are derived rather than stored apart,
    because a winner that was not the first rule to fire is not a state this ruleset has.
    """

    fired: tuple[str, ...] = ()

    @property
    def winner(self) -> str | None:
        """The rule whose claim stands, or ``None`` when no rule fired."""
        return self.fired[0] if self.fired else None

    @property
    def disputed(self) -> tuple[str, ...]:
        """The fired rules the winner beat, in the ruleset's order."""
        return self.fired[1:]

    @property
    def is_heading(self) -> bool:
        """Whether the paragraph is a heading: exactly whether a rule fired."""
        return bool(self.fired)


def decide(facts: ParagraphFacts) -> RuleFiring:
    """The rules that fire on one paragraph, in the ruleset's order (Turn 4a)."""
    fired: list[str] = []
    if is_heading_style(facts.style):
        fired.append(RULE_STYLE)
    if facts.outline_level is not None:
        fired.append(RULE_OUTLINE_LVL)
    if facts.numbering_level is not None and is_heading_style(facts.numbering_style):
        fired.append(RULE_OUTLINE_ILVL)
    if (
        facts.bold_all
        and 0 < facts.chars <= BOLD_MAX_CHARS
        and not facts.in_cell
        and not facts.list_continuation
    ):
        fired.append(RULE_BOLD_SHORT)
    return RuleFiring(fired=tuple(fired))


@dataclass(frozen=True)
class HeadingSummary:
    """What a whole walk's decisions add up to: the verdict and the two counts."""

    detection: HeadingDetection
    #: How many paragraphs a rule claimed: the headings the walk found.
    headings: int
    #: How many paragraphs two or more rules claimed.
    disagreements: int


def summarize(decisions: Iterable[HeadingDecision]) -> HeadingSummary:
    """The detection verdict and the counts over one walk's decisions (Turn 5).

    ``degraded`` is the fail-open verdict the design asks for: a walk that found no
    heading at all cannot be section-chunked, so the chunker has to fall back to size
    chunks -- the outcome is a weaker tree, never a failed extraction.

    ``disagreements`` counts the **paragraphs** two or more rules claimed, not the rule
    pairs: the aggregate answers "how contested is this outline", and one paragraph is
    one dispute however many rules argued over it.
    """
    headings = 0
    disagreements = 0
    for decision in decisions:
        if decision.winner is not None:
            headings += 1
        if decision.disputed_rules:
            disagreements += 1
    return HeadingSummary(
        detection=HeadingDetection.NORMAL if headings else HeadingDetection.DEGRADED,
        headings=headings,
        disagreements=disagreements,
    )
