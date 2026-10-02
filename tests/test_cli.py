"""Turn 9: ``ingest`` and ``hits`` -- canonical JSON on stdout, exit 2 on a bad run.

The output is the whole point: it is either a run record or a hit record, it is the same
bytes on every machine, and nothing else is printed to stdout, so a caller can pipe it
into a fixture. The subprocess test runs the documented invocation itself
(``python -m wordextract``), so the module entry point is not merely assumed to work.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from docextract_core import from_json, to_json

from wordextract.cli import main
from wordextract.model import RunRecord
from wordextract.store import Store

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
DOCUMENT = FIXTURES / "program_review_v3.docx"
EXAMPLE_REGISTRY = FIXTURES / "terms" / "synthetic.example.json"

#: ``program_review_v3.docx`` against the synthetic registry: see ``tests/test_store.py``.
FIXTURE_HITS = 3


def _argv(command: str, store: Path, *extra: str) -> list[str]:
    return [command, str(DOCUMENT), "--terms", str(EXAMPLE_REGISTRY), "--store", str(store), *extra]


def test_ingest_prints_the_run_record_and_stores_a_fresh_run(tmp_path, capsys):
    store = tmp_path / "store"
    assert main(_argv("ingest", store)) == 0
    printed = capsys.readouterr()
    assert printed.err == ""
    record = from_json(RunRecord, printed.out)
    assert [a.artifact_id for a in record.artifact_cache] == ["raw", "parse", "chunks", "hits", "terms"]
    assert {a.status.value for a in record.artifact_cache} == {"miss"}
    assert Store(store).load_run(record.run_id) is not None


def test_ingest_twice_reports_the_same_artifacts_as_hits(tmp_path, capsys):
    store = tmp_path / "store"
    main(_argv("ingest", store))
    first = json.loads(capsys.readouterr().out)["record"]
    assert main(_argv("ingest", store)) == 0
    second = json.loads(capsys.readouterr().out)["record"]
    assert {a["artifact_id"]: a["status"] for a in second["artifact_cache"]} == {
        kind: "hit" for kind in ("raw", "parse", "chunks", "hits", "terms")
    }
    assert first["hashed_inputs"] == second["hashed_inputs"]
    assert first["run_id"] != second["run_id"]


def test_ingest_accepts_a_run_id_and_a_view(tmp_path, capsys):
    store = tmp_path / "store"
    assert main(_argv("ingest", store, "--run-id", "fixed", "--view", "original")) == 0
    record = json.loads(capsys.readouterr().out)["record"]
    assert record["run_id"] == "fixed"
    assert record["hashed_inputs"]["view_id"] == "original"


def test_hits_prints_the_stored_hit_records(tmp_path, capsys):
    store = tmp_path / "store"
    assert main(_argv("hits", store)) == 0
    printed = capsys.readouterr()
    assert printed.err == ""
    rows = json.loads(printed.out)
    assert len(rows) == FIXTURE_HITS
    assert [row["group"] for row in rows].count("termination") == 2
    # one JSON object per hit, with every field of the record
    assert set(rows[0]) == {
        "group",
        "present_in",
        "spans",
        "view_spans",
        "node_id",
        "location",
        "match_type",
        "move_group_id",
        "context_node_id",
    }
    assert set(rows[0]["present_in"]) == {span["view"] for span in rows[0]["view_spans"]}
    assert set(rows[0]["present_in"]) <= {"accepted", "original", "superseded"}
    # `hits` ingests if it has to, and the run is recorded like any other
    assert len(Store(store).run_ids()) == 1


def test_hits_output_is_the_stored_artifact_encoded(tmp_path, capsys):
    store = tmp_path / "store"
    main(_argv("hits", store))
    printed = capsys.readouterr().out
    (run_id,) = Store(store).run_ids()
    record = Store(store).load_run(run_id)
    key = {a.artifact_id: a.recomputed_key for a in record.artifact_cache}["hits"]
    assert json.loads(printed) == json.loads(to_json(Store(store).hits.find(key).hits))["record"]


def test_a_missing_document_or_term_list_exits_two_with_no_stdout(tmp_path, capsys):
    store = tmp_path / "store"
    assert main(["ingest", str(tmp_path / "nope.docx"), "--terms", str(EXAMPLE_REGISTRY)]) == 2
    assert main(["ingest", str(DOCUMENT), "--terms", str(tmp_path / "nope.json")]) == 2
    printed = capsys.readouterr()
    assert printed.out == ""
    assert "no document at" in printed.err and "no term list at" in printed.err
    assert not store.exists()


def test_the_command_and_its_options_are_required():
    with pytest.raises(SystemExit) as missing_command:
        main([])
    assert missing_command.value.code == 2
    with pytest.raises(SystemExit) as bad_view:
        main(["ingest", str(DOCUMENT), "--terms", str(EXAMPLE_REGISTRY), "--view", "union"])
    assert bad_view.value.code == 2


def test_the_documented_invocation_runs_as_a_module(tmp_path):
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(ROOT), str(ROOT / "docextract-core"), os.environ.get("PYTHONPATH", "")]
        ),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "wordextract", *_argv("ingest", tmp_path / "store")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    record = from_json(RunRecord, proc.stdout)
    assert (tmp_path / "store" / "runs" / f"{record.run_id}.json").is_file()


def test_ingest_writes_into_the_default_store_when_told_nothing(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["ingest", str(DOCUMENT), "--terms", str(EXAMPLE_REGISTRY)]) == 0
    capsys.readouterr()
    assert (tmp_path / ".wordextract" / "parse").is_dir()
