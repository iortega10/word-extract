"""Turn 4a: the ordered heading rule list, over facts stated by hand.

``headings.decide`` reads nothing but the :class:`ParagraphFacts` it is given, so the ruleset
is pinned here without any XML: each rule on its own, the whole order, the guards each rule
carries, and the two counts ``summarize`` reports. What the walker reads those facts *off*
is checked in ``test_walker.py``, where the same verdict -- the fired rules, the winner, the
disagreements and the kind -- is re-derived from the parts' own XML.

D3: a decision is every rule that fired, the winner among them and the disagreements. There
is no confidence scalar anywhere in this ruleset.
"""
from __future__ import annotations

import pytest

from wordextract.headings import (
    BOLD_MAX_CHARS,
    RULES,
    ParagraphFacts,
    decide,
    is_heading_style,
    summarize,
)
from wordextract.model import HeadingDecision, HeadingDetection


def test_the_ruleset_is_the_agreed_rule_order():
    assert RULES == ("style", "outlineLvl", "outlineIlvl", "boldShort")


def test_no_fact_at_all_fires_no_rule():
    firing = decide(ParagraphFacts())
    assert firing.fired == ()
    assert firing.winner is None
    assert firing.disputed == ()
    assert not firing.is_heading


@pytest.mark.parametrize(
    ("facts", "rule"),
    [
        (ParagraphFacts(style="Heading2"), "style"),
        (ParagraphFacts(style="Title"), "style"),
        (ParagraphFacts(outline_level=0), "outlineLvl"),
        (ParagraphFacts(numbering_level=0, numbering_style="Heading1"), "outlineIlvl"),
        (ParagraphFacts(bold_all=True, chars=3), "boldShort"),
    ],
    ids=["heading-style", "title-style", "outline-lvl", "outline-ilvl", "bold-short"],
)
def test_each_rule_fires_on_its_own_fact(facts, rule):
    """Every rule is individually sufficient: a fired rule *is* a heading."""
    firing = decide(facts)
    assert firing.fired == (rule,)
    assert firing.winner == rule
    assert firing.disputed == ()
    assert firing.is_heading


def test_the_first_rule_to_fire_wins_and_the_rest_are_the_disagreements():
    """All four rules claimed the paragraph; the ruleset's order picks the winner."""
    firing = decide(
        ParagraphFacts(
            style="Heading1",
            outline_level=0,
            numbering_level=0,
            numbering_style="Heading1",
            bold_all=True,
            chars=4,
        )
    )
    assert firing.fired == RULES
    assert firing.winner == "style"
    assert firing.disputed == ("outlineLvl", "outlineIlvl", "boldShort")


def test_a_later_rule_wins_only_when_no_earlier_rule_fired():
    firing = decide(ParagraphFacts(outline_level=2, bold_all=True, chars=4))
    assert firing.fired == ("outlineLvl", "boldShort")
    assert firing.winner == "outlineLvl"
    assert firing.disputed == ("boldShort",)


@pytest.mark.parametrize(
    ("style", "expected"),
    [
        ("Heading1", True),
        ("Heading9", True),
        ("heading 1", True),
        ("HEADING 2", True),
        ("Title", True),
        ("title", True),
        ("Heading10", False),
        ("Heading0", False),
        ("Subtitle", False),
        ("ListParagraph", False),
        ("Titre 2", False),
        (None, False),
    ],
)
def test_the_style_rule_matches_heading_n_and_title_and_nothing_else(style, expected):
    assert is_heading_style(style) is expected
    assert decide(ParagraphFacts(style=style)).fired == (("style",) if expected else ())


@pytest.mark.parametrize("style", ["Heading3", "Title", "heading 3"])
def test_the_outline_rule_fires_when_the_level_links_a_heading_style(style):
    firing = decide(ParagraphFacts(numbering_level=1, numbering_style=style))
    assert firing.fired == ("outlineIlvl",)


@pytest.mark.parametrize(
    "numbering_style",
    ["ListNumber", "ListParagraph", "List Bullet", "Normal", "Titre 2", None],
)
def test_the_outline_rule_never_fires_on_a_list_level(numbering_style):
    """A list level names its own list style, never a heading style: that is the guard."""
    assert decide(ParagraphFacts(numbering_level=0, numbering_style=numbering_style)).fired == ()


def test_the_outline_rule_needs_the_paragraph_to_be_numbered():
    """The level link is only read for a level the paragraph actually sits on."""
    assert decide(ParagraphFacts(numbering_level=None, numbering_style="Heading1")).fired == ()


def test_an_outline_level_fires_even_though_the_paragraph_is_numbered():
    """A numbered paragraph can state an outline level and still be a list item."""
    firing = decide(
        ParagraphFacts(outline_level=0, numbering_level=0, numbering_style="ListNumber")
    )
    assert firing.fired == ("outlineLvl",)
    assert firing.winner == "outlineLvl"


def test_the_bold_rule_fires_up_to_and_including_the_length_limit():
    assert decide(ParagraphFacts(bold_all=True, chars=1)).winner == "boldShort"
    assert decide(ParagraphFacts(bold_all=True, chars=BOLD_MAX_CHARS)).winner == "boldShort"
    assert decide(ParagraphFacts(bold_all=True, chars=BOLD_MAX_CHARS + 1)).fired == ()


@pytest.mark.parametrize("level", range(0, 9))
def test_every_heading_outline_level_zero_to_eight_fires_the_outline_rule(level):
    assert decide(ParagraphFacts(outline_level=level)).fired == ("outlineLvl",)


@pytest.mark.parametrize("level", [9, 10, 99, -1])
def test_outline_level_nine_is_word_body_text_and_no_other_value_fires(level):
    """9 is Word's explicit body text; values outside 0-9 are not levels at all."""
    firing = decide(ParagraphFacts(outline_level=level))
    assert firing.fired == () and not firing.is_heading


def test_body_text_level_does_not_stop_another_rule_claiming_the_paragraph():
    firing = decide(ParagraphFacts(style="Heading1", outline_level=9))
    assert firing.fired == ("style",)


@pytest.mark.parametrize(
    "facts",
    [
        ParagraphFacts(bold_all=True, chars=0),
        ParagraphFacts(chars=10),
        ParagraphFacts(bold_all=True, chars=10, in_cell=True),
        ParagraphFacts(bold_all=True, chars=10, list_continuation=True),
    ],
    ids=["nothing-visible", "not-all-bold", "in-a-cell", "a-list-continuation"],
)
def test_each_bold_rule_guard_alone_stops_it_firing(facts):
    assert "boldShort" not in decide(facts).fired


def test_a_guarded_bold_paragraph_is_still_a_heading_when_another_rule_fires():
    """A guard is not a veto: it stops the bold rule, and the ruleset decides the rest."""
    firing = decide(ParagraphFacts(style="Heading1", bold_all=True, chars=10, in_cell=True))
    assert firing.fired == ("style",)


def test_a_walk_with_no_heading_is_degraded_and_a_walk_with_one_is_not():
    """Fail open: no heading at all is ``degraded`` -- one flat root, never a failure."""
    nothing = summarize([])
    assert nothing.detection is HeadingDetection.DEGRADED
    assert (nothing.headings, nothing.disagreements) == (0, 0)


def test_summarize_counts_headings_and_the_paragraphs_two_rules_disagreed_on():
    decisions = [
        HeadingDecision(node_id="a", fired_rules=["style"], winner="style"),
        HeadingDecision(
            node_id="b",
            fired_rules=["style", "outlineLvl"],
            winner="style",
            disputed_rules=["outlineLvl"],
        ),
        HeadingDecision(
            node_id="c",
            fired_rules=["outlineLvl", "outlineIlvl", "boldShort"],
            winner="outlineLvl",
            disputed_rules=["outlineIlvl", "boldShort"],
        ),
        HeadingDecision(node_id="d"),
    ]
    summary = summarize(decisions)
    assert summary.detection is HeadingDetection.NORMAL
    assert summary.headings == 3
    assert summary.disagreements == 2  # two paragraphs were contested, not three rule pairs
