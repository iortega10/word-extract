"""flip-one-input: every member of HashedInputs participates in the key (D10)."""
from __future__ import annotations

from dataclasses import fields, replace

from wordextract import HashedInputs, compute_key

BASE = HashedInputs(
    source_content_hash="src",
    spec_parser_version="1",
    textmodel_version="1",
    view_id="accepted",
    heading_ruleset_version="1",
    chunker_version="1",
    chunker_params_hash="cp",
    term_list_hash="tl",
    matcher_version="1",
    summarizer_version="1",
    model_id="m",
    model_params_hash="mp",
    prompt_hash="pp",
    output_schema_version="1",
)


def test_identical_inputs_give_identical_key():
    assert compute_key(BASE) == compute_key(replace(BASE))


def test_changing_any_single_input_changes_the_key():
    base_key = compute_key(BASE)
    for spec in fields(BASE):
        flipped = replace(BASE, **{spec.name: f"{getattr(BASE, spec.name)}!"})
        assert compute_key(flipped) != base_key, spec.name


def test_view_id_participates_in_the_key():
    assert compute_key(BASE) != compute_key(replace(BASE, view_id="original"))
