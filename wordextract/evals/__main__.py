"""``python -m wordextract.evals`` -- print the metrics table as JSON."""
from __future__ import annotations

from pathlib import Path
import argparse
import json

from .harness import gate_failures, run


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m wordextract.evals",
        description="Emit the Phase 0 eval metrics table (empty until Phase 1).",
    )
    parser.add_argument("--fixtures", default="fixtures", help="fixtures directory (default: fixtures)")
    parser.add_argument("--out", default=None, help="write JSON here instead of stdout")
    args = parser.parse_args(argv)

    table = run(Path(args.fixtures) if args.fixtures else None)
    text = json.dumps(table, indent=2, sort_keys=True)

    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)

    failed = gate_failures(table)
    if failed:
        print(f"gated layers failed: {', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
