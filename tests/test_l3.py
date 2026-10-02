"""Turn 7: L3 — the rubric loader, the sample, and every state that is not "graded".

L3 is reported, never gated: a summary's faithfulness is judged by a person, so these
tests pin what keeps the layer honest instead of green. The example grades file documents
the format and never scores (`iter_grades` skips it by name); an ungraded chunk stays
ungraded, a grade written under another prompt or model is stale, and a grade for a chunk
outside the sample is unmatched — none of them become a pass, and `result` is None at
every turn (phase2-build-spec Turn 7).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from wordextract.evals.l3 import (
    SAMPLE_SIZE,
    Sample,
    build_report,
    criterion_from_dict,
    gradeset_from_dict,
    grade_from_dict,
    iter_grades,
    select,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
EVALS = FIXTURES / "evals"

#: The pass the sample and the grades were recorded under (canned client identity).
PROMPT = "sha256:aaa"
MODEL = "canned"


def _criterion(identifier="faithful", question="Does the summary say what the chunk says?"):
    return {"id": identifier, "question": question, "grades": ["yes", "no"]}


def _grade(
    document="d.docx",
    chunk_id="c0",
    prompt_hash=PROMPT,
    model=MODEL,
    criteria=None,
    note=None,
):
    entry = {
        "document": document,
        "chunk_id": chunk_id,
        "prompt_hash": prompt_hash,
        "model": model,
        "criteria": criteria if criteria is not None else {"faithful": "yes", "complete": "yes"},
    }
    if note is not None:
        entry["note"] = note
    return entry


def _gradeset(grades=(), rubric=None, name="test", provenance="human"):
    return {
        "gradeset": name,
        "labels_provenance": provenance,
        "rubric": {"criteria": list(rubric) if rubric is not None else [_criterion("faithful"), _criterion("complete")]},
        "grades": list(grades),
    }


def _set(**kwargs):
    return gradeset_from_dict(_gradeset(**kwargs))


def _sample(chunk_id, document="d.docx", prompt_hash=PROMPT, model=MODEL):
    return Sample(document=document, chunk_id=chunk_id, prompt_hash=prompt_hash, model=model)


# ---------------------------------------------------------------- the loader


def test_the_committed_example_documents_the_rubric_and_never_scores():
    """``l3_grades.example.json`` is the only committed grades file, and it never scores."""
    assert iter_grades(EVALS) == {}
    documented = gradeset_from_dict(
        json.loads((EVALS / "l3_grades.example.json").read_text(encoding="utf-8"))
    )
    # documented for whoever copies it -- well-formed, provenance human, criteria named
    assert documented.gradeset == "l3_grades.example"
    assert documented.labels_provenance == "human"
    assert [criterion.id for criterion in documented.rubric] == ["faithful", "complete"]


def test_a_gradeset_that_is_not_human_is_refused():
    """A model grading a model is the circularity L3 exists to see through."""
    with pytest.raises(ValueError, match="labels_provenance must be 'human'"):
        gradeset_from_dict(_gradeset(provenance="canned"))


def test_a_gradeset_missing_a_field_is_refused():
    for key in ("gradeset", "labels_provenance", "rubric", "grades"):
        data = _gradeset()
        del data[key]
        with pytest.raises(ValueError, match=f"missing '{key}'"):
            gradeset_from_dict(data)


def test_an_unknown_top_level_key_is_refused():
    data = _gradeset()
    data["model"] = MODEL
    with pytest.raises(ValueError, match="unknown keys.*model"):
        gradeset_from_dict(data)


def test_a_rubric_is_validated():
    with pytest.raises(ValueError, match="rubric must be an object"):
        gradeset_from_dict({**_gradeset(), "rubric": "faithful, complete"})
    with pytest.raises(ValueError, match="unknown keys.*weight"):
        gradeset_from_dict({**_gradeset(), "rubric": {"criteria": [_criterion()], "weight": 1}})
    with pytest.raises(ValueError, match="missing 'criteria'"):
        gradeset_from_dict({**_gradeset(), "rubric": {}})
    with pytest.raises(ValueError, match="criteria must be a list"):
        gradeset_from_dict({**_gradeset(), "rubric": {"criteria": {"id": "faithful"}}})
    with pytest.raises(ValueError, match="no criterion grades nothing"):
        gradeset_from_dict({**_gradeset(), "rubric": {"criteria": []}})


def test_a_criterion_is_validated():
    with pytest.raises(ValueError, match="must be an object"):
        criterion_from_dict("faithful")
    with pytest.raises(ValueError, match="missing 'question'"):
        criterion_from_dict({"id": "faithful", "grades": ["yes"]})
    with pytest.raises(ValueError, match="id must be a non-empty string"):
        criterion_from_dict({"id": "", "question": "q", "grades": ["yes"]})
    with pytest.raises(ValueError, match="needs a non-empty question"):
        criterion_from_dict({"id": "faithful", "question": " ", "grades": ["yes"]})
    with pytest.raises(ValueError, match="non-empty list of grade labels"):
        criterion_from_dict({"id": "faithful", "question": "q", "grades": []})
    with pytest.raises(ValueError, match="non-empty list of grade labels"):
        criterion_from_dict({"id": "faithful", "question": "q", "grades": [3]})
    with pytest.raises(ValueError, match="unknown keys"):
        criterion_from_dict({"id": "faithful", "question": "q", "grades": ["yes"], "weight": 2})


def test_two_rubric_criteria_may_not_share_an_id():
    with pytest.raises(ValueError, match="both named 'faithful'"):
        gradeset_from_dict(_gradeset(rubric=[_criterion(), _criterion()]))


def test_a_grade_is_validated_against_the_rubric():
    rubric = {"faithful": _set().rubric[0]}
    with pytest.raises(ValueError, match="names criterion 'complete'"):
        grade_from_dict(_grade(criteria={"complete": "yes"}), rubric)
    with pytest.raises(ValueError, match="graded 'maybe'"):
        grade_from_dict(_grade(criteria={"faithful": "maybe"}), rubric)
    with pytest.raises(ValueError, match="missing 'criteria'"):
        grade_from_dict({k: v for k, v in _grade().items() if k != "criteria"}, rubric)
    with pytest.raises(ValueError, match="chunk_id must be a non-empty string"):
        grade_from_dict(_grade(chunk_id=""), rubric)
    with pytest.raises(ValueError, match="criteria must be an object"):
        grade_from_dict(_grade(criteria="yes"), rubric)
    with pytest.raises(ValueError, match="note must be a string"):
        grade_from_dict(_grade(criteria={"faithful": "yes"}, note=1), rubric)
    with pytest.raises(ValueError, match="unknown keys"):
        data = _grade(criteria={"faithful": "yes"})
        data["grader"] = "someone"
        grade_from_dict(data, rubric)


def test_two_grades_may_not_address_the_same_chunk():
    with pytest.raises(ValueError, match="one chunk, one entry"):
        _set(grades=[_grade(), _grade(note="a second opinion")])


def test_two_grades_files_may_not_share_a_name(tmp_path):
    (tmp_path / "l3_grades_a.json").write_text(json.dumps(_gradeset()), encoding="utf-8")
    (tmp_path / "l3_grades_b.json").write_text(json.dumps(_gradeset()), encoding="utf-8")
    with pytest.raises(ValueError, match="both named 'test'"):
        iter_grades(tmp_path)


def test_the_loader_records_the_path_it_loaded(tmp_path):
    (tmp_path / "l3_grades.json").write_text(json.dumps(_gradeset()), encoding="utf-8")
    loaded = iter_grades(tmp_path)
    assert set(loaded) == {"test"}
    assert loaded["test"].path == tmp_path / "l3_grades.json"


# ---------------------------------------------------------------- the sample


def test_select_takes_the_pool_in_order_when_it_is_short():
    pool = [_sample("c0"), _sample("c1"), _sample("c2")]
    assert select(pool) == tuple(pool)
    assert select(pool, n=3) == tuple(pool)


def test_select_spreads_the_sample_evenly_and_deterministically():
    pool = [_sample(f"c{i:02d}") for i in range(50)]
    picked = select(pool, n=SAMPLE_SIZE)
    assert len(picked) == SAMPLE_SIZE
    # the same run tomorrow grades the same chunks: a regrade must be comparable
    assert picked == select(pool, n=SAMPLE_SIZE)
    # endpoints stay in the sample, and pool order is preserved (no reshuffle)
    assert picked[0] is pool[0] and picked[-1] is pool[-1]
    positions = [pool.index(slot) for slot in picked]
    assert positions == sorted(positions)


def test_select_degrades_to_the_edges():
    pool = [_sample("c0"), _sample("c1")]
    assert select(pool, n=1) == (pool[0],)
    assert select(pool, n=0) == ()
    assert select([], n=SAMPLE_SIZE) == ()


# ---------------------------------------------------------------- the report


def test_no_sample_means_not_graded_and_no_metrics():
    report = build_report([], {})
    assert report.sample == ()
    assert report.metrics == {}
    assert report.result is None
    assert report.note.startswith("not graded: no summary was sampled")
    assert report.coverage()["evaluated"] is False


def test_candidates_without_grades_are_ungraded_not_faithful():
    candidates = [_sample("c0"), _sample("c1"), _sample("c2")]
    report = build_report(candidates, {})
    assert [row.status for row in report.rows] == ["ungraded", "ungraded", "ungraded"]
    assert report.evaluated is False and report.result is None
    assert report.note == (
        "not graded: no human L3 grades exist "
        "(fixtures/evals/l3_grades.example.json documents the rubric)"
    )
    assert report.metrics == {"samples": 3, "graded": 0, "partial": 0, "stale": 0, "ungraded": 3}


def test_every_grade_state_lands_on_its_row():
    candidates = [_sample("c0"), _sample("c1"), _sample("c2"), _sample("c3")]
    gradeset = _set(
        grades=[
            _grade(chunk_id="c0"),  # fresh, every criterion answered
            _grade(chunk_id="c1", criteria={"faithful": "yes"}),  # one criterion missing
            _grade(chunk_id="c2", prompt_hash="sha256:old"),  # graded under another prompt
            # c3: nobody graded it
        ]
    )
    report = build_report(candidates, {"test": gradeset})
    assert [row.status for row in report.rows] == ["graded", "partial", "stale", "ungraded"]
    assert (report.graded, report.partial, report.stale, report.ungraded) == (1, 1, 1, 1)
    assert report.evaluated is True
    # reported, never gated: even a fully graded sample has no result to pass
    assert report.result is None
    assert report.note == "graded: 1 of 4 sampled chunks complete; 1 partial, 1 stale, 1 ungraded"
    assert report.metrics == {"samples": 4, "graded": 1, "partial": 1, "stale": 1, "ungraded": 1}
    by_chunk = {row.chunk_id: row for row in report.rows}
    assert by_chunk["c1"].missing_criteria == ("complete",)
    assert by_chunk["c2"].recorded == {"prompt_hash": "sha256:old", "model": MODEL}
    assert by_chunk["c3"].gradeset is None
    assert set(report.coverage()) == {
        "grade_sets",
        "rubric",
        "samples",
        "graded",
        "unmatched",
        "shadowed",
        "evaluated",
    }
    assert report.coverage()["rubric"] == ["faithful", "complete"]
    assert set(by_chunk["c0"].as_dict()) == {
        "document",
        "chunk_id",
        "prompt_hash",
        "model",
        "status",
        "gradeset",
        "criteria",
        "missing_criteria",
        "recorded",
    }


def test_a_fully_stale_sample_is_reported_and_never_evaluated():
    """A grades file carried over from another prompt grades nothing under this one."""
    candidates = [_sample("c0")]
    stale = _set(grades=[_grade(prompt_hash="sha256:elsewhere", model="real")])
    report = build_report(candidates, {"test": stale})
    assert [row.status for row in report.rows] == ["stale"]
    assert report.evaluated is False and report.result is None
    assert report.note == "not graded: the sample has no fresh grade (1 stale, 0 ungraded)"


def test_a_grade_for_a_chunk_outside_the_sample_is_unmatched():
    candidates = [_sample("c0")]
    gradeset = _set(grades=[_grade(), _grade(document="other.docx", chunk_id="c9")])
    report = build_report(candidates, {"test": gradeset})
    assert [row.status for row in report.rows] == ["graded"]
    assert report.unmatched == (
        {"gradeset": "test", "document": "other.docx", "chunk_id": "c9"},
    )
    assert report.coverage()["unmatched"] == 1


def test_the_first_name_ordered_gradeset_governs_and_shadowing_is_counted():
    """Two files grading one chunk: the name-ordered first wins, the other is counted."""
    candidates = [_sample("c0")]
    alpha = _set(name="a-early", rubric=[_criterion("alpha")], grades=[_grade(criteria={"alpha": "yes"})])
    zeta = _set(name="z-late", rubric=[_criterion("zeta")], grades=[_grade(criteria={"zeta": "yes"})])
    report = build_report(candidates, {"z-late": zeta, "a-early": alpha})
    assert report.gradesets == ("a-early", "z-late")
    assert [criterion.id for criterion in report.rubric] == ["alpha"]
    (row,) = report.rows
    assert row.gradeset == "a-early"
    assert report.shadowed == 1
    assert report.coverage()["shadowed"] == 1


def test_the_sample_comes_from_whatever_pass_ran():
    """The pool is the fixture summaries in document order; n is the spec's band."""
    assert SAMPLE_SIZE == 12
    candidates = [_sample(f"c{i}") for i in range(20)]
    report = build_report(candidates, {})
    assert len(report.sample) == 12
    assert report.sample == select(candidates, n=12)
