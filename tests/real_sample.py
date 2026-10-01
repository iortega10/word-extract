"""The one machine-local Word document some tests read, and how they behave without it.

``local-private/review_sample.docx`` is git-ignored (real-shaped documents never enter the
repository). Tests that need it are skipped, not failed, on a
machine that does not have it -- the same convention the form-extract sibling uses for its
real samples. Everything it checked beyond what the committed fixtures cover (note parts with
separator runs, zip directory entries, a Word-style bullet list) is therefore checked only
where a document with those properties has been dropped in.
"""
from pathlib import Path

import pytest

REAL_SAMPLE = Path(__file__).resolve().parents[1] / "local-private" / "review_sample.docx"

requires_real_sample = pytest.mark.skipif(
    not REAL_SAMPLE.exists(),
    reason="local-private/review_sample.docx is machine-local and absent",
)
