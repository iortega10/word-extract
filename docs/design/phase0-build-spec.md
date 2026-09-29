# Phase 0 build spec: contracts, core, fixtures, harness (no parser)

Source of truth: `docs/design/word-extraction-design.md` (read it fully first,
especially D2, D9, D10, D11, "Phase 0", and Open risk #1). This file turns
Phase 0 into a build order. If they conflict, the design doc wins; report the
conflict instead of resolving it silently.

Reference implementation to adapt (read-only, do not modify):
`C:\Users\ivan_\App_repos\form-extract\formextract\{model.py,store.py}`.

## Ground rules

- **Scope: Phase 0 only.** No OPC reader, walker, chunker, term matcher,
  summarizer, or FTS5 index. Those are Phase 1+.
- Python 3.11+ (3.14 installed). python-docx 1.2 is available. Check lxml and
  pytest before installing anything.
- Style matches form-extract: dataclasses, type hints, small modules, minimal
  comments.
- Do not change the content of existing fixtures. Regenerating them must be
  reproducible.
- Never commit. Report the diff and the test output.
- Every turn ends with a green `pytest` run for the package touched.

## Layout to create

```
word-extract/
  pyproject.toml                  # wordextract; depends on docextract-core by path
  docextract-core/
    pyproject.toml                # importable as docextract_core
    docextract_core/
      __init__.py                 # CORE_VERSION, git-rev helper
      hashing.py                  # content_hash (sha256)
      codec.py                    # encode/decode/to_json/from_json, strict mode, schema_version
      jsonio.py                   # read_json / write_json
      archive.py                  # archive_raw, archive_llm_call, LLMCall
      llm.py                      # LLMClient protocol, LLMResponse
      collection.py               # Collection[T] (provisional)
    tests/
  wordextract/
    __init__.py
    model.py                      # frozen contracts
    versions.py                   # version constants + key computation
    evals/
      __init__.py
      __main__.py
      harness.py
      labels.py                   # loads sidecars independently of any implementation
  tools/
    make_fixtures.py              # extended: emits expected.json sidecars
    make_spec_fixtures.py         # new: hand-built commentsExtended fixture
  fixtures/
    *.docx, *.expected.json
    real/README.md                # placeholder; contents gitignored
  docs/design/text-model-spec.md
  tests/
```

## Turn 1: `docextract-core`

Adapt from form-extract, generalizing away form-specific types.

1. `hashing.content_hash` from store.py:23-32.
2. `codec`: dataclass to JSON and back (model.py:383-431), with two changes.
   - **Strict mode** that raises on unknown keys. Strict is the default;
     lenient must be opted into explicitly. This closes Open risk #1
     (model.py:401 silently drops unknown keys, which can cause false cache
     hits under version skew).
   - Every encoded top-level blob carries `schema_version`. Decoding a blob
     with a newer schema_version than the reader supports raises.
   - Collapse the duplicate flag set (store.py:35-43 vs model.py:426-427)
     into one `jsonio`.
3. `archive.archive_raw` (store.py:63-86); `archive.archive_llm_call` and
   `LLMCall` (store.py:139-166, model.py:267-277).
4. `llm.LLMClient` protocol: `complete(prompt, *, model, params) -> LLMResponse`.
5. `collection.Collection[T]`: generalizes store.py:88-137. Mark the class
   provisional in its docstring (generalized from one consumer's three methods).
6. `CORE_VERSION` plus a git-revision helper that returns None outside a repo.

Tests (`docextract-core/tests`):
- codec round-trips nested dataclasses, enums, optionals, lists.
- strict mode raises on an extra key; lenient mode does not.
- a blob with a future schema_version raises.
- archives are content-addressed and idempotent (same bytes, same path, no rewrite).
- **Contract test**: build form-extract's instance/template/batch record
  shapes and replay them through `Collection` and the archives. Skip
  gracefully (as `form-extract/tests/test_real_samples.py` does) if
  `formextract` is not importable.

## Turn 2: contracts, versions, text-model spec

**`wordextract/model.py`**: frozen dataclasses, encoded through the core codec.
Fields come from D2 to D8; do not invent extras.

- `Node`: id, kind (heading, para, list_item, table, row, cell, header,
  footer, footnote, sdt), part_id, source_ref, style, level, numbering label,
  child ids, id_stability flag (`paraid | content_hash | path`), and spans
  in union coordinates.
- `Revision`: id, kind (ins, del, moveFrom, moveTo), author, date (explicit
  None when absent), `move_group_id`, and the ancestor stack
  (`[revision_id, ...]` outer to inner).
- `Comment`: keyed on `w14:paraId` of the last paragraph, raw author and
  initials, optional resolved identity, date (explicit None when absent),
  anchor range in union coordinates plus anchored text, `parent_id`,
  `threading_status: verified | absent | unknown`, `resolved: bool | None`.
- `Chunk`: content-derived id, context hash, section path, node ids,
  `view_id`, `textmodel_version`, size, `heading_detection` (normal | degraded),
  fired and disputed heading rules (no confidence scalar).
- `TermGroup`: canonical, synonyms, stemming and rule config, opaque tags.
  `TermHit`: group, `present_in` (set of accepted, original, superseded),
  `spans` in union coordinates, contiguous `view_span`, node_id, location kind,
  match type (exact | synonym | stem). **Fuzzy is not a match type.**
- `RunRecord`: the D10 (A) hashed inputs, (B) recorded-only inputs, and
  per-artifact cache hit/miss with the recomputed input tuple.
- Every label-carrying record has `labels_provenance: human | generator | spec`.

**`wordextract/versions.py`**: version constants (`textmodel_version`,
`heading_ruleset_version`, `chunker_version`, `matcher_version`,
`summarizer_version`, `output_schema_version`, and so on) and
`compute_key(inputs: HashedInputs) -> str`. `HashedInputs` is exactly D10 (A).
`view_id` is always a member.

**`docs/design/text-model-spec.md`**: precise enough to implement Phase 1 from.
- per-part union stream, address = (part_id, offset), ancestor stacks;
- view masks (`accepted`, `original`, `superseded`) with exact definitions;
- moveTo/moveFrom classification and `move_group_id`;
- the within-paragraph gap-closing rule, and that it never closes across a
  paragraph boundary;
- **worked examples** at minimum: `right of [del recovery][ins subrogation]`;
  a `w:del` nested inside a `w:ins`; a move; a comment range that starts in a
  deleted run; text inside `w:hyperlink` and a field result.
- explicit "unspecified" list for Open risk #2 (table cells, footnotes,
  fields) so the gap is visible, not implied.

Tests:
- every contract round-trips through the strict codec.
- **flip-one-input**: changing any single member of `HashedInputs` changes
  the key; identical inputs give an identical key.
- `view_id` participates in the key.
- fuzzy is not a valid `TermHit.match_type`.

## Turn 3: fixtures and sidecars

Extend `tools/make_fixtures.py` (existing fixture content unchanged) to emit
one `<name>.expected.json` per fixture:
- comment ranges, anchor text, authors and initials;
- revision spans (kind, author, text);
- section outline (heading text, level, order);
- table shapes (rows, columns, merged regions).
- Every sidecar sets `labels_provenance: "generator"`.

New `tools/make_spec_fixtures.py`: build a raw-OOXML `.docx` (zipfile, not
python-docx) containing `word/commentsExtended.xml` with a threaded reply and
a resolved flag, wired via relationships and content types. Its sidecar sets
`labels_provenance: "spec"`; tests reading it are tagged `@spec_derived`.

`fixtures/real/README.md`: a placeholder explaining that a real
Word-authored doc with a comment reply and a tracked change is needed,
contents gitignored. This is **blocked on the user**; leave a documented TODO,
not a fake.

Tests: each sidecar is verified against the `.docx` XML directly with
`zipfile` + `lxml`, not python-docx (python-docx drops text inside `w:ins`).

## Turn 4: eval harness skeleton

- `evals/labels.py` loads sidecars with no import of any parser code.
- `evals/harness.py` emits a well-formed but empty metrics table, JSON:
  - **L1** parse/perception: gated at 100% (empty for now);
  - **L2** terms: gated on recall = 1.0 (empty);
  - **L3** summaries: reported, never gated;
  - cost metrics kept separate from quality;
  - a `producer_verified_coverage` field.
- CLI: `python -m wordextract.evals`.

## Turn 5: exit check

Phase 0 exit criteria (design doc):
- contracts round-trip;
- flip-one-input green;
- sidecars match fixtures;
- harness emits an empty metrics table;
- **not met by design**: real producer doc (blocked on the user).

Final report: files created, full pytest output for both packages, any
ambiguity in the design you resolved and how, and anything you could not
do.

## Things to watch (from the design's open risks)

- Do not let the codec fall back to silent key-dropping anywhere.
- Do not import python-docx inside `wordextract/` or `docextract-core/`. It
  is allowed only in `tools/` and tests.
- Do not add any confidence scalar to heading rules or term hits.
- Anything unspecified in D2 stays unspecified in the spec doc, listed
  explicitly, rather than being guessed.
