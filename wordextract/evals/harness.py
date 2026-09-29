"""Eval harness skeleton (design D11): emit a well-formed but empty metrics table.

Phase 0 ships the table, not the measurements. Every gate is declared so the shape is
fixed, but the metric rows stay empty until Phase 1 fills them. Nothing here parses a
document; it only reads sidecars (through :mod:`wordextract.evals.labels`, which imports
no implementation code).
"""
from __future__ import annotations

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


def build_metrics_table(
    *, documents: int = 0, producer_verified: int = 0, labels: dict | None = None
) -> dict:
    """A complete, JSON-serializable metrics table with empty metric rows."""
    quality = [
        {**LAYERS[key], "layer": key, "n": 0, "rows": [], "result": None}
        for key in ("L1", "L2", "L3")
    ]
    coverage = (producer_verified / documents) if documents else 0.0
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
            "producer_verified": producer_verified,
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
    """Build the metrics table from the labels available on disk (metrics stay empty)."""
    labels = {"generator": 0, "spec": 0, "human": 0}
    documents = producer_verified = 0
    if fixtures_dir is not None:
        fixtures_dir = Path(fixtures_dir)
        for sidecar in iter_sidecars(fixtures_dir).values():
            labels[sidecar.labels_provenance] = labels.get(sidecar.labels_provenance, 0) + 1
        real_dir = fixtures_dir / "real"
        if real_dir.is_dir():
            documents = len(list(real_dir.glob("*.docx")))
    return build_metrics_table(
        documents=documents, producer_verified=producer_verified, labels=labels
    )
