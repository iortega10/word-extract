"""Turn 9: append this code's fingerprints to the behavior ledger.

Run it in the same commit as the behavior change that caused it, after bumping the
component's version constant (``tools/behavior_ledger.py`` names the constant per
component):

    python tools/update_behavior_ledger.py

It appends one line per component -- and **refuses to overwrite** a version that is
already recorded with a different fingerprint, naming the constant to bump instead. That
refusal is what makes the ledger append-only: a new fingerprint under an old version is
exactly the change that needs a bump, and a reviewer sees the ledger line in the same
commit as the change.

``--new-corpus`` records a new corpus version when the committed fixtures grew: fixtures are
corpus, not behavior, so no component version is bumped, and every older corpus is
recomputed first -- a behavior change hiding in the same commit as a new fixture is refused.

``--check`` reports the same disagreements without writing anything, which is what
``tests/test_behavior_ledger.py`` and a reviewer both need.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import behavior_ledger  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ledger", default=None, help="the ledger JSON (default: tests/ledger/behavior_ledger.json)")
    parser.add_argument("--fixtures", default=None, help="the fixture corpus (default: fixtures/)")
    parser.add_argument(
        "--new-corpus",
        action="store_true",
        help="the committed fixtures grew or changed: add a corpus version (tests/ledger/"
        "corpus.json) before recording. Bumps no component version; refused if any older "
        "corpus's fingerprint moved",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report every disagreement and exit non-zero, writing nothing",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    ledger_path = Path(args.ledger) if args.ledger else behavior_ledger.LEDGER_PATH
    ledger = behavior_ledger.load_ledger(ledger_path)
    if args.check:
        problems = behavior_ledger.check(ledger, fixtures_dir=args.fixtures)
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1 if problems else 0
    if not behavior_ledger.load_corpora():
        # first run: today's fixtures are corpus 1, written down so growth is noticed later
        behavior_ledger.write_corpora(
            [{"version": 1, "files": behavior_ledger.discovered(args.fixtures)}]
        )
        print("corpus: created version 1", file=sys.stderr)
    elif args.new_corpus:
        corpora, added = behavior_ledger.add_corpus_version(args.fixtures)
        if added:
            behavior_ledger.write_corpora(corpora)
            print(f"corpus: added version {behavior_ledger.latest_corpus(corpora)}", file=sys.stderr)
    try:
        updated, lines = behavior_ledger.record(ledger, fixtures_dir=args.fixtures)
    except ValueError as refused:
        print(refused, file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    # Byte comparison, not text: `read_text` translates newlines, so a CRLF checkout (git
    # `core.autocrlf=true`) would compare equal and never converge back to the LF bytes
    # `write_ledger` guarantees.
    on_disk = ledger_path.read_bytes() if ledger_path.is_file() else b""
    if on_disk == behavior_ledger.render(updated).encode("utf-8"):
        print(f"{ledger_path}: unchanged", file=sys.stderr)
        return 0
    behavior_ledger.write_ledger(updated, ledger_path)
    print(f"{ledger_path}: written", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
