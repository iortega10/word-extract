"""Core version constant and the git-revision helper (None outside a repo)."""
from __future__ import annotations

import docextract_core
from docextract_core import CORE_VERSION, git_revision


def test_core_version_is_a_nonempty_string():
    assert isinstance(CORE_VERSION, str) and CORE_VERSION


def test_git_revision_returns_head_inside_this_repo():
    rev = git_revision()
    assert isinstance(rev, str) and len(rev) == 40


def test_git_revision_returns_none_outside_a_repo(tmp_path):
    assert git_revision(tmp_path) is None
