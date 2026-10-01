"""``python -m wordextract.evals`` -- print the metrics table as JSON, and gate on it.

The table is **all** stdout carries, so ``python -m wordextract.evals | jq`` works and the
output is a fixture. Every diagnostic -- a gated layer that was not evaluated, why the exit
code is what it is -- goes to stderr.
"""
from __future__ import annotations

from pathlib import Path
import argparse
import json
import sys

from .harness import gate_failures, run


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m wordextract.evals",
        description="Emit the Phase 1 eval metrics table: L1 (parse), L2 (must-find), L3.",
    )
    parser.add_argument("--fixtures", default="fixtures", help="fixtures directory (default: fixtures)")
    parser.add_argument("--out", default=None, help="write JSON here instead of stdout")
    parser.add_argument(
        "--store",
        default=None,
        help="store directory for the L2 run (default: a temporary store, discarded)",
    )
    args = parser.parse_args(argv)

    try:
        table = run(Path(args.fixtures) if args.fixtures else None, store_root=args.store)
    except ValueError as exc:
        # a label set the harness refuses (not human provenance, malformed): say so plainly
        print(f"error: {exc}", file=sys.stderr)
        return 2
    text = json.dumps(table, indent=2, sort_keys=True)

    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)

    for layer in table["quality"]:
        if layer["gated"] and layer.get("result") is None:
            print(
                f"{layer['layer']} not gated this run: {layer.get('note', 'not evaluated')}",
                file=sys.stderr,
            )

    failed = gate_failures(table)
    if failed:
        print(f"gated layers failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
