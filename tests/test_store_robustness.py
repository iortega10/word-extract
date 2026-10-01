"""Turn 7 validation: what keeps a store honest as the code changes under it.

Three ways the first Turn 7 let a long-lived store go wrong:

* **Stale chunks.** The chunk key did not read the parser version, so after a walker change
  the parse and hits rebuilt while chunks cut from the *old* parse stood beside the new one.
* **Stale hits.** The hits key did not read the heading rules, but a hit names a node and a
  node's id embeds its kind (heading or paragraph), which the heading rules decide.
* **A crash instead of a rebuild.** An artifact written under an older schema is rejected by
  the codec (it will not decode with new fields silently defaulted), and the next ingest into
  that store failed on it. The schema has moved five times in this project.

Chunks and hits are *derived from the stored parse*, so the parse key is part of their keys;
an unreadable artifact is evicted and rebuilt rather than trusted or fatal.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from docextract_core import Collection, from_json, to_json

from wordextract import store as store_module
from wordextract.model import CacheStatus, ParseArtifact, TermGroup, View
from wordextract.store import Store, ingest
from wordextract.terms import TermRegistry

ROOT = Path(__file__).resolve().parents[1]
DOCUMENT = ROOT / "fixtures" / "samples" / "review_sample.docx"
REGISTRY = TermRegistry(
    groups=[TermGroup(canonical="named storm deductible"), TermGroup(canonical="aggregate")]
)


def _statuses(run) -> dict[str, str]:
    return {entry.artifact_id: entry.status.value for entry in run.artifact_cache}


def _populated(tmp_path: Path) -> Store:
    store = Store(tmp_path / "store")
    ingest(store, DOCUMENT, registry=REGISTRY)
    return store


def test_a_parser_change_invalidates_the_chunks_cut_from_the_old_parse(tmp_path, monkeypatch):
    store = _populated(tmp_path)
    monkeypatch.setattr(store_module, "SPEC_PARSER_VERSION", "parser-bumped")
    run = ingest(store, DOCUMENT, registry=REGISTRY)
    assert _statuses(run) == {
        "raw": "hit",
        "parse": "miss",
        "chunks": "miss",
        "hits": "miss",
        "terms": "hit",
    }


def test_a_heading_rule_change_invalidates_the_hits_whose_node_ids_it_changes(tmp_path, monkeypatch):
    store = _populated(tmp_path)
    monkeypatch.setattr(store_module, "HEADING_RULESET_VERSION", "heading-bumped")
    run = ingest(store, DOCUMENT, registry=REGISTRY)
    assert _statuses(run) == {
        "raw": "hit",
        "parse": "miss",
        "chunks": "miss",
        "hits": "miss",
        "terms": "hit",
    }


def test_a_new_term_list_still_re_parses_and_re_chunks_nothing(tmp_path):
    store = _populated(tmp_path)
    other = TermRegistry(groups=[TermGroup(canonical="aggregate"), TermGroup(canonical="limit")])
    run = ingest(store, DOCUMENT, registry=other)
    assert _statuses(run) == {
        "raw": "hit",
        "parse": "hit",
        "chunks": "hit",
        "hits": "miss",
        "terms": "miss",
    }


def _downgrade(directory: Path) -> int:
    """Rewrite every artifact in ``directory`` as if an older schema had written it."""
    count = 0
    for path in sorted(directory.glob("*.json")):
        if path.name == "index.json":
            continue
        blob = json.loads(path.read_text(encoding="utf-8"))
        blob["schema_version"] = "4"
        path.write_text(json.dumps(blob), encoding="utf-8")
        count += 1
    return count


@pytest.mark.parametrize("kind", ["parse", "chunks", "hits", "terms"])
def test_an_artifact_written_under_an_older_schema_is_rebuilt_not_fatal(tmp_path, kind):
    store = _populated(tmp_path)
    assert _downgrade(tmp_path / "store" / kind) == 1

    run = ingest(store, DOCUMENT, registry=REGISTRY)  # used to raise CodecError
    statuses = _statuses(run)
    assert statuses[kind] == "miss", statuses
    assert all(status == "hit" for name, status in statuses.items() if name != kind)

    # the rebuilt record is at the current schema, so the store is healed
    healed = ingest(store, DOCUMENT, registry=REGISTRY)
    assert set(_statuses(healed).values()) == {"hit"}
    for path in (tmp_path / "store" / kind).glob("*.json"):
        if path.name != "index.json":
            assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] != "4"


def test_every_artifact_older_than_the_schema_is_rebuilt_in_one_run(tmp_path):
    store = _populated(tmp_path)
    for kind in ("parse", "chunks", "hits", "terms"):
        _downgrade(tmp_path / "store" / kind)
    run = ingest(store, DOCUMENT, registry=REGISTRY)
    assert _statuses(run) == {
        "raw": "hit",
        "parse": "miss",
        "chunks": "miss",
        "hits": "miss",
        "terms": "miss",
    }
    assert set(_statuses(ingest(store, DOCUMENT, registry=REGISTRY)).values()) == {"hit"}


# --- the core's evict --------------------------------------------------------------------


def _collection(tmp_path: Path) -> Collection:
    return Collection(tmp_path / "c", ParseArtifact, id_of=lambda r: r.key, key_of=lambda r: r.key)


def test_evict_removes_the_file_and_the_index_entry(tmp_path):
    from wordextract.model import ParseResult

    collection = _collection(tmp_path)
    record = ParseArtifact(key="k1", parsed=ParseResult())
    collection.save(record)
    assert collection.find("k1") == record

    assert collection.evict("k1") is True
    assert collection.find("k1") is None
    assert not (tmp_path / "c" / "k1.json").exists()
    assert "k1" not in json.loads((tmp_path / "c" / "index.json").read_text(encoding="utf-8"))
    # and a fresh save after an evict writes the record again
    _, written = collection.save(record)
    assert written is True


def test_evicting_an_unknown_key_is_not_an_error(tmp_path):
    assert _collection(tmp_path).evict("nope") is False


def test_evict_clears_an_unindexed_file_left_by_a_crash(tmp_path):
    collection = _collection(tmp_path)
    (tmp_path / "c").mkdir(parents=True, exist_ok=True)
    (tmp_path / "c" / "k2.json").write_text("{}", encoding="utf-8")
    assert collection.evict("k2") is True
    assert not (tmp_path / "c" / "k2.json").exists()


def test_the_view_is_still_part_of_the_chunk_and_hit_keys(tmp_path):
    store = _populated(tmp_path)
    run = ingest(store, DOCUMENT, registry=REGISTRY, view=View.ORIGINAL)
    statuses = _statuses(run)
    assert statuses["parse"] == "hit"
    assert statuses["chunks"] == "miss" and statuses["hits"] == "miss"
    assert CacheStatus.MISS.value == "miss"
