"""Turn 4 + Turn 9: the eval harness -- the empty table's shape, and the real one's gates."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

from wordextract.evals import harness
from wordextract.evals import l1
from wordextract.evals import labels as labels_mod
from wordextract.evals.harness import build_metrics_table, gate_failures, run

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
LABELS_PATH = ROOT / "wordextract" / "evals" / "labels.py"
GAPS_DOC = ROOT / "docs" / "design" / "phase1-gaps.md"
OPEN_INPUTS = ROOT / "docs" / "design" / "open-inputs.md"


def test_metrics_table_shape_and_gates():
    table = build_metrics_table()
    assert set(table) == {
        "output_schema_version",
        "quality",
        "cost",
        "producer_verified_coverage",
        "labels",
    }
    layers = {layer["layer"]: layer for layer in table["quality"]}
    assert set(layers) == {"L1", "L2", "L3"}

    assert layers["L1"]["gated"] is True
    assert layers["L1"]["gate"] == {"metric": "exact_fact_accuracy", "op": "==", "value": 1.0}
    assert layers["L2"]["gated"] is True
    assert layers["L2"]["gate"] == {"metric": "recall", "op": "==", "value": 1.0}
    assert layers["L3"]["gated"] is False and layers["L3"]["gate"] is None

    for layer in table["quality"]:
        assert layer["rows"] == [] and layer["n"] == 0 and layer["result"] is None

    # cost lives outside quality and is never gated
    assert table["cost"]["gated"] is False
    assert table["cost"]["rows"] == []


def test_metrics_table_is_json_serializable_and_has_coverage():
    table = build_metrics_table(
        documents=4, producer_verified={"opc_reader": True, "parser": False}
    )
    json.dumps(table)
    cov = table["producer_verified_coverage"]
    # `documents` stays the fixtures/real count the per-path tally is reported against
    assert cov["documents"] == 4
    assert cov["producer_verified"] == {"opc_reader": True, "parser": False}
    assert (cov["verified_paths"], cov["total_paths"], cov["coverage"]) == (1, 2, 0.5)
    # a bare iterable names the verified paths
    assert build_metrics_table(producer_verified=["opc_reader"])["producer_verified_coverage"] == {
        "documents": 0,
        "producer_verified": {"opc_reader": True},
        "verified_paths": 1,
        "total_paths": 1,
        "coverage": 1.0,
    }
    # nothing tallied -> coverage is defined, not a ZeroDivisionError / NaN
    assert build_metrics_table()["producer_verified_coverage"]["coverage"] == 0.0


def test_empty_table_passes_and_a_failed_gate_does_not():
    table = build_metrics_table()
    assert gate_failures(table) == []
    table["quality"][0]["result"] = False
    assert gate_failures(table) == ["L1"]
    table["quality"][2]["result"] = False  # L3 is never gated
    assert gate_failures(table) == ["L1"]


def test_an_l1_that_compared_nothing_fails_the_gate_end_to_end(tmp_path):
    """L1's corpus is the committed fixtures, so comparing nothing is a broken run (a wrong
    path, missing fixtures), never an unmeasured layer: the table fails it, as the report
    does. (L2 is the layer that is legitimately "not evaluated": human labels do not exist.)"""
    report = l1.score_fixtures(tmp_path)  # no sidecar, so no fact was compared
    assert report.facts == () and report.result is False
    table = build_metrics_table(l1=report)
    (layer,) = [row for row in table["quality"] if row["layer"] == "L1"]
    assert layer["result"] is False
    assert layer["note"].startswith("FAILED") and "--fixtures" in layer["note"]
    assert layer["coverage"]["vacuous"] is True
    assert gate_failures(table) == ["L1"]


def test_an_l1_that_was_never_asked_to_score_is_not_a_failure():
    """No corpus given at all (the empty table) is the one case L1 is simply not run."""
    table = build_metrics_table()
    (layer,) = [row for row in table["quality"] if row["layer"] == "L1"]
    assert layer["result"] is None
    assert gate_failures(table) == []


def test_the_cli_exits_non_zero_on_an_empty_fixtures_directory(tmp_path, capsys):
    from wordextract.evals.__main__ import main

    empty = tmp_path / "empty"
    empty.mkdir()
    assert main(["--fixtures", str(empty)]) == 1
    captured = capsys.readouterr()
    assert "gated layers failed: L1" in captured.err


def test_the_cli_exits_non_zero_on_a_fixtures_path_that_does_not_exist(tmp_path, capsys):
    from wordextract.evals.__main__ import main

    assert main(["--fixtures", str(tmp_path / "no-such-directory")]) == 1
    assert "gated layers failed: L1" in capsys.readouterr().err


def test_run_scores_l1_green_and_leaves_l2_not_evaluated():
    table = run(FIXTURES)
    # every committed sidecar, root and fixtures/model alike, is counted
    assert table["labels"] == {"generator": 4, "spec": 24, "human": 0}

    layers = {layer["layer"]: layer for layer in table["quality"]}
    assert layers["L1"]["result"] is True
    assert layers["L1"]["metrics"]["exact_fact_accuracy"] == 1.0
    assert layers["L1"]["n"] > 0 and layers["L1"]["coverage"]["vacuous"] is False
    # no human must-find labels exist, so L2 measured nothing and is not a gate failure
    assert layers["L2"]["rows"] == [] and layers["L2"]["result"] is None
    assert layers["L2"]["metrics"] == {}
    assert layers["L2"]["note"].startswith("not evaluated")
    assert layers["L3"]["result"] is None
    assert gate_failures(table) == []

    # the roster is populated, and every path on it is False until a Word doc lands
    cov = table["producer_verified_coverage"]
    assert cov["producer_verified"] and not any(cov["producer_verified"].values())
    assert (cov["verified_paths"], cov["coverage"]) == (0, 0.0)
    # the real fixtures need a producer doc that does not exist yet
    assert cov["documents"] == 0


def test_run_is_json_serializable_and_l1_is_per_family():
    table = run(FIXTURES)
    json.dumps(table)
    rows = {row["family"]: row for row in table["quality"][0]["rows"]}
    # every labelled family is scored, tiling included: a green L1 must not be vacuous
    assert set(rows) == {
        "comments",
        "revisions",
        "sections",
        "paragraphs",
        "tables",
        "tiling",
    }
    assert all(row["result"] is True and row["failed"] == 0 for row in rows.values())
    assert rows["tiling"]["compared"] > 0


def test_labels_module_imports_no_implementation_code():
    """labels.py must be loadable with nothing from wordextract/docextract_core in play."""
    code = textwrap.dedent(
        f"""
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location("_labels_probe", r"{LABELS_PATH}")
        module = importlib.util.module_from_spec(spec)
        sys.modules["_labels_probe"] = module
        spec.loader.exec_module(module)
        leaked = sorted(m for m in sys.modules if m.split(".")[0] in {{"wordextract", "docextract_core"}})
        assert not leaked, leaked
        assert hasattr(module, "load_sidecar") and hasattr(module, "iter_sidecars")
        print("ok")
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "ok"


ROOT_SIDECARS = {
    "program_review_v2",
    "program_review_v3",
    "binder_summary",
    "edge_cases",
    "spec_threaded",
}
MODEL_SIDECARS = {
    "breaks_and_specials",
    "comment_in_deletion",
    "content_controls",
    "deleted_paragraph_mark",
    "empty_parts",
    "hyperlink_and_fields",
    "mixed_para_ids",
    "move",
    "nested_revisions",
    "renamed_comments_extended",
    "strict_namespaces",
    "text_box",
    "nested_field_in_instruction",
    "transparent_containers",
    "unanchored_comment",
    "threading_edge_cases",
    "outline_level_body_text",
    "unrecognized_container",
    "revision_missing_id",
    "style_numbering",
    "style_chain_cycle",
    "revision_id_collision",
    "unrecorded_revision_kinds",
}


def test_loads_every_committed_sidecar():
    # iter_sidecars globs every *.expected.json under fixtures/, keyed by stem
    sidecars = labels_mod.iter_sidecars(FIXTURES)
    assert set(sidecars) == ROOT_SIDECARS | MODEL_SIDECARS
    pr = sidecars["program_review_v3"]
    assert pr.labels_provenance == "generator"
    assert [c.author for c in pr.comments][:2] == ["R. Alvarez", "M. Chen"]
    assert pr.tables[0].merges == ((3, 0, 3, 2),)
    assert sidecars["spec_threaded"].labels_provenance == "spec"


def test_loads_model_sidecars_with_spans_and_annotations():
    sidecars = labels_mod.iter_sidecars(FIXTURES / "model")
    assert set(sidecars) == MODEL_SIDECARS
    assert {sc.labels_provenance for sc in sidecars.values()} == {"spec"}

    tb = sidecars["text_box"]
    assert tb.terminator == "\n"
    assert tb.span_rule.startswith("spans are maximal runs of equal ancestor stack")
    assert tb.known_gaps == ("textbox",)
    (para,) = tb.paragraphs
    assert (para.index, para.union, para.accepted, para.original, para.superseded) == (
        0,
        "Before box.After box.",
        "Before box.After box.",
        "Before box.After box.",
        "",
    )
    assert para.spans == (
        labels_mod.SpanLabel(start=0, end=21, stack=(), text="Before box.After box."),
    )
    # unmodelled keys are kept verbatim, never dropped
    assert tb.annotations["fragments"][0]["preferred"] == "mc:Choice"

    thread = sidecars["renamed_comments_extended"].comments
    assert [c.para_id for c in thread] == ["00000021", "00000022"]
    assert (thread[0].parent_id, thread[0].resolved) == (None, False)
    assert (thread[1].parent_id, thread[1].resolved) == ("00000021", True)
    assert thread[1].threading_status == "verified" and thread[1].text is None


def test_every_model_paragraph_sidecar_tiles_its_union():
    """Independent of the parser: the hand-typed spans cover the union exactly."""
    for name, sidecar in labels_mod.iter_sidecars(FIXTURES / "model").items():
        for para in sidecar.paragraphs:
            assert para.spans and para.spans[0].start == 0, (name, para.index)
            assert para.spans[-1].end == len(para.union), (name, para.index)
            for span, nxt in zip(para.spans, para.spans[1:]):
                assert span.end == nxt.start, (name, para.index)
            for span in para.spans:
                assert span.text == para.union[span.start : span.end], (name, para.index)


def test_duplicate_sidecar_stem_is_an_error(tmp_path):
    for sub in ("a", "b"):
        (tmp_path / sub).mkdir()
        (tmp_path / sub / "same.expected.json").write_text(
            json.dumps({"fixture": "same.docx", "labels_provenance": "human"}),
            encoding="utf-8",
        )
    with pytest.raises(ValueError, match="two sidecars for 'same'"):
        labels_mod.iter_sidecars(tmp_path)


def test_sidecar_validation_rejects_bad_input():
    with pytest.raises(ValueError):
        labels_mod.sidecar_from_dict({"fixture": "x", "labels_provenance": "made-up"})
    with pytest.raises(ValueError):
        labels_mod.sidecar_from_dict({"fixture": "x"})  # no provenance
    with pytest.raises(ValueError):
        labels_mod.sidecar_from_dict({"fixture": "x", "labels_provenance": "generator", "commentz": []})
    good = {
        "fixture": "x.docx",
        "labels_provenance": "generator",
        "comments": [],
        "revisions": [],
        "sections": [],
        "tables": [],
    }
    assert labels_mod.sidecar_from_dict(good).name == "x"


def test_cli_emits_the_metrics_table_as_pure_stdout_json():
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(ROOT), str(ROOT / "docextract-core"), os.environ.get("PYTHONPATH", "")]),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "wordextract.evals", "--fixtures", str(FIXTURES)],
        cwd=ROOT, capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    # strict: json.loads rejects a trailing diagnostic line, so this is the purity check
    table = json.loads(proc.stdout)
    assert [layer["layer"] for layer in table["quality"]] == ["L1", "L2", "L3"]
    assert table["quality"][0]["result"] is True
    assert table["labels"]["spec"] == 24
    # the "L2 was not evaluated" diagnostic is stderr's, never stdout's
    assert proc.stdout.strip().startswith("{")
    assert "not gated this run" in proc.stderr


def test_cli_writes_the_table_to_a_file_without_narrowing_stdout():
    """`--out` moves the table off stdout and leaves stdout empty, diagnostics and all."""
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(ROOT), str(ROOT / "docextract-core"), os.environ.get("PYTHONPATH", "")]),
    }
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "table.json"
        proc = subprocess.run(
            [sys.executable, "-m", "wordextract.evals", "--fixtures", str(FIXTURES), "--out", str(out)],
            cwd=ROOT, capture_output=True, text=True, env=env,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == ""
        table = json.loads(out.read_text(encoding="utf-8"))
        assert table["quality"][0]["result"] is True


def test_every_gap_id_a_sidecar_names_is_documented():
    """`known_gaps` is a closed vocabulary: an id a sidecar names is in the gap list.

    The walker's own ids are checked against ``WALKER_OWNED_GAPS`` in ``test_walker.py``;
    this is the corpus-level half -- the ids the fixtures declare but the walker does not
    report (a hazard a document is *about* rather than one its content triggers) are still
    entries here, so no sidecar can name an id no reviewer can look up.
    """
    documented = set(re.findall(r"`([a-z0-9_]+)`", GAPS_DOC.read_text(encoding="utf-8")))
    named = {
        gap for sidecar in labels_mod.iter_sidecars(FIXTURES).values() for gap in sidecar.known_gaps
    }
    assert named, "no sidecar names a known gap: this test would be vacuous"
    assert named <= documented, f"gap ids no entry documents: {sorted(named - documented)}"


def test_the_gap_list_documents_the_ids_it_says_the_walker_owns():
    """The id list and the prose list agree: no id is documented only in a test."""
    documented = set(re.findall(r"`([a-z0-9_]+)`", GAPS_DOC.read_text(encoding="utf-8")))
    assert set(harness._walker_gaps().values()) <= documented


def test_the_unverified_constructs_are_the_ones_open_inputs_records():
    """The roster reads its spellings off ``open-inputs.md`` section 2, not from memory."""
    section = OPEN_INPUTS.read_text(encoding="utf-8").split("## 2.")[1].split("## 3.")[0]
    listed = section.split("Specifically unverified:")[1].split(".")[0]
    records = [item.strip().replace("`", "") for item in listed.split(",")]
    assert records == list(harness.UNVERIFIED_CONSTRUCTS)


def test_the_producer_verified_roster_is_fixed_and_all_false_without_a_word_doc():
    """The roster is the stage paths, the unverified constructs and every walker gap."""
    names = harness.producer_verified_names()
    roster = harness.producer_verified_roster()
    assert sorted(roster) == list(names)
    assert set(roster.values()) == {False}  # no test is tagged @producer_verified yet
    assert set(harness.STAGE_PATHS) <= set(names)
    assert {f"open-inputs#2: {name}" for name in harness.UNVERIFIED_CONSTRUCTS} <= set(names)
    assert {f"gap: {gap}" for gap in harness._walker_gaps().values()} <= set(names)


def test_naming_a_path_outside_the_roster_is_an_error():
    """A tally can only grow a row a test stands behind, never a typo'd path."""
    assert harness.producer_verified_roster(["wordextract/walker.py"])["wordextract/walker.py"]
    with pytest.raises(KeyError):
        harness.producer_verified_roster(["wordextract/not_a_module.py"])


def test_the_cli_refuses_a_must_find_set_that_is_not_human_labelled_with_a_clean_error(tmp_path, capsys):
    import json
    import shutil

    from wordextract.evals.__main__ import main

    fixtures = tmp_path / "fx"
    shutil.copytree(FIXTURES, fixtures)
    (fixtures / "evals").mkdir(exist_ok=True)
    (fixtures / "evals" / "generated.json").write_text(
        json.dumps(
            {
                "label_set": "generated",
                "labels_provenance": "generator",
                "term_list": "terms/synthetic.example.json",
                "documents": {},
            }
        ),
        encoding="utf-8",
    )
    assert main(["--fixtures", str(fixtures)]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error:") and "human" in err and "Traceback" not in err
