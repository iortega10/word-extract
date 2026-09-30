"""Turn 4: eval harness skeleton -- empty metrics table, labels loaded independently."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from wordextract.evals import labels as labels_mod
from wordextract.evals.harness import build_metrics_table, gate_failures, run

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
LABELS_PATH = ROOT / "wordextract" / "evals" / "labels.py"


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


def test_run_reads_labels_but_reports_no_metrics():
    table = run(FIXTURES)
    # every committed sidecar, root and fixtures/model alike, is counted
    assert table["labels"] == {"generator": 3, "spec": 22, "human": 0}
    assert all(layer["rows"] == [] for layer in table["quality"])
    # no test is tagged @producer_verified yet, so the per-path tally is empty
    cov = table["producer_verified_coverage"]
    assert cov["producer_verified"] == {} and cov["coverage"] == 0.0
    # the real fixtures need a producer doc that does not exist yet
    assert cov["documents"] == 0


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


ROOT_SIDECARS = {"program_review_v3", "binder_summary", "edge_cases", "spec_threaded"}
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


def test_cli_emits_empty_metrics_table():
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(ROOT), str(ROOT / "docextract-core"), os.environ.get("PYTHONPATH", "")]),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "wordextract.evals", "--fixtures", str(FIXTURES)],
        cwd=ROOT, capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    table = json.loads(proc.stdout)
    assert [layer["layer"] for layer in table["quality"]] == ["L1", "L2", "L3"]
    assert all(layer["rows"] == [] for layer in table["quality"])
    assert table["labels"]["spec"] == 22
