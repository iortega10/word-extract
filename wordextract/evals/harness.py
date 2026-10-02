"""Turn 9: the eval harness -- the metrics table over the real parser, and its gates.

Four quality layers (three of them gated), plus a cost section that is never folded into a
gate:

* **L1** (``exact_fact_accuracy == 1.0``): every sidecar fact checked against a fresh parse
  of its fixture, with a tiling/coverage report so a green L1 cannot be vacuous
  (:mod:`wordextract.evals.l1`);
* **L2** (``recall == 1.0``, and every false positive classified): the human must-find list,
  run through the pipeline and scored against the hits the run *stored*
  (:mod:`wordextract.evals.must_find`). With no human labels it reports **not evaluated**
  and Phase 1 closes conditionally (open-inputs section 3);
* **L3**: summary faithfulness over a sampled rubric -- human-graded, reported, never gated
  (:mod:`wordextract.evals.l3`). The sample comes from the cost pass below;
* **retrieval** (``citation_recall == 1.0``): a human's example query set scored over
  :func:`wordextract.query.search`'s own results (:mod:`wordextract.evals.retrieval`).
  With no real set -- the committed ``queries.example.json`` documents the format and never
  scores -- it reports **not evaluated**, result ``None``.

The **cost** section carries one row per top-level fixture (summary calls, roll-up calls,
cache hits, tokens when the client reports them, ``cost_usd`` None without a price table)
plus the v2/v3 pair's cache row: only the sidecar's changed chunks re-summarize. It runs the
real summarizer with the canned client, so calls and hits are true store behaviour.

:func:`build_metrics_table` is **pure**: it takes the layer reports a caller already has and
lays them out, so nothing here parses a document or reads a store. :func:`run` is the
caller that does: it scores the fixtures, runs the human label sets through
:mod:`wordextract.pipeline`, and hands the reports in.

``producer_verified_coverage`` is the third report, and it is a *roster*: the code paths a
Word-produced document would exercise, the constructs ``docs/design/open-inputs.md`` section
2 records as unverified, and every ``GAP_*`` id the walker can report. All ``False`` until a
Microsoft-Word-produced document lands in ``fixtures/real`` with a test tagged
``@producer_verified`` -- so the coverage reads 0.0 because the work is outstanding, never
because the roster is empty (an empty roster would read 0.0 too, and mean nothing).
"""
from __future__ import annotations

import json
import operator
import tempfile
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from ..model import ParseResult, TermHit
from .. import pipeline, walker
from ..llm import CANNED, CannedClient
from ..query import search
from ..store import Store
from ..summarize import summarize
from ..terms import load_registry_text, term_list_hash
from ..versions import OUTPUT_SCHEMA_VERSION
from .l1 import L1Report, score_fixtures
from .l3 import L3Report, Sample, build_report as build_l3_report, iter_grades
from .labels import iter_sidecars
from .must_find import L2Report, iter_must_find, merge, score
from .retrieval import RetrievalReport, iter_query_sets, merge_reports, score as score_retrieval

# Gate declarations, by layer. A layer with a non-empty `result` of False is what makes the
# CLI exit non-zero; a metric the layer did not measure leaves `result` None (L2 with no
# human labels, retrieval with no human query set, L1 before the rows existed).
LAYERS = {
    "L1": {
        "name": "parse/perception",
        "gated": True,
        "gate": {"metric": "exact_fact_accuracy", "op": "==", "value": 1.0},
    },
    "L2": {
        "name": "terms",
        "gated": True,
        "gate": {"metric": "recall", "op": "==", "value": 1.0},
    },
    "L3": {
        "name": "summaries",
        "gated": False,
        "gate": None,
    },
    "retrieval": {
        "name": "example retrieval",
        "gated": True,
        "gate": {"metric": "citation_recall", "op": "==", "value": 1.0},
    },
}

_OPS = {"==": operator.eq, "!=": operator.ne, ">=": operator.ge, "<=": operator.le}

#: The code paths a Word-produced document would exercise, in pipeline order. Named by
#: module, because a producer-verified document is evidence about the *stage* that read it.
STAGE_PATHS = (
    "wordextract/opc.py",
    "wordextract/walker.py",
    "wordextract/views.py",
    "wordextract/chunker.py",
    "wordextract/terms.py",
    "wordextract/store.py",
    "wordextract/pipeline.py",
    "wordextract/cli.py",
    # Phase 2: summaries, pending changes, retrieval and the query layer. All False today --
    # no Word-produced document has been through the summarizer or the query API, so the
    # roster names them the day one is.
    "wordextract/summarize.py",
    "wordextract/pending.py",
    "wordextract/rank.py",
    "wordextract/query.py",
    "wordextract/mcp/",
)

#: The constructs `docs/design/open-inputs.md` section 2 records as **unverified** until a
#: Word-produced document lands. Spelled as that list spells them, so the two can be read
#: against each other (a test does).
UNVERIFIED_CONSTRUCTS = (
    "w14:paraId identity",
    "commentsExtended threading",
    "numbering label counting",
    "heading/numbering conventions",
    "strict namespaces",
    "deleted paragraph marks",
)


def _walker_gaps() -> dict[str, str]:
    """Every known-gap id the walker can report, read off the module's own constants.

    Read rather than retyped: a new ``GAP_*`` is then in the roster the moment it is in the
    code, instead of the day someone remembers to add it here.
    """
    return {
        name: value
        for name, value in vars(walker).items()
        if name.startswith("GAP_") and isinstance(value, str)
    }


def producer_verified_names() -> tuple[str, ...]:
    """Every name in the roster, sorted: stage paths, unverified constructs, walker gaps."""
    return tuple(
        sorted(
            [
                *STAGE_PATHS,
                *(f"open-inputs#2: {name}" for name in UNVERIFIED_CONSTRUCTS),
                *(f"gap: {gap}" for gap in _walker_gaps().values()),
            ]
        )
    )


def producer_verified_roster(verified: Iterable[str] = ()) -> dict[str, bool]:
    """The roster, all ``False`` except the paths named as verified.

    A path is verified only by being named here from a test tagged ``@producer_verified`` --
    there is no way to set a flag from inside this function, and naming a path that is not
    in the roster is an error rather than a silent entry, so the tally can never grow a row
    that no test stands behind.
    """
    roster = {name: False for name in producer_verified_names()}
    for name in verified:
        if name not in roster:
            raise KeyError(f"not a path in the producer-verified roster: {name!r}")
        roster[name] = True
    return roster


@contextmanager
def _store_dir(store_root: str | Path | None):
    """The store to run an eval against: the caller's, or a temporary one.

    An eval that scored against the default ``.wordextract`` would leave a store in the
    working tree of whoever ran it, and a *stale* store there could agree with itself.
    """
    if store_root is not None:
        yield Path(store_root)
        return
    with tempfile.TemporaryDirectory(prefix="wordextract-eval-") as tmp:
        yield Path(tmp)


def hit_texts(parsed: ParseResult, hits: Iterable[TermHit]) -> list[str]:
    """The text each hit covers, read from the parse's own union streams.

    A hit's ``spans`` are union addresses that already skip what the view elided, so
    concatenating them gives exactly the text the matcher matched -- the string a human's
    must-find phrase is compared with, with no view of the document needed here.
    """
    streams = {stream.part_id: stream.text for stream in parsed.union_streams}
    return [
        "".join(streams[span.part_id][span.start : span.end] for span in hit.spans)
        for hit in hits
    ]


def evaluate_l2(fixtures_dir: str | Path, *, store_root: str | Path | None = None) -> L2Report | None:
    """Score every human must-find label set, through the one run path. ``None`` if none exist.

    Each labelled document that is on disk is ingested against the registry the label set
    names, and the hit texts are read back **from the store** (D8) -- so L2 scores what the
    run stored, not what the matcher happened to hold in memory. A document that is not on
    disk is skipped inside the scorer, and a label set whose registry is not on disk scores
    nothing rather than crashing the whole table.
    """
    label_sets = iter_must_find(Path(fixtures_dir) / "evals")
    if not label_sets:
        return None
    reports: list[L2Report] = []
    with _store_dir(store_root) as root:
        for labels in label_sets.values():
            term_list = Path(fixtures_dir) / labels.term_list
            found: dict[str, list[str]] = {}
            if term_list.is_file():
                for document in labels.documents:
                    path = Path(fixtures_dir) / document.document
                    if not path.is_file():
                        continue
                    record = pipeline.run(path, term_list, store_root=root)
                    parsed = pipeline.stored_parse(record, store_root=root)
                    found[document.document] = hit_texts(
                        parsed, pipeline.stored_hits(record, store_root=root)
                    )
            reports.append(score(labels, found))
    return merge(reports)


# --- cost: what summarizing the corpus costs, reported outside every gate ----------------


@dataclass(frozen=True)
class CostRow:
    """One document's summary pass: what was called, what the store answered, what it cost.

    ``tokens`` sums only the calls **this pass** made, and only when every one of them
    reported them (a client that reports nothing yields ``None``, never a 0 that claims the
    calls were free); ``cost_usd`` is always None until a price table exists to multiply by
    (open-inputs: pricing is the owner's number to supply).
    """

    document: str
    chunks: int
    chunk_calls: int
    rollup_calls: int
    cache_hits: int
    calls: int
    tokens: int | None
    cost_usd: float | None = None

    def as_dict(self) -> dict:
        return {
            "document": self.document,
            "chunks": self.chunks,
            "chunk_calls": self.chunk_calls,
            "rollup_calls": self.rollup_calls,
            "cache_hits": self.cache_hits,
            "calls": self.calls,
            "tokens": self.tokens,
            "cost_usd": self.cost_usd,
        }


@dataclass(frozen=True)
class CacheRow:
    """The v2/v3 pair's cache claim: only the sidecar's changed chunks re-summarize.

    ``matches_sidecar`` is True when the called set equals the hand-typed changed set on a
    cold store, False when a chunk outside it was called (or the sidecar names a chunk the
    run does not have), and None when the store already answered part of the pass -- a warm
    store cannot show what *would* have re-summarized, so the claim goes unevaluated rather
    than half-proven (rule 8: unknown stays unknown).
    """

    pair: tuple[str, str]
    revised: str
    chunks: int
    calls: int
    cache_hits: int
    hit_rate: float | None
    changed_chunks: tuple[int, ...]
    called_chunks: tuple[int, ...]
    matches_sidecar: bool | None
    note: str

    def as_dict(self) -> dict:
        return {
            "pair": list(self.pair),
            "revised": self.revised,
            "chunks": self.chunks,
            "calls": self.calls,
            "cache_hits": self.cache_hits,
            "hit_rate": self.hit_rate,
            "changed_chunks": list(self.changed_chunks),
            "called_chunks": list(self.called_chunks),
            "matches_sidecar": self.matches_sidecar,
            "note": self.note,
        }


@dataclass(frozen=True)
class CostReport:
    """The cost section: per-document rows, the pair's cache row, and the L3 sample pool."""

    rows: tuple[CostRow, ...] = ()
    cache: CacheRow | None = None
    samples: tuple[Sample, ...] = ()
    metrics: dict = field(default_factory=dict)
    note: str = ""

    @property
    def evaluated(self) -> bool:
        return bool(self.rows)

    def as_table(self) -> dict:
        return {
            "gated": False,
            "rows": [row.as_dict() for row in self.rows],
            "metrics": dict(self.metrics),
            "cache": self.cache.as_dict() if self.cache else {},
            "note": self.note,
        }


def _pair_cache(sidecar: Mapping, pair: tuple[str, str], summary_run) -> CacheRow:
    """The v2/v3 row from a processed v3 run and the hand-typed ``changed`` ordinals."""
    changed = tuple(sorted(int(entry["chunk"]) for entry in sidecar.get("changed", ())))
    called = tuple(
        index for index, outcome in enumerate(summary_run.outcomes) if outcome.called
    )
    chunks = len(summary_run.outcomes)
    hits = chunks - len(called)
    called_set, changed_set = set(called), set(changed)
    if any(ordinal >= chunks for ordinal in changed):
        matches: bool | None = False
        note = "the sidecar names a chunk the run does not have"
    elif called_set - changed_set:
        matches = False
        note = "a chunk outside the sidecar's changed set re-summarized"
    elif called_set == changed_set:
        matches = True
        note = "exactly the sidecar's changed chunks re-summarized (cold store)"
    else:
        matches = None
        note = "the store already answered part of the pass (warm store): what would have re-summarized is not observable"
    return CacheRow(
        pair=pair,
        revised=pair[1],
        chunks=chunks,
        calls=len(called),
        cache_hits=hits,
        hit_rate=(hits / chunks) if chunks else None,
        changed_chunks=changed,
        called_chunks=called,
        matches_sidecar=matches,
        note=note,
    )


def collect_cost(
    fixtures_dir: str | Path, *, store_root: str | Path | None = None
) -> CostReport:
    """Summarize every top-level fixture with the canned client and report what that costs.

    The pass runs through the real summarizer -- store keys, cache hits, roll-ups and
    rejections included -- with the provider swapped for the canned one, so *calls* and
    *hits* are true store behaviour while ``tokens`` is whatever the client reports (the
    canned one reports none) and ``cost_usd`` stays None with no price table. It writes to
    ``store_root`` (or a temporary store): cost is the price of the real path, so the real
    path is what runs. The pair sidecar's cache row is asserted from the hand-typed
    ``changed`` ordinals, never from a previous run. The L3 report's sample comes from here,
    in document order.
    """
    fixtures = Path(fixtures_dir)
    documents = sorted(fixtures.glob("*.docx"), key=lambda path: path.name)
    term_list = fixtures / "terms" / "synthetic.example.json"
    if not documents:
        return CostReport(
            note="not evaluated: no fixture document at the top of the fixtures directory"
        )
    if not term_list.is_file():
        return CostReport(
            note=f"not evaluated: the fixture term list {term_list.name} is not on disk"
        )
    rows: list[CostRow] = []
    samples: list[Sample] = []
    cache: CacheRow | None = None
    with _store_dir(store_root) as root:
        store = Store(root)
        client = CannedClient()
        processed: dict[str, object] = {}
        for path in documents:
            record = pipeline.run(path, term_list, store_root=root)
            summary_run = summarize(store, record, client=client, model=CANNED)
            processed[path.name] = summary_run
            chunk_calls = sum(1 for outcome in summary_run.outcomes if outcome.called)
            rollup_calls = sum(1 for rollup in summary_run.rollups if rollup.called)
            chunk_hits = len(summary_run.outcomes) - chunk_calls
            rollup_hits = len(summary_run.rollups) - rollup_calls
            called_artifacts = [
                outcome.summary
                for outcome in summary_run.outcomes
                if outcome.called and outcome.summary is not None
            ] + [
                rollup.rollup
                for rollup in summary_run.rollups
                if rollup.called and rollup.rollup is not None
            ]
            calls = chunk_calls + rollup_calls
            if calls == 0:
                tokens: int | None = 0
            elif len(called_artifacts) == calls and all(
                artifact.tokens is not None for artifact in called_artifacts
            ):
                tokens = sum(artifact.tokens for artifact in called_artifacts)
            else:
                tokens = None
            rows.append(
                CostRow(
                    document=path.name,
                    chunks=len(summary_run.outcomes),
                    chunk_calls=chunk_calls,
                    rollup_calls=rollup_calls,
                    cache_hits=chunk_hits + rollup_hits,
                    calls=calls,
                    tokens=tokens,
                    cost_usd=None,
                )
            )
            samples.extend(
                Sample(
                    document=path.name,
                    chunk_id=outcome.chunk_id,
                    prompt_hash=outcome.summary.prompt_hash,
                    model=outcome.summary.model_id,
                )
                for outcome in summary_run.outcomes
                if outcome.summary is not None
            )
        pair_path = fixtures / "program_review_pair.json"
        if pair_path.is_file():
            sidecar = json.loads(pair_path.read_text(encoding="utf-8"))
            pair = sidecar.get("fixture_pair")
            if (
                isinstance(pair, list)
                and len(pair) == 2
                and all(isinstance(name, str) for name in pair)
                and all(name in processed for name in pair)
            ):
                names = [path.name for path in documents]
                if names.index(pair[0]) < names.index(pair[1]):
                    cache = _pair_cache(sidecar, tuple(pair), processed[pair[1]])
    tokens_known = all(row.tokens is not None for row in rows)
    metrics = {
        "documents": len(rows),
        "chunks": sum(row.chunks for row in rows),
        "calls": sum(row.calls for row in rows),
        "tokens": sum(row.tokens for row in rows) if tokens_known else None,
        "cost_usd": None,
    }
    note = (
        f"measured with the canned client over {len(rows)} document(s): calls and cache hits "
        "are the store's real behaviour, tokens appear only when the client reports them "
        "(canned reports none), and cost_usd stays None without a price table"
    )
    return CostReport(
        rows=tuple(rows), cache=cache, samples=tuple(samples), metrics=metrics, note=note
    )


def evaluate_retrieval(
    fixtures_dir: str | Path, *, store_root: str | Path | None = None
) -> RetrievalReport | None:
    """Score each human query set through the query layer. ``None`` if no set exists.

    Each set's own documents are ingested against the registry the set names, every example
    runs through :func:`wordextract.query.search` with that term list pinned, and the pure
    scorer lays the expected citations over the results. A bad provenance or shape fails
    here -- before any summary pass writes anything -- because a set that is not human is a
    load error (rule 8), not an empty measurement. With no set on disk the layer is not
    evaluated: the committed example documents the format and never scores.
    """
    fixtures = Path(fixtures_dir)
    query_sets = iter_query_sets(fixtures / "evals")
    if not query_sets:
        return None
    reports: list[RetrievalReport] = []
    with _store_dir(store_root) as root:
        for query_set in query_sets.values():
            term_list = fixtures / query_set.term_list
            if not term_list.is_file():
                reports.append(
                    RetrievalReport(
                        query_sets=(query_set.query_set,),
                        term_lists=(query_set.term_list,),
                        skipped=tuple(
                            (example.query, "the set's term list is not on disk where it names it")
                            for example in query_set.examples
                        ),
                    )
                )
                continue
            chunks: dict[str, tuple[str, ...]] = {}
            ids: dict[str, str] = {}
            pool = sorted(
                {citation.document for example in query_set.examples for citation in example.expected}
            )
            for document in pool:
                path = fixtures / document
                if not path.is_file():
                    continue
                record = pipeline.run(path, term_list, store_root=root)
                ids[record.hashed_inputs.source_content_hash] = document
                chunks[document] = tuple(
                    chunk.id for chunk in pipeline.stored_chunks(record, store_root=root)
                )
            store = Store(root)
            chosen = term_list_hash(load_registry_text(term_list.read_text(encoding="utf-8")))
            ranked: dict[str, list[tuple[str, str]]] = {}
            for example in query_set.examples:
                result = search(store, example.query, term_list=chosen)
                ranked[example.query] = [
                    (ids[hit.document_id], hit.chunk_id)
                    for hit in result.results
                    if hit.document_id in ids
                ]
            reports.append(score_retrieval(query_set, ranked, chunks))
    return merge_reports(reports)


def _producer_verified_tally(producer_verified) -> dict[str, bool]:
    """Normalize the per-code-path producer-verified tally.

    A mapping maps a code path to truthy (a count of documents that exercise it, or a plain
    flag); an iterable names the verified paths. Always sorted, so the emitted table is
    stable.
    """
    if producer_verified is None:
        return {}
    if isinstance(producer_verified, Mapping):
        return {str(path): bool(verified) for path, verified in sorted(producer_verified.items())}
    return {str(path): True for path in sorted(producer_verified)}


def _gate_result(layer: dict, metrics: Mapping[str, object]) -> bool | None:
    """Evaluate a layer's declared gate against its metrics; ``None`` if it was not measured.

    The gate declaration is what decides, so a layer whose metric is missing (L2 with no
    human labels) reports nothing rather than a pass it did not earn.
    """
    gate = layer.get("gate")
    if not gate or gate["metric"] not in metrics:
        return None
    return _OPS[gate["op"]](metrics[gate["metric"]], gate["value"])


def _layer(key: str, *, n=0, rows=None, metrics=None, coverage=None, note=None) -> dict:
    layer = {
        **LAYERS[key],
        "layer": key,
        "n": n,
        "rows": rows or [],
        "metrics": dict(metrics or {}),
        "coverage": dict(coverage or {}),
    }
    layer["result"] = _gate_result(LAYERS[key], layer["metrics"])
    if note is not None:
        layer["note"] = note
    return layer


def build_metrics_table(
    *,
    documents: int = 0,
    producer_verified: Mapping[str, object] | Iterable[str] | None = None,
    labels: dict | None = None,
    l1: L1Report | None = None,
    l2: L2Report | None = None,
    l3: L3Report | None = None,
    retrieval: RetrievalReport | None = None,
    cost: CostReport | None = None,
) -> dict:
    """A complete, JSON-serializable metrics table laid out from the reports a caller has.

    ``producer_verified`` is a per-code-path tally (design D3: a producer-verified path is
    one exercised by a Microsoft-Word-produced document), never a scalar. ``documents``
    stays the `fixtures/real` count the tally is reported against, and ``coverage`` is the
    verified fraction of the tally -- over an empty tally it is a defined 0.0, not a
    ZeroDivisionError / NaN.

    ``l1``/``l2``/``retrieval`` are the gated layers' reports, ``l3`` the ungated one, and
    ``cost`` the cost section (outside ``quality``, never gated). Passing none of them
    leaves every layer empty, which is what the Phase 0 table was. A layer that *measured
    nothing* -- an L1 whose corpus compared no labelled fact -- is a **failure**
    (``result: False``): L1's corpus is the committed fixtures, so comparing nothing means a
    wrong path or missing fixtures. An L2 or retrieval with no human labels carries
    ``result: None`` and a note saying it was not evaluated: that absence is the documented
    conditional close, not a bug, and it is the only reason their gates are not failing.
    """
    # L1 was asked to score a corpus (``l1`` is not None) and compared nothing: a wrong
    # ``--fixtures`` path, or a checkout without the fixtures. That is a broken run, not an
    # unmeasured layer -- the corpus is committed, so "no labelled fact" can never be the
    # honest state, and passing it would let a typo or a missing directory clear the gate.
    # (L2 differs: human labels genuinely do not exist yet, so it is "not evaluated".)
    l1_metrics = l1.metrics if l1 is not None else {}
    quality = [
        _layer(
            "L1",
            n=l1.compared if l1 else 0,
            rows=l1.rows() if l1 else [],
            metrics=l1_metrics,
            coverage=l1.coverage() if l1 else {},
            note=(
                "FAILED: the corpus compared no labelled fact "
                "(check the --fixtures path; L1's corpus is the committed fixtures)"
                if l1 is not None and not l1.facts
                else None
            ),
        ),
        _layer(
            "L2",
            n=len(l2.rows) if l2 else 0,
            rows=l2.table_rows() if l2 else [],
            metrics=l2.metrics if l2 else {},
            coverage=l2.coverage() if l2 else {},
            note=l2.note if l2 else "not evaluated: no human must-find labels exist",
        ),
        _layer(
            "L3",
            n=len(l3.sample) if l3 else 0,
            rows=[row.as_dict() for row in l3.rows] if l3 else [],
            metrics=l3.metrics if l3 else {},
            coverage=l3.coverage() if l3 else {},
            note=l3.note if l3 else "not graded: no human L3 grades exist",
        ),
        _layer(
            "retrieval",
            n=len(retrieval.rows) if retrieval else 0,
            rows=[row.as_dict() for row in retrieval.rows] if retrieval else [],
            metrics=retrieval.metrics if retrieval else {},
            coverage=retrieval.coverage() if retrieval else {},
            note=(
                retrieval.note
                if retrieval
                else "not evaluated: no human query set exists "
                "(fixtures/evals/queries.example.json documents the format)"
            ),
        ),
    ]
    tally = _producer_verified_tally(producer_verified)
    verified = sum(1 for ok in tally.values() if ok)
    coverage = (verified / len(tally)) if tally else 0.0
    return {
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "quality": quality,
        "cost": cost.as_table()
        if cost
        else {
            "gated": False,
            "rows": [],
            "metrics": {},
            "cache": {},
            "note": "cost/doc and cache-hit metrics are reported here, never folded into quality gates",
        },
        "producer_verified_coverage": {
            "documents": documents,
            "producer_verified": tally,
            "verified_paths": verified,
            "total_paths": len(tally),
            "coverage": coverage,
        },
        "labels": labels or {"generator": 0, "spec": 0, "human": 0},
    }


def gate_failures(table: dict) -> list[str]:
    """Names of gated layers whose (non-empty) result is False. Empty table -> [].

    A layer that was not evaluated (``result`` is ``None``) is not a failure: L2 with no
    human labels closes Phase 1 conditionally, which is a documented state and not a bug.
    """
    return [
        layer["layer"]
        for layer in table["quality"]
        if layer["gated"] and layer.get("result") is False
    ]


def run(
    fixtures_dir: str | Path | None = "fixtures",
    *,
    store_root: str | Path | None = None,
    producer_verified_paths: Iterable[str] = (),
) -> dict:
    """Score the corpus and lay the metrics table out: L1, L2, retrieval, cost, L3.

    L1 re-parses every labelled fixture; L2 runs each human must-find set through the
    pipeline (into ``store_root``, or a temporary store) and scores the stored hits;
    retrieval scores each human query set through the query layer, or reports *not
    evaluated* when none exists -- a set whose provenance is not human raises before
    anything is written. The cost pass then summarizes every top-level fixture with the
    canned client (into ``store_root`` or a temporary store) and reports calls, cache hits
    and the pair row; L3's report is laid over that pass's sample, ungraded until a human
    grades it. The producer-verified tally stays all-False until a Microsoft-Word-produced
    document lands in `fixtures/real` and a test is tagged `@producer_verified`.
    """
    labels = {"generator": 0, "spec": 0, "human": 0}
    documents = 0
    l1_report = l2_report = retrieval_report = l3_report = cost_report = None
    if fixtures_dir is not None:
        fixtures_dir = Path(fixtures_dir)
        for sidecar in iter_sidecars(fixtures_dir).values():
            labels[sidecar.labels_provenance] = labels.get(sidecar.labels_provenance, 0) + 1
        real_dir = fixtures_dir / "real"
        if real_dir.is_dir():
            documents = len(list(real_dir.glob("*.docx")))
        l1_report = score_fixtures(fixtures_dir)
        l2_report = evaluate_l2(fixtures_dir, store_root=store_root)
        # Both loaders raise on a bad provenance or shape, before the cost pass writes:
        # a label file that is not what it claims is a format error, not an empty result.
        retrieval_report = evaluate_retrieval(fixtures_dir, store_root=store_root)
        gradesets = iter_grades(fixtures_dir / "evals")
        cost_report = collect_cost(fixtures_dir, store_root=store_root)
        l3_report = build_l3_report(cost_report.samples, gradesets)
    return build_metrics_table(
        documents=documents,
        producer_verified=producer_verified_roster(producer_verified_paths),
        labels=labels,
        l1=l1_report,
        l2=l2_report,
        l3=l3_report,
        retrieval=retrieval_report,
        cost=cost_report,
    )
