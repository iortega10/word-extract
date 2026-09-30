# Phase 1 build spec: deterministic parse, structure, terms (no LLM)

Revision 2. Incorporates `docs/design/phase1-spec-review.md` (adversarial debate
with hearth, session `s7716a420`) and the user's decisions below. Read the
review for the reasoning behind each change.

Source of truth: `docs/design/word-extraction-design.md` (D1 to D11, "Phase 1",
Open risks) and `docs/design/text-model-spec.md`. If this file conflicts with
either, they win; report the conflict, do not resolve it silently. Where this
file adds text-model decisions (section "Text-model decisions"), Turn 0.5 also
folds them into `text-model-spec.md` so there is one source.

Phase 0 is committed (`24848ee`): `docextract-core`, contracts in
`wordextract/model.py`, `versions.py`, sidecars, harness skeleton. Read those
before writing anything and reuse them.

## Decisions made by the user

1. **FTS5** stays in Phase 1 as the last, minimal turn. **L2**: build only the
   loader, format and `must_find.example.json`; scoring runs when human labels
   exist and otherwise reports "not evaluated". L2 scorer is otherwise deferred.
2. **The underwriting-sample sidecar is labeled by a human**
   (`labels_provenance: human`). Never generate or edit it. Until it exists,
   claims about that file (6 headings -- the Title and 5 Heading 1 -- range starts 0,2,1,3,4,5, part
   resolution) are **unenforced**, and the final report must say so.
3. **Deleted paragraph marks**: a loud known gap in Phase 1 (`paragraph_mark_revision`),
   not honored. Revisit with a Word-produced doc.
4. **Moves**: two `TermHit` records (one per location) sharing `move_group_id`,
   deduped at query time. This deviates from the wording "one hit, two
   locations" in text-model-spec section 5 and design D4; Turn 0.5 amends both.
5. **L2 precision**: every false positive must be classified (union artifact /
   stem overreach / synonym gap). Exit gate is recall = 1.0 **and** zero
   unclassified false positives, once labels exist.
6. **Text boxes**: `Span.part_id` stays a real OPC part. Add
   `Span.fragment_id: str | None` (text box fragment: host node id + ordinal)
   and a `host_node_id`. Chunk assembly order for box text is
   (host node id, ordinal), stated in the spec and tested.
7. **Word-produced document**: user-supplied. Make it from
   `fixtures/samples/review_sample.docx`
   (open in Word, reply to two comments, resolve one, Track Changes on, edit a
   clause, save into `fixtures/real/`). Until it exists, `commentsExtended`,
   `w14:paraId` identity and strict namespaces stay recorded as **unverified**.

## Ground rules

- **No LLM anywhere in Phase 1.** No summarizer, embeddings, or network.
- **No python-docx in `wordextract/` or `docextract-core/`.** lxml + zipfile
  only. python-docx stays in `tools/` and tests.
- **No confidence scalars.** Heading rules record which fired, the winner and
  disagreements. Term hits are deterministic facts.
- **Fuzzy is not a hit.** No fuzzy match type; candidates are Phase 3.
- **Determinism is a hard requirement.** Same bytes give identical tree, ids,
  chunks, hits, **byte for byte**. No wall clock, dict order, set order or
  frozenset order in any output; sort explicitly (the codec sorts sets but not
  dicts or frozensets).
- **Unspecified constructs** are handled per "Text-model decisions" below. Any
  case still not covered fails visibly: recorded in `known_gaps`, listed in
  `docs/design/phase1-gaps.md`. `known_gaps` ids must equal the doc's ids,
  asserted by a test.
- **Sidecar independence.** Literals in model sidecars are hand-typed in
  generator source, each tagged with the text-model-spec clause it encodes.
  They are never computed by any extractor, never edited to match output.
- Style matches Phase 0. Never commit; report the diff and full test output.
- Each turn ends with green `pytest` for everything touched, run with no
  `PYTHONPATH` set. Turns known to be large (0.5, 2a, 2b, 6c) may be split
  into separate, individually green commits-worth of work; report each part.

## Turn order

0a fixtures + literals; **0b spike, report, STOP**; 0.5 contracts;
1 OPC; 2a-2e walker; 3 views; 4a-4b headings; 5 chunker; 6a-6e terms;
7 store/run record; 8 FTS5; 9 CLI + L1 + gaps + exit check.

## Layout to create

```
wordextract/
  opc.py  walker.py  views.py  headings.py  sections.py  chunker.py
  terms.py  stem.py (vendored Porter, attributed)  store.py  index.py
  pipeline.py  cli.py
  evals/            # extend labels.py, harness.py; L1 scoring; L2 loader
tools/
  make_revision_fixtures.py     # Turn 0a
  crossparagraph_report.py      # Turn 6e, report only
docs/design/
  opc-spike.md  phase1-gaps.md
tests/
```

## Turn 0a: model fixture package (before any parser code)

New `tools/make_revision_fixtures.py`, raw OOXML via zipfile. python-docx
cannot author any of these. Produce one fixture per construct, or combined
where noted:

- nested `w:del` inside `w:ins` (stack depth 2), and `w:ins` inside `w:del`;
- a move (`moveFrom` / `moveTo` with a shared name);
- `w:hyperlink`; a nested complex field (`fldChar` begin/separate/end) and a
  `w:fldSimple`;
- inline and block `w:sdt`;
- `w:tab`, `w:br` (all types), `w:softHyphen`, `w:noBreakHyphen`, `w:sym`;
- a comment whose range **starts in a deleted run**;
- a **deleted paragraph mark** (`w:pPr/w:rPr/w:del`);
- `w14:paraId` present on some paragraphs and absent on others, in one body;
- a strict-namespace variant; a renamed `commentsExtended`; an empty part;
- a text box (`mc:AlternateContent` with Choice + Fallback).

Each fixture gets a sidecar with `labels_provenance: "spec"` containing
**hand-typed literals in the generator source**: union offsets, ancestor
stacks by revision id, and the accepted / original / superseded strings, each
tagged with the text-model-spec clause (A to E or section 8) it encodes.
`labels.py` and the sidecar-count assertions are updated in Turn 0.5, not here;
0a writes the files only.

## Turn 0b: OPC + walker spike, report, STOP

Sizing is a **cost gate** (design D1, Open risk #6). Build the smallest
scaffold that reads the 0a package, the existing spec fixtures, the
underwriting sample, `program_review_v3.docx` and `edge_cases.docx`, and prints
per-paragraph union text with ancestor stacks and comment anchors. It is a
scaffold that Turn 2a rewrites against the revised contracts, **not production
code**; contracts are not frozen by it.

Write `docs/design/opc-spike.md` with: `wc -l` per module and total; every
producer-variance site (relationship resolution, renamed/absent parts, strict
vs transitional namespaces) and its cost; which constructs were implemented vs
deferred; agreement with the 0a literals; a projected Phase 1 total with the
coverage ratio stated; and an explicit **proceed / re-slice / descope**
recommendation against the ~150-line expectation. State "sized, validated only
against repo-authored literals", never "validated".

**Stop after 0b and report. Do not start 0.5 in the same run.** The user decides.

## Spike findings (Turn 0b result; required inputs to Turn 0.5 and Turn 1)

From `docs/design/opc-spike.md` (sized, validated only against repo-authored
literals; 12 of 12 agree). Decision: **proceed**, not re-slice or descope.
Estimate raised to roughly 2,900 production lines plus a vendored stemmer and
tests; the spike's 402 lines exceeded the ~150 expectation because of the
producer-variance sites below, each load-bearing.

Freeze these:

1. **Span rule.** A span is a **maximal run of equal ancestor stack**;
   `w:tab`, `w:br` and `w:sym` do **not** split it. The spike derived this from
   the literals; it is not yet in `text-model-spec.md`. Amend the spec in Turn
   0.5 and keep the sidecars' `span_rule` in agreement.
2. **Relationship canonicalization.** Strict OOXML writes relationship types
   under `http://purl.oclc.org/ooxml/...` and strict namespaces differ from
   transitional ones for elements **and attributes**. Canonicalize
   relationship types at every comparison and match namespaces against a set,
   reading attributes by local name. **A reader that skips this silently
   returns an empty document**, so the strict fixture is a required test, and
   an unresolvable officeDocument relationship must raise, never yield zero
   paragraphs.
3. **Relative-target normalization** for `.`, `..` and leading `/`.
   `program_review_v3.docx` has a `../customXml/item1.xml` target; without
   normalization the part is dropped with no diagnostic. Test it.
4. **Revision ids** are `<kind>:<w:id>`; **move group ids** come from the
   `w:name` on the `moveFromRangeStart` / `moveToRangeStart` markers that wrap
   the move, not from the move element (needs a tracking stack).
5. **Literal `
` / `
` inside `w:t`** map to `U+000B`, like `w:br` / `w:cr`.

Also carried forward:

- **paraId caveat.** Every non-authored package we have is generator output; the
  spike shows generator documents lack `w14:paraId`, not that Word documents do.
  Treat the content-hash id path as needed either way, but do **not** describe
  paraId as "absent in the wild" anywhere. That claim is decided by a
  Word-produced document.
- "Part exists" must not mean "has content" (the sample's footnotes/endnotes
  are separator-only stubs with their own `.rels`); text-box `Choice` is
  preferred so boxes are not counted twice.
- Cut order if size must come out of Phase 1: FTS5 and field-kind
  classification and the 6e report first. **Never** cut the OPC reader's
  strict handling or target normalization.
- Turn 0.5 lands **on its own** (largest diff, schema hub) and is validated
  before Turn 1 starts.

## Turn 0.5: contract revision (one SCHEMA_VERSION bump)

Land together so the schema bumps once. Strict-codec round-trip tests for every
new or changed record, plus a test that an old-version blob is rejected.

- **`UnionStream`** (per part): literal text plus elementary spans
  `(start, end, ancestor revision ids)`. **Paragraph terminators are
  elementary spans** (`"\n"`, empty stack) so spans tile the union exactly.
  `Node.spans` for a paragraph excludes its terminator. View text and offset
  maps are **derived on demand** from the stored UnionStream (no stored
  `ViewText` record).
- **`ParseResult`** with `known_gaps` (ids) and heading decisions per node:
  fired rules, **winner**, disputed rules (`Chunk` has no winner field today).
- **`Chunk`**: add `content_hash`; `Chunk.id = hash(canonical(content_hash,
  occurrence_index))` (canonical encoding, no naive concatenation).
  `section_path` is a field, **never part of any key**. Store key is
  `(source_content_hash, chunk_id)`.
- **`Node`**: occurrence ordinal for the content-hash fallback id. **`Span`**:
  add `fragment_id`. Add `host_node_id` for text-box fragments.
- **`Comment`**: add `id_stability`. Fallback id when no `w14:paraId`:
  `hash:` of canonical (author, date, text, ordinal among equal hashes in
  document order), nulls encoded deterministically. **Do not fold the anchor
  start into the id** (it is a union offset and would churn with
  `textmodel_version`).
- **`TermHit`**: replace `view_span` with `view_spans: list[ViewSpan(view,
  start, end)]` sorted by view, offsets in the whole-part view projection
  (terminators kept); `present_in` is derivable and must not disagree. Add
  `move_group_id` and `context_node_id`. `LocationKind` gains `COMMENT`,
  `ENDNOTE`, `TEXTBOX`.
- **`labels.py`**: extend for the hand-typed model sidecars under the existing
  no-implementation-import rule. `sidecar_from_dict` is strict and
  `iter_sidecars` globs everything, so update the assertions that count
  sidecars (`tests/test_evals.py`, `tests/test_fixtures_sidecars.py`).
- **Harness**: `producer_verified` becomes a per-path tally, not a hardcoded
  scalar. A producer-verified path is one exercised by a Microsoft
  Word-produced document, tagged `@producer_verified`. Denominator stays
  `fixtures/real`.
- **Docs**: add view projection and gap-closing to the `textmodel_version`
  bundle in `text-model-spec.md` section 7, record "paragraph-mark revisions
  are not honored", replace section 5's "one hit, two locations" with the
  two-hits-plus-dedupe rule, and merge the "Text-model decisions" below into
  the spec so it is one source.

## Text-model decisions (made now; small-context builders must not improvise)

- **Union text**: one `"\n"` terminator span per paragraph. `w:tab` = `"\t"`.
  Every `w:br` type and any literal `\n`/`\r` inside `w:t` = `U+000B` (content
  inside the paragraph; a phrase can cross a page break, documented).
  `w:noBreakHyphen` = `U+2011`, `w:softHyphen` = `U+00AD`, `w:sym` = its
  character. `w:instrText` is never content. `w:t` and `w:delText` are both content.
- **Fields**: excluded via a `fldChar` depth counter (nested fields);
  `w:fldSimple` instruction is the attribute. Results carry whatever revision
  ancestry encloses them; no special masking.
- **Comment ranges** clamp to exclude terminators. A multi-paragraph
  `anchor_text` contains `"\n"` (tests expect it). A `commentReference` with no
  matching range yields `anchor = None`, recorded in `known_gaps`, never silent.
- **Per-kind span rule** (asserted by test): paragraph, heading, list_item,
  cell = exactly one contiguous span; table, row, block-sdt = empty spans with
  `child_ids`. `NodeKind.SDT` is block-level only; inline sdt is transparent
  (no node, no boundary, no ancestor).
- **Matching unit** is always a `w:p`. Table cells never fuse; each cell
  paragraph is an ordinary paragraph; cell/row/table order is document order.
  Footnotes, endnotes, headers and footers are separate parts; no view spans
  parts. Read every part reachable via `headerReference`, deduplicated by part.
- **Deleted/inserted paragraph mark** (`w:pPr/w:rPr/w:del|ins`): not honored;
  the break is retained in both views. Loud `known_gap:
  paragraph_mark_revision`, affected nodes flagged, fixture included.
- **Text boxes**: exactly one body per anchored drawing, preferring
  `mc:Choice`, then `mc:Fallback`, then a bare `w:txbxContent`. Content lives
  in its own fragment (`Span.fragment_id` = host node id + ordinal), so host
  document order is undisturbed. Recorded as a known gap.
- **Gap-closing keeps no placeholder** (Word itself renders `right of ` +
  accepted `subrogation` as `right ofsubrogation` when the space sat inside
  the deleted run). Tests document that miss, and a `w:br` inside a deleted run.
- **Offline-reproduction closure** (for Turn 7): `UnionStream` + pinned views
  projection + the term registry. "From the stored node tree alone" is wrong.

## Turn 1: OPC reader

- Open the zip; parse `[Content_Types].xml`, `_rels/.rels` and each part's
  relationships.
- **Resolve parts by relationship type and content type, never by hardcoded
  path.** `document.xml` itself is found via the officeDocument relationship.
- Support strict and transitional namespaces for document, styles, numbering,
  comments, commentsExtended, footnotes, endnotes, headers, footers.
  Normalize to one internal namespace map.
- Tolerate directory entries, missing optional parts, empty parts (the
  underwriting sample has an empty footnotes part).
- `part_id` is stable and path-independent (relationship type + ordinal).

Tests: all fixtures including the 0a package and the underwriting sample; a
renamed `commentsExtended`; an absent part yields `None`, not an exception.

## Carried follow-ups (from validating Turns 0.5 and 1)

**Status: items 1-5 fixed and committed (hardened parser with DOCTYPE rejection, zip size caps, codec `MIN_SUPPORTED_VERSION`, `TermHit` consistency check, `Part.has_text`). Only item 6 remains, for Turn 2a.** The original text is kept below for the reasoning.

1. **Hardened XML parser (security).** Documents arrive from outside senders.
   `opc.py` parses with a bare `etree.fromstring`; safety today comes from
   lxml 6.1.3 / libxml2 2.11.9 defaults (external entities not resolved,
   amplification capped), not from our code. Add **one shared parser** used by
   every parse in the package: `resolve_entities=False`, `no_network=True`,
   `huge_tree=False`, no DTD loading. Tests: an XXE fixture (external entity
   pointing at a local file) must not leak its content, and an entity-expansion
   fixture must be rejected. OOXML never legitimately uses a DTD.
2. **Zip size cap.** `Package` reads every member into memory unbounded. Cap total
   uncompressed size and per-member size (configurable, sensible default) and
   raise `OpcError` past it; test with a small over-cap fixture.
3. **Codec minimum version.** The codec accepts older-`schema_version` blobs, so
   a v1 `Node`/`Comment` decodes with new fields silently defaulted
   (`occurrence_index=0`, `id_stability=PATH`). Add `MIN_SUPPORTED_VERSION` in
   `docextract-core`; persisted records must be at or above it; test rejection.
4. **`TermHit` consistency.** `present_in` is derivable from `view_spans` and must
   not disagree, but nothing enforces it. Validate at construction (or through a
   validator the matcher must call) and test the disagreement case.
5. **"Has content" vs "exists".** `Part.empty` does not flag separator-only
   footnote/endnote stubs (they have child elements). The walker must treat
   content and existence separately; add a helper and a test on the underwriting
   sample.
6. **`TEXTMODEL_VERSION`.** Bump to `"2"` when Turn 2a produces the first real
   output, so the first data carries the amended semantics (span rule,
   terminators, projection).

## Turn 2: walker (slices 2a to 2e)

Implement text-model-spec sections 1, 2 and 5 plus "Text-model decisions".
Add a **walker-independent tiling test**: spans tile the union exactly, every
stack id resolves to a revision, masked spans reconstruct the three view
strings from the 0a literals.

- **2a union stream**: per-part elementary spans, offsets, ancestor stacks,
  against the 0a literals. Rewrites the 0b scaffold.
- **2b nodes and ids**: paragraph, heading, list_item, table, row, cell,
  header, footer, footnote, sdt. `source_ref`, `style` (`pStyle` value only,
  **no style cascade**), `level` (`outlineLvl`), numbering label from a small
  counter over `numbering.xml`. Ids: `w14:paraId`, else content hash of own
  union text + kind (+ style, level) plus occurrence ordinal, else positional
  path flagged `IdStability.PATH`. Record which fallback each node used.
  Limitation to record in `phase1-gaps.md`: inserting a duplicate ahead of an
  identical paragraph re-ids the later one, so a v2 to v3 diff shows spurious
  churn there.
- **2c revisions**: ins / del / moveFrom / moveTo with author and date
  (missing date is `None`), nested ancestor stacks, `move_group_id`. **No
  synthesized net change.**
- **2d comments and anchoring**: anchor is a union `Span`; `anchor_text` is the
  union text of that range. A range crossing sections belongs to the section
  where it **starts**. Must work inside a table cell, across runs, and inside
  a deleted run. Anchoring is by document position, not `w:id` order.
- **2e `commentsExtended`**: join key is the `w14:paraId` of the comment's
  **last** paragraph. `threading_status`: `verified` only when the part is
  present **and** the join succeeded; `absent` when the part is missing (and
  therefore always for a paraId-less document, with `parent_id = None`);
  `unknown` otherwise. `resolved` is `None` when not stated. Absent part is
  never "no replies". Tagged `@spec_derived`.

## Turn 3: views

Text-model-spec sections 3 and 4. `accepted`, `original`, `superseded` as masks;
per-paragraph view text with gap-closing **within the paragraph only**, and an
offset map back to union coordinates (for `TermHit.spans` / `view_spans`).
Never match or summarize the union directly.

Tests: every worked example A to E from the spec, reproduced exactly against
the 0a literals; the no-placeholder miss; `w:br` inside a deleted run.

## Turn 4: headings (4a rules, 4b section tree)

Ordered rule list: style, `outlineLvl`, outline-numbering `ilvl`, all-bold
short paragraph. Record **every** rule that fired per paragraph, the winner,
and disagreements. Guards: the `ilvl` rule applies only to outline-numbered
`abstractNum`s and never to list-context paragraphs (`List Number`,
`ListParagraph`); the bold rule requires all runs bold, not in a table cell,
not a list continuation. Fail open: no headings gives one flat root with
`heading_detection: degraded` and a disagreement count; it must still be
size-chunked. 4b builds the section tree and `section_path`.

Tests: sidecar outlines match; list items are never headings; a styleless doc
degrades and still chunks. The underwriting sample's "exactly 6 headings (the Title plus 5 Heading 1; Title is a level-0 heading)" is
asserted **only** once its human sidecar exists.

## Turn 5: chunker v1

- Heading tree plus size cap. Size = `len(view_text)` in Python code points of
  the chunk's own view; member paragraphs joined by `"\n"` (feeds
  `content_hash`). No tokenizer dependency.
- Split long sections at paragraph boundaries. Merge tiny ones up, **except**
  numbered-list runs: a run is a maximal sequence of consecutive `LIST_ITEM`
  nodes of length >= N (N = 2, hashed into `chunker_params_hash`); forced
  boundary before and after; list items merge only with list items.
- Tables are atomic; oversize tables split into row groups with the header row
  repeated (this exercises the `occurrence_index` path; test it).
- A range comment belongs to the chunk where its range **starts**.
- `Chunk.context_hash` additionally includes comment texts and is a
  summary-key input only, so editing or resolving a comment does not re-id.
- Record `fired_rules` / `disputed_rules` and `heading_detection`.

Tests: determinism; comment-only edit keeps `Chunk.id` and changes
`context_hash`; body edit changes `Chunk.id`; **collision case** (identical
paragraphs in two sections get distinct ids and both survive the store); list
not merged into parent; oversize table splits with repeated header. **Churn
measurement** (reported, not gated): chunk ids changed / chunks whose own bytes
changed, for a body edit, a comment-only edit, and resizing a paragraph near
the cap.

## Turn 6: term registry and matcher (6a to 6e)

- **6a registry + exact/synonym + overlap policy.** Persistent, versioned JSON
  canonical (core `Collection`). `term_list_hash` over a canonical
  serialization; `matcher_version` and `term_list_hash` stamped on every run.
  `TermGroup` is generic; `tags` are opaque. Match over normalized **token
  sequences**, never substrings; normalization (case, punctuation,
  whitespace, hyphens) is identical for registry entries and corpus and is part
  of `matcher_version`. Fused seam tokens (`non` + `compliance`) are faithful,
  not false positives. **Overlap precedence**: longest span, then leftmost,
  then exact > synonym > stem; disjoint matches all reported; `match_type`
  resolved before dedupe; dedupe key `(group, node_id, view, start, end)`;
  distinct groups report distinct hits.
- **6b stemmer**: vendor a **pinned Porter with attribution** in `stem.py`;
  its own version inside `matcher_version`; tested against **external
  published vectors**, plus adversarial portmanteau cases
  (`included / including / inclusion`). Do not write your own stemmer or golden
  table (that is the same circularity as the sidecars). Per-group algorithm
  choice lives in the registry and `term_list_hash`.
- **6c locations**: body, table cells, headers/footers, footnotes/endnotes,
  text boxes, and **comments**. Comment hits use `LocationKind.COMMENT`,
  `node_id = comment:<para_id or fallback id>`, `context_node_id` = anchor
  node, `present_in` = empty set (comment hits resolve via the `Comment`
  record, not the node tree).
- **6d moves**: two `TermHit`s (one per location) with `move_group_id`; "one
  hit" is realized by query-time dedupe on `(group, move_group_id, normalized
  text, intra-group ordinal)`.
- **6e cross-paragraph measurement**: `tools/crossparagraph_report.py`,
  report-only, permissive matcher over the golden set; reports phrases it finds
  that the real matcher missed. Not package code; never promoted here.

Tests: `right of recovery` (original) and `right of subrogation` (accepted) are
two hits in one group; the raw-union false adjacency is never a hit; synonym,
stem and overlap cases; hit inside a comment; determinism; one synonym changed
changes `term_list_hash`; an unapproved candidate never affects hits.

## Turn 7: store, run record, idempotency

- Content-addressed canonical JSON: raw archive, `UnionStream` per part, the
  resolved node tree, chunks, hits. Use `docextract_core`; extend core only
  for a real gap, with a test.
- Run record per D10: hashed inputs (A), recorded-only inputs (B: git revs,
  dependency versions, run id), per-artifact hit/miss with the **recomputed**
  key.
- Re-ingest of identical bytes and inputs is a no-op: no rewrites, every
  artifact `hit`.

Tests (D10 C): **byte-level determinism replay** (run twice, diff bytes);
flip one hashed input and the key changes; hits reproducible **offline from
`UnionStream` + views projection + registry** (delete the docx, re-derive,
compare).

## Turn 8: FTS5 (minimal, last)

One FTS5 table over chunk view text, keyed `(chunk_id, view_id)`, plus a
manifest (`index_schema_version`, source record hashes, SQLite and FTS5
versions). Derived and rebuildable: deleting the index and rebuilding
reproduces the manifest and query results. Term matching is never done in FTS5.
Cut from Phase 1: separate comment indexing, rank-time dedupe, any fused score
(ranking is Phase 2). Results are returned with their source and no blended number.

## Turn 9: pipeline, CLI, evals, exit check

- `pipeline.py` orchestrates the above and returns the run record.
- `python -m wordextract ingest <docx> --terms <file>` and `... hits`.
  (`reindex` is dropped with the extra FTS5 scope; keep only if trivial.)
- **L1**: wire the harness to the real parser; score every sidecar (comment
  anchors, authors, revision spans, section outline, table shapes, and the 0a
  literal offsets/stacks/views). Gated at **100%**. Add a tiling/coverage
  report so L1 is not vacuous.
- **L2**: loader, format, `must_find.example.json`. Scoring implemented; with
  no human labels, L2 reports **not evaluated** and Phase 1 closes
  conditionally. The must-find list must come from real documents and be
  human-labeled; never author it, never edit labels to match output.
- `producer_verified_coverage` per code path (expected low until a
  Word-produced doc exists).
- Reduce the python-docx "smoke oracle" to the single eyeball diff design D1
  allows; never an equality check.

## Blocked on the user (do not fake)

1. Human sidecar for the underwriting sample, and the must-find term list.
2. A Word-produced doc in `fixtures/real/` (see decision 7).
3. Real "like terms" groups. Ship only a clearly synthetic example registry.

## Exit criteria (design Phase 1, as amended)

- L1 = 100% on all generator and spec sidecars.
- L2: recall = 1.0 **and** every false positive classified, against the human
  must-find list; **not evaluated** (conditional close) if it does not exist.
- Determinism (byte-level) and offline reproduction green.
- Unchanged re-ingest is a no-op.
- FTS5 delete-and-rebuild reproduces manifest and query results.
- `commentsExtended`, `paraId` identity and strict namespaces recorded
  **unverified** unless a Word-produced doc validates them.
- `docs/design/phase1-gaps.md` lists every known gap (table-cell boundaries as
  decided, sdt, fields, text boxes, paragraph-mark revisions, duplicate-content
  id churn, anything else met), and `known_gaps` ids match it (tested).
- Final report: files created, full pytest output, each ambiguity resolved,
  each contract change, Turn 0b size vs the ~150-line expectation, the churn
  measurement, and anything not done.

## Watch-list

- Codec strictness: no silent key-dropping; any contract change bumps
  `schema_version` with a test.
- Spec-conformance is not Word-conformance: repo-authored literals prove the
  code implements the spec, not that the spec matches Word. Say so in reports.
- L2 label circularity: labels come from humans and real documents only.
- Do not proceed past the Turn 0b gate without the user.
