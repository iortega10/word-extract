"""Turn 6b: the vendored Porter stemmer.

``word-extraction-design.md`` D6 and the 6b slice of ``phase1-build-spec.md``'s Turn 6:
term matching is exact, curated synonyms and stemming only, and every algorithmic choice is
pinned. This module is the "pinned Porter with attribution" that slice calls for -- never a
stemmer of our own.

**Attribution.** The algorithm is M. F. Porter, *An algorithm for suffix stripping*,
``Program`` 14(3):130-137, 1980. This file is a transcription of that published algorithm:
the same suffixes, the same measure ``m``, the same conditions, applied in the same order.
Where the paper's prose leaves a reading open, the behaviour is pinned to the worked
examples published with the algorithm and to ``tests/test_stem.py``, which is those
examples -- an external vector, not a table this module generated.

**Why vendored.** A stemmer shipped as a dependency is unpinned (D8 makes FTS5's stemmer the
standing example of why that is not allowed), and a home-grown one is circular: tested only
against its own output. So the code lives here, its identity is
:data:`STEM_ALGORITHM_VERSION`, and that version is folded into ``matcher_version``
(:mod:`wordextract.versions`) -- re-vendoring a new Porter changes every cache key even if no
matcher line does. Which algorithm a *group* asks for is registry data, not matcher data: it
is :attr:`wordextract.model.TermGroup.stemming`, and it travels in ``term_list_hash``.
"""
from __future__ import annotations

from typing import Callable

#: The vendored transcriptions's pinned identity, folded into ``matcher_version`` (D6). A
#: group names an *algorithm* (``TermGroup.stemming``); this names the *code* behind it, so
#: bumping it invalidates every key that stemmed anything.
STEM_ALGORITHM_VERSION = "porter-1"

#: Porter's vowels. ``y`` is a consonant unless it has a vowel before it, handled in
#: :func:`_is_consonant`.
_VOWELS = "aeiou"


def _is_consonant(word: str, index: int) -> bool:
    """Whether ``word[index]`` is a consonant, Porter's ``*c`` (``y`` is context-sensitive).

    A character that is neither a vowel nor ``y`` counts as a consonant. :func:`porter_stem`
    only ever passes it ASCII words (a non-ASCII token is returned unchanged), but the rule
    is pinned here all the same so the helper is total.
    """
    char = word[index]
    if char in _VOWELS:
        return False
    if char == "y":
        return index == 0 or not _is_consonant(word, index - 1)
    return True


def _measure(word: str) -> int:
    """``m``: the number of VC sequences with which ``word`` measures (Porter §1).

    Roughly "how many syllables of the CVC kind fit in the word", and the quantity every
    suffix rule is conditioned on. Counted over the runs of vowels and consonants: each
    vowel run that is followed by a consonant run is one sequence.
    """
    sequences = 0
    in_vowel_run = False
    for index in range(len(word)):
        if _is_consonant(word, index):
            if in_vowel_run:
                sequences += 1
                in_vowel_run = False
        else:
            in_vowel_run = True
    return sequences


def _has_vowel(stem: str) -> bool:
    """Porter's ``*v*``: whether ``stem`` contains a vowel anywhere."""
    return any(not _is_consonant(stem, index) for index in range(len(stem)))


def _ends_double_consonant(word: str) -> bool:
    """Porter's ``*d``: whether ``word`` ends with the same consonant twice."""
    return len(word) >= 2 and word[-1] == word[-2] and _is_consonant(word, len(word) - 1)


def _ends_cvc(word: str) -> bool:
    """Porter's ``*o``: whether ``word`` ends consonant-vowel-consonant.

    The final consonant is not ``w``, ``x`` or ``y`` -- the spelling after which a word may
    take a second consonant in English, which is what the ``-e`` restoration in Step 1b and
    the ``-e`` removal in Step 5a turn on.
    """
    if len(word) < 3:
        return False
    return (
        _is_consonant(word, len(word) - 1)
        and word[-1] not in "wxy"
        and not _is_consonant(word, len(word) - 2)
        and _is_consonant(word, len(word) - 3)
    )


#: Step 2's suffix table, longest forms first where one is a suffix of another so the first
#: match is the intended one (Porter's own ordering).
_STEP_2: tuple[tuple[str, str], ...] = (
    ("ational", "ate"),
    ("tional", "tion"),
    ("enci", "ence"),
    ("anci", "ance"),
    ("izer", "ize"),
    ("abli", "able"),
    ("alli", "al"),
    ("entli", "ent"),
    ("eli", "e"),
    ("ousli", "ous"),
    ("ization", "ize"),
    ("ation", "ate"),
    ("ator", "ate"),
    ("alism", "al"),
    ("iveness", "ive"),
    ("fulness", "ful"),
    ("ousness", "ous"),
    ("aliti", "al"),
    ("iviti", "ive"),
    ("biliti", "ble"),
)

#: Step 3's suffix table.
_STEP_3: tuple[tuple[str, str], ...] = (
    ("icate", "ic"),
    ("ative", ""),
    ("alize", "al"),
    ("iciti", "ic"),
    ("ical", "ic"),
    ("ful", ""),
    ("ness", ""),
)

#: Step 4's suffix table. ``ion`` is a suffix only after ``s`` or ``t``; that extra
#: condition lives in :func:`_step_4`.
_STEP_4: tuple[tuple[str, str], ...] = (
    ("al", ""),
    ("ance", ""),
    ("ence", ""),
    ("er", ""),
    ("ic", ""),
    ("able", ""),
    ("ible", ""),
    ("ant", ""),
    ("ement", ""),
    ("ment", ""),
    ("ent", ""),
    ("ion", ""),
    ("ou", ""),
    ("ism", ""),
    ("ate", ""),
    ("iti", ""),
    ("ous", ""),
    ("ive", ""),
    ("ize", ""),
)


def _step_1a(word: str) -> str:
    """Step 1a: plural forms. ``SSES``/``IES`` drop two letters, ``S`` drops one."""
    if word.endswith("sses"):
        return word[:-2]
    if word.endswith("ies"):
        return word[:-2]
    if word.endswith("ss"):
        return word
    if word.endswith("s"):
        return word[:-1]
    return word


def _step_1b(word: str) -> str:
    """Step 1b: the past-tense and progressive forms, plus their spelling repairs."""
    applied = False
    if word.endswith("eed"):
        if _measure(word[:-3]) > 0:
            return word[:-1]
        return word
    if word.endswith("ed"):
        if _has_vowel(word[:-2]):
            word = word[:-2]
            applied = True
    elif word.endswith("ing"):
        if _has_vowel(word[:-3]):
            word = word[:-3]
            applied = True
    if not applied:
        return word
    if word.endswith(("at", "bl", "iz")):
        return word + "e"
    if _ends_double_consonant(word):
        return word[:-1] if word[-1] not in "lsz" else word
    if _measure(word) == 1 and _ends_cvc(word):
        return word + "e"
    return word


def _step_1c(word: str) -> str:
    """Step 1c: a ``y`` with a vowel before it becomes ``i``."""
    if word.endswith("y") and _has_vowel(word[:-1]):
        return word[:-1] + "i"
    return word


def _apply_table(word: str, table: tuple[tuple[str, str], ...], minimum: int) -> str:
    """Replace the longest-matching suffix in ``table`` if the stem's measure exceeds ``minimum``."""
    for suffix, replacement in table:
        if word.endswith(suffix) and _measure(word[: len(word) - len(suffix)]) > minimum:
            return word[: len(word) - len(suffix)] + replacement
    return word


def _step_2(word: str) -> str:
    return _apply_table(word, _STEP_2, minimum=0)


def _step_3(word: str) -> str:
    return _apply_table(word, _STEP_3, minimum=0)


def _step_4(word: str) -> str:
    """Step 4: the derivational suffixes, conditioned on ``m > 1`` (``-ion`` after ``s``/``t``)."""
    for suffix, replacement in _STEP_4:
        if not word.endswith(suffix):
            continue
        stem = word[: len(word) - len(suffix)]
        if suffix == "ion" and (not stem or stem[-1] not in "st"):
            continue
        if _measure(stem) > 1:
            return stem + replacement
    return word


def _step_5a(word: str) -> str:
    """Step 5a: drop a final ``e``, unless a bare ``m == 1`` word still needs it (*o*)."""
    if not word.endswith("e"):
        return word
    stem = word[:-1]
    measure = _measure(stem)
    if measure > 1 or (measure == 1 and not _ends_cvc(stem)):
        return stem
    return word


def _step_5b(word: str) -> str:
    """Step 5b: a doubled ``l`` after ``m > 1`` becomes a single ``l``."""
    if word.endswith("ll") and _measure(word) > 1:
        return word[:-1]
    return word


def porter_stem(word: str) -> str:
    """The Porter stem of ``word``: the published algorithm, Step 1a to Step 5b in order.

    Pinned and pure: the same word always stems to the same string, with no dictionary and
    no state. Two guards keep "stem" a well-defined reading of *a token*, not of any string:

    * a word of two letters or fewer is returned unchanged -- none of the suffixes is
      reachable, which is what the reference implementation short-circuits on;
    * a word that is not plain ASCII letters is returned unchanged. Porter is an English
      algorithm over an ASCII alphabet; running it on ``überweisung`` would strip the ASCII
      ``-ing`` and produce a nonsense "stem", and on ``r2d2`` a stem of nothing. Returning
      such a token as its own stem is the honest, pinned reading -- and it costs nothing,
      because the registry side does the same to the form.
    """
    word = word.lower()
    if len(word) <= 2 or not (word.isascii() and word.isalpha()):
        return word
    word = _step_1a(word)
    word = _step_1b(word)
    word = _step_1c(word)
    word = _step_2(word)
    word = _step_3(word)
    word = _step_4(word)
    word = _step_5a(word)
    word = _step_5b(word)
    return word


#: The vendored algorithms a ``TermGroup.stemming`` may name, by name. One entry today; the
#: matcher reads it per group, so adding another is a registration, not a matcher change.
STEMMERS: dict[str, Callable[[str], str]] = {"porter": porter_stem}


def stemmer(name: str) -> Callable[[str], str]:
    """The vendored stemmer ``name`` names, or :class:`ValueError` if none does.

    Loud rather than silent: a ``TermGroup.stemming`` that names no shipped algorithm is a
    registry the matcher cannot honour, and quietly not stemming it would turn a typo into a
    silent miss (D6 -- the miss is the failure mode, not the extra hit).
    """
    try:
        return STEMMERS[name]
    except KeyError:
        raise ValueError(
            f"unknown stemmer {name!r}: this build vendors {sorted(STEMMERS)}"
        ) from None
