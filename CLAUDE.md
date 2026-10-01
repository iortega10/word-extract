# word-extract: guide for Claude

Deterministic extraction of `.docx` structure, comments, tracked changes and term hits, plus
(in progress) summaries and an MCP query layer. Sibling of `../form-extract`. Read `README.md`
first for what it does; this file is what you need to **work on it safely**.

Two skills in `.claude/skills/` go deeper: `using-word-extract` (running the package and
interpreting results) and `working-on-word-extract` (the contributor and validation workflow).

## Where things are

- `wordextract/` the package; `docextract-core/` the shared substrate (hashing, strict codec,
  archives, `Collection`, LLM protocol). Layout table is in `README.md`.
- `docs/design/`: `word-extraction-design.md` (D1 to D12, the source of truth),
  `text-model-spec.md` (union stream and views), `phase1-build-spec.md` and
  `phase2-build-spec.md` (the turn-by-turn specs), `phase1-gaps.md` (every known-gap id),
  `open-inputs.md` (what only the owner can supply).
- `fixtures/`: synthetic `.docx` files with **hand-typed** ground-truth sidecars
  (`*.expected.json`). `fixtures/real/` is git-ignored and machine-local.
- `tools/`: fixture generators, `behavior_ledger.py` / `update_behavior_ledger.py`,
  `crossparagraph_report.py`, `make_stem_vectors.py` (needs nltk, dev-time only).

## Commands

```bash
pip install -e docextract-core -e ".[dev]"      # clean install (lxml and python-docx come with it)
python -m pytest -q                              # whole suite, ~40 s; needs no install (pytest pythonpath)
python -m wordextract ingest|hits DOC --terms T.json [--store DIR] [--view V]
python -m wordextract.evals [--fixtures DIR] [--out FILE]     # L1/L2 metrics; exit 1 = a gate failed
python tools/update_behavior_ledger.py --check   # exit 1 = behavior changed without a version bump
python tools/update_behavior_ledger.py [--new-corpus]          # record ledger lines
```

Outside pytest the packages must be importable: either `pip install -e` as above, or set
`PYTHONPATH="docextract-core;."` (use `:` on POSIX).

## Status (update when it changes)

- **Phase 1 is built and validated** (opc, walker, styles, headings, sections, views, chunker,
  terms with a vendored Porter stemmer, store, pipeline, CLI, L1/L2 evals, behavior ledger).
  Closed **conditionally**: L2 "not evaluated" (no human labels, no real term lists), 0 of 25
  producer-verified paths (no Word-saved document), FTS5 deferred (not needed at ~25 documents).
- **Phase 2** (summaries, `pending_changes`, search, MCP tools) is specified in
  `docs/design/phase2-build-spec.md` (revision 2). **Turn 0e is done** (the v2/v3 fixture pair);
  Turn 0a to 0d (read-only store, `chunk_text`, union-markup renderer, section helper, document
  catalog) are next. No LLM code exists yet.

## Rules that are easy to break (each was a real defect once)

1. **Never edit a hand-typed fixture literal to make a test pass.** Ground truth lives in the
   generator source and is never read back from the parser. If a literal contradicts the spec,
   say so explicitly in the commit and the report.
2. **Behavior change means version bump.** Store keys trust `versions.py` constants. Any change to
   what a component outputs bumps its constant **and** records a ledger line in the same commit
   (`tests/test_behavior_ledger*.py` fail otherwise).
3. **Contract change means schema bump.** Adding or changing a record field bumps
   `docextract_core.codec.SCHEMA_VERSION` and `MIN_SUPPORTED_VERSION` (the codec rejects older
   blobs rather than default new fields). The ledger's `contracts` fingerprint enforces it.
4. **Growing the fixtures is not a behavior change.** Add a corpus version with
   `update_behavior_ledger.py --new-corpus`; do not bump component versions.
5. **No python-docx in the library** (it drops tracked-change text); lxml only. python-docx is for
   `tools/make_fixtures.py` and tests.
6. **Never match the raw union.** Match view text, per paragraph. Hit `spans` use
   `Projection.union_spans` (one per retained run), never `union_range`, which covers elided text.
7. **No confidence scalars, no fuzzy hits.** A hit is exact, synonym or stem.
8. **Unknown is recorded as unknown**: named known-gap ids (`docs/design/phase1-gaps.md`), tri-state
   threading, never "no replies" from an absent part. Do not silently drop what is not modelled.
9. **Downstream store keys include the parse key.** Chunks and hits are derived from the stored
   parse. Phase 2 summaries must key on the hash of the **rendered input**, never on
   `versions.compute_key` or a view's `content_hash`.
10. **The empty case must fail loudly where the corpus is committed** (an L1 that compares nothing
    is a failure). "Not evaluated" is only for genuinely absent human input (L2).

## Working conventions

- The owner runs **hearth** (another model) to build one spec turn at a time and tells you when a
  turn is done. Your job is to **validate adversarially** (run the suite, then probe with crafted
  documents and an independent reference), report findings with a proposed fix, and fix and commit
  when asked.
- **Commit with explicit paths** (`git add <files>`, never `-A`): hearth may have unreviewed files in
  the same tree. Commit hearth's work and your fixes as separate commits when you can.
- Do not commit unless asked. Never invent real term lists, labels or documents; the fallback is the
  synthetic fixtures plus an honest "unverified" marking.
- Windows: shell heredocs have mangled `\n` and `\t` inside test files; write such files with the
  file tools. Do not write temp files to a drive root. Scratch space belongs in the session's
  scratchpad, not the repository.
- A one-off full test run took 13 minutes once (machine contention, not a regression); a normal run is
  about 40 seconds.

## License and data hygiene

- Licensed **Apache-2.0**, copyright Ivan Ortega (`LICENSE`, `NOTICE`). Keep `NOTICE` current when a
  third-party component is vendored or a dependency is added.
- **No employer's or client's confidential material in the repository, ever**: real documents, term
  lists, checklists, labels or carrier forms. Fixtures are synthetic. Real inputs live outside the repo.
- Real term lists should be built from **public sources** (ISO policy-form language, NAIC model laws,
  public regulations) or supplied by the owner with permission. Do not reconstruct a former
  employer's curated vocabularies.
- Ownership questions (employer IP claims) are the owner's to settle with their agreement and, if
  needed, an attorney. Do not make legal claims about them in code, docs or commits.

## Don't

- Don't build FTS5 or embeddings (deferred / Phase 3), or any LLM code outside the Phase 2 spec.
- Don't add `mcp` as a hard dependency; it is an optional extra (Phase 2, Turn 6).
- Don't weaken a gate to get green. Fix the code, or fix a label only with a spec-based reason.
