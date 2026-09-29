# Turn 0b: OPC reader + document walker sizing spike

**Status: sized, validated only against repo-authored literals.** The scaffold reaches
every 0a literal and walks five non-authored packages without crashing, but no
Word-produced document exists to check it against, so nothing here is *validated* in the
sense the build spec uses for the exit gate. Where this document says "validated" it is
always as part of that qualifier.

The spike is `tools/opc_spike.py`, a throwaway scaffold. Phase 1 Turn 1 (`opc.py`) and
Turn 2a (`walker.py`) rewrite it against the contracts Turn 0.5 revises. It exists to
answer two questions: are the 0a literals reachable from raw OOXML without a model, and
how many lines does the OPC + walker work actually owe.

## 1. Size

Per module, plus the Turn 0a generator for the total:

| module | lines | role |
|---|---:|---|
| `tools/opc_spike.py` | 402 | the 0b scaffold (throwaway) |
| `tools/make_revision_fixtures.py` | 758 | Turn 0a fixtures + hand-typed literals |
| **total** | **1160** | |

Inside the spike, by section (`# --- ... ---` boundaries):

| section | lines (approx) | Phase 1 destination |
|---|---:|---|
| header, ns/rel constants, `canon_rel`, dataclasses, ns helpers | 120 | `opc.py` + `walker.py` |
| `Package` (parts, rels, rel-by-type, document) | 30 | `opc.py` |
| run + inline text (`run_text`, `box_text`, `_iter_*`) | 54 | `walker.py` |
| document walk (`walk_paragraph`, `_para_id`, `walk_document`) | 93 | `walker.py` |
| comments / `commentsExtended` | 31 | `walker.py` |
| reporting (`show`, `compare`, `main`) | 72 | **thrown away** |

So ~330 lines are candidate core and 72 lines (18%) are the print/diff harness that
Phase 1 never ships.

## 2. Producer-variance sites, and what each cost

The spike's size is almost entirely these. Every one was found by running it, not by
reading the design.

1. **Relationship type, not path, selects a part.** `by_type()` maps
   canonicalised rel type -> target. Cost: ~10 lines, and it is what makes the renamed
   parts work (below). Nothing else needed.
2. **Strict relationship URIs.** ISO-strict writes rel types under
   `http://purl.oclc.org/ooxml/...`. `strict_namespaces.docx`'s officeDocument rel is
   `http://purl.oclc.org/ooxml/officeDocument/relationships/officeDocument`; with a
   transitional-only table `document()` returns `None` and the document silently has no
   paragraphs. Cost: a 2-entry `REL_STRICT` table plus a `canon_rel()` call at every
   comparison (~8 lines). Load-bearing: silent empty output, not an error.
3. **Strict `w` namespace.** Same shape in element/attribute space. Cost: match
   against a **set** of URIs (`W_URIS`) instead of a hardcoded prefix, and read
   attributes by localname (`wattr`) because strict and transitional differ on the
   namespace of attributes too. Cheap per call site, but it infects every `find`/`get`
   in the walker — this is the single largest driver of "the walker is not short".
4. **Renamed parts.** `renamed_comments_extended.docx` targets `ext/commentsExt.xml`,
   not `commentsExtended.xml`. Cost: zero, given (1). Resolving by path would have
   required a fallback list.
5. **Relative targets with `..`.** `program_review_v3.docx` has a `customXml` rel whose
   target is `../customXml/item1.xml`; the zip entry is `customXml/item1.xml`.
   Naive `base + target` yields `word/../customXml/item1.xml` and misses. The spike
   does not normalise targets, so this part is dropped. Cost in Phase 1: a real
   target-normalisation function (`.`/`..`/leading `/`), ~20 lines, plus tests.
6. **Non-XML parts in the package.** `docProps/thumbnail.jpeg` is enumerated and must
   not be parsed as XML. Cost: the `.xml`/`.rels` filter is a guess; `[Content_Types].xml`
   is unparsed here (see §3).
7. **`w14:paraId` is absent from every generator-made package we have.** 0 of 48
   paragraphs across the five non-authored packages carry it; 10 of 77 in total, all
   ten in fixtures we authored. All six comments in the underwriting sample have
   `paraId=None`. **Correction (review):** these five packages are docx-generator
   output, not Word output, and Word writes `w14:paraId` on every paragraph. The
   spike shows generator docs lack paraIds; it does **not** show that Word docs do,
   so the design's paraId assumption is unverified, not refuted. Cost: the
   content-hash fallback is the primary identity path *for generator docs*; whether
   it is the fallback or the primary for the real corpus is decided by a
   Word-produced document. `commentsExtended` threading is unexercised by anything a
   producer made.
8. **No `commentsExtended` for any non-authored comment.** Only `spec_threaded.docx` and one
   fixture carry it. Cost: `threading_status` is "absent" for every corpus doc; the
   design's threading support is spec-conformance only.
9. **`w:tbl` flattening.** Three corpus docs have tables. The spike recurses through
   `w:tbl` and emits their paragraphs as ordinary ones. Cost: the cell-boundary
   decision is deferred, and the spike shows why — nothing in the package tells you
   which boundary the design wants.
10. **Text boxes.** `text_box.docx`'s text lives in `mc:AlternateContent/wps:txbxContent`
    inside `mc:Choice`, with a VML `mc:Fallback` carrying the *same* `w:txbxContent`.
    Cost: prefer `Choice` and stop, else every box is counted twice.
11. **Stub parts.** The sample's `footnotes.xml`/`endnotes.xml` are separator-only and
    have their own `.rels`. Cost: "part exists" must not mean "has content".
12. **A literal `\n` inside `w:t`.** The spike's first cut emitted the raw newline and
    disagreed with the literal; `\n`/`\r` must map to U+000B alongside `w:br`/`w:cr`.
    Cost: ~2 lines, but it is the reason one 0a literal is spelled the way it is.
13. **Move group id is not on the move element.** It comes from `w:name` on the
    `moveFromRangeStart`/`moveToRangeStart` markers that *wrap* it. Cost: a tracking
    stack in the walker (~6 lines).

Sites 2 and 5 are the two that fail silently. Both were found only by walking real
packages; neither is visible in the design text.

## 3. Constructs implemented vs deferred

Implemented end-to-end (18):

| construct | notes |
|---|---|
| root + part rels resolved by type | renamed part found |
| strict/transitional rel URI canonicalisation | site 2 |
| strict/transitional `w` namespace normalisation | site 3 |
| `w:t` / `w:delText`, literal `\n`/`\r` -> U+000B | |
| `w:tab`, `w:br`, `w:cr`, `w:softHyphen`, `w:noBreakHyphen`, `w:sym` | |
| nested revision stacks, arbitrary depth, distinct ids | `<kind>:<w:id>` |
| move group from range markers | site 13 |
| `w:hyperlink` / `w:fldSimple` / `w:smartTag` transparency | |
| complex field: result text in, `w:instrText` out | |
| inline `w:sdt` transparency, block `w:sdt` descent | |
| `commentRangeStart`/`End` -> (start, end) on the union stream | |
| comments part (id, author, initials, text, paraId) | |
| `commentsExtended` threading by paraId (renamed part) | |
| deleted paragraph mark detected | reported, not honoured |
| `w14:paraId` read when present | site 7 |
| text box, `mc:Choice` preferred | site 10 |
| union stream + accepted / original / superseded masks | 14 lines |

Deferred to Phase 1 (18): node tree and `NodeKind`; paragraph-terminator spans;
table-cell nodes and the boundary decision; text-box `fragment_id`/`host_node_id`;
heading detection and anchor text; view projection with gap closing and the offset map;
`[Content_Types].xml`-driven part selection; relationship target normalisation (site 5);
part-level rels for footnotes/endnotes/headers/footers and walking those parts;
edits/styles/numbering/`docProps` metadata; content-hash id fallback (site 7); comment
id fallback and threading `status`; duplicate-content id churn; field kind
classification; chunker; terms and stem; store and run record; FTS5; CLI; L1/L2 evals
and `phase1-gaps.md`; determinism/no-op re-ingest.

**Coverage ratio: 18 of 36 constructs, ~50%.** The masks are in place, so the *shape* of
the data model is proven; everything deferred is downstream of the node tree, which is
exactly what Turn 0.5 freezes and Turn 2a builds.

## 4. Agreement with the 0a literals

`python tools/opc_spike.py` exits 0 and prints, for every model fixture:

```
=== agreement with the 0a literals ===
  breaks_and_specials.docx               OK
  comment_in_deletion.docx               OK
  content_controls.docx                  OK
  deleted_paragraph_mark.docx            OK
  empty_parts.docx                       OK
  hyperlink_and_fields.docx              OK
  mixed_para_ids.docx                    OK
  move.docx                              OK
  nested_revisions.docx                  OK
  renamed_comments_extended.docx         OK
  strict_namespaces.docx                 OK
  text_box.docx                          OK
```

12 of 12, comparing paragraph count, `union`, every `Span(text, stack)`, and the three
views. Three points are worth recording:

- **`strict_namespaces.docx` agrees only after site 2.** With transitional rel types
  alone it produced zero paragraphs against a literal that expects two — the strict
  fixture is the test that catches strict *relationship* handling, not just strict
  element namespaces.
- **Span boundaries were forced by the literals.** The first cut treated every
  `w:tab`/`w:br` as a boundary and disagreed on `breaks_and_specials.docx` and
  `program_review_v3.docx`. The rule that reconciles them, and that Turn 0.5 must
  freeze: *a span is a maximal run of equal ancestor stack; `w:tab`/`w:br`/`w:sym` do
  not split it.* The sidecars encode this (`SPAN_RULE`).
- **Disagreements were fixed by the literals, never the other way round.** Two edits
  landed in the spike (the `\n` mapping, the span rule) and zero in any
  `.expected.json`.

The sample and the four older fixtures are walked but not scored: they have no
sidecars, and none may be authored for them (site 7 means an authored one would be
fiction).

## 5. Projected Phase 1 size

Line estimates for the slices the spike informs, using its measured 402 lines as the
floor for the parts it already reaches:

| slice | lines (low-high) | what is added over the spike |
|---|---:|---|
| 0.5 contracts | 250-350 | ~6 undecided contract points the spike hit |
| 1 OPC (`opc.py`) | 250-350 | content types, target normalisation, part rels, errors |
| 2a-2e walker (`walker.py`) | 450-650 | node tree, terminators, `tbl`/`sdt` nodes, fragments, ids |
| 3 views (`views.py`) | 150-220 | projection, gap closing, terminators, offset map |
| 4a-4b headings | 150-250 | |
| 5 chunker | 150-220 | |
| 6a-6e terms | 350-500 | plus ~400 vendored Porter |
| 7 store / run record | 200-300 | |
| 8 FTS5 | 120-180 | |
| 9 CLI + L1 + gaps | 300-450 | |
| tests | 800-1200 | L1 is gated at 100%, so this is not optional |
| **production total** | **2,400-3,500** | **+ ~400 vendored, + tests** |

Call it **~2,900 lines of production code** at the midpoint, plus ~400 vendored and
~800-1,200 of tests.

**Coverage ratio, stated both ways.** The spike reaches 18 of 36 Phase 1 constructs
(~50%). Against the projection it is 402 lines of a ~2,900-line body, ~14% — or ~330
core lines (excluding its throwaway harness) against the ~1,850-2,220 lines that
`opc.py` + `walker.py` + `views.py` will take, ~16%. Turn 0b therefore buys
measurement, not code reuse: no more than a sixth of the OPC/walker work is in hand.

## 6. Recommendation: proceed

**Proceed.** Not re-slice, not descope. Two binding adjustments:

1. **The ~150-line expectation is exceeded, and the estimate absorbs it.** 402 lines is
   2.7x that figure; 330 lines of core is 2.2x; against the ~1,850-2,220 lines the three
   slices it informs actually owe, it is under a quarter. The overage is not
   scaffolding waste, it is sites 2, 3, 5 and 7 — each load-bearing, each found only by
   walking a package. Turn 1's budget moves from ~150 to 250-350 and 2a-2e to 450-650.
2. **Descoping is the wrong lever.** The cheap-looking cuts are the ones that fail
   silently: drop strict rel canonicalisation and `strict_namespaces.docx` yields an
   empty document, not an error; skip target normalisation and `program_review_v3.docx`
   loses a part with no diagnostic. Both are exactly the class of bug the design gates
   on `producer_verified_coverage`. If size must come out of Phase 1, take it from the
   deferred list's tail (FTS5, field kind classification, the separate 6e report), not
   from the OPC reader.

Re-slicing is unnecessary: Turn 0.5 already precedes 1, and the turn list already
sanctions splitting 1 and 2a into separately green parts. **No 0.5 work starts in this
run.** The next run's first job is the contract freeze, with sites 2, 5 and the span
rule in §4 as required inputs.
