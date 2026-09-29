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
    table = build_metrics_table(documents=4, producer_verified=1)
    json.dumps(table)
    cov = table["producer_verified_coverage"]
    assert cov == {"documents": 4, "producer_verified": 1, "coverage": 0.25}
    # no documents -> coverage defined, not a ZeroDivisionError / NaN
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
    assert table["labels"] == {"generator": 3, "spec": 1, "human": 0}
    assert all(layer["rows"] == [] for layer in table["quality"])
    # the real fixtures need a producer doc that does not exist yet
    assert table["producer_verified_coverage"]["documents"] == 0


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


def test_loads_every_committed_sidecar():
    sidecars = labels_mod.iter_sidecars(FIXTURES)
    assert set(sidecars) == {
        "program_review_v3",
        "binder_summary",
        "edge_cases",
        "spec_threaded",
    }
    pr = sidecars["program_review_v3"]
    assert pr.labels_provenance == "generator"
    assert [c.author for c in pr.comments][:2] == ["R. Alvarez", "M. Chen"]
    assert pr.tables[0].merges == ((3, 0, 3, 2),)
    assert sidecars["spec_threaded"].labels_provenance == "spec"


def test_sidecar_validation_rejects_bad_input():
    with pytest.raises(ValueError):
        labels_mod.sidecar_from_dict({"fixture": "x", "labels_provenance": "made-up"})
    with pytest.raises(ValueError):
        labels_mod.sidecar_from_dict({"fixture": "x"})  # no provenance
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
    assert table["labels"]["spec"] == 1
