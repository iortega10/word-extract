# Phase 2 build spec: summaries, routing, query API, MCP tools

Revision 2. Rewritten after checking revision 1 against the Phase 1 code as built. The
changes, and why, are listed in "What revision 2 changed" at the end; read that first if
you know revision 1.

**Prerequisite: Phase 1 is built, validated and committed** (it is: Turns 0 to 9, with
Turn 8 deferred). Phase 1 closed **conditionally**: L2 is not evaluated (no human labels,
no real term lists), producer-verified coverage is 0 of 25 paths (no Word-saved document).
Say so in your report; it changes what can be claimed here, not what can be built.

Source of truth: `docs/design/word-extraction-design.md` (D5, D7, D8, D10, D11, D12,
"Phase 2"), `docs/design/text-model-spec.md`, `docs/design/phase1-gaps.md`,
`docs/design/open-inputs.md`, and the Phase 1 code as committed. If this file conflicts
with the design doc, the design doc wins; report the conflict. Scale target: **about 25
documents, roughly 1,000 to 1,500 chunks in total.** Design for correctness and
reproducibility, not scale.

## Decisions made by the user

1. **Document id** is the full `source_content_hash`; the **title** is the original
   filename. No new parse fields (titles are not parsed today).
2. A **Turn 0** of shared helpers runs first, as its own hearth run, before any LLM work.
3. `search` takes an explicit `term_list` (a `term_list_hash`), or uses the store's term
   list when it holds exactly one; otherwise it is an error that names the candidates.

## Ground rules

- **The LLM never produces facts that the deterministic layer owns.** Term hits, comment
  authors and dates, revision facts and `pending_changes` are computed from stored
  records. The LLM only writes summary prose.
- **Summaries are a cache, not truth.** A summary is keyed by the exact input it was
  written from (Turn 1); a hit is verified by recomputing the key. A summary is never an
  input to the hashing of chunks, hits or the term index.
- **Every query result carries a citation**: document id, chunk id and/or node id, the
  view(s) used, union spans, and a `comment:<id>` reference for comment text. No result
  without one.
- **No blended relevance score.** Results are labeled by source and ordered by a
  documented, deterministic rule (Turn 3). Never fuse text scores, embeddings or model
  scores into one number.
- **Views are always named.** No tool defaults to accepted-only silently: every response
  states the view(s) it used. The summarizer's own view permit is `union-markup` (D5).
- **Everything except the model's text is byte-reproducible**; the model's text is
  reproducible by replaying the archived call. The core's `LLMCall.latency_ms` and any
  timestamp are **recorded-only** and never enter a summary record or a key.
- **Behavior changes bump versions and the ledger.** Phase 1's behavior ledger
  (`tools/behavior_ledger.py`, `tests/ledger/behavior_ledger.json`) applies: any change to
  what a component outputs bumps its version constant and appends a ledger line in the
  same commit. Phase 2 adds components to it (Turn 0 to Turn 3).
- No python-docx. No network calls in tests: a canned LLM client drives all tests.
- Style matches Phases 0 and 1. Never commit; report the diff and full test output. Each
  turn ends green. A large turn may be split into separately green parts.

## Layout to create

```
wordextract/
  render.py        # chunk text in a view; union-with-markup rendering (Turn 0)
  catalog.py       # document catalog over the store (Turn 0)
  summarize.py     # per-chunk summarizer, cache, archived calls
  rollup.py        # derived, non-authoritative section/document summaries
  pending.py       # pending_changes derived from union stacks
  query.py         # plain functions; NO MCP imports
  rank.py          # documented deterministic ordering
  llm.py           # adapters on the core LLMClient protocol; canned client
  prompts/         # versioned prompt files, shipped as package data
  mcp/
    tools.py       # register_tools(mcp, backend, settings)
    server.py      # FastMCP entrypoint
    settings.py
tools/make_fixtures.py   # extended: the v2 / v3 pair (Turn 0)
docs/design/phase2-gaps.md
```

MCP pattern to mirror (read-only reference): `C:\Users\ivan_\App_repos\code-mcp`
(`src/code_mcp/{tools.py,server.py,config.py}`): `FastMCP` from the `mcp` package
(`mcp>=1.10,<2`), tools registered as closures inside `register_tools(mcp, backend,
settings)`, all logic in plain modules, MCP-awareness quarantined to `tools.py` +
`server.py`. `mcp` is an **optional extra** (`pip install -e ".[mcp]"`).

## Turn 0: shared helpers (before any LLM work)

None of this calls a model. Revision 1 assumed these existed; they do not.

**0a. Read-only store.** `Collection` creates its directory in its constructor, so merely
opening a store writes. Add `Store(root, read_only=True)` (and the matching `Collection`
mode): it never creates a directory, never writes, and raises a clear error when asked to.
Test: opening a populated store read-only leaves every file's bytes and mtime unchanged;
opening a non-existent root read-only raises instead of creating it.

**0b. Chunk text and the marked-up union (`render.py`).** Chunks store ids and hashes, not
text; the tests rebuild text with a helper. Production code needs the same, once:

- `chunk_text(parsed, chunk, view) -> str`: the chunk's own view text, its member leaf
  nodes (paragraph, heading, list item) joined by `"\n"` in node order, each leaf's text
  taken from the view projection of its union span. It must equal what `Chunk.size` and
  `Chunk.content_hash` were computed over; test that on every fixture.
- `render_union_markup(parsed, chunk, comments=...) -> str`: the **union** text of the
  chunk's leaves with revision markup, deterministic and exactly this format:
  - each elementary span is wrapped, outermost revision first, in
    `[[<kind> author="<author>" date="<date>"]]` ... `[[/<kind>]]`, where `<kind>` is
    `ins`, `del`, `moveFrom` or `moveTo`; a move adds `group="<move_group_id>"`; `date` is
    omitted when `None`; attribute values are escaped (`"` as `&quot;`, `]` as `&#93;`);
    adjacent spans with the identical stack share one wrapper; text with no revision is
    bare; leaves are joined by `"\n"` in node order;
  - revision ids come from the span's ancestor stack and are resolved through
    `ParseResult.revisions`; an id with no record renders `author=""` and is counted;
  - **comment context** follows the text: for each comment that *starts* in the chunk (the
    chunker's containment rule), sorted by `para_id`, one line
    `[[comment by="<author>" date="<date>" resolved="<true|false|unknown>"]]<anchor text>
    => <comment text>[[/comment]]`;
  - a **revision manifest** follows: one line per revision touching the chunk, from
    `pending_changes` (Turn 2), so the model is told the facts it must not invent.
  Test: hand-typed expected strings for the `program_review_v3` shortfall paragraph (the
  tracked deletion and insertion), a nested `del` inside an `ins`, and a move; a chunk with
  no revisions renders as its plain union text plus context.

**0c. Anchor to node to section (public).** `terms._anchor_node` is private. Move it to a
public helper (with its `_parents` and `_ancestor_of_kind` support) used by terms, query
and catalog. Add `section_of(parsed, node_id)`. Behavior unchanged; the ledger must stay
green.

**0d. Document catalog (`catalog.py`).** The store has content hashes, a raw archive index
(`original_filename`) and run records, but no catalog. Derive one, writing nothing:
`list_documents(store)` returns, per document, `document_id` (the full
`source_content_hash`), `title` (the original filename), the latest run's `run_id`, and the
artifact keys for each view and term list ingested. A document ingested twice is one row.

**0e. The v2 / v3 fixture pair.** Only `program_review_v3.docx` exists. Extend
`tools/make_fixtures.py` to also write `program_review_v2.docx` (v3 minus a defined set of
edits) and a sidecar for the pair listing, **hand-typed**, exactly which chunks differ and
why: one body paragraph edited, one comment's text edited, one paragraph unchanged. v2 and
v3 must otherwise be identical so the pair isolates those edits. Existing fixtures'
bytes must not change (the regeneration test must stay green).

Exit for Turn 0: all helpers tested, ledger green, no behavior change to Phase 1 output.

## Turn 1: summarizer and cache

**Which chunks.** Summaries are produced for the **accepted-view chunk set** (the chunk
record of the store). The original view has the same boundaries, and its chunks map to the
accepted ones by identical `node_ids`; query tools use that mapping. One summary per
chunk, not per view.

**The key is a subset, never `compute_key`.** `versions.compute_key` hashes every
`HashedInputs` field, including `source_content_hash`, `term_list_hash`, the chunker
parameters and the matcher version. Using it would re-summarize every chunk when one
paragraph changed, or when a term list was added, which defeats the point. Build the key
the way Phase 1's store builds its own (`_key(kind, subset)`):

```
summary key = sha256 of canonical JSON of {
  "kind": "summary",
  "input_hash":          sha256 of the exact rendered input (Turn 0b: union markup text,
                         comment context and revision manifest),
  "view_id":             "union-markup",
  "summarizer_version":  SUMMARIZER_VERSION,
  "model_id", "model_params_hash", "prompt_hash",
  "output_schema_version"
}
```

`input_hash` replaces revision 1's `Chunk.content_hash + context_hash`. Those are
view-specific or comment-specific stand-ins; a change to *deleted* text alone changes the
accepted-view `content_hash` not at all, so a key built on it would call a stale summary a
hit. Hashing the rendered input makes the key track exactly what the model saw. A test must
show: editing only a tracked-deleted word changes the key.

**Record and storage.** New contract records `SummaryArtifact` (key, chunk id, document id,
`summary`, `topics`, `open_questions`, `pending_changes`, production provenance: `model_id`,
`prompt_hash`, `params_hash`, `call_ref` from the archived call) and an `output validation`
rejection record. Store collection `summaries/` through the same `Collection` machinery as
Phase 1. These are new dataclasses, so the **contracts fingerprint changes: bump
`SCHEMA_VERSION` to 6** and append the ledger line in the same commit.

**Run record.** `ARTIFACT_KINDS` is a static tuple, but a document has one summary per
chunk. Report each as an `ArtifactCache` entry with `artifact_id = "summary:<chunk_id>"`,
keeping the existing five kinds. A hit is verified by recomputing the key.

**Calls.** Archive every call with the core's `archive_llm_call` (prompt, response, model,
params, tokens). Replaying an archived call reproduces the stored summary byte for byte.
The summary record embeds the call's `prompt_ref` and `response_ref` but **not**
`latency_ms` or `call_id` timestamps.

**Prompt.** A versioned file under `wordextract/prompts/` shipped as package data (add it
to `pyproject.toml`'s package-data); `prompt_hash` is its content hash. It instructs:
summarize what this section contains, name the topics and terms it covers, note open
questions raised in comments, and **do not state whether text is in force when revision
markup is present; report only that revisions exist**.

**Output.** Structured JSON validated on decode through the strict codec: `summary`,
`topics: list[str]`, `open_questions: list[str]`. Reject and record output that fails
validation; never store partial.

**Client and CLI.** The summarizer accepts any `LLMClient` from the core protocol; ship a
canned client for tests. Do not assume a provider. Add `python -m wordextract summarize
<docx> --terms <file> --client <name>`, which ingests if needed and then summarizes; a real
client is configured outside the repo (no key, URL or model id is committed).

**Ledger.** Add components `render` (the Turn 0b output for every chunk of the corpus,
keyed by a `RENDER_VERSION`) and `summary_key` (the key computed for the corpus with a
fixed model id, params and prompt), plus the contracts bump above.

Tests: cache hit skips the call; flip each key member and the key changes; a **comment-only
edit changes only the affected chunk's key**; a body edit elsewhere leaves it a hit; adding
a term list re-summarizes nothing; replayed archived call equals the cached summary;
malformed model output is rejected and recorded, not stored.

## Turn 2: `pending_changes` (`pending.py`)

Computed from the **union stream and revision records**, never from LLM text. A `Revision`
record has no span and no text (id, kind, author, date, move group, ancestors), so
"revisions touching a chunk" is derived:

- for each leaf node in `chunk.node_ids`, take the elementary spans its union span covers;
  collect the revision ids on their ancestor stacks; the revision's text excerpt is the
  union text of the spans whose stacks carry it (truncated to a fixed length, with an
  explicit marker when truncated);
- each entry: `{revision_id, kind, author, date, spans, text_excerpt, move_group_id}`,
  ordered by first span start then id; missing dates are `None`; `has_pending` is true when
  any entry exists;
- attached to every summary record and returned by every chunk-level tool.

**Revisions that cannot be attributed to a chunk.** A paragraph-mark revision (the
`paragraph_mark_revision` gap) is a `Revision` record with no stack and no node link, and
tracked formatting changes (`unrecorded_revision_kind`) are not records at all. They must
not be silently absent: expose a **document-level** `unattributed_revisions` count and the
relevant `known_gaps` ids on the document row and on `get_revisions`, so a reviewer sees
that the document has revisions no chunk accounts for.

**Regression test (the D5 exit test):** in `program_review_v3`, the shortfall exclusion has
a pending deletion ("excluded in all cases") and insertion. The summary record for that
chunk must report `has_pending = true` with both revisions listed (author `R. Alvarez`),
and a query for "was this exclusion deleted?" must surface the deletion with author and
date. It must pass with the canned LLM returning a summary that **omits** any mention of
the deletion.

Ledger: add `pending` (output over the corpus).

## Turn 3: retrieval and ranking (closes Open risk #4)

`rank.py` defines the ordering; **amend D12 in `word-extraction-design.md`** (it already
exists with the tiering) with the details below in the same change. The rule is
deterministic and explainable.

- Retrieval is the **union of three sources**, each result labeled with its source:
  1. **term hits** (the precision path): the free-text query is **resolved through the
     registry**: normalize it with the matcher's own `normalize`, and if it equals a
     group's canonical or synonym form, that group's stored hits are the term tier. No
     match, no term tier (never a fuzzy guess). The term list is explicit or the store's
     only one (decision 3);
  2. **text match** over chunk view text, using `chunk_text` (Turn 0b), by normalized
     token containment; an in-memory scan is correct at this scale (FTS5 is deferred);
  3. **summary match** over summary text and topics (the discovery path).
- Ordering is a **fixed source-priority tiering**, not a fused score: term hits first
  (ordered by group, then document order), then text matches, then summary matches.
  Within a tier: document id, then node order. A chunk in several tiers is returned
  **once** with all its sources listed; comment hits dedupe into their anchor chunk.
- Each result records: source(s), matched view(s), citation (document, chunk, node or
  `comment:<id>`, union spans), and the term group if any. No numeric relevance.
- Moves dedupe at query time with `dedupe_moves`, in the order `match_document` returned
  the hits (the fold's ordinals depend on that order; do not reorder first).
- A future embedding index slots in as a fourth, separately labeled source; **do not
  build it here**.

Ledger: add `rank` (results for a fixed query set over the corpus with the synthetic
registry and canned summaries).

Tests: same query, same order, every time; a chunk hit by a term and by text is returned
once with both sources; a comment hit dedupes into its anchor chunk; move hits collapse to
one result with two locations; an ambiguous term list is an error naming the candidates;
ordering follows the tiering on a hand-built mixed case.

## Turn 4: roll-up (derived, non-authoritative)

- Section and document summaries built **from child summaries**, not raw text, with their
  own versioned prompt (`prompt_hash`) and their own key: the children's summary keys, the
  roll-up prompt and the model fields. Each record lists its child chunk ids and is
  stamped `non_authoritative: true`. Report in the run record as
  `rollup:<section or document id>`.
- Roll-ups feed routing only (outline and `list_documents`); never the sole basis for a
  claim, never an input to term hits or `pending_changes`.
- `has_pending` on a roll-up is the **union of its children's flags, computed**, never
  model-written.
- A section with many chunks is rolled up hierarchically so the input fits a small
  model's context; state the fan-in limit as a parameter hashed into the key.

Tests: changing one chunk re-summarizes that chunk and rebuilds only the roll-ups above
it; the roll-up's `has_pending` equals the union of its children's.

## Turn 5: query API (`query.py`, no MCP imports)

Plain functions over a **read-only** store (Turn 0a), returning plain dataclasses with
citations. Each takes an explicit `view` where views matter, echoed back in the result.

- `list_documents()`: from the catalog (Turn 0d): document id, title (filename), doc
  roll-up summary, `has_pending`, comment count, `unattributed_revisions`.
- `get_outline(doc)`: section tree with per-section roll-up summary and `has_pending`.
- `search(query, term_list=None, scope=None, sources=None)`: Turn 3 retrieval.
- `get_chunk(chunk_id, view)`: text in the chosen view (`chunk_text`), member node ids,
  comments anchored in it (with their own text), `pending_changes`.
- `find_terms(group, term_list=None, doc=None)`: every hit for a term group, grouped by
  document and section (`section_of`), with view(s), location kind and citation.
- `get_comments(doc=None, section=None, author=None)`: author, initials, date, thread
  (`parent_id`, `threading_status`, `resolved`), anchored text, comment text. Report
  `threading_status` honestly: never say "no replies" when it is `unknown` or `absent`.
- `get_revisions(doc=None, section=None)`: tracked changes with text, author, date and
  move groups; **deleted text included**; plus the document-level unattributed count.
- `compare(doc_a, doc_b, group=None)`: two **explicit document ids** (the store has no
  notion that one document is a revision of another): which chunks changed by `Chunk.id`,
  and optionally term-hit differences. No LLM.

Every function is pure over stored records; none calls an LLM. Tests cover each function
on the fixtures, the sample document and the v2 / v3 pair.

## Turn 6: MCP tools and CLI

- `mcp/tools.py`: `register_tools(mcp, backend, settings)`; one closure per Turn 5
  function, thin: validate arguments, call `query.py`, return the dataclass as JSON.
- `mcp/server.py`: `create_mcp(settings)` returning a `FastMCP`; entrypoint
  `python -m wordextract serve` (and `python -m wordextract.mcp`). `settings.py`: store
  path, default view, default term list.
- Tool descriptions state the view semantics and that results carry citations.
- **Read-only**: the backend opens the store with `read_only=True`. No tool mutates the
  store or triggers ingestion or summarization; those stay on the CLI.
- `mcp` is an optional extra; its tests skip when it is not installed, and tests for
  everything else never import it.

Tests: each tool returns valid JSON with citations; tool schemas list the expected
arguments; a tool call cannot write to the store (every file's bytes and mtime unchanged,
no directory created); the server constructs without network.

## Turn 7: evals and exit check

- **L3 summary faithfulness** (reported, never gated): a rubric over a small sample (n
  about 10 to 15 chunks), regraded on any prompt or model change. Build the sample
  selector, the rubric file and the report; **grading is done by a human** (or clearly
  marked model-graded and non-authoritative). Do not gate on it.
- **Cost metrics** (separate from quality): calls, tokens and cost per document;
  **revised-document cache hit rate** on the v2 / v3 pair (Turn 0e): exactly the chunks the
  hand-typed sidecar lists as changed are re-summarized, every other chunk is a hit.
- **Retrieval checks** against a query set: `queries.example.json` of
  `{query, expected_citations}` with `labels_provenance: human`, never scored while it is
  the example (same rule as `fixtures/evals/must_find.example.json`). Score whether
  expected citations appear; gate only once real human labels exist.
- Update `producer_verified_coverage` and known-gaps reporting; add the Phase 2
  components' ledger lines (`render`, `summary_key`, `pending`, `rank`) and keep
  `behavior_ledger.check()` green.

## Blocked on the user (do not fake)

1. **A real query set** with expected citations (the questions actually asked of these
   documents). Without it retrieval is unscored.
2. **Model choice and access** for the summarizer (provider, model id, params). Build
   against the protocol and the canned client; do not assume a provider.
3. **Human grading** for L3.
4. **A Word-saved document** (carried over from Phase 1; see `docs/design/open-inputs.md`),
   and the **real term lists** (also tracked there; currently unavailable).

## Exit criteria (design Phase 2, as amended)

- On the v2 / v3 pair, **only the chunks the sidecar lists as changed re-summarize** (test),
  and editing only a tracked-deleted word changes that chunk's summary key (test).
- A cached summary equals its archived call byte for byte (replay test).
- The **D5 regression test** passes: "was this exclusion deleted?" surfaces the deletion
  with author and date even when the summary omits it.
- Retrieval order is deterministic and follows the documented tiering; every result has a
  citation; no blended score.
- Query API and MCP tools are read-only (store bytes and directory set unchanged), cite
  sources and name their views.
- Cost per document and revised-document cache hit rate are reported.
- L3 reported, not gated; retrieval checks reported, gated only with human labels.
- The behavior ledger covers the Phase 2 components and is green; `SCHEMA_VERSION` is the
  current value in `docextract_core/codec.py` (it was 6 after Turn 1 and 7 after Turn 4; each
  bump was recorded in the ledger in its own commit).
- `docs/design/phase2-gaps.md` lists every known gap (including the unattributed-revision
  case); D12 is amended in the design doc.
- Final report: files created, full pytest output, each ambiguity resolved and how, each
  contract change, and anything not done.

## Watch-list

- Never let summary text answer a question the deterministic records own (revisions,
  comment authorship, term hits). The tools return the records.
- A roll-up is a router, not evidence. Do not cite it as a source.
- The summary key tracks the **rendered input**, not a view's content hash, so a change
  hidden from the accepted view (a deleted word) still invalidates the summary.
- Do not use `versions.compute_key` for summaries; it hashes inputs a summary must not
  depend on.
- Threading and identity stay labeled unverified until a Word-saved document validates them.
- Keep `mcp` optional and `query.py` free of MCP imports so the layer is testable and
  reusable without a server.
- Opening the store read-only must never create or modify anything.

## What revision 2 changed

1. **Summary key**: a subset keyed on the rendered input hash, not `compute_key` and not a
   view's `content_hash` (revision 1 would have re-summarized every chunk on any edit, and
   called a stale summary a hit when only deleted text changed).
2. **Turn 0 added**: read-only store, chunk text and union-markup renderer (format
   specified), public anchor-to-section helper, document catalog, and the v2 / v3 fixture
   pair that the exit test needs and that did not exist.
3. **`pending_changes`** is derived from union stacks (a `Revision` has no span or text),
   with a document-level flag for revisions no chunk can account for.
4. **Search** resolves the query through the registry for the term tier and requires an
   explicit or unique term list.
5. **Process**: schema bump to 6, Phase 2 components in the behavior ledger, per-summary
   run-record ids, a `summarize` and `serve` CLI, no `latency_ms` in records, prompts as
   package data, D12 amended rather than added.
