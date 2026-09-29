"""Eval harness (design D11). CLI: ``python -m wordextract.evals [--fixtures DIR]``."""
from .harness import LAYERS, build_metrics_table, gate_failures, run
from .labels import (
    CommentLabel,
    RevisionLabel,
    SectionLabel,
    Sidecar,
    TableLabel,
    iter_sidecars,
    load_sidecar,
)

__all__ = [
    "run",
    "build_metrics_table",
    "gate_failures",
    "LAYERS",
    "load_sidecar",
    "iter_sidecars",
    "Sidecar",
    "CommentLabel",
    "RevisionLabel",
    "SectionLabel",
    "TableLabel",
]
