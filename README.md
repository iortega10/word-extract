# word-extract

Deterministic extraction and structuring of **Word (`.docx`) documents**: body text, tables,
lists, headings, **comments** (with threads), **tracked changes**, headers, footers and
footnotes, parsed into an addressable, stored tree, with **reproducible term flagging** and
(in progress) per-section summaries so an LLM can find what it needs.

It is a sibling of [`form-extract`](../form-extract), built for the case where someone sends a
Word document full of comments and tracked changes and you need to find, cite and compare
what it says, in any setting with messy documents where a missed clause or a wrong attribution
is a real failure (contracts, policies, reviews, audits, regulated work) or where you need to get
a document to an LLM cleanly.

> **Status.** Phase 1 (the deterministic parse, structure, term matching, store and evals) is
> built and validated. It closed **conditionally**: the term matcher is tested only against a
> synthetic registry, and nothing is yet checked against a Word-saved document (see
> [Open inputs](#open-inputs)). Phase 2 (summaries, search, an MCP server) is specified in
> `docs/design/phase2-build-spec.md`; its Turn 0 helpers (read-only store, chunk text and
> revision-markup rendering, document catalog) Turn 1 (per-chunk summaries cached on the
> rendered input, `python -m wordextract summarize`) Turn 2 (`pending_changes`: the
> revisions each chunk carries, derived from the parse and never from model text) and Turn 3
> (retrieval and ranking: term, text and summary tiers in a fixed order) Turn 4 (section and
> document roll-ups built from child summaries, non-authoritative) and Turn 5 (the query API:
> eight read-only functions that return cited results) are built. Nothing in Phase 1 calls an LLM.

## What it does

- **Parses `.docx` directly** (lxml over the zip, never python-docx, which silently drops text
  inside tracked changes). Parts are found by relationship and content type, not by path;
  strict and transitional OOXML are both read; XML is parsed with a hardened parser
  (no DTDs, no external entities) and size-capped, because documents come from other people.
- **Keeps the whole document.** Text is one *union stream* per part holding inserted **and**
  deleted text, with each span's stack of enclosing revisions. A **view** is a mask over it:
  `accepted` (changes applied), `original` (changes rejected) and `superseded` (inserted then
  deleted). Nothing is thrown away, so "was this exclusion deleted?" is answerable.
- **Structures it**: nodes (paragraph, heading, list item, table, row, cell, header, footer,
  footnote) with stable ids, numbering labels, a section tree, and **chunks** (heading tree
  plus a size cap; list runs and tables kept whole).
- **Extracts comments and revisions**: each comment's anchor range, author, date, text and
  thread (`commentsExtended`), and each revision's kind, author, date and move group.
- **Flags your terms.** A term registry of groups (a canonical phrase, curated synonyms,
  optional stemming) is matched deterministically over every part in each view. A phrase
  matches whole tokens only, under either reading of a hyphen (`hold-harmless` =
  `hold harmless`, `non-compliance` = `noncompliance`), never across a paragraph, and hits
  are labelled `exact`, `synonym` or `stem`. Moves are two hits that fold at query time.
- **Stores everything content-addressed** and reproducibly: re-ingesting the same bytes is a
  no-op, two stores from the same document are byte-identical, and a hit can be reproduced with
  the `.docx` gone.
- **Records what it could not do.** Constructs it does not model (text boxes, tracked
  formatting, deleted paragraph marks, and others) are reported as named *known gaps*
  instead of being silently dropped.

## Install

Python 3.11+. From the repository root:

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e docextract-core -e ".[dev]"
```

Both packages are installed together because `word-extract` depends on the sibling
`docextract-core` (hashing, the strict JSON codec, archives and the LLM-client protocol).
The `dev` extra adds pytest and python-docx (only the fixture generators use it).

```bash
python -m pytest -q          # about 40 seconds
```

## Quick start

### 1. A term list

A term list is canonical JSON. Write one from Python:

```python
from wordextract.model import TermGroup
from wordextract.terms import TermRegistry, dump_registry

registry = TermRegistry(groups=[
    TermGroup(canonical="waiver of subrogation",
              synonyms=["right of recovery", "waive subrogation"]),
    TermGroup(canonical="exclusion", stemming="porter"),   # opt in to stemming per group
])
open("terms.json", "w").write(dump_registry(registry) + "\n")
```

An example lives at `fixtures/terms/synthetic.example.json` (synthetic only: real term lists
are not committed). **Stemming is off unless a group asks for it**, because Porter merges
words that name different things (`universe`, `university` and `universal` are one stem).

### 2. The command line

```bash
python -m wordextract ingest contract.docx --terms terms.json --store .wordextract
python -m wordextract hits   contract.docx --terms terms.json --store .wordextract
```

`ingest` parses, chunks, matches and stores the document and prints the run record (JSON)
showing which artifacts were already stored (`hit`) or built (`miss`). `hits` prints the
stored term hits. Both write canonical JSON to stdout and diagnostics to stderr; exit code 2
means the run could not start. `--view accepted|original|superseded` chooses the view the
chunks are cut from (default `accepted`).

### 3. The Python API

```python
from wordextract import opc
from wordextract.walker import walk_document
from wordextract.chunker import chunk
from wordextract.store import body_part_id
from wordextract.terms import compile_registry, match_document

parsed = walk_document(opc.Package("contract.docx"))
parsed.nodes, parsed.sections, parsed.comments, parsed.revisions   # the structure
parsed.known_gaps                                                  # what was not modelled

chunks = chunk(parsed, body_part_id(parsed))                       # retrieval units
hits = match_document(compile_registry(registry), parsed)          # term hits, all views
```

To run the whole stored pipeline from code, use `wordextract.pipeline.run(...)`, then
`stored_parse(...)` and `stored_hits(...)` to read the artifacts back from the store.

### Reading a result

- A **`TermHit`** carries the group, `match_type`, the `present_in` views, `spans` (union
  addresses, **one per retained run** so a citation never points at deleted text),
  `view_spans` (offsets in each view), the `node_id`, and a `LocationKind` (body, table
  cell, header, footer, footnote, endnote, comment). Comment hits are view-less and named
  `comment:<id>`.
- A hit in text that is deleted in the accepted view appears only in the `original` view; a
  moved passage yields one hit at the source and one at the destination (fold them with
  `terms.dedupe_moves`).
- **Views are always explicit.** There is no silent "accepted only".

## How it is organized

```
wordextract/
  opc.py        read-only OPC reader (zip, rels, content types), hardened XML
  walker.py     union streams, nodes, ids, revisions, comments, threading, known gaps
  styles.py     numbering and outline level from styles.xml (basedOn chain)
  headings.py   ordered heading rules (style, outlineLvl, outline-numbering, bold)
  sections.py   the section tree and each node's section path
  views.py      accepted / original / superseded projections and the offset map
  chunker.py    chunks: heading tree + size cap, list runs, atomic tables
  terms.py      registry, normalization, matcher, locations, comments, moves
  stem.py       vendored Porter stemmer (pinned, tested against an independent one)
  store.py      content-addressed store, run record, idempotent ingest
  pipeline.py   one way into a run (the CLI and the evals use it)
  cli.py        `python -m wordextract ingest|hits`
  evals/        L1 exact oracle, L2 must-find scoring, the metrics table
  versions.py   every version constant a store key trusts
  model.py      the frozen record contracts
docextract-core/    shared substrate: hashing, strict codec, archives, Collection, LLM protocol
tools/              fixture generators, the behavior ledger, the cross-paragraph report
fixtures/           synthetic .docx files with hand-typed ground-truth sidecars
docs/design/        the design, every build spec, the gaps and the open inputs
tests/              the suite, including the behavior-ledger guard
```

## Evals and guards

```bash
python -m wordextract.evals                       # L1 / L2 / L3 metrics table (JSON)
python tools/update_behavior_ledger.py --check    # has behavior changed without a version bump?
python tools/crossparagraph_report.py             # how often do terms straddle a paragraph break?
```

- **L1** re-parses every fixture and compares every fact its hand-typed sidecar asserts
  (comments, revisions, sections, paragraph text and view strings, tables) plus a tiling
  check. It is gated at 100%; an L1 that compared nothing **fails**.
- **L2** scores term recall against a *human-labelled* must-find list. With no labels it
  reports "not evaluated" (a documented conditional state, not a pass).
- The **behavior ledger** (`tests/ledger/`) fingerprints each component's output on the
  fixture corpus, keyed by the version constants the store uses. Changing behavior without
  bumping its version (or adding a contract field without a schema bump) fails a test.

## Design principles

1. **A default view is a projection, never the content.** Every record names the projection
   and the inputs it depends on.
2. **Deterministic first.** Parsing, structure, matching and storage use no model. A model, when
   added, only writes summary prose; it never produces facts the deterministic layer owns
   (terms, revisions, comment authorship).
3. **Unknown is recorded as unknown.** Gaps are named, thread status is tri-state
   (`verified` / `absent` / `unknown`), and absent is never read as "no replies".
4. **No confidence scores.** Rules fire or they do not and say which. A fuzzy match is never a hit.
5. **Reproducible.** Same bytes and inputs give byte-identical artifacts; a cache hit is
   verified by recomputing its key.

## Documentation

All under `docs/design/`:

| File | What it is |
|---|---|
| `word-extraction-design.md` | the agreed design (D1 to D12) |
| `text-model-spec.md` | the union stream, views and gap-closing, precisely |
| `phase1-gaps.md` | every known gap, with its id |
| `open-inputs.md` | what only the owner can supply |
| `phase1-build-spec.md`, `phase2-build-spec.md` | the turn-by-turn build specs |
| `phase1-spec-review.md`, `opc-spike.md` | the review and the sizing spike behind them |

## Open inputs

Nothing blocks building, but these change what can be *claimed* (full detail in
`docs/design/open-inputs.md`):

- **Real term lists**: not currently available. The matcher is validated against a synthetic
  registry only.
- **A Word-saved document** (a comment reply, a resolved comment and a tracked change, saved
  by real Word into `fixtures/real/`): without it, `w14:paraId` identity, comment threading,
  numbering labels and strict namespaces are checked only against files this repository wrote.
- **Human labels** for the sample document and a must-find term list, so L2 can be scored.

## Fixtures

Fixtures are synthetic and generated reproducibly (pinned timestamps), and their ground truth
is **hand-typed in the generators**, never read back from the parser:

```bash
python tools/make_fixtures.py                    # fixtures/*.docx + sidecars (needs python-docx)
python tools/make_revision_fixtures.py           # fixtures/model/ (raw OOXML)
python tools/make_spec_fixtures.py               # the threaded-comments fixture
```

`fixtures/real/` is git-ignored and local to a machine; put real documents there. The
fixture corpus is versioned for the ledger (`tests/ledger/corpus.json`): after adding a
fixture run `python tools/update_behavior_ledger.py --new-corpus`.

## License

Licensed under the **Apache License, Version 2.0**; see [`LICENSE`](LICENSE) and
[`NOTICE`](NOTICE). Copyright 2026 Ivan Ortega.

The test fixtures are synthetic. Do not commit any employer's or client's real documents, term
lists or labels to this repository; keep them outside it (see `docs/design/open-inputs.md`).
