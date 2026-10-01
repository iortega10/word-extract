"""Turn 9: the behavior ledger -- a version bump as a failing test, not a habit.

Two things are being defended here. The first is the exit criterion: every component's
fingerprint matches the one recorded for its *current* version string. The second is that
the guard can actually fail -- the spec asks for exactly that, so half these tests
monkeypatch a constant or reshape a dataclass and require the check to notice. A guard
nobody has watched fail is a guard nobody should trust.
"""
from __future__ import annotations

import dataclasses
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import behavior_ledger  # noqa: E402
import update_behavior_ledger  # noqa: E402
from wordextract import versions  # noqa: E402

LEDGER = ROOT / "tests" / "ledger" / "behavior_ledger.json"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------- the committed ledger


def test_the_ledger_is_committed_with_a_header_and_every_component():
    assert LEDGER.is_file()
    body = json.loads(LEDGER.read_text(encoding="utf-8"))
    assert set(body) - {"_comment"} == set(behavior_ledger.COMPONENTS)
    # the header is what stops the file from reading as full history
    assert "not reconstructed" in body["_comment"]
    assert "fixtures/real" in body["_comment"]
    for component in behavior_ledger.COMPONENTS:
        entry = body[component]
        assert entry, f"{component} records no version"
        assert all(_SHA256.match(fingerprint) for fingerprint in entry.values())


def test_every_current_version_is_recorded_and_its_fingerprint_matches():
    """The exit criterion, through the default path a test or reviewer would take."""
    assert behavior_ledger.check(behavior_ledger.load_ledger()) == []


def test_the_check_names_the_constant_to_bump_when_a_version_is_missing(monkeypatch):
    monkeypatch.setattr(versions, "CHUNKER_VERSION", "999")
    problems = behavior_ledger.check()
    assert any(problem.startswith("chunks:") and "999" in problem for problem in problems)
    assert any("CHUNKER_VERSION" in problem for problem in problems)
    # only the bumped component disagrees: `views` still matches its recorded fingerprint
    assert not any(problem.startswith("views:") for problem in problems)


def test_the_parse_version_string_is_the_composite_of_its_three_constants(monkeypatch):
    """One `ParseResult` is the product of the walker, the text model and the heading
    rules, so a bump to any of the three invalidates the parse fingerprint."""
    assert behavior_ledger.version_strings()["parse"] == "1|3|2"
    monkeypatch.setattr(versions, "TEXTMODEL_VERSION", "999")
    assert behavior_ledger.version_strings()["parse"] == "1|999|2"
    problems = behavior_ledger.check()
    assert any(problem.startswith("parse:") for problem in problems)
    assert any(problem.startswith("views:") for problem in problems)  # the shared constant


def test_changing_behavior_without_bumping_the_version_fails_the_check():
    """The other half: the version is in the ledger, but no longer describes the code."""
    computed = behavior_ledger.fingerprints()
    computed["chunks"] = "0" * 64
    problems = behavior_ledger.check(behavior_ledger.load_ledger(), computed=computed)
    assert any(
        problem.startswith("chunks:") and "behavior changed without a bump" in problem
        for problem in problems
    )
    assert not any(problem.startswith("views:") for problem in problems)


def test_fingerprints_are_reproducible_and_cover_every_component():
    first = behavior_ledger.fingerprints()
    assert set(first) == set(behavior_ledger.COMPONENTS)
    assert all(_SHA256.match(fingerprint) for fingerprint in first.values())
    assert behavior_ledger.fingerprints() == first


def test_the_corpus_is_the_committed_fixtures_not_the_machine_local_ones(tmp_path):
    """`fixtures/real/` is git-ignored and machine-local, so it can never be part of a
    committed constant -- see the module docstring."""
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "chubb-cgl-form.docx").write_bytes(b"")
    (tmp_path / "model").mkdir()
    (tmp_path / "model" / "b.docx").write_bytes(b"")
    (tmp_path / "a.docx").write_bytes(b"")
    assert [path.name for path in behavior_ledger.corpus(tmp_path)] == ["a.docx", "b.docx"]

    committed = {
        path for path in (ROOT / "fixtures").rglob("*.docx") if "real" not in path.parts
    }
    corpus = behavior_ledger.corpus()
    assert set(corpus) == committed  # everything else under fixtures/ is in


# ---------------------------------------------------------------- the contracts component


@dataclasses.dataclass(frozen=True)
class _Double:
    alpha: int = 0
    beta: str = "b"


@dataclasses.dataclass(frozen=True)
class _Added(_Double):
    gamma: bool = False


@dataclasses.dataclass(frozen=True)
class _Removed:
    alpha: int = 0


@dataclasses.dataclass(frozen=True)
class _Renamed(_Double):
    alpha: int = 0
    beta_renamed: str = "b"


@dataclasses.dataclass(frozen=True)
class _Retyped(_Double):
    alpha: int = 0
    beta: float = 0.5


def test_every_contract_record_is_fingerprinted():
    """`wordextract.model`'s dataclasses plus the core's own records, by name."""
    names = {cls.__qualname__ for cls in behavior_ledger.contract_records()}
    assert {"RunRecord", "ParseResult", "TermHit", "Chunk"} <= names
    assert {"LLMCall", "LLMResponse", "RawArchiveRecord"} <= names
    assert "HashedInputs" in names  # the reproducibility key is a contract too


def test_a_field_added_removed_renamed_or_retyped_in_a_double_changes_the_contracts():
    """A shape change is the contract change a schema bump is for, so the fingerprint
    must move for every way a field can change."""
    base = behavior_ledger.contracts_fingerprint([_Double])
    assert behavior_ledger.contracts_fingerprint([_Double]) == base  # stable, not random
    shapes = {
        name: behavior_ledger.contracts_fingerprint([double])
        for name, double in (
            ("added", _Added),
            ("removed", _Removed),
            ("renamed", _Renamed),
            ("retyped", _Retyped),
        )
    }
    assert all(fingerprint != base for fingerprint in shapes.values())
    assert len({base, *shapes.values()}) == 1 + len(shapes)  # no two of them agree
    # and the shape of a set of records is the shapes of its members
    assert behavior_ledger.contracts_fingerprint([_Double, _Added]) not in {
        base,
        *shapes.values(),
    }


# ---------------------------------------------------------------- the update tool


def test_record_refuses_to_overwrite_a_version_it_already_holds():
    computed = behavior_ledger.fingerprints()
    computed["matcher"] = "0" * 64
    with pytest.raises(ValueError, match="MATCHER_VERSION"):
        behavior_ledger.record(behavior_ledger.load_ledger(), computed=computed)


def test_the_update_tool_refuses_a_same_version_overwrite_and_writes_nothing(tmp_path, capsys):
    ledger_path = tmp_path / "behavior_ledger.json"
    broken = behavior_ledger.load_ledger()
    broken["chunks"] = {version: "0" * 64 for version in broken["chunks"]}
    behavior_ledger.write_ledger(broken, ledger_path)
    before = ledger_path.read_bytes()

    assert update_behavior_ledger.main(["--ledger", str(ledger_path)]) == 1
    printed = capsys.readouterr()
    assert printed.out == ""
    assert "CHUNKER_VERSION" in printed.err and "never overwritten" in printed.err
    assert ledger_path.read_bytes() == before


def test_the_update_tool_appends_every_version_and_is_a_no_op_when_nothing_changed(tmp_path, capsys):
    ledger_path = tmp_path / "behavior_ledger.json"
    assert update_behavior_ledger.main(["--ledger", str(ledger_path)]) == 0
    assert capsys.readouterr().out.count("appended") == len(behavior_ledger.COMPONENTS)
    written = behavior_ledger.load_ledger(ledger_path)
    assert set(written) == set(behavior_ledger.COMPONENTS)
    assert behavior_ledger.check(written) == []

    before = ledger_path.read_bytes()
    assert update_behavior_ledger.main(["--ledger", str(ledger_path)]) == 0
    assert "unchanged" in capsys.readouterr().out
    assert ledger_path.read_bytes() == before


def test_the_check_option_reports_the_disagreement_and_exits_non_zero(monkeypatch, capsys):
    assert update_behavior_ledger.main(["--check"]) == 0
    assert capsys.readouterr().err == ""

    monkeypatch.setattr(versions, "CHUNKER_VERSION", "999")
    assert update_behavior_ledger.main(["--check"]) == 1
    assert "CHUNKER_VERSION" in capsys.readouterr().err


def test_a_ledger_whose_newlines_were_translated_is_written_back_to_lf(tmp_path, capsys):
    """`write_ledger` promises the same bytes everywhere; a checkout that translated them
    must not read as `unchanged` and stay that way."""
    ledger_path = tmp_path / "behavior_ledger.json"
    behavior_ledger.write_ledger(behavior_ledger.load_ledger(), ledger_path)
    ledger_path.write_bytes(ledger_path.read_bytes().replace(b"\n", b"\r\n"))
    assert b"\r\n" in ledger_path.read_bytes()

    assert update_behavior_ledger.main(["--ledger", str(ledger_path)]) == 0
    assert "written" in capsys.readouterr().err
    assert b"\r" not in ledger_path.read_bytes()
