"""Turn 0e validation: growing the fixture corpus is not a behavior change.

The first ledger fingerprinted whatever fixtures were committed, so adding one fixture
(``program_review_v2.docx``) changed every corpus-derived fingerprint and the only way to keep
the ledger green was to bump ``TEXTMODEL_VERSION``, ``CHUNKER_VERSION`` and
``MATCHER_VERSION`` -- with no behavior change. Those constants are store keys, so a real
store would have been invalidated by adding a test document.

The ledger now versions the corpus separately (``tests/ledger/corpus.json``) and keys each
line ``<component version>|corpus:<N>``. Adding a fixture adds a corpus version and lines
under the *same* component versions, and the tool recomputes every older corpus first, so a
behavior change hiding in the same commit as a new fixture is still refused.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import behavior_ledger as ledger  # noqa: E402
import update_behavior_ledger as tool  # noqa: E402

from wordextract import versions, views  # noqa: E402

FIXTURES = ROOT / "fixtures"


def _isolated(tmp_path: Path, monkeypatch, names=("program_review_v3.docx", "edge_cases.docx")):
    """A tiny fixtures directory, its own manifest and ledger: nothing shared with the repo's."""
    fixtures = tmp_path / "fixtures"
    (fixtures / "terms").mkdir(parents=True)
    shutil.copy(FIXTURES / ledger.TERM_LIST, fixtures / ledger.TERM_LIST)
    for name in names:
        shutil.copy(FIXTURES / name, fixtures / name)
    monkeypatch.setattr(ledger, "CORPUS_PATH", tmp_path / "corpus.json")
    return fixtures, tmp_path / "ledger.json"


def _run(fixtures: Path, ledger_path: Path, *extra: str) -> int:
    return tool.main(["--fixtures", str(fixtures), "--ledger", str(ledger_path), *extra])


def test_adding_a_fixture_adds_a_corpus_version_and_bumps_no_component_version(tmp_path, monkeypatch, capsys):
    fixtures, ledger_path = _isolated(tmp_path, monkeypatch)
    assert _run(fixtures, ledger_path) == 0
    before = ledger.load_ledger(ledger_path)
    assert all(key.endswith("|corpus:1") for key in before["parse"])

    shutil.copy(FIXTURES / "binder_summary.docx", fixtures / "binder_summary.docx")
    problems = ledger.check(ledger.load_ledger(ledger_path), fixtures_dir=fixtures)
    assert any(p.startswith("corpus:") and "--new-corpus" in p for p in problems)  # noticed

    versions_before = (versions.SPEC_PARSER_VERSION, versions.TEXTMODEL_VERSION, versions.CHUNKER_VERSION)
    assert _run(fixtures, ledger_path, "--new-corpus") == 0
    assert (versions.SPEC_PARSER_VERSION, versions.TEXTMODEL_VERSION, versions.CHUNKER_VERSION) == versions_before

    after = ledger.load_ledger(ledger_path)
    assert ledger.check(after, fixtures_dir=fixtures) == []
    for component in ledger.CORPUS_DEPENDENT:
        keys = sorted(after[component])
        assert [k.split("|corpus:")[1] for k in keys if "|corpus:" in k] == ["1", "2"]
        # the old line is untouched and shares its component version with the new one
        assert before[component].items() <= after[component].items()
        assert len({k.split("|corpus:")[0] for k in keys}) == 1
    assert after["contracts"] == before["contracts"]  # corpus-independent: one line, unchanged
    assert [e["version"] for e in ledger.load_corpora()] == [1, 2]


def test_a_behavior_change_hidden_in_the_same_commit_as_a_new_fixture_is_refused(tmp_path, monkeypatch, capsys):
    fixtures, ledger_path = _isolated(tmp_path, monkeypatch)
    assert _run(fixtures, ledger_path) == 0
    shutil.copy(FIXTURES / "binder_summary.docx", fixtures / "binder_summary.docx")
    # the behavior changes too (the view mask), without a version bump
    monkeypatch.setattr(views, "keeps", lambda stack, view: True)
    capsys.readouterr()
    assert _run(fixtures, ledger_path, "--new-corpus") == 1
    err = capsys.readouterr().err
    assert "already recorded" in err and "Bump" in err


def test_the_manifest_is_written_once_per_change_and_a_second_run_changes_nothing(tmp_path, monkeypatch):
    fixtures, ledger_path = _isolated(tmp_path, monkeypatch)
    assert _run(fixtures, ledger_path, "--new-corpus") == 0
    assert [e["version"] for e in ledger.load_corpora()] == [1]
    assert _run(fixtures, ledger_path, "--new-corpus") == 0  # nothing grew: no version 2
    assert [e["version"] for e in ledger.load_corpora()] == [1]


def test_the_contracts_line_has_no_corpus_suffix():
    assert ledger.entry_key("contracts", "5", 3) == "5"
    assert ledger.entry_key("parse", "1|2|2", 3) == "1|2|2|corpus:3"


def test_every_committed_corpus_version_is_a_growing_superset_of_the_last():
    corpora = ledger.load_corpora()
    assert [e["version"] for e in corpora] == list(range(1, len(corpora) + 1))
    for earlier, later in zip(corpora, corpora[1:]):
        assert set(earlier["files"]) <= set(later["files"])
    assert sorted(corpora[-1]["files"]) == sorted(ledger.discovered())


def test_the_v2_fixture_is_corpus_two_and_its_arrival_bumped_nothing():
    corpora = {e["version"]: e["files"] for e in ledger.load_corpora()}
    assert "program_review_v2.docx" not in corpora[1]
    assert "program_review_v2.docx" in corpora[2]
    recorded = ledger.load_ledger()
    version = versions.TEXTMODEL_VERSION
    # the same views version carries a line for both corpora
    assert f"{version}|corpus:1" in recorded["views"] and f"{version}|corpus:2" in recorded["views"]
