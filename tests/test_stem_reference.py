"""Turn 6b validation: the vendored Porter against an independent implementation.

``fixtures/stem/porter_reference_vectors.tsv`` is produced by NLTK's original-algorithm
Porter (``tools/make_stem_vectors.py``), a separate implementation of the same paper
(M. F. Porter, *An algorithm for suffix stripping*, Program 14(3):130-137, 1980). Agreement
here is agreement with something that is not this module, which is what the build spec asks
for ("tested against external published vectors"): a stemmer tested only against a table it
produced proves only that it agrees with itself.

The first transcription disagreed with the reference on 45 of 55,461 words, all of them in
the ``-ment`` family. The cause: Porter's rule is that the **longest** matching suffix
decides a step, and if its condition fails the step does nothing. The transcription fell back
to a shorter suffix instead, so ``agreement`` (``-ement`` fails on measure) lost ``-ent`` and
became ``agreem``. Those words are pinned below as hand-typed literals.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from wordextract.stem import porter_stem

VECTORS = Path(__file__).resolve().parents[1] / "fixtures" / "stem" / "porter_reference_vectors.tsv"
TAB = chr(9)
NEWLINE = chr(10)


def _vectors() -> list[tuple[str, str]]:
    rows = []
    for line in VECTORS.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            word, stem = line.split(TAB)
            rows.append((word, stem))
    return rows


def test_the_reference_vector_file_is_large_and_carries_its_provenance():
    header = NEWLINE.join(
        line for line in VECTORS.read_text(encoding="utf-8").splitlines() if line.startswith("#")
    )
    assert len(_vectors()) > 2000
    assert "ORIGINAL_ALGORITHM" in header and "independent" in header
    assert "Porter" in header and "1980" in header


@pytest.mark.parametrize("word,stem", _vectors())
def test_porter_stem_equals_the_independent_reference(word, stem):
    assert porter_stem(word) == stem


@pytest.mark.parametrize(
    ("word", "stem"),
    [
        # Hand-typed from the reference: the -ment family the first transcription got wrong.
        ("agreement", "agreement"),
        ("agreements", "agreement"),
        ("document", "document"),
        ("documents", "document"),
        ("element", "element"),
        ("instrument", "instrument"),
        ("movement", "movement"),
        ("settlement", "settlement"),
        ("statement", "statement"),
        ("payment", "payment"),
        # where -ement does pass its condition it is stripped
        ("endorsement", "endors"),
        ("requirement", "requir"),
    ],
)
def test_a_failed_longest_suffix_condition_ends_the_step(word, stem):
    assert porter_stem(word) == stem


def test_words_that_name_different_things_share_a_stem_which_is_why_stemming_is_opt_in():
    """Porter's true behavior: universe / university / universal are one stem. They name
    different things, so a registry must opt a group in to stemming, never a default
    (``TermGroup.stemming``)."""
    assert {porter_stem(w) for w in ("universe", "university", "universal", "universes")} == {"univers"}
