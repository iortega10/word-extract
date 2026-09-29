# Phase 2 build spec: summaries, routing, query API, MCP tools

**Prerequisite: Phase 1 must be built, validated and committed first.** This
spec consumes Phase 1's stored chunks, `UnionStream`, node tree, comments,
revisions, term hits and run record. Do not start it before then. If Phase 1
closed conditionally (L2 not evaluated, no Word-produced document), say so in
your report; it changes what can be claimed here, not what can be built.

Source of truth: `docs/design/word-extraction-design.md` (D5, D7, D8, D10,
D11, D12, "Phase 2"), `docs/design/text-model-spec.md`, and the Phase 1 code as
committed. If this file conflicts with the design doc, the design doc wins;
report the conflict. Scale target: **about 25 documents, roughly 1,000 to 1,500
chunks in total.** Design for correctness and reproducibility, not scale.

## Ground rules

- **The LLM never produces facts that the deterministic layer owns.** Term
  hits, comment authors and dates, revision facts and `pending_changes` are
  computed from stored records. The LLM only writes summary prose.
- **Summaries are a cache, not truth.** Keyed by the full tuple below; a hit
  is verified by recomputing the key. A summary is never an input to hashing
  of chunks, hits or the term index.
- **Every query result carries a citation**: document id, chunk id and/or node
  id, `view_id`, and offsets or a `comment:<id>` reference. No result without one.
- **No blended relevance score.** Results are labeled by source and ordered by
  a documented, deterministic rule (see Turn 3). Never fuse BM25, embeddings
  or model scores into one number.
- **Views are always named.** No tool defaults to accepted-only silently: every
  response states the view(s) it used; the summarizer input uses the union with
  revision markup (D5).
- Determinism where possible: everything except the model's text output is
  byte-reproducible; the model output is reproducible by replaying the
  archived call.
- No python-docx. No network calls in tests: a canned LLM client (like
  form-extract's `evals/canned.py`) drives all tests.
- Style matches Phases 0 and 1. Never commit; report the diff and full test
  output. Each turn ends green.

## Layout to create

```
wordextract/
  summarize.py     # per-chunk summarizer, cache, archived calls
  rollup.py        # derived, non-authoritative section/doc summaries
  pending.py       # pending_changes computed from revision records
  query.py         # plain functions; NO MCP imports
  rank.py          # documented deterministic ordering
  llm.py           # LLMClient adapter(s) on the core protocol; canned client
  mcp/
    tools.py       # register_tools(mcp, backend, settings)
    server.py      # FastMCP entrypoint
    settings.py
tests/
docs/design/phase2-gaps.md
```

MCP pattern to mirror (read-only reference): `C:\Users\ivan_\App_repos\code-mcp`
(`src/code_mcp/{tools.py,server.py,config.py}`): `FastMCP` from the `mcp`
package (`mcp>=1.10,<2`), tools registered as closures inside
`register_tools(mcp, backend, settings)`, all logic in plain modules, MCP-awareness
quarantined to `tools.py` + `server.py`. `mcp` is an **optional extra**
(`pip install -e ".[mcp]"`), so the core package and its tests do not require it.

## Turn 1: summarizer and cache

- Input to the model for a chunk: the chunk's **union text with revision
  markup** (inserted and deleted text both visible and marked, with author and
  date), plus the comment texts anchored in the chunk as context. Never the
  accepted-only text.
- Summary record fields: chunk id, summary text, the full cache key inputs,
  `pending_changes` (see Turn 2), `labels_provenance`-style provenance
  (`model`, `prompt_hash`, `params_hash`), and the archived call reference.
- **Cache key** (D7): `Chunk.content_hash` + `Chunk.context_hash` + `view_id` +
  `SUMMARIZER_VERSION` + `model_id` + `model_params_hash` + `prompt_hash` (+
  `output_schema_version`). Compute with `versions.compute_key`. Changing any
  member changes the key; identical inputs never call the LLM again.
- **A hit is verified by recomputing the key** and recorded in the run record
  (`ArtifactCache`), same as Phase 1.
- Archive every call with the core's `archive_llm_call` (prompt, response,
  model, params, tokens). Replaying an archived call reproduces the cached
  summary byte for byte.
- The prompt is a versioned file under `wordextract/prompts/`; `prompt_hash` is
  its content hash. The prompt instructs: summarize what this section
  contains, name the topics and terms it covers, note open questions raised in
  comments, and **do not state whether text is in force when revision markup is
  present; report only that revisions exist** (the deterministic
  `pending_changes` field carries the facts).
- Output is structured JSON validated on decode (strict codec): `summary`,
  `topics: list[str]`, `open_questions: list[str]`. Reject and record output
  that fails validation; never store partial.
- The summarizer accepts any `LLMClient` from the core protocol. Ship a canned
  client for tests. A small local model is a valid target; do not assume a
  specific provider.

Tests: cache hit skips the call; flip each key member and the key changes; a
comment-only edit changes `context_hash` and re-summarizes **only** that chunk;
replayed archived call equals cached summary; malformed model output is
rejected and recorded, not stored.

## Turn 2: `pending_changes`

Computed by `pending.py` from stored `Revision` and `Comment` records, **never
from LLM text**.

- Per chunk: list of `{kind, author, date, span, text_excerpt, move_group_id}`
  for every revision touching the chunk, plus a boolean `has_pending`.
- Deterministic ordering (document order, then id). Missing dates are `None`.
- Attached to every summary record and returned by every chunk-level tool.
- **Regression test (the D5 exit test):** in the `program_review_v3` fixture,
  the pollution exclusion has a pending deletion ("excluded in all cases") and
  insertion. The summary record for that chunk must report `has_pending = true`
  with both revisions listed, and a query for "was this exclusion deleted?"
  must surface the deletion with author and date. This test must pass with the
  canned LLM returning a summary that **omits** any mention of the deletion.

## Turn 3: retrieval and ranking (closes Open risk #4)

`rank.py` defines the ordering; write it into `docs/design/word-extraction-design.md`
as D12 in the same change. The rule must be deterministic and explainable.

- Retrieval is the **union of three sources**, each result labeled with its source:
  1. **term hits** (deterministic matcher; precision path),
  2. **text match** over chunk view text (FTS5 if Phase 1 built it, else an
     in-memory scan; at this scale either is fine),
  3. **summary match** over summary text/topics (discovery path).
- Ordering is a **fixed source-priority tiering**, not a fused score: term
  hits first (ordered by group, then document order), then text matches, then
  summary matches. Within a tier, deterministic tie-breaks (document id, node
  order). A chunk appearing in several tiers is returned **once** with all its
  sources listed (dedupe at rank time; comment results dedupe into their anchor chunk).
- Each result records: source(s), matched view(s), citation (document, chunk,
  node or `comment:<id>`, offsets), and the term group if any. No numeric relevance.
- Moves dedupe at query time on `(group, move_group_id, normalized text,
  intra-group ordinal)` as decided in Phase 1.
- If an embedding index is ever added (Phase 3) it slots in as a fourth,
  separately labeled source; **do not build it here**.

Tests: same query, same order, every time; a chunk hit by a term and by text is
returned once with both sources; a comment hit dedupes into its anchor chunk;
move hits collapse to one result with two locations; ordering follows the
tiering rule on a hand-built mixed case.

## Turn 4: roll-up (derived, non-authoritative)

- Section and document summaries built **from child summaries**, not raw text.
  Each roll-up record lists its child chunk ids and is stamped
  `non_authoritative: true`.
- Roll-ups feed routing only (outline and `list_documents`); they are **never
  the sole basis for a claim** and never an input to term hits or `pending_changes`.
- A roll-up cache key includes its children's summary keys, so it invalidates
  when any child changes.

Tests: changing one chunk re-summarizes that chunk and rebuilds only the
roll-ups above it; roll-up carries the union of its children's `has_pending`
flags (computed, not model-written).

## Turn 5: query API (`query.py`, no MCP imports)

Plain functions over the store, all returning plain dataclasses with citations.
Each takes an explicit `view` argument (default documented and echoed back).

- `list_documents()`: id, title, doc roll-up summary, `has_pending`, comment count.
- `get_outline(doc)`: section tree with per-section roll-up summary and
  `has_pending`.
- `search(query, scope=None, sources=None)`: Turn 3 retrieval.
- `get_chunk(chunk_id, view)`: text in the chosen view, member node ids,
  comments anchored in it, `pending_changes`.
- `find_terms(group, doc=None)`: every hit for a term group, grouped by
  document and section, with view(s), location kind and citation. Deterministic.
- `get_comments(doc=None, section=None, author=None)`: comments with author,
  initials, date, thread (`parent_id`, `threading_status`, `resolved`), and the
  anchored text. Report `threading_status` honestly; never say "no replies" when
  it is `unknown` or `absent`.
- `get_revisions(doc=None, section=None)`: tracked changes with text,
  author, date, and move groups; **deleted text included**.
- `compare(doc_a, doc_b, group=None)`: for two documents (or versions), which
  chunks changed by `Chunk.id`, and optionally the term-hit differences. No LLM.

Every function is pure over stored records; none calls an LLM. Tests cover each
function on the fixtures and on the underwriting sample.

## Turn 6: MCP tools

- `mcp/tools.py`: `register_tools(mcp, backend, settings)`; one closure per
  Turn 5 function, thin: validate args, call `query.py`, return the dataclass as
  JSON. No logic beyond argument handling.
- `mcp/server.py`: `create_mcp(settings)` returning a `FastMCP`; entrypoint
  `python -m wordextract.mcp`. `settings.py`: store path, default view.
- Tool descriptions state the view semantics and that results carry citations.
- Read-only: **no tool mutates the store or triggers ingestion or summarization.**
  Ingest and summarize stay on the CLI.
- `mcp` is an optional extra; tests for `mcp/` skip gracefully if it is not
  installed, and tests for everything else never import it.

Tests: each tool returns valid JSON with citations; tool schemas list the
expected arguments; a tool call cannot write to the store (assert store bytes
unchanged); server constructs without network.

## Turn 7: evals and exit check

- **L3 summary faithfulness** (reported, never gated): a rubric over a small
  sample (n about 10 to 15 chunks), regraded on any prompt or model change.
  Build the sample selector and rubric file, and the report; **grading is done
  by a human** (or clearly marked model-graded and non-authoritative). Do not
  gate on it.
- **Cost metrics** (kept separate from quality): calls, tokens and cost per
  document; **revised-document cache hit rate** on the v2 to v3 pair.
- **Retrieval checks** against a query set: a small file
  `queries.example.json` of `{query, expected_citations}` with
  `labels_provenance: human`. Score whether expected citations appear; report,
  and gate only once real human labels exist.
- Update `producer_verified_coverage` and known-gaps reporting.

## Blocked on the user (do not fake)

1. **A real query set** with expected citations (the questions actually asked of
   these documents). Without it retrieval is unscored.
2. **Model choice and access** for the summarizer (provider, model id, params).
   Build against the protocol and the canned client; do not assume a provider.
3. **Human grading** for L3.
4. **A Word-produced document** (carried over from Phase 1).

## Exit criteria (design Phase 2, as amended)

- On the v2 to v3 pair, **only changed chunks re-summarize** (test).
- A cached summary equals its archived call byte for byte (replay test).
- The **D5 regression test** passes: "was this exclusion deleted?" surfaces the
  deletion with author and date even when the summary omits it.
- Retrieval order is deterministic and follows the documented tiering; every
  result has a citation; no blended score.
- Query API and MCP tools are read-only, cite sources, and name their views.
- Cost per document and revised-document cache hit rate are reported.
- L3 reported, not gated; retrieval checks reported, gated only with human labels.
- `docs/design/phase2-gaps.md` lists every known gap; D12 is added to the design doc.
- Final report: files created, full pytest output, each ambiguity resolved and
  how, each contract change, and anything not done.

## Watch-list

- Never let summary text answer a question the deterministic records own
  (revisions, comment authorship, term hits). The tools return the records.
- A roll-up is a router, not evidence. Do not cite it as a source.
- Threading and identity stay labeled unverified until a Word-produced doc validates them.
- Keep `mcp` optional and `query.py` free of MCP imports so the layer is
  testable and reusable without a server.
