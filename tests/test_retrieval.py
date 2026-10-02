"""Turn 7: retrieval — the loader, the format, the scorer, and what arms the gate.

The layer asks the query layer's own question: for an example in the words a user typed,
do the citations a human says should appear, appear? These tests pin both ends. The loader
scores nothing but a human set — the committed ``queries.example.json`` documents the format
and is never scored — and the scorer counts each citation once, present or missed: no rank
cut, no similarity, no confidence (rules 6 and 7 restated for retrieval).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from wordextract.evals import harness
from wordextract.evals.retrieval import (
    RetrievalReport,
    citation_from_dict,
    example_from_dict,
    iter_query_sets,
    merge_reports,
    query_set_from_dict,
    score,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
EVALS = FIXTURES / "evals"

#: A fixture document, addressed the way a query set addresses one (fixture-relative).
DOCUMENT = "program_review_v3.docx"


def _citation(document="d.docx", chunk=None, note=None):
    entry = {"document": document}
    if chunk is not None:
        entry["chunk"] = chunk
    if note is not None:
        entry["note"] = note
    return entry


def _example(query, citations, note=None):
    entry = {"query": query, "expected_citations": citations}
    if note is not None:
        entry["note"] = note
    return entry


def _query_set(examples, *, name="test", term_list="terms/synthetic.json", provenance="human"):
    return {
        "query_set": name,
        "labels_provenance": provenance,
        "term_list": term_list,
        "examples": examples,
    }


def _set(examples, **kwargs):
    return query_set_from_dict(_query_set(examples, **kwargs))


# ---------------------------------------------------------------- the loader


def test_the_committed_example_documents_the_format_and_never_scores():
    """The example is the only query file committed, and every loader skips it by name."""
    assert iter_query_sets(EVALS) == {}
    documented = query_set_from_dict(
        json.loads((EVALS / "queries.example.json").read_text(encoding="utf-8"))
    )
    # documented for whoever copies it -- well-formed, provenance human, citations shaped
    assert documented.query_set == "queries.example"
    assert documented.labels_provenance == "human"
    assert len(documented.examples) == 2
    assert all(example.expected for example in documented.examples)


def test_a_query_set_that_is_not_human_is_refused():
    """The example a human chose is the point of the layer (open-inputs section 3)."""
    with pytest.raises(ValueError, match="labels_provenance must be 'human'"):
        query_set_from_dict(_query_set([], provenance="generator"))


def test_a_query_set_missing_a_field_is_refused():
    for key in ("query_set", "labels_provenance", "term_list", "examples"):
        data = _query_set([])
        del data[key]
        with pytest.raises(ValueError, match=f"missing '{key}'"):
            query_set_from_dict(data)


def test_an_unknown_top_level_key_is_refused():
    """A key the format does not have is a typo, not a field quietly dropped."""
    data = _query_set([])
    data["documents"] = {}
    with pytest.raises(ValueError, match="unknown keys.*documents"):
        query_set_from_dict(data)


def test_an_example_is_validated():
    with pytest.raises(ValueError, match="missing 'query'"):
        example_from_dict({"expected_citations": []})
    with pytest.raises(ValueError, match="query must be a non-empty string"):
        example_from_dict({"query": "   ", "expected_citations": []})
    with pytest.raises(ValueError, match="missing 'expected_citations'"):
        example_from_dict({"query": "q"})
    with pytest.raises(ValueError, match="expected_citations must be a list"):
        example_from_dict({"query": "q", "expected_citations": "a document"})
    with pytest.raises(ValueError, match="unknown keys"):
        example_from_dict({"query": "q", "expected_citations": [], "rank": 1})


def test_a_citation_is_validated():
    with pytest.raises(ValueError, match="missing 'document'"):
        citation_from_dict({})
    with pytest.raises(ValueError, match="document must be a non-empty string"):
        citation_from_dict({"document": ""})
    with pytest.raises(ValueError, match="chunk must be a 0-based ordinal"):
        citation_from_dict({"document": "d.docx", "chunk": "0"})
    with pytest.raises(ValueError, match="chunk must be a 0-based ordinal"):
        citation_from_dict({"document": "d.docx", "chunk": True})  # bool is not an ordinal
    with pytest.raises(ValueError, match="chunk must be >= 0"):
        citation_from_dict({"document": "d.docx", "chunk": -1})
    with pytest.raises(ValueError, match="unknown keys"):
        citation_from_dict({"document": "d.docx", "view": "original"})
    with pytest.raises(ValueError, match="note must be a string"):
        citation_from_dict({"document": "d.docx", "note": 7})


def test_two_examples_may_not_ask_the_same_question():
    with pytest.raises(ValueError, match="both the query"):
        _set(
            [
                _example("what is the limit?", [_citation()]),
                _example("what is the limit?", [_citation(chunk=1)]),
            ]
        )


def test_two_query_sets_may_not_share_a_name(tmp_path):
    (tmp_path / "queries_a.json").write_text(json.dumps(_query_set([])), encoding="utf-8")
    (tmp_path / "queries_b.json").write_text(json.dumps(_query_set([])), encoding="utf-8")
    with pytest.raises(ValueError, match="both named 'test'"):
        iter_query_sets(tmp_path)


def test_the_loader_records_the_path_it_loaded(tmp_path):
    (tmp_path / "queries.json").write_text(
        json.dumps(_query_set([_example("q", [_citation()])])), encoding="utf-8"
    )
    loaded = iter_query_sets(tmp_path)
    assert set(loaded) == {"test"}
    assert loaded["test"].path == tmp_path / "queries.json"
    assert loaded["test"].examples[0].query == "q"


# ---------------------------------------------------------------- the scorer


def test_a_citation_matches_by_document_or_by_ordinal():
    query_set = _set(
        [
            _example("any chunk", [_citation()]),
            _example("the right ordinal", [_citation(chunk=1)]),
            _example("the wrong ordinal", [_citation(chunk=0)]),
        ]
    )
    ranked = {
        "any chunk": [("d.docx", "c1")],
        "the right ordinal": [("d.docx", "c1")],
        "the wrong ordinal": [("d.docx", "c1")],
    }
    report = score(query_set, ranked, {"d.docx": ("c0", "c1")})
    assert report.evaluated is True
    assert (report.expected, report.found) == (3, 2)
    assert report.recall == pytest.approx(2 / 3)
    assert report.result is False
    by_query = {row.query: row for row in report.rows}
    assert by_query["any chunk"].found == 1
    assert [c.label for c in by_query["the wrong ordinal"].missed] == ["d.docx#0"]


def test_an_ordinal_the_run_cannot_show_is_unresolved_and_missed():
    """The human named a chunk that does not exist: a miss, kept in the denominator."""
    query_set = _set([_example("q", [_citation(chunk=9)])])
    report = score(query_set, {"q": [("d.docx", "c0")]}, {"d.docx": ("c0", "c1")})
    (row,) = report.rows
    assert row.expected == 1 and row.found == 0
    assert [c.label for c in row.unresolved] == ["d.docx#9"]
    assert [c.label for c in row.missed] == ["d.docx#9"]
    assert report.result is False
    assert report.note == "failed: 1 expected citation(s) did not appear"


def test_a_document_not_on_disk_is_skipped_not_missed():
    """A real document's absence is not a recall failure (L2's precedent, rule 8)."""
    query_set = _set(
        [
            _example("all absent", [_citation(document="real/missing.docx")]),
            _example(
                "partly absent",
                [_citation(), _citation(document="real/missing.docx")],
            ),
        ]
    )
    ranked = {
        "all absent": [],
        "partly absent": [("d.docx", "c0")],
    }
    report = score(query_set, ranked, {"d.docx": ("c0",)})
    assert [row.query for row in report.rows] == ["partly absent"]
    (row,) = report.rows
    assert (row.expected, row.found, row.not_on_disk) == (1, 1, 1)
    assert report.skipped == (
        ("all absent", "the document is not on disk where the set names it"),
    )
    assert report.coverage()["examples_not_on_disk"] == 1
    assert report.coverage()["citations_not_on_disk"] == 1


def test_an_example_the_search_returned_nothing_for_is_a_miss():
    query_set = _set([_example("q", [_citation()])])
    report = score(query_set, {"q": []}, {"d.docx": ("c0",)})
    (row,) = report.rows
    assert (row.expected, row.found, row.returned) == (1, 0, 0)
    assert report.result is False


def test_an_empty_report_measures_nothing_and_never_passes():
    """No row, no expectation: result None is a report, never a silent pass."""
    report = RetrievalReport()
    assert report.evaluated is False
    assert report.recall is None and report.result is None
    assert report.metrics == {}
    assert report.note == "not evaluated: the query set has no example"
    assert report.coverage()["evaluated"] is False


def test_the_note_reports_the_gate_in_words():
    query_set = _set([_example("q", [_citation()])])
    found = score(query_set, {"q": [("d.docx", "c0")]}, {"d.docx": ("c0",)})
    assert found.result is True
    assert found.note == "evaluated: all 1 expected citations appear across 1 example(s)"
    missing = score(query_set, {"q": []}, {"d.docx": ("c0",)})
    assert missing.note.startswith("failed:")


def test_a_skipped_only_report_says_why_it_was_not_evaluated():
    query_set = _set([_example("q", [_citation(document="real/missing.docx")])])
    report = score(query_set, {"q": []}, {"d.docx": ("c0",)})
    assert report.rows == ()
    assert report.result is None
    assert report.note == "not evaluated: the document is not on disk where the set names it"


def test_merge_reports_unions_every_set():
    first = score(
        _set([_example("one", [_citation()])], name="a"),
        {"one": [("d.docx", "c0")]},
        {"d.docx": ("c0",)},
    )
    second = score(
        _set([_example("two", [_citation()])], name="b"),
        {"two": []},
        {"d.docx": ("c0",)},
    )
    merged = merge_reports([first, second])
    assert merged.query_sets == ("a", "b")
    assert merged.term_lists == ("terms/synthetic.json",)
    assert [row.query for row in merged.rows] == ["one", "two"]
    assert (merged.expected, merged.found) == (2, 1)
    # nothing to merge is still the not-evaluated report, not a pass
    empty = merge_reports([])
    assert empty.evaluated is False and empty.result is None


# ---------------------------------------------------------------- the harness


def test_evaluate_retrieval_is_none_on_the_committed_fixtures():
    """The committed example documents the format; with no set the layer is not evaluated."""
    assert harness.evaluate_retrieval(FIXTURES) is None


def _retrieval_case(tmp_path: Path, examples) -> Path:
    """A fixtures directory with one fixture document, one registry and one query set."""
    fixtures = tmp_path / "fixtures"
    (fixtures / "terms").mkdir(parents=True)
    (fixtures / "evals").mkdir()
    (fixtures / DOCUMENT).write_bytes((FIXTURES / DOCUMENT).read_bytes())
    (fixtures / "terms" / "synthetic.json").write_bytes(
        (FIXTURES / "terms" / "synthetic.example.json").read_bytes()
    )
    (fixtures / "evals" / "queries.json").write_text(
        json.dumps(_query_set(examples)), encoding="utf-8"
    )
    return fixtures


def test_evaluate_retrieval_scores_a_real_set_through_the_query_layer(tmp_path):
    """The candidates are ``query.search``'s own results, not a re-implementation."""
    fixtures = _retrieval_case(
        tmp_path,
        [
            _example("termination", [_citation(document=DOCUMENT)]),
            _example("waiver of termination", [_citation(document=DOCUMENT, chunk=0)]),
        ],
    )
    report = harness.evaluate_retrieval(fixtures)
    assert report is not None
    assert report.evaluated is True
    assert (report.expected, report.found) == (2, 2)
    assert report.recall == 1.0 and report.result is True
    assert report.metrics["citation_recall"] == 1.0
    assert report.coverage()["examples_scored"] == 2
    assert report.note.startswith("evaluated: all 2 expected citations appear")


def test_an_ordinal_the_store_cannot_show_fails_the_layer(tmp_path):
    """A green retrieval layer cannot be vacuous: a chunk that does not exist is a miss."""
    fixtures = _retrieval_case(
        tmp_path, [_example("termination", [_citation(document=DOCUMENT, chunk=99)])]
    )
    report = harness.evaluate_retrieval(fixtures)
    assert report is not None
    (row,) = report.rows
    assert [c.label for c in row.unresolved] == [f"{DOCUMENT}#99"]
    assert report.result is False
    assert report.metrics["citation_recall"] < 1.0


def test_a_query_set_whose_term_list_is_missing_skips_every_example(tmp_path):
    fixtures = tmp_path / "fixtures"
    (fixtures / "evals").mkdir(parents=True)
    (fixtures / DOCUMENT).write_bytes((FIXTURES / DOCUMENT).read_bytes())
    (fixtures / "evals" / "queries.json").write_text(
        json.dumps(
            _query_set(
                [_example("termination", [_citation(document=DOCUMENT)])],
                term_list="terms/gone.json",
            )
        ),
        encoding="utf-8",
    )
    report = harness.evaluate_retrieval(fixtures)
    assert report is not None
    assert report.rows == ()
    assert report.result is None
    assert report.skipped == (
        ("termination", "the set's term list is not on disk where it names it"),
    )
