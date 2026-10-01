# Phase 1 build spec: adversarial review

Reviewed: `docs/design/phase1-build-spec.md` against `word-extraction-design.md`,
`text-model-spec.md`, `wordextract/model.py`, `versions.py`, `docextract-core`,
the fixtures/sidecars and the two fixture generators. Method: a four-round
debate with hearth's model (session `s7716a420`, read-only). This file is the
outcome, not a transcript. Nothing in `phase1-build-spec.md` has been edited;
each change below names the turn and the text to change.

Citations are to the tree at review time (Phase 0, `24848ee`).

## Headline findings

1. **The frozen contracts cannot carry what Phase 1 hashes and matches.** No
   record stores text or elementary spans (`model.py:101-112`, `92-98`,
   `143-154`), so `Chunk.id` ("hash of view text", spec Turn 5) and the Turn 7
   "reproducible offline from the stored node tree alone" invariant are not
   implementable as written.
2. **Turn 0 as written measures the easy path.** Across every current fixture
   the ancestor stack depth is at most 1 and there is no nested ins/del, move,
   hyperlink, field, sdt or text box (`edge_cases.expected.json:22` has
   `"revisions": []`; `spec_threaded` has one flat del+ins,
   `make_spec_fixtures.py:113-114`). python-docx also cannot emit renamed or
   absent parts or strict namespaces, so "every place the code had to handle
   producer variance" cannot appear. The line count would understate Turn 2.
3. **L1 = 100% is close to vacuous for the union/view model.** Sidecar
   revisions are `{kind, author, text}` with no offsets, stacks or views
   (`make_fixtures.py:158-165`), and `_union_text` (`:132-138`) is the same
   idiom the walker will reimplement.
4. **`Chunk.id` collides** for identical content in different sections, as
   specified (content-only hash, `section_path`/`node_ids` excluded).

## Agreed changes

### Turn 0 (split; gate stays)

- **0a: model fixture package.** New `tools/make_revision_fixtures.py` (raw
  OOXML) producing: nested `w:del` in `w:ins`; a move (moveFrom/moveTo with
  shared name); `w:hyperlink`; complex field (nested) + `w:fldSimple`; inline
  and block `w:sdt`; `w:tab`, `w:br`, `w:softHyphen`, `w:noBreakHyphen`,
  `w:sym`; a comment starting in a deleted run; a **deleted paragraph mark**;
  paraId present and absent in one body; a strict-namespace variant; a
  renamed `commentsExtended`; an empty part. Each gets a sidecar with
  `labels_provenance: "spec"` whose union offsets, ancestor stacks (by
  revision id) and accepted/original/superseded strings are **hand-typed
  literals in the generator source**, each tagged with the text-model-spec
  clause it encodes (A to E, section 8). Not computed by any extractor.
- **0b: spike + report + stop.** Inputs: the 0a package, existing spec
  fixtures, the sample document, and the two generator fixtures. Replace
  the spec sentence "Build the smallest thing that reads
  `program_review_v3.docx` and `edge_cases.docx`". Spike code is a scaffold
  that Turn 2a rewrites against the revised contracts, not production.
  `opc-spike.md` must carry: `wc -l` per module and total; every producer
  variance site and its cost; which model constructs were implemented vs
  deferred; agreement with the 0a literals; a projected Phase 1 total with
  the coverage ratio stated; an explicit proceed / re-slice / descope
  recommendation against the ~150-line expectation. State "sized, validated
  only against repo-authored literals", not "validated". Hard stop; the user
  decides.
- Turn 0 comes **before** the contract revision (the spike may reveal contract
  needs); contracts are not frozen by it.

### New Turn 0.5: contract revision (one `SCHEMA_VERSION` bump, after the gate)

Land together so the schema is bumped once, with strict-codec round-trip
tests for every new record and a test that an old-version blob is rejected:

- `UnionStream` (per part): literal text plus elementary spans
  `(start, end, ancestor revision ids)`. Paragraph terminators are elementary
  spans (`"\n"`, stack `[]`) so spans tile the union exactly.
  `Node.spans` for a paragraph excludes its terminator. View text and offset
  maps are **derived on demand** from the stored UnionStream (no stored
  `ViewText` record).
- `ParseResult` with `known_gaps` (ids), and heading decisions per node:
  fired rules, **winner**, disputed (`Chunk` has no winner field today).
- `Chunk.content_hash` separate from `Chunk.id`;
  `Chunk.id = hash(canonical(content_hash, occurrence_index))`;
  `section_path` is a field, never part of any key; store key is
  `(source_content_hash, chunk_id)`. Canonical encoding (no naive
  concatenation).
- `Node` occurrence ordinal for content-hash fallback; `Comment.id_stability`
  and the `Comment.para_id` fallback (below).
- `TermHit`: replace `view_span` with `view_spans: list[ViewSpan(view, start,
  end)]` sorted by view, offsets in the **whole-part** view projection (with
  terminators kept); `present_in` derivable, must not disagree. Add
  `move_group_id` and `context_node_id`. `LocationKind` gains `COMMENT`,
  `ENDNOTE`, `TEXTBOX`.
- `labels.py`: extend for the hand-typed model sidecars under the existing
  no-implementation-import rule; update the three assertions that count
  sidecars (`tests/test_evals.py:67`, `:96-101`;
  `tests/test_fixtures_sidecars.py:63-72`), since `sidecar_from_dict` is
  strict (`labels.py:74-89`) and `iter_sidecars` globs everything (`:101`).
- Harness: `producer_verified` becomes a per-path tally (list/dict), not the
  hardcoded scalar (`harness.py:74`); define a producer-verified path as one
  exercised by a Microsoft-Word-produced document, marked
  `@producer_verified`. Denominator stays `fixtures/real`.
- Add view projection and gap-closing to the `textmodel_version` bundle
  (`text-model-spec.md` section 7 does not name them today); record
  "paragraph-mark revisions are not honored" there too.

### Comment and node identity (replaces the spec's "for example hash:" hint)

- Comment fallback when no `w14:paraId`: `hash:` of canonical
  (author, date, text, ordinal among equal hashes in document order), nulls
  encoded deterministically, plus `id_stability`. **Do not fold the anchor
  start into the id**: it is a union offset, so ids would churn with
  `textmodel_version`.
- Threading: `commentsExtended` is the only source of `parent_id` and is
  paraId-keyed, so a paraId-less document has `threading_status = absent`,
  `parent_id = None`. No identity-space match is needed.
- Node content-hash fallback basis = own union text + `kind` (+ `style`,
  `level`), then occurrence ordinal. Limitation (record in `phase1-gaps.md`):
  inserting a duplicate ahead of an identical paragraph re-ids the later one;
  the D3 v2 to v3 diff will show spurious churn there.

### Turn 2 and Turn 3 (text-model decisions made now, not "fails visibly")

Add a "Text-model decisions" section to the spec (or amend
`text-model-spec.md` section 8) so a small-context builder does not
improvise:

- Union text: one `"\n"` terminator span per paragraph; `w:tab` = `"\t"`,
  every `w:br` type and any literal `\n`/`\r` in `w:t` = `U+000B` (content,
  inside the paragraph; a phrase can cross a page break, documented);
  `w:noBreakHyphen` = `U+2011`, `w:softHyphen` = `U+00AD`, `w:sym` = its
  char; `w:instrText` never content; `w:t` and `w:delText` both content.
- Fields: exclusion is a `fldChar` depth counter (nested fields);
  `w:fldSimple` instruction is the attribute. Results carry whatever revision
  ancestry encloses them; no special masking.
- Comment ranges **clamp to exclude terminators**; a multi-paragraph
  `anchor_text` contains `"\n"` (tests expect it). A `commentReference` with
  no matching range yields `anchor = None`, recorded in `known_gaps`, never
  silent.
- Per-kind span rule, asserted by test: paragraph, heading, list_item, cell =
  exactly one contiguous span; table, row, block-sdt = empty spans with
  `child_ids`. `NodeKind.SDT` is for block-level sdt only; inline sdt is
  transparent (no node, no boundary, no ancestor).
- Matching unit is always a `w:p` after the mark-revision policy. Table cells
  never fuse (each cell paragraph is an ordinary paragraph); cell/row/table
  order is document order. Footnotes, endnotes, headers, footers are separate
  parts; no view spans parts. Headers/footers: read every part reachable via
  `headerReference`, deduplicated by part.
- **Deleted/inserted paragraph mark (`w:pPr/w:rPr/w:del|ins`)**: not honored
  (the break is retained in both views); loud `known_gap:
  paragraph_mark_revision`, affected nodes flagged, fixture included. This
  can split a phrase Word's accepted view would join; see Open Questions.
- Text boxes: exactly one body per anchored drawing, preferring
  `mc:Choice` > `mc:Fallback` > bare `w:txbxContent`. Content lives in its own
  stream with a synthetic stream id (host node id + ordinal), so host
  document order is not disturbed; a `host_node_id` field records the
  relationship. Recorded as a known gap. Chunk assembly order for box text
  (host id, ordinal) must be stated. (Representation is an open question.)
- Gap-closing keeps **no placeholder** (Word itself renders
  `right of ` + accepted `subrogation` as `right ofsubrogation` when the
  space sat inside the deleted run). Turn 3 adds a test documenting that miss,
  and one showing a `w:br` inside a deleted run.
- Turn 3 also states the closure for offline reproduction (Turn 7):
  `UnionStream` + the pinned views projection + the term registry. "From the
  stored node tree alone" is wrong as written.

### Turn 2 slicing

Split into 2a union stream (against the 0a literals), 2b nodes + ids +
numbering, 2c revisions, 2d comments + anchoring (Comment contract already in
0.5), 2e `commentsExtended` (tri-state, `@spec_derived`). Add a
walker-independent tiling test: spans tile the union exactly, every stack id
resolves to a revision, masked spans reconstruct the three view strings.

### Turn 4 (heading rules)

Slice 4a rules (fired / winner / disputed) and 4b section tree.

### Turn 5 (chunker)

- Size = `len(view_text)` (Python code points) of the chunk's own view; member
  paragraphs joined by `"\n"` (feeds `content_hash`).
- Merge predicate: a numbered-list run = maximal consecutive `LIST_ITEM`
  nodes, length >= N (N = 2, in `chunker_params_hash`); forced boundary before
  and after; list items merge only with list items.
- Add a churn measurement (report, not a gate): amplification = chunk ids
  changed / chunks whose own bytes changed, for a body edit, a comment-only
  edit, and resizing a paragraph near the cap. Repeated table-header rows
  exercise the `occurrence_index` path; test it.
- Tests: add the collision case (identical paragraphs in two sections get
  distinct ids and both survive the store).

### Turn 6 (matcher)

- Slice: 6a registry + exact/synonym + overlap policy; 6b stemmer; 6c
  locations; 6d moves; 6e cross-paragraph measurement as a `tools/`
  report-only script, not package code.
- Matching is over normalized **token sequences**, never substrings;
  normalization applied identically to registry entries and corpus. Fused
  seam tokens (`non` + `compliance`) are faithful, not false positives.
- Stemmer: **vendor a pinned Porter with attribution**, own version inside
  `matcher_version`, tested against external published vectors, plus
  adversarial portmanteau cases (`included/including/inclusion`).
  `matcher_version` is the code version; per-group algorithm choice lives in
  the registry and `term_list_hash`.
- Overlap precedence: longest span, then leftmost, then exact > synonym >
  stem; disjoint matches all reported; `match_type` resolved before dedupe;
  dedupe key `(group, node_id, view, start, end)`; distinct groups report
  distinct hits.
- Comment hits: `LocationKind.COMMENT`, `node_id` is
  `comment:<para_id or fallback id>` (comment hits resolve via the `Comment`
  record, not the node tree), `context_node_id` = anchor node, `present_in` =
  empty set.
- Moves: two `TermHit` records (one per location), both with
  `move_group_id`; "one hit" is realised by dedupe at query/rank time on
  `(group, move_group_id, normalized text, intra-group ordinal)`. Flagged as a
  contract change (see Open Questions).

### Turn 7 to 9

- Turn 7: add a byte-level determinism replay (run twice, diff bytes; codec
  sorts `set` but not dicts or frozensets, `codec.py:39-40`). Offline
  reproduction test uses the closure stated in Turn 3.
- `phase1-gaps.md` becomes machine-checked: each gap has an id; `known_gaps`
  ids must equal the doc's ids, asserted by a test.
- Turn 8 (if kept): minimal FTS5 = one table over chunk view text keyed
  `(chunk_id, view_id)`, manifest (`index_schema_version`, source record
  hashes, SQLite/FTS5 versions), delete-and-rebuild reproduces manifest and
  query results. Cut from it: separate comment indexing and rank-time dedupe
  (rank is Phase 2).
- Turn 9: keep `ingest`/`hits` CLI and L1 wiring; drop `reindex` with FTS5.
  Reduce the python-docx "smoke oracle" to the single eyeball diff design D1
  allows, never an equality check.

### Resulting turn list

0a fixtures+literals; 0b spike+report (stop); 0.5 contracts; 1 OPC; 2a-2e;
3 views; 4a-4b; 5 chunker; 6a-6e; 7 store/run record; 8 FTS5 (open
question); 9 CLI + L1 + gaps. Turns hearth still judges too big: 0.5 (largest
diff, schema hub), 2a/2b, 6c. Sit them as separate commits within a turn if
needed.

## Rejected hearth points and why

- **"Turn 0 does not measure OPC risk, drop the claim / keep it a pure line
  count."** Partly rejected: the gate is a cost gate (design D1, Open risk
  #6), which hearth agreed. Its narrower conclusion was rejected in favour of
  widening the input set, since a line count over generator output cannot
  change the decision.
- **"Insert a placeholder space when an elided span contained whitespace."**
  Hearth first raised the miss as a bug; it withdrew after the fidelity
  argument (Word's accepted view really shows `right ofsubrogation`; a
  placeholder asserts text no view contains). No-placeholder stays, with
  tests for the miss.
- **Comment `parent_id` identity-space objection.** Withdrawn (see above).
- **Chunk id options.** `hash(first_node_id, content_hash)` rejected (PATH-id
  churn); my `(doc, section_path, content_hash)` key rejected (non-unique,
  rename-sensitive). Final form is the occurrence-index variant above.
- **My own ~30-line suffix stripper** rejected by hearth and accepted: a
  self-authored golden table is the same circularity as the sidecars.
- **My "Turn 0 = Turn 2a"** rejected: the spike must be free of the contracts
  Turn 0.5 changes.
- **"Honor deleted paragraph marks"** (view-dependent matching unit): not
  adopted for Phase 1 as too expensive; loud known gap instead (Open Question
  3).
- **Hearth's `LocationKind` split into `part_kind` + `container`:** noted,
  not adopted in Phase 1 (Open Question 5).

## Open questions for the user

1. **Defer FTS5/manifest (Turn 8) and the L2 scorer to Phase 2?** Both are
   explicit Phase 1 exit criteria (`word-extraction-design.md:203-205`), so
   this is your call. Recommendation: keep FTS5 as the last, minimal turn;
   keep only the L2 loader and `must_find.example.json` (L2 reports "not
   evaluated" until labels exist).
2. **Who labels the sample document sidecar?** It must be you
   (`labels_provenance: human`); a tool-generated sidecar is circular and
   mis-provenanced. Until it exists, the spec's claims (Turn 1 resolution,
   exactly 5 headings, range start order 0,2,1,3,4,5) are unenforced and the
   final report must say so.
3. **Deleted paragraph marks.** Real Word documents delete paragraph breaks
   in tracked edits routinely. Not honoring them can split a phrase the
   accepted view joins (a silent miss where a missed exclusion is a real
   failure). Accept a loud known gap for Phase 1, or pay for view-dependent
   paragraph units now?
4. **Moves as two hits + query-time dedupe** deviates from the wording "one
   hit with two locations" in text-model-spec section 5 and design D4
   (hearth notes "deduped in the term index" arguably implies the mechanism).
   Approve as a contract change?
5. **Text-box representation (unresolved disagreement).** I recommend a
   synthetic stream id in `Span.part_id` plus a `host_node_id` field. Hearth
   prefers keeping `part_id` a real OPC part (relationship type + ordinal,
   Turn 1) and adding a separate `fragment_id` or a first-class textbox
   record, and wants a stated ordering key for chunk assembly. Either works
   if the rule is written down; it needs your pick.
6. **Precision gate.** The L2 gate is recall only; over-stemming and synonym
   gaps pass green (`word-extraction-design.md:178-180`). Add "every false
   positive classified" (or a precision floor) to the exit criteria?
7. **A Word-produced document.** Nothing owns acquiring one. paraId identity,
   `commentsExtended`, deleted paragraph marks and strict namespaces stay
   unverified, and hand-typed literals fix only "did the code implement the
   spec", not "is the spec what Word does". Decide who supplies it and when.

## Where minds changed

- **Mine:** Turn 0 = 2a is incoherent; Turn 0 must include the hand-typed
  literals so the gate validates something; the terminator must be an
  elementary span (else the tiling test fails by construction); a home-grown
  stemmer is circular; `view_span` cannot stay single once `present_in` has
  two views; sdt/paragraph contiguity must be stated per kind; text boxes
  cannot be appended after the host paragraph; the deleted-paragraph-mark case
  contradicts "matching unit is always a `w:p`" and must be a loud gap.
- **Hearth's:** dropped the placeholder-space proposal, dropped the
  `parent_id` identity objection, conceded derived-on-demand view text over a
  stored `ViewText` record (adding that view projection and gap-closing join
  `textmodel_version`), and accepted two-hits + dedupe for moves as consistent
  with spec section 5.

## Risks the revised plan still does not cover (hearth's top 3, endorsed)

1. No producer-verified document (Open risk #3); nothing forces acquisition.
2. Repo-authored fixtures: spec-conformance is not Word-conformance.
3. Recall-only L2 gate with a blocked must-find list (Open Question 6).
