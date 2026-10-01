"""Turn 9: L2 -- the loader, the format, the scorer, and the gate's two halves.

L2 is the layer whose labels can make the *product* wrong: a term list that misses a phrase
a human says is in the document fails at recall, and a hit nobody claimed is a false
positive until a human classifies it. With no human label set committed, L2 must report
**not evaluated** rather than a pass -- which these tests pin down from both ends: the
loader refuses anything but ``human`` provenance, and the scorer's gate needs recall 1.0
*and* every extra classified.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from wordextract.evals import harness
from wordextract.evals import must_find
from wordextract.evals.must_find import (
    L2Report,
    must_find_from_dict,
    iter_must_find,
    score,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
EVALS = FIXTURES / "evals"

#: A phrase that is in ``program_review_v3.docx`` -- read out of the document, not the run.
IN_THE_DOCUMENT = "waiver of subrogation"


def _labels(documents: dict, *, label_set="test", term_list="terms/synthetic.json"):
    return must_find_from_dict(
        {
            "label_set": label_set,
            "labels_provenance": "human",
            "term_list": term_list,
            "documents": documents,
        }
    )


def _document(*must_find, expected_extra=()):
    return {
        "must_find": [{"term": term} for term in must_find],
        "expected_extra": [
            {"term": term, "classification": classification}
            for term, classification in expected_extra
        ],
    }


def _case(tmp_path: Path, labels: dict, document: str = "program_review_v3.docx") -> Path:
    """A fixtures directory with one document, one registry and one label set."""
    fixtures = tmp_path / "fixtures"
    (fixtures / "terms").mkdir(parents=True)
    (fixtures / "evals").mkdir()
    (fixtures / document).write_bytes((FIXTURES / document).read_bytes())
    (fixtures / "terms" / "synthetic.json").write_bytes(
        (FIXTURES / "terms" / "synthetic.example.json").read_bytes()
    )
    (fixtures / "evals" / "labels.json").write_text(json.dumps(labels), encoding="utf-8")
    return fixtures


# ---------------------------------------------------------------- the loader


def test_the_committed_example_is_never_scored():
    """The example carries invented phrases, so iter_must_find skips it by name."""
    assert must_find.EXAMPLE_SUFFIX in "must_find.example.json"
    assert (EVALS / "must_find.example.json").is_file()
    assert "must_find.example" not in iter_must_find(EVALS)
    # whatever is committed here is a real label set: human, and naming a document
    for labels in iter_must_find(EVALS).values():
        assert labels.labels_provenance == "human"
        assert labels.documents


def test_a_generator_or_model_provenance_is_refused():
    """A label set that a generator wrote is the circularity L2 exists to avoid."""
    for provenance in ("generator", "spec", "model"):
        with pytest.raises(ValueError, match="human"):
            must_find_from_dict(
                {
                    "label_set": "x",
                    "labels_provenance": provenance,
                    "term_list": "t.json",
                    "documents": {},
                }
            )


def test_the_loader_rejects_unknown_keys_and_classifications():
    with pytest.raises(ValueError, match="unknown keys"):
        must_find_from_dict(
            {
                "label_set": "x",
                "labels_provenance": "human",
                "term_list": "t.json",
                "documents": {},
                "extra": 1,
            }
        )
    with pytest.raises(ValueError, match="unknown keys"):
        must_find_from_dict(
            {
                "label_set": "x",
                "labels_provenance": "human",
                "term_list": "t.json",
                "documents": {"d.docx": {"must_find": [{"term": "a", "why": "no"}]}},
            }
        )
    with pytest.raises(ValueError, match="unknown classification"):
        must_find_from_dict(
            {
                "label_set": "x",
                "labels_provenance": "human",
                "term_list": "t.json",
                "documents": {
                    "d.docx": {
                        "expected_extra": [{"term": "a", "classification": "probably_fine"}]
                    }
                },
            }
        )


def test_two_label_sets_with_one_name_are_an_error(tmp_path):
    directory = tmp_path / "evals"
    directory.mkdir()
    body = json.dumps(
        {"label_set": "same", "labels_provenance": "human", "term_list": "t.json", "documents": {}}
    )
    (directory / "a.json").write_text(body, encoding="utf-8")
    (directory / "b.json").write_text(body, encoding="utf-8")
    with pytest.raises(ValueError, match="two must-find label sets"):
        iter_must_find(directory)


# ---------------------------------------------------------------- the scorer


def test_recall_is_found_over_expected_and_the_gate_needs_all_of_them():
    labels = _labels({"d.docx": _document("alpha", "beta", "gamma")})
    report = score(labels, {"d.docx": ["alpha", "beta", "gamma"]})
    assert report.recall == 1.0
    assert report.result is True
    assert report.metrics == {
        "recall": 1.0,
        "must_find": 3,
        "found": 3,
        "false_positives_unclassified": 0,
    }

    report = score(labels, {"d.docx": ["alpha", "gamma"]})
    assert report.recall == pytest.approx(2 / 3)
    assert report.result is False
    assert report.missed == ("beta",)
    assert report.note.startswith("failed: 1 missed")


def test_an_unclassified_extra_fails_the_layer_even_at_perfect_recall():
    """Recall alone is not the gate: a hit no human claimed is a false positive."""
    labels = _labels({"d.docx": _document("alpha")})
    report = score(labels, {"d.docx": ["alpha", "something else"]})
    assert report.recall == 1.0
    assert report.result is False
    assert report.unclassified == ("something else",)
    assert report.rows[0].extra == 1


def test_a_classified_extra_keeps_the_layer_green():
    labels = _labels(
        {"d.docx": _document("alpha", expected_extra=[("Something  Else", "true_positive")])}
    )
    report = score(labels, {"d.docx": ["ALPHA", "something  else"]})
    # comparison is case-folded and whitespace-collapsed, and nothing else
    assert report.recall == 1.0 and report.result is True
    assert report.extra == 1 and report.unclassified == ()


def test_a_document_that_is_not_on_disk_is_skipped_not_scored_as_a_miss():
    labels = _labels({"real/absent.docx": _document("alpha")})
    report = score(labels, {})
    assert report.rows == ()
    assert report.skipped == (
        ("real/absent.docx", "the document is not on disk where the labels name it"),
    )
    assert report.evaluated is False
    assert report.recall is None and report.metrics == {}
    assert report.result is None  # not a gate failure: nothing was measured
    assert report.note.startswith("not evaluated: no labelled document is on disk (real/absent")


def test_not_evaluated_is_never_a_pass_or_a_failure():
    empty = L2Report()
    assert empty.evaluated is False
    assert empty.result is None
    assert empty.recall is None
    assert empty.metrics == {}
    assert empty.note.startswith("not evaluated: no human must-find labels exist")

    nothing_to_find = score(_labels({"d.docx": _document()}), {"d.docx": ["whatever"]})
    assert nothing_to_find.evaluated is False
    assert nothing_to_find.result is None
    assert nothing_to_find.note == "not evaluated: the label set labels no terms"


def test_merge_scores_recall_over_all_terms_not_over_label_sets():
    weak = score(_labels({"a.docx": _document("alpha", "beta")}), {"a.docx": ["alpha"]})
    strong = score(_labels({"b.docx": _document("gamma")}), {"b.docx": ["gamma"]})
    merged = must_find.merge([weak, strong])
    assert (merged.expected, merged.found) == (3, 2)
    assert merged.recall == pytest.approx(2 / 3)  # not (0.5 + 1.0) / 2
    assert merged.missed == ("beta",)
    assert merged.label_sets == ("test",)
    assert {row.document for row in merged.rows} == {"a.docx", "b.docx"}


# ---------------------------------------------------------------- through the harness


def test_the_harness_scores_a_human_label_set_through_the_pipeline(tmp_path, monkeypatch):
    """End to end: the label set names a term list and a document, the harness runs the
    pipeline, and the hit texts it scores are read back **out of the store**."""
    fixtures = _case(
        tmp_path,
        {
            "label_set": "program-review",
            "labels_provenance": "human",
            "term_list": "terms/synthetic.json",
            "documents": {
                "program_review_v3.docx": _document(
                    IN_THE_DOCUMENT, expected_extra=[("subrogation", "stem_match")]
                )
            },
        },
    )
    monkeypatch.chdir(tmp_path)  # a default store would land here, so we would see it
    table = harness.run(fixtures)
    (layer,) = [row for row in table["quality"] if row["layer"] == "L2"]
    assert layer["result"] is True
    assert layer["metrics"]["recall"] == 1.0
    assert layer["metrics"]["found"] == 1
    assert layer["rows"][0]["document"] == "program_review_v3.docx"
    assert layer["coverage"]["evaluated"] is True
    # this corpus has no sidecar, so L1 compares nothing and (correctly) fails; the test is
    # about L2, which passed
    assert "L2" not in harness.gate_failures(table)
    # the run used a temporary store: no `.wordextract` was left in the working tree
    assert not (tmp_path / ".wordextract").exists()


def test_the_harness_fails_l2_when_the_term_list_misses_a_labelled_phrase(tmp_path):
    fixtures = _case(
        tmp_path,
        {
            "label_set": "program-review",
            "labels_provenance": "human",
            "term_list": "terms/synthetic.json",
            "documents": {"program_review_v3.docx": _document("aggregate limit")},
        },
    )
    table = harness.run(fixtures)
    (layer,) = [row for row in table["quality"] if row["layer"] == "L2"]
    assert layer["result"] is False
    assert layer["metrics"]["recall"] == 0.0
    # (the same sidecar-less corpus also fails L1; this test is about L2 failing)
    assert "L2" in harness.gate_failures(table)


def test_a_label_set_whose_term_list_is_missing_scores_nothing_rather_than_crashing(tmp_path):
    """A real term list lives outside the repo, so its absence is reported, not raised."""
    fixtures = _case(
        tmp_path,
        {
            "label_set": "program-review",
            "labels_provenance": "human",
            "term_list": "terms/not-there.json",
            "documents": {"program_review_v3.docx": _document(IN_THE_DOCUMENT)},
        },
    )
    table = harness.run(fixtures)
    (layer,) = [row for row in table["quality"] if row["layer"] == "L2"]
    assert layer["result"] is None
    assert layer["coverage"]["documents"] == 0


def test_a_labelled_document_that_is_not_on_disk_is_skipped_end_to_end(tmp_path):
    fixtures = _case(
        tmp_path,
        {
            "label_set": "program-review",
            "labels_provenance": "human",
            "term_list": "terms/synthetic.json",
            "documents": {"real/not-dropped-in-yet.docx": _document(IN_THE_DOCUMENT)},
        },
    )
    table = harness.run(fixtures)
    (layer,) = [row for row in table["quality"] if row["layer"] == "L2"]
    assert layer["result"] is None
    assert layer["coverage"]["skipped_documents"] == ["real/not-dropped-in-yet.docx"]
