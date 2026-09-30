"""Turn 6b: the vendored Porter stemmer, tested against published vectors.

A stemmer tested against a table *it* produced proves only that it agrees with itself --
the same circularity as a sidecar derived from the code it pins, which the build spec calls
out by name (6b: "Do not write your own stemmer or golden table"). So the expectations here
are the **worked examples published with the algorithm**: M. F. Porter, *An algorithm for
suffix stripping*, ``Program`` 14(3):130-137, 1980, the ``example -> stem`` pairs given in
sections 2 to 5 of the paper, one list per step. They are external and they are immutable:
the paper is not ours to regenerate.

The portmanteau trio the spec names (``included / including / inclusion``) is here too, as a
deliberate adversarial case: original Porter unifies the first two and *not* the third, and
the test asserts that rather than a tidier story. That non-unification is a property of the
published algorithm, so pinning it is how we pin the algorithm -- not a bug to smooth over.
"""
from __future__ import annotations

import pytest

from wordextract.stem import (
    STEMMERS,
    STEM_ALGORITHM_VERSION,
    porter_stem,
    stemmer,
)

#: Porter (1980) section 2, step 1a: plural forms.
STEP_1A = {
    "caresses": "caress",
    "ponies": "poni",
    "ties": "ti",
    "caress": "caress",
    "cats": "cat",
}

#: Porter (1980) section 2, step 1b: past-tense and progressive forms and their repairs.
STEP_1B = {
    "feed": "feed",
    "agreed": "agre",
    "plastered": "plaster",
    "bled": "bled",
    "motoring": "motor",
    "sing": "sing",
    "conflated": "conflat",
    "troubled": "troubl",
    "sized": "size",
    "hopping": "hop",
    "tanned": "tan",
    "falling": "fall",
    "hissing": "hiss",
    "fizzed": "fizz",
    "failing": "fail",
    "filing": "file",
}

#: Porter (1980) section 2, step 1c: ``y`` to ``i``.
STEP_1C = {"happy": "happi", "sky": "sky"}

#: Porter (1980) section 3, step 2: derivational suffixes, ``m > 0``.
STEP_2 = {
    "relational": "relat",
    "conditional": "condit",
    "rational": "ration",
    "valenci": "valenc",
    "hesitanci": "hesit",
    "digitizer": "digit",
    "conformabli": "conform",
    "radicalli": "radic",
    "differentli": "differ",
    "vileli": "vile",
    "analogousli": "analog",
    "vietnamization": "vietnam",
    "predication": "predic",
    "operator": "oper",
    "feudalism": "feudal",
    "decisiveness": "decis",
    "hopefulness": "hope",
    "callousness": "callous",
    "formaliti": "formal",
    "sensitiviti": "sensit",
    "sensibiliti": "sensibl",
}

#: Porter (1980) section 4, step 3.
STEP_3 = {
    "triplicate": "triplic",
    "formative": "form",
    "formalize": "formal",
    "electriciti": "electr",
    "electrical": "electr",
    "hopeful": "hope",
    "goodness": "good",
}

#: Porter (1980) section 4, step 4: derivational suffixes, ``m > 1``.
STEP_4 = {
    "revival": "reviv",
    "allowance": "allow",
    "inference": "infer",
    "airliner": "airlin",
    "gyroscopic": "gyroscop",
    "adjustable": "adjust",
    "defensible": "defens",
    "irritant": "irrit",
    "replacement": "replac",
    "adjustment": "adjust",
    "dependent": "depend",
    "adoption": "adopt",
    "homologou": "homolog",
    "communism": "commun",
    "activate": "activ",
    "angulariti": "angular",
    "homologous": "homolog",
    "effective": "effect",
    "bowdlerize": "bowdler",
}

#: Porter (1980) section 5: steps 5a and 5b.
STEP_5 = {
    "probate": "probat",
    "rate": "rate",
    "cease": "ceas",
    "controll": "control",
    "roll": "roll",
}

VECTORS = {**STEP_1A, **STEP_1B, **STEP_1C, **STEP_2, **STEP_3, **STEP_4, **STEP_5}


@pytest.mark.parametrize(("word", "expected"), sorted(VECTORS.items()))
def test_the_stemmer_reproduces_every_published_vector(word, expected):
    assert porter_stem(word) == expected


def test_the_vector_table_is_the_union_of_its_published_steps():
    """A guard on the transcription: the seven step tables concatenate with no key colliding,
    and the count is the 75 worked examples transcribed above."""
    steps = (STEP_1A, STEP_1B, STEP_1C, STEP_2, STEP_3, STEP_4, STEP_5)
    assert sum(len(step) for step in steps) == len(VECTORS)
    assert len(VECTORS) == 75


def test_the_portmanteau_trio_unifies_two_and_not_the_third():
    """The spec's adversarial case. Original Porter maps ``included`` and ``including`` to
    ``includ`` but ``inclusion`` to ``inclus``: the first two are one stem, the third is a
    different one. Asserting the mismatch is asserting the algorithm."""
    assert porter_stem("included") == porter_stem("including") == "includ"
    assert porter_stem("inclusion") == "inclus"
    assert porter_stem("inclusion") != porter_stem("included")
    # and the same split on a second, independent family -- not a one-off quirk
    assert porter_stem("excluded") == "exclud"
    assert porter_stem("exclusion") == "exclus"


def test_the_stemmer_is_pure_and_idempotent_as_a_reading():
    """Same word, same stem, every time; and a word of two letters or fewer is returned
    unchanged -- none of the suffixes is reachable."""
    for word in VECTORS:
        assert porter_stem(word) == porter_stem(word)
    for short in ("a", "of", "by", ""):
        assert porter_stem(short) == short
    assert porter_stem("XY") == "xy"


def test_a_token_that_is_not_plain_ascii_letters_is_its_own_stem():
    """The corpus is whatever the document contains: a token that is digits, a non-ASCII
    word, or a mix keeps a single pinned reading -- itself -- rather than letting the ASCII
    rules strip an ``-ing`` off a German word or a digit off an identifier."""
    for token in ("2019", "123", "überweisung", "subrogación", "r2d2", "subrogation2"):
        assert porter_stem(token) == token


def test_the_registry_names_porter_and_only_porter():
    assert set(STEMMERS) == {"porter"}
    assert stemmer("porter") is porter_stem


def test_an_unknown_algorithm_is_a_loud_failure():
    """As in :func:`wordextract.terms.compile_registry`: a name no algorithm answers to is
    an error, never a silent no-op."""
    with pytest.raises(ValueError, match="no-such-algorithm"):
        stemmer("no-such-algorithm")


def test_the_stemmer_version_is_the_nested_matcher_half():
    from wordextract.versions import MATCHER_VERSION

    assert STEM_ALGORITHM_VERSION == "porter-1"
    assert MATCHER_VERSION.endswith("+" + STEM_ALGORITHM_VERSION)
