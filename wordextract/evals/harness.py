"""Turn 9: the eval harness -- the metrics table over the real parser, and its gates.

Three layers, two of them gated, plus a cost section that is never folded into a gate:

* **L1** (``exact_fact_accuracy == 1.0``): every sidecar fact checked against a fresh parse
  of its fixture, with a tiling/coverage report so a green L1 cannot be vacuous
  (:mod:`wordextract.evals.l1`);
* **L2** (``recall == 1.0``, and every false positive classified): the human must-find list,
  run through the pipeline and scored against the hits the run *stored*
  (:mod:`wordextract.evals.must_find`). With no human labels it reports **not evaluated**
  and Phase 1 closes conditionally (open-inputs section 3);
* **L3**: summaries. Ungated, and reported as such.

:func:`build_metrics_table` is **pure**: it takes the layer reports a caller already has and
lays them out, so nothing here parses a document or reads a store. :func:`run` is the
caller that does: it scores the fixtures, runs the human label sets through
:mod:`wordextract.pipeline`, and hands both reports in.

``producer_verified_coverage`` is the third report, and it is a *roster*: the code paths a
Word-produced document would exercise, the constructs ``docs/design/open-inputs.md`` section
2 records as unverified, and every ``GAP_*`` id the walker can report. All ``False`` until a
Microsoft-Word-produced document lands in ``fixtures/real`` with a test tagged
``@producer_verified`` -- so the coverage reads 0.0 because the work is outstanding, never
because the roster is empty (an empty roster would read 0.0 too, and mean nothing).
"""
from __future__ import annotations

import operator
import tempfile
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from pathlib import Path

from ..model import ParseResult, TermHit
from .. import pipeline, walker
from ..versions import OUTPUT_SCHEMA_VERSION
from .l1 import L1Report, score_fixtures
from .labels import iter_sidecars
from .must_find import L2Report, iter_must_find, merge, score

# Gate declarations, by layer. A layer with a non-empty `result` of False is what makes the
# CLI exit non-zero; a metric the layer did not measure leaves `result` None (L2 with no
# human labels, L1 before the rows existed).
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
) -> dict:
    """A complete, JSON-serializable metrics table laid out from the reports a caller has.

    ``producer_verified`` is a per-code-path tally (design D3: a producer-verified path is
    one exercised by a Microsoft-Word-produced document), never a scalar. ``documents``
    stays the `fixtures/real` count the tally is reported against, and ``coverage`` is the
    verified fraction of the tally -- over an empty tally it is a defined 0.0, not a
    ZeroDivisionError / NaN.

    ``l1``/``l2`` are the layer reports; passing neither leaves every layer empty, which is
    what the Phase 0 table was. A layer that *measured nothing* -- an L1 whose corpus
    compared no labelled fact -- is a **failure** (``result: False``): L1's corpus is the
    committed fixtures, so comparing nothing means a wrong path or missing fixtures. An L2 with
    no human labels carries ``result: None`` and a note saying it was not evaluated: that
    absence is the documented conditional close, not a bug.
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
        _layer("L3"),
    ]
    tally = _producer_verified_tally(producer_verified)
    verified = sum(1 for ok in tally.values() if ok)
    coverage = (verified / len(tally)) if tally else 0.0
    return {
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "quality": quality,
        "cost": {
            "gated": False,
            "rows": [],
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
    """Score the corpus and lay the metrics table out: L1, L2, and the producer roster.

    L1 re-parses every labelled fixture; L2 runs each human must-find set through the
    pipeline (into ``store_root``, or a temporary store) and scores the stored hits. The
    producer-verified tally stays all-False until a Microsoft-Word-produced document lands
    in `fixtures/real` and a test is tagged `@producer_verified`.
    """
    labels = {"generator": 0, "spec": 0, "human": 0}
    documents = 0
    l1_report = l2_report = None
    if fixtures_dir is not None:
        fixtures_dir = Path(fixtures_dir)
        for sidecar in iter_sidecars(fixtures_dir).values():
            labels[sidecar.labels_provenance] = labels.get(sidecar.labels_provenance, 0) + 1
        real_dir = fixtures_dir / "real"
        if real_dir.is_dir():
            documents = len(list(real_dir.glob("*.docx")))
        l1_report = score_fixtures(fixtures_dir)
        l2_report = evaluate_l2(fixtures_dir, store_root=store_root)
    return build_metrics_table(
        documents=documents,
        producer_verified=producer_verified_roster(producer_verified_paths),
        labels=labels,
        l1=l1_report,
        l2=l2_report,
    )
