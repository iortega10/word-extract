"""Turn 9: the two commands the design names -- ``ingest`` and ``hits``.

    python -m wordextract ingest <docx> --terms <terms.json> [--store DIR] [--view VIEW]
    python -m wordextract hits   <docx> --terms <terms.json> [--store DIR] [--view VIEW]

There is no ``reindex``: FTS5 (Turn 8) is deferred, and a command that reindexed nothing
would be a promise the build does not keep.

Both commands go through :mod:`wordextract.pipeline`, so a CLI run and an eval-harness run
are the same run. Each prints **canonical JSON on stdout** and nothing else, so the output
is a fixture: ``ingest`` prints the D10 run record, ``hits`` prints the stored hit record
read back under the record's own recomputed key. Diagnostics go to stderr, and a run that
cannot start exits ``2`` without a partial JSON document on stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from docextract_core import encode, to_json

from .model import View
from .pipeline import DEFAULT_STORE, run, stored_hits


def build_parser() -> argparse.ArgumentParser:
    """``ingest`` and ``hits``, sharing the three options that describe a run."""
    parser = argparse.ArgumentParser(prog="python -m wordextract", description=__doc__.splitlines()[0])
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("ingest", "parse, chunk, match and store one document; print the run record"),
        ("hits", "ingest if needed, then print the hits stored for the document"),
    ):
        command = subcommands.add_parser(name, help=help_text)
        command.add_argument("document", help="the .docx to ingest")
        command.add_argument("--terms", required=True, help="the term list (canonical JSON)")
        command.add_argument(
            "--store",
            default=str(DEFAULT_STORE),
            help=f"the store directory (default: {DEFAULT_STORE})",
        )
        command.add_argument(
            "--view",
            choices=[view.value for view in View],
            default=View.ACCEPTED.value,
            help="the view the chunks are (default: accepted)",
        )
        command.add_argument(
            "--run-id",
            default=None,
            help="write the run under this id instead of a fresh one",
        )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for path, what in ((args.document, "document"), (args.terms, "term list")):
        if not Path(path).is_file():
            print(f"wordextract: no {what} at {path}", file=sys.stderr)
            return 2

    record = run(
        args.document,
        args.terms,
        store_root=args.store,
        view=View(args.view),
        run_id=args.run_id,
    )
    if args.command == "ingest":
        print(to_json(record))
    else:
        print(json.dumps(encode(stored_hits(record, store_root=args.store)), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
