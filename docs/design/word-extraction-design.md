# word-extract design

Outcome of a design debate between Claude (opening proposal,
`proposal-draft.md`) and hearth's configured model, which read the
form-extract reference code (`formextract/model.py`, `store.py`,
`docs/design/form-extraction-design.md`). Not a transcript; the reasoning that
changed the design is kept.

## Goal

Deterministically parse a .docx (body, tables, lists, comments, tracked
changes, headers/footers, footnotes) into an addressable, stored node tree;
chunk it; attach small-model per-section summaries as a RAG routing layer;
and flag/group user-supplied "like terms" with reproducible, offline-derivable
hits. Regulated-industry stakes: a missed exclusion or a wrong comment
attribution is a real failure. Everything not summarizing is LLM-free.

Guiding principle (found independently in every decision below): **a default
view is a projection of the content, never the content.** Every record,
chunk hash, hit and cache key names the projection and the inputs it depends on.

## Decided

### D1. Parser: lxml over the zip, no python-docx in the library
- python-docx is silently lossy on our constructs: `CT_P.r_lst` sees only direct
  `w:r` children, so runs inside `w:ins`, `w:hyperlink`, and field results
  vanish; `Document.paragraphs` skips `w:sdt` content. A hybrid that uses its
  proxies yields two text paths. "Use only `doc.part`/`doc.element`" is
  unenforceable discipline against a library whose docs teach the proxies.
- Read-only OPC (zipfile + rels + styles/numbering/comments) is small. It
  must resolve parts **via relationships and content types**, never hardcoded
  paths (`word/commentsExtended.xml` may be absent or renamed).
- **Spike first, sized**: build the OPC + walker for `program_review_v3.docx`
  and `edge_cases.docx` and record the actual line count before Phase 1 commits.
- **Do NOT reimplement**: the effective style cascade (`basedOn`/`docDefaults`/
  `latentStyles`; we need `pStyle` references and `outlineLvl`, not resolved
  fonts), any authoring/`add_*`/save API. Vendor with attribution only if
  needed: `ST_OnOff` decoding, `gridSpan`/`vMerge` normalization.
- python-docx stays in `tools/make_fixtures.py` and as a **smoke oracle** in
  tests (non-empty, key phrases present), plus a one-time human eyeball diff on
  a fixture free of hyperlinks/sdt/fields. Exact-equality oracle rejected: it
  fails on legitimate input (no `w:tab`/`w:br` rendering).

### D2. Text model (the real decision behind D1)
- Canonical address space = **per part** (`part_id`, offset), `source_ref` lives
  here. Within a part, text is a **union stream** in document order containing
  inserted AND deleted text; each span carries an **ancestor stack**
  `[revision_id, ...]` outer to inner (handles `w:del` inside `w:ins`).
- Views are masks over the union: `accepted` = no `del` ancestor;
  `original` = no `ins` ancestor; `superseded` = in the union but in neither
  (inserted-then-deleted). `moveTo` is ins-family, `moveFrom` del-family, with a
  `move_group_id`.
- Masks close gaps **within a paragraph only**, never across paragraph
  boundaries. Views are what get matched and summarized; the union is only an
  address space (matching raw union invents adjacencies no view asserts, e.g.
  `right of [del transfer][ins termination]` -> false hit "transfer termination").
- Consequence: `Node.text` becomes `view_text(view)`; a comment anchored in a
  deleted run has an ordinary non-empty range.
- `textmodel_version` bundles union/ancestor semantics + revision application order.
- Union gives stable offsets across views within one version. It is **not**
  the cross-version diff mechanism (see D3 ids).

### D3. Identity and chunking
- Node ids: `w14:paraId` where present, else content hash, else positional path
  explicitly flagged non-stable. Path ids (`s2/p3`) renumber on insertion and
  cannot support "cheap v2->v3 diffs". Numbering labels ("2.1(a)") are derived
  display values from counter state, never id components.
- Section detection is an **ordered deterministic rule list**: style -> outlineLvl
  -> outline-numbering ilvl -> all-bold short paragraph. Record **every** rule
  that fired, the winner, and disagreements; no confidence scalar. Guards:
  the ilvl rule applies only to outline-numbered `abstractNum`s and never to
  list-context paragraphs (`List Number` exclusions in the fixtures would
  otherwise become headings); the bold rule requires all runs bold, not in a
  table cell, not a list continuation. Fail-open = one flat root with
  `heading_detection: degraded` + disagreement count, and it must still be size-chunked.
- Chunking: heading tree + size cap; **format-aware merge** (never merge a
  numbered list into its parent); tables atomic (huge: row groups with repeated
  header); a range comment crossing sections belongs to the **start-of-range** chunk.
- Chunk id = hash of **content** (view text + `textmodel_version` + `view_id`).
  A separate **context hash** (content + comment texts) is a summary-key input
  only, so resolving/editing a comment does not re-id the chunk.

### D4. Comments and revisions
- Comments keyed on `w14:paraId`, not `w:id` (Word reassigns `w:id`). Indexed
  separately for retrieval and folded into the anchor chunk as summary
  *context*; deduped at rank time (no double return).
- Attribution: store raw `w:author` and `initials`; identity is a separate
  optional resolved field. Missing/local `w:date` is explicit, not defaulted.
- Threading via `commentsExtended` is in Phase 1, from spec-derived fixtures.
  The join key is the `w14:paraId` of the comment's **last** paragraph.
  `threading_status: verified | absent | unknown`; `resolved: bool | None`
  (`None` = not stated). Absent part is *unknown*, never "no replies".
  Tests are tagged `@spec_derived` / `@producer_verified`; CI reports
  producer-verified coverage per code path.
- Revisions: store raw ins/del/move facts with author/date; **no synthesized
  "net change"** (OOXML does not pair a del with its ins). Moves render as
  **two hits, one per location**, each carrying the shared `move_group_id`, and
  are deduped in the term index **at query time**.

### D5. Revision default permit (changed from the proposal)
"Accepted default, deleted searchable" is rejected: a pending deletion of an
exclusion makes the accepted-view summary say no exclusion exists, and
searchable-but-unsummarized text is invisible to the routing layer. "Original"
default fails symmetrically (hides an inserted exclusion). Decided:
- The summarizer input is the **union with revision markup** plus a revision manifest.
- Summary records carry a deterministic structured **`pending_changes`** field
  (what/by whom/when), populated from revision records, NOT from LLM prose.
- View/permit is a hash input everywhere, so different licenses never collide on a cache key.

### D6. Term matching: in package, generic, deterministic
- Span matching is a pure function of the resolved node tree, so it lives here;
  a store-side consumer has lost node/offset fidelity.
- Matcher is generic (term set + rules -> spans). Domain data (`category`, etc.)
  is opaque tags on the registry, not in the matcher types.
- The **term-list registry is persistent and versioned**; `term_list_hash` +
  `matcher_version` are stamped on every run. Exact + curated synonyms + stemming
  only. **Fuzzy is not a `TermHit`** (a threshold is a confidence in disguise,
  cf. model.py:319-331); fuzzy/LLM output is a *candidate* artifact and merges
  into the registry only by recorded human approval.
- Matching unit = paragraph per view. `TermHit.present_in` is a set of
  `{accepted, original, superseded}`; hits carry `spans: list` in union coords
  plus `view_spans: list[ViewSpan(view, start, end)]` (sorted by view, offsets in
  the whole-part view projection with terminators kept). `right of transfer`
  (original) and `right of termination` (accepted) are two hits in one group.
- Cross-paragraph phrases: deferred, with the miss **measured** (a permissive
  cross-paragraph matcher run offline on the golden set only; non-zero finds
  promote it). `superseded`: mask + fixture only in Phase 1, not a matcher target.
- Co-location analytics deferred.

### D7. Summaries
- Cache key: `chunk content hash` + `context hash` + `view_id` + `summarizer_version`
  + `model_id` + `model_params_hash` + `prompt_hash` (the proposal omitted params
  and view).
- Hierarchical roll-up is **derived / non-authoritative** (cannot see
  cross-section facts) and never the sole routing basis.
- Retrieval = union of term hits, FTS5 over text, FTS5 over summaries;
  summaries are for discovery, the term index for precision.

### D8. Storage
- Content-addressed canonical JSON. **The resolved node tree is stored**, not
  just chunks: "reproduce this hit" must work offline from stored data.
- FTS5 index is derived, with a manifest (`index_schema_version`, source record
  hashes, embedding model id). Rows keyed `(chunk_id, view_id, ...)`. Term
  matching is never done in FTS5 (its stemmer is unpinned). Embeddings:
  optional, derived, never in records; any BM25/cosine fusion records its
  parameters and emits no single fused score.

### D9. Shared core: create `docextract-core` now, substrate only
- Contents: sha256/`content_hash` (store.py:23-32), dataclass codec
  (`_encode/_decode/to_json/from_json`, model.py:383-431), `_read_json/_write_json`
  (store.py:35-43; collapses the duplicate flag set vs model.py:426-427),
  `archive_raw` (store.py:63-86), `archive_llm_call` + `LLMCall`
  (store.py:139-166, model.py:267-277), LLM-client protocol, `Collection[T]`
  generalizing store.py:88-137, `CORE_VERSION` + git rev.
- Never shared: domain records, `Provenance`, metrics, the indexer (FTS5).
- Seam proof without touching form-extract: a **core-CI contract test** replaying
  form-extract's record shapes (instance/template/batch) through the
  registries and archives; core API changes re-run it. form-extract migration is
  a separate, unscheduled PR and not a word-extract exit criterion. `Collection[T]`
  is marked provisional; hashing/codec/archive behavior is frozen.
- Path/editable dependency; no PyPI/semver ceremony.
- Note: this fixes the proposal's header/#8 contradiction ("shares a core" vs "copy for now").

### D10. Reproducibility contract (rewritten)
- (A) **Hashed inputs**: `source_content_hash`, `spec_parser_version`,
  `textmodel_version`, `view_id`, `heading_ruleset_version`, `chunker_version`+params,
  `term_list_hash`+`matcher_version`, `summarizer_version`+`model_id`+
  `model_params_hash`+`prompt_hash`, `output_schema_version`.
- (B) **Recorded, not hashed**: core and package git revs, dependency versions,
  run id/timestamps, `producer_verified` flags, per-artifact cache hit/miss with
  the recomputed input tuple (a hit is *verified by recomputing the key*).
- (C) **Invariant tests**: same bytes -> identical tree/ids/hits; flip-one-input
  -> key changes; cached summary byte-identical to a replayed call; hits
  reproducible offline from the stored node tree.

### D11. Eval
- L1 parse/perception: exact, **gated** at 100% on generator-sidecar facts
  (`expected.json` per fixture: comment ranges, revision spans, sections);
  reported alongside producer-verified coverage.
- L2 terms: **gated on recall = 1.0** over a must-find list drawn from **real
  documents only**; every false positive classified (union artifact / stem
  overreach / synonym gap).
- L3 summaries: **reported, never gated** (rubric faithfulness, n~10-15,
  regraded on prompt/model change). Cost/doc and revised-doc cache hit rate are
  cost metrics, kept out of quality.
- Labels carry `labels_provenance: human | generator | spec`, load independently
  of the implementation, and are never edited to match output. Adversarial
  fixtures label expected behavior (e.g. "must fail open with flag").

### D12. Query API and LLM-facing tools (added after the Phase 1 review)
- Retrieval for an LLM is a **query layer over the stored records**, not the
  JSON files themselves. Plain functions in `query.py` (no MCP imports); a thin
  `mcp/tools.py` + `server.py` wrap them, mirroring the sibling `code-mcp` /
  `exec-mcp` split. `mcp` is an optional extra. Tools are **read-only**.
- **Every result carries a citation** (document, chunk/node or `comment:<id>`,
  `view_id`, offsets). Every tool names its view; none defaults silently to
  accepted-only (D5).
- Facts the deterministic layer owns (term hits, comment attribution, revision
  facts, `pending_changes`) are returned from records, never from summary prose.
- **Ranking (closes Open risk #4):** union of term hits, text match and summary
  match, ordered by a **fixed source-priority tiering** (term hits, then text,
  then summary), deterministic tie-breaks, one result per chunk with all its
  sources listed. **No blended relevance score.** Embeddings, if ever added, are
  a separately labeled fourth source (Phase 3). The rule (Turn 3, `rank.py`):
  - The query is **resolved through the registry** — both sides through the
    matcher's `normalize`, equality against a group's canonical or synonym form
    only (no stem, no fuzzy guess, no substring). No match, no term tier; a
    query equal to forms of several groups is an error naming them. The term
    list is the caller's or the store's only one — zero or several stored lists
    is an error naming the candidates.
  - Text match is normalized **token containment** over `chunk_text`, per view,
    never the raw union. Summary match is the same containment over a chunk's
    summary text and topics; a summary contributes no view (written from union
    markup) and no node (the summary cites its chunk).
  - Ordering is (tier, document id, node order, chunk id) — a total sort, so the
    same inputs give the same order every time. Comment hits dedupe into their
    anchor chunk. The two hits of a move fold at query time (`dedupe_moves`, in
    the order `match_document` returned them; never reordered first): one
    result, whose two locations keep both addresses when both ends sit in one
    chunk.
  - Each result records source(s), view(s), citation (document, chunk, node or
    `comment:<id>`, union spans) and the term group, if any. No numeric
    relevance exists anywhere on the record.
- Scale target is about 25 documents; correctness and reproducibility over
  throughput. FTS5 is optional at this scale.
- Detailed build order: `phase2-build-spec.md`.

## Phasing

**Phase 0: contracts, core, fixtures, harness (no parser).**
Deliver: `docextract-core` + form-extract contract test; written text-model spec
(D2); frozen contracts (Node/Comment/Revision/Chunk/TermGroup/TermHit/RunRecord);
versioning machinery (D10); generator sidecars; at least one **real producer
doc** obtained from the user with contested facts hand-written; eval harness skeleton.
Exit: contracts round-trip; flip-one-input green; sidecars match fixtures;
harness emits an empty metrics table.

**Phase 1: deterministic parse, structure, terms (no LLM).**
Deliver: sized OPC spike then walker; node tree; three views; nested revisions;
heading rules; section tree; comments + `commentsExtended` (spec-derived);
chunker v1; term matcher + registry hash; store (raw + node tree + chunks +
hits); FTS5 + manifest; run record.
Exit: L1 100% on sidecars; term recall 1.0 on must-find with misses classified;
determinism and offline hit-reproduction green; unchanged re-ingest is a no-op;
FTS5 rebuild reproduces manifest; `commentsExtended` recorded **unverified**
unless producer doc validates it.

**Phase 2: summaries + routing.**
Deliver: summarizer with full cache key, archived calls, union-marked input +
`pending_changes` field; derived roll-up; fused retrieval with a **defined**
ranking; verified cache hit/miss.
Exit: on the v2->v3 pair only changed chunks re-summarize; cached == archived
call byte-for-byte; a query set returns correct sections including
"was this exclusion deleted?" (the D5 regression test); cost/doc reported.

**Phase 3 (named only):** embeddings if a query set justifies them,
co-location analytics, `superseded` as a matcher target, cross-paragraph
matching if measured non-zero, form-extract migration onto the core.

## Open risks (ranked)

1. **Codec silent field-drop under version skew.** `_decode` drops unknown keys
   (model.py:401), so a record from a newer core read by an older one loses
   fields silently and can cause a false cache hit. Fix: strict mode (raise on
   unknown key) + `schema_version` in the blob; must land in the core in Phase 0.
2. **View-mask boundary semantics** beyond the within-paragraph rule (tables
   cells, footnotes, fields) are unspecified and gated by nothing.
3. **Producer-verified coverage has no owner/date.** If no real Word doc with
   threading arrives, the threading path ships unverified; absent `w14:paraId`
   also degrades diffing to content-hash ids. Unconfirmed: whether LibreOffice
   writes `commentsExtended` (neither side could verify); if no available writer
   does, Phase 1 closes with it recorded unverified.
4. **Retrieval fusion/ranking undefined** (union of term hits, text FTS, summary
   FTS) - the user-visible behavior; must be defined before Phase 2 exit.
5. **L2 label circularity**: must-find terms must come from real docs, not the generator.
6. OPC spike may size well above ~150 lines (relationship resolution, producer
   part renames, strict vs transitional namespaces); the decision stands but the
   Phase 1 estimate depends on it.
7. `Collection[T]` generalizes from a single consumer's three methods; provisional.

## Where minds changed

- **hearth changed Claude's mind on:** python-docx hybrid vs lxml-only was
  argued to lxml-only, but by Claude's side; hearth first pushed "python-docx
  for OPC/style" then conceded lxml-only once the enforcement cost was shown.
  Hearth changed Claude on: text-address model as the primary decision; accepted
  default unsafe (D5); path ids unstable across revisions; comment `w:id`
  instability; term-list hash missing from the reproducibility contract; fuzzy
  cannot be a `TermHit`; extract the core substrate now instead of
  copy-then-diverge; cache key missing model params and view; roll-up
  non-authoritative; verified (not asserted) cache hits; L1/L2/L3 eval split.
- **Claude changed hearth's mind on:** lxml-only (above); `commentsExtended`
  stays in Phase 1 (hearth first proposed deferring it) with tri-state fail-open
  and spec-derived labeling; union-canonical text stream (Claude's refinement,
  which hearth accepted and then hardened with per-part addressing, ancestor
  stacks, and matching on views not union); `superseded` and cross-paragraph
  matching deferred; core created in Phase 0 rather than Phase 3.

## Disagreements

- Effectively none standing. One item to make explicit: hearth insisted the
  summary record carry a structured `pending_changes` field rather than relying
  on markup in the summarizer input. Claude accepts, on the condition that the
  field is computed deterministically from revision records, never authored by the LLM.
- `Collection[T]` is provisional (hearth) vs. simply part of the core (Claude);
  resolved as provisional.
