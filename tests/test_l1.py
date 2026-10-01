"""Turn 9: L1 -- the exact-fact oracle, and the ways it is able to fail.

L1's whole value is that a green run means something. So the tests here are mostly
*negative*: a label that disagrees with the parser must turn its family red, a walker that
loses a span must be caught even though no label mentions the span, an empty corpus must
fail the gate rather than pass vacuously, and a fact a label does not pin must be skipped
*on the record*. The positive case is the committed corpus, scored green.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from wordextract import opc
from wordextract.evals import l1
from wordextract.model import NodeKind
from wordextract.walker import walk_document

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
MODEL = FIXTURES / "model"

_SIDECAR = "nested_revisions"
_FIXTURE = f"{_SIDECAR}.docx"


def _labels_dir(tmp_path: Path, mutate=None, sidecar: str = _SIDECAR) -> Path:
    """A one-fixture corpus: the real ``.docx`` beside a sidecar a test may have edited.

    L1 re-parses the ``.docx`` itself, so the document is the committed one and only the
    labels move -- which is what makes a failure here about the comparison, not the parser.
    """
    labels = json.loads((MODEL / f"{sidecar}.expected.json").read_text(encoding="utf-8"))
    if mutate is not None:
        mutate(labels)
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / labels["fixture"]).write_bytes((MODEL / labels["fixture"]).read_bytes())
    (tmp_path / f"{sidecar}.expected.json").write_text(json.dumps(labels), encoding="utf-8")
    return tmp_path


def _parsed(name: str = _FIXTURE):
    return walk_document(opc.Package(MODEL / name))


def test_the_committed_corpus_scores_green_in_every_family():
    report = l1.score_fixtures(FIXTURES)
    assert report.result is True
    assert report.exact_fact_accuracy == 1.0
    rows = {row["family"]: row for row in report.rows()}
    assert set(rows) == set(l1.FAMILIES)
    # every family compared something: a family that quietly stopped scoring would
    # otherwise still show result True over zero facts
    assert all(row["compared"] > 0 for row in rows.values())
    assert report.coverage()["vacuous"] is False


def test_an_empty_corpus_fails_the_gate_instead_of_passing_vacuously(tmp_path):
    report = l1.score_fixtures(tmp_path)
    assert report.facts == ()
    assert report.exact_fact_accuracy == 0.0
    assert report.result is False  # 0.0, not a free pass
    assert report.coverage()["vacuous"] is True


@pytest.mark.parametrize(
    "mutate, family",
    [
        pytest.param(
            lambda labels: labels["paragraphs"][0].__setitem__("union", "Coverage applies nowhere."),
            "paragraphs",
            id="wrong union text",
        ),
        pytest.param(
            lambda labels: labels["paragraphs"][0]["spans"][0].__setitem__("text", "CoverageX "),
            "paragraphs",
            id="wrong span text",
        ),
        pytest.param(
            lambda labels: labels["paragraphs"][0].__setitem__("accepted", "Coverage applies worldwide!"),
            "paragraphs",
            id="wrong accepted view",
        ),
        pytest.param(
            lambda labels: labels["paragraphs"][0]["spans"][0].__setitem__("start", 1),
            "paragraphs",
            id="wrong span offset",
        ),
        pytest.param(
            lambda labels: labels["paragraphs"][0]["spans"].reverse(),
            "paragraphs",
            id="spans out of order",
        ),
        pytest.param(
            lambda labels: labels["paragraphs"][0]["spans"][1]["stack"].append("del:9"),
            "paragraphs",
            id="wrong ancestor stack",
        ),
        pytest.param(
            lambda labels: labels["revisions"][0].__setitem__("author", "Someone Else"),
            "revisions",
            id="wrong revision author",
        ),
        pytest.param(
            lambda labels: labels["revisions"][0].__setitem__("kind", "del"),
            "revisions",
            id="wrong revision kind",
        ),
        pytest.param(
            lambda labels: labels["revisions"].pop(),
            "revisions",
            id="a revision the parser found is unlabelled",
        ),
        pytest.param(
            lambda labels: labels["revisions"].append(dict(labels["revisions"][0], id="ins:99")),
            "revisions",
            id="a revision the parser never found",
        ),
    ],
)
def test_a_label_that_disagrees_with_the_parser_fails_its_family(tmp_path, mutate, family):
    report = l1.score_fixtures(_labels_dir(tmp_path, mutate))
    assert report.result is False
    assert report.exact_fact_accuracy < 1.0
    # the failure is attributed to the family the label lied about, and no other
    assert {fact.family for fact in report.failures} == {family}
    # ... and it says what the label claimed and what the parser produced
    for fact in report.failures:
        assert fact.want != fact.got
        assert fact.line.startswith(f"{_SIDECAR}: ")


def test_a_label_that_pins_nothing_is_skipped_on_the_record_not_passed(tmp_path):
    """A JSON null is not an assertion, so it is a counted skip and never a silent pass."""
    report = l1.score_fixtures(
        _labels_dir(tmp_path, lambda labels: labels["revisions"][0].__setitem__("date", None))
    )
    assert report.result is True  # nothing was claimed about that date
    skip = next(s for s in report.skips if s.what == "revisions[0].date")
    assert skip.why == "the sidecar pins no date"
    rows = {row["family"]: row for row in report.rows()}
    assert rows["revisions"]["skipped"] >= 1
    assert next(r.family for r in report.facts if r.what == "revisions[1].date") == "revisions"


def test_a_family_the_label_does_not_assert_is_not_compared_at_all(tmp_path):
    """An absent family is not the same as an empty one: it asserts nothing, so it scores
    nothing -- while ``"revisions": []`` would still be asserted (and would fail here)."""
    dropped = l1.score_fixtures(_labels_dir(tmp_path, lambda labels: labels.pop("revisions")))
    assert dropped.result is True
    assert not [fact for fact in dropped.facts if fact.family == "revisions"]
    assert not [skip for skip in dropped.skips if skip.family == "revisions"]

    empties = l1.score_fixtures(
        _labels_dir(tmp_path / "empty", lambda labels: labels.__setitem__("revisions", []))
    )
    assert empties.result is False
    failed = next(fact for fact in empties.failures if fact.what == "revision count")
    assert (failed.want, failed.got) == (0, 4)


def test_tiling_catches_a_walker_that_loses_an_elementary_span(tmp_path, monkeypatch):
    """No sidecar states the tiling: this is L1 catching a parser regression on its own."""
    real = _parsed()
    stream = next(s for s in real.union_streams if len(s.spans) > 1)
    broken = dataclasses.replace(stream, spans=stream.spans[1:])
    parsed = dataclasses.replace(
        real, union_streams=[broken if s is stream else s for s in real.union_streams]
    )
    monkeypatch.setattr(l1, "walk_document", lambda package: parsed)

    report = l1.score_fixtures(_labels_dir(tmp_path))
    assert report.result is False
    assert "tiling" in {fact.family for fact in report.failures}
    gap = next(fact for fact in report.failures if "tile [0," in fact.what)
    assert "starts at" in gap.got


def test_tiling_catches_a_walker_that_drops_a_text_node(tmp_path, monkeypatch):
    real = _parsed()
    node = next(n for n in real.nodes if n.kind is NodeKind.PARA and n.spans)
    parsed = dataclasses.replace(real, nodes=[n for n in real.nodes if n.id != node.id])
    monkeypatch.setattr(l1, "walk_document", lambda package: parsed)

    report = l1.score_fixtures(_labels_dir(tmp_path))
    assert report.result is False
    lost = next(
        fact for fact in report.failures if "in a node span or is a terminator" in fact.what
    )
    assert lost.got.startswith("not accounted for at [")


def test_scoring_reads_the_labels_and_never_writes_them(tmp_path):
    directory = _labels_dir(tmp_path, sidecar="text_box")
    before = {path.name: path.read_bytes() for path in sorted(directory.iterdir())}
    l1.score_fixtures(directory)
    assert {path.name: path.read_bytes() for path in sorted(directory.iterdir())} == before
