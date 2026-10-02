"""Turn 9: the commands the design names -- ``ingest``, ``hits``, ``summarize`` and ``serve``.

    python -m wordextract ingest    <docx> --terms <terms.json> [--store DIR] [--view VIEW]
    python -m wordextract hits      <docx> --terms <terms.json> [--store DIR] [--view VIEW]
    python -m wordextract summarize <docx> --terms <terms.json> --client NAME [--model ID]
    python -m wordextract serve     [--store DIR] [--view VIEW] [--terms TERMS]

There is no ``reindex``: FTS5 (Turn 8) is deferred, and a command that reindexed nothing
would be a promise the build does not keep.

The first two go through :mod:`wordextract.pipeline`, so a CLI run and an eval-harness run
are the same run. Each prints **canonical JSON on stdout** and nothing else, so the output
is a fixture: ``ingest`` prints the D10 run record, ``hits`` prints the stored hit record
read back under the record's own recomputed key. Diagnostics go to stderr, and a run that
cannot start exits ``2`` without a partial JSON document on stdout.

``summarize`` ingests if needed and then runs the summary pass, printing **that pass's own
run record** -- one ``ArtifactCache`` entry per chunk (Phase 2, Turn 1). It takes no
``--view``: a summary is keyed on the rendered union markup, which is wider than any view,
and the chunks it summarizes are the accepted view's, the view an ingest defaults to.
``--client`` is required and names the implementation
(:func:`~wordextract.llm.client`: ``canned``, or ``module:attribute`` for a real one);
``--model`` is the model id recorded in every summary key, so a real client must be given it.

``serve`` is the MCP server (Phase 2, Turn 6): the read-only tools over the stored
records, on stdio. It prints **no JSON** -- the stdio transport is the protocol's own
channel -- and it needs the optional extra (``pip install -e ".[mcp]"``); without it the
command exits ``2`` like any other run that cannot start, as does a store that will not
open read-only. ``--store``/``--view``/``--terms`` set the three
:class:`~wordextract.mcp.settings.Settings` fields (defaults: the store below,
``accepted``, the store's only term list), and ``python -m wordextract.mcp`` serves the
same tools with those defaults.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from docextract_core import encode, to_json

from .llm import DEFAULT_MODEL, client
from .model import View
from .pipeline import DEFAULT_STORE, run, stored_hits
from .store import Store
from .summarize import summarize


def build_parser() -> argparse.ArgumentParser:
    """``ingest``, ``hits``, ``summarize`` and ``serve``, sharing the options that describe a run."""
    parser = argparse.ArgumentParser(prog="python -m wordextract", description=__doc__.splitlines()[0])
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("ingest", "parse, chunk, match and store one document; print the run record"),
        ("hits", "ingest if needed, then print the hits stored for the document"),
    ):
        _run_options(subcommands.add_parser(name, help=help_text), summarize=False)
    summarize_command = subcommands.add_parser(
        "summarize", help="ingest if needed, then summarize its chunks; print the summary pass"
    )
    # No ``--view`` (a summary is keyed on the rendered union markup, not on a view) and no
    # ``--run-id`` (the pass records its own run, so naming one would only name the ingest).
    _run_options(summarize_command, summarize=True)
    summarize_command.set_defaults(run_id=None, view=View.ACCEPTED.value)
    summarize_command.add_argument(
        "--client",
        required=True,
        help=f"the LLM client ({DEFAULT_MODEL} for the canned one, or '<module>:<attribute>')",
    )
    summarize_command.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"the model id recorded in every summary key (default: {DEFAULT_MODEL})",
    )
    serve_command = subcommands.add_parser(
        "serve",
        help="serve the read-only MCP tools on stdio (needs the optional mcp extra)",
    )
    # No positional document and no --run-id: serve reads a store that exists already and
    # ingests nothing, so its options are only the settings the tools default from.
    serve_command.add_argument(
        "--store",
        default=str(DEFAULT_STORE),
        help=f"the store directory (default: {DEFAULT_STORE})",
    )
    serve_command.add_argument(
        "--view",
        choices=[view_.value for view_ in View],
        default=View.ACCEPTED.value,
        help="get_chunk's default view (default: accepted)",
    )
    serve_command.add_argument(
        "--terms",
        default=None,
        help="the default term list (default: the store's only one)",
    )
    return parser


def _run_options(command: argparse.ArgumentParser, *, summarize: bool) -> None:
    """The document, its term list and the store: what the three commands have in common."""
    command.add_argument("document", help="the .docx to ingest")
    command.add_argument("--terms", required=True, help="the term list (canonical JSON)")
    command.add_argument(
        "--store",
        default=str(DEFAULT_STORE),
        help=f"the store directory (default: {DEFAULT_STORE})",
    )
    if summarize:
        return
    command.add_argument(
        "--view",
        choices=[view_.value for view_ in View],
        default=View.ACCEPTED.value,
        help="the view the chunks are (default: accepted)",
    )
    command.add_argument(
        "--run-id",
        default=None,
        help="write the run under this id instead of a fresh one",
    )


def _serve(store: Path, view: View, terms: str | None) -> int:
    """Run the MCP server on stdio; exit 2 when the optional extra is not installed.

    The import is deliberately late: ``serve`` is the only command that needs the
    third-party ``mcp`` framework, so ``ingest``/``hits``/``summarize`` and their tests
    run without it installed.
    """
    try:
        from .mcp.server import serve
        from .mcp.settings import Settings
    except ImportError as missing:
        print(
            "wordextract: serve needs the optional extra (pip install -e '.[mcp]'): "
            f"{missing}",
            file=sys.stderr,
        )
        return 2
    return serve(Settings(store=store, view=view, term_list=terms))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        # Before the document loop: serve has no positional document, and it runs the
        # blocking stdio server instead of printing a record.
        return _serve(Path(args.store), View(args.view), args.terms)
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
    if args.command == "summarize":
        try:
            summarizer = client(args.client)
        except ValueError as unknown:
            print(f"wordextract: {unknown}", file=sys.stderr)
            return 2
        # The summary pass mints its own run id: a run file is one run, and reusing the ingest
        # run's id would overwrite the ingest record with a summary pass.
        pass_ = summarize(Store(args.store), record, client=summarizer, model=args.model)
        print(to_json(pass_.record))
    elif args.command == "ingest":
        print(to_json(record))
    else:
        print(json.dumps(encode(stored_hits(record, store_root=args.store)), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
