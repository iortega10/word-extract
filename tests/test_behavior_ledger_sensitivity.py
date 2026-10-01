"""Turn 9 validation: the behavior ledger can fail, and for the right reasons.

A guard that has never been seen failing proves nothing. Each test changes one behavior in
place, without bumping any version, and requires ``behavior_ledger.check()`` to name the
component. The first ledger could not see a hyphen-normalization or stemming change (the
fixture corpus yields only a handful of hits against the synthetic registry) or a packing
change on documents too small to reach the default size cap, so the matcher is also
fingerprinted on a fixed probe set and the chunker under stress parameters.

Every mutation patches the name the code looks up *at call time*; a patch to a reference the
code captured at import would leave behavior unchanged and make the test pass for nothing.
"""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import behavior_ledger as ledger  # noqa: E402

from wordextract import chunker, headings, model, render, stem, store, terms, views  # noqa: E402


def _components(problems) -> set[str]:
    problems = problems if isinstance(problems, (list, tuple)) else [problems]
    return {str(problem).split(":", 1)[0] for problem in problems}


def test_unmodified_code_matches_its_ledger():
    assert ledger.check() == []


def test_a_heading_rule_change_without_a_bump_is_caught(monkeypatch):
    monkeypatch.setattr(headings, "BOLD_MAX_CHARS", 3)
    assert "parse" in _components(ledger.check())


def test_a_view_mask_change_without_a_bump_is_caught(monkeypatch):
    monkeypatch.setattr(views, "keeps", lambda stack, view: True)
    assert "views" in _components(ledger.check())


def test_a_hyphen_normalization_change_is_caught_by_the_matcher_probe(monkeypatch):
    monkeypatch.setattr(terms, "HYPHENS", frozenset())
    assert "matcher" in _components(ledger.check())


def test_a_stemmer_output_change_is_caught_by_the_matcher_probe(monkeypatch):
    monkeypatch.setitem(stem.STEMMERS, "porter", lambda word: word)
    assert "matcher" in _components(ledger.check())


def test_a_chunk_packing_change_is_caught(monkeypatch):
    monkeypatch.setattr(chunker, "_join", lambda blocks: "|".join(block.text for block in blocks))
    assert "chunks" in _components(ledger.check())


def test_a_list_run_boundary_change_is_caught_under_stress_parameters(monkeypatch):
    monkeypatch.setattr(chunker, "_segments", lambda blocks, length: [list(blocks)])
    assert "chunks" in _components(ledger.check())


def test_a_merge_up_rule_change_is_caught_under_stress_parameters(monkeypatch):
    monkeypatch.setattr(chunker._Groups, "_dissolves", lambda self, group, params: False)
    assert "chunks" in _components(ledger.check())


def test_a_new_contract_record_without_a_schema_bump_is_caught(monkeypatch):
    monkeypatch.setattr(
        model, "ExtraRecord", dataclasses.make_dataclass("ExtraRecord", [("x", int)]), raising=False
    )
    assert "contracts" in _components(ledger.check())


def test_a_rendering_format_change_without_a_bump_is_caught(monkeypatch):
    """The rendering is a versioned prompt's contract, and a summary's key is over its bytes:
    change either and the fingerprint must move."""
    monkeypatch.setattr(render, "_JOIN", "|")
    assert {"render", "summary_key"} <= _components(ledger.check())


def test_a_key_member_change_without_a_bump_is_caught(monkeypatch):
    monkeypatch.setattr(store, "SUMMARY_VIEW_ID", "accepted")
    assert "summary_key" in _components(ledger.check())


def test_the_probe_exercises_what_the_corpus_cannot():
    """The corpus alone yields almost no hits; the probe is what makes the matcher
    fingerprint sensitive to normalization, hyphen readings, stems and overlap."""
    index = terms.compile_registry(ledger.PROBE_REGISTRY)
    found = [match for text in ledger.PROBE_TEXTS for match in terms.match_text(index, text)]
    kinds = {match.match_type.value for match in found}
    assert kinds == {"exact", "synonym", "stem"}
    groups = {match.group for match in found}
    assert {"hold harmless", "non-compliance", "subrogation", "exclusion", "insured"} <= groups
    assert len(found) >= 20


def test_stress_parameters_force_the_paths_the_default_never_reaches():
    from wordextract import opc
    from wordextract.store import body_part_id
    from wordextract.walker import walk_document

    document = ROOT / "fixtures" / "program_review_v3.docx"
    parsed = walk_document(opc.Package(document))
    part = body_part_id(parsed)
    default = chunker.chunk(parsed, part, params=chunker.DEFAULT_PARAMS)
    stressed = chunker.chunk(parsed, part, params=ledger.STRESS_PARAMS)
    assert len(stressed) > len(default)  # splits happen only under stress
