"""Eval harness skeleton (design D11): emit a well-formed but empty metrics table.

Phase 0 ships the table, not the measurements. Every gate is declared so the shape is
fixed, but the metric rows stay empty until Phase 1 fills them. Nothing here parses a
document; it only reads sidecars (through :mod:`wordextract.evals.labels`, which imports
no implementation code).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path

from ..versions import OUTPUT_SCHEMA_VERSION
from .labels import iter_sidecars

# Gate declarations, by layer. `rows` stay empty in Phase 0; a gated layer with a
# non-empty `result` of False is what makes the CLI exit non-zero once the rows exist.
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


def _producer_verified_tally(producer_verified) -> dict[str, bool]:
    """Normalize the per-code-path producer-verified tally.

    A mapping maps a code path to truthy (a count of documents that exercise it,
    or a plain flag); an iterable names the verified paths. Always sorted, so the
    emitted table is stable.
    """
    if producer_verified is None:
        return {}
    if isinstance(producer_verified, Mapping):
        return {str(path): bool(verified) for path, verified in sorted(producer_verified.items())}
    return {str(path): True for path in sorted(producer_verified)}


def build_metrics_table(
    *,
    documents: int = 0,
    producer_verified: Mapping[str, object] | Iterable[str] | None = None,
    labels: dict | None = None,
) -> dict:
    """A complete, JSON-serializable metrics table with empty metric rows.

    ``producer_verified`` is a per-code-path tally (design D3: a producer-verified
    path is one exercised by a Microsoft-Word-produced document), never a scalar.
    ``documents`` stays the `fixtures/real` count the tally is reported against,
    and ``coverage`` is the verified fraction of the tally -- over an empty tally
    it is a defined 0.0, not a ZeroDivisionError / NaN.
    """
    quality = [
        {**LAYERS[key], "layer": key, "n": 0, "rows": [], "result": None}
        for key in ("L1", "L2", "L3")
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
    """Names of gated layers whose (non-empty) result is False. Empty table -> []."""
    return [
        layer["layer"]
        for layer in table["quality"]
        if layer["gated"] and layer.get("result") is False
    ]


def run(fixtures_dir: str | Path | None = "fixtures") -> dict:
    """Build the metrics table from the labels available on disk (metrics stay empty).

    The producer-verified tally stays empty until a Microsoft-Word-produced document
    lands in `fixtures/real` and a test is tagged `@producer_verified`.
    """
    labels = {"generator": 0, "spec": 0, "human": 0}
    documents = 0
    if fixtures_dir is not None:
        fixtures_dir = Path(fixtures_dir)
        for sidecar in iter_sidecars(fixtures_dir).values():
            labels[sidecar.labels_provenance] = labels.get(sidecar.labels_provenance, 0) + 1
        real_dir = fixtures_dir / "real"
        if real_dir.is_dir():
            documents = len(list(real_dir.glob("*.docx")))
    return build_metrics_table(documents=documents, labels=labels)
