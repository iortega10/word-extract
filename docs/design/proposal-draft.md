# word-extract — opening proposal (Claude's draft, to be debated)

Sibling of `form-extract` (C:\Users\ivan_\App_repos\form-extract; read its
docs/design/form-extraction-design.md and formextract/model.py for the shared
philosophy). Not a fork: shares a small core, different problem.

## Problem / use case

Someone sends a Word doc (.docx) with comments, tracked changes, tables, lists,
headings, headers/footers. We want to:

1. Deterministically parse it into a structured, addressable tree (no LLM).
2. Chunk it into sections and have a *small* model summarize each section
   ("this section holds X") so a retrieving LLM can find what it needs
   (RAG with section-level summaries as a routing layer).
3. Persist everything in a content-addressed store while structuring, so runs
   are efficient (idempotent, cached per chunk) and reproducible (pinned
   parser/model/prompt versions, archived LLM I/O).
4. Given a list of "like terms" (insurance / regulated: "waiver of
   subrogation" ~ "right of recovery" ~ "subrogation rights"), flag every
   occurrence in body text, tables, AND comments, and group them together.

Real docs have regulated-industry stakes: a missed exclusion or a wrong
attribution ("who commented that?") is a real failure. Auditability matters.

## Key difference vs form-extract

.docx has *explicit* structure (paragraphs, runs, styles, numbering, tables,
comment anchors, revisions) but no geometry. form-extract recovers structure
from geometry; here the work is *normalizing* explicit structure and resolving
anchors. Far more of the pipeline is deterministic; the LLM is only used for
summaries (and optionally fuzzy term-group expansion).

## Proposed pipeline

ingest (docx zip/XML -> Node tree) -> structure (sections, blocks, resolved
numbering/styles, comment anchors, revisions) -> chunk (stable, hash-addressed
section chunks) -> enrich (small-model summary per chunk, cached by chunk hash +
prompt/model version) -> term-index (deterministic match + grouping) -> store
(content-addressed, JSON canonical, optional retrieval index).

### Core model (sketch)

- `Node`: id (stable path-based, e.g. `s2/p3`), kind (heading|para|list_item|
  table|row|cell|comment|revision|header|footer|sdt), text, style, level,
  numbering label ("2.1(a)"), children, `source_ref` (part + xpath/paragraph id).
- `Comment`: id, author, initials, date, text, `parent_id` (threading from
  commentsExtended), `anchor` = (start node/offset, end node/offset) + the
  anchored text span, resolved flag (commentsExtended done).
- `Revision`: ins/del/move, author, date, text, anchor.
- `Chunk`: id = hash(text of nodes + comment texts + parser version), section
  path, node_ids, token count, `summary` (nullable), provenance
  {model, prompt_hash, params}.
- `TermGroup` (user supplied): canonical, synonyms[], optional regex/stemming
  rules, category. `TermHit`: group, matched surface form, node_id, char span,
  location kind (body|table|comment|revision|header), match type
  (exact|stem|fuzzy), confidence-free (deterministic = provenance only).

### Decisions I'm proposing (attack these)

1. **Parse OOXML directly (lxml over the zip) rather than only python-docx.**
   python-docx 1.2 has comments, but not: comment threading (commentsExtended),
   tracked changes, content controls, numbering resolution, text boxes,
   fields, footnotes. Use python-docx only where it's enough, or not at all.
2. **Chunk by heading tree, with comments folded into the chunk of their
   anchor**, plus size-capping (split long sections at paragraph boundaries,
   merge tiny ones up). Tables are atomic chunks unless huge (then row-group
   split, header row repeated).
3. **Summaries are a cache, not truth.** Per-chunk summary keyed by
   (chunk_hash, model, prompt_hash). Unchanged chunks never re-call the LLM
   when a doc revision arrives -> cheap diffs between v2 and v3 of a doc.
   Also a hierarchical roll-up (section -> parent -> doc) built from child
   summaries, not re-read raw text.
4. **Term flagging is deterministic first** (normalized phrase match with
   lemma/stem + user synonym lists; matches are spans with node refs).
   LLM only proposes *candidate new synonyms* for human approval — never
   silently expands the list, because regulated use needs reproducible hits.
   Grouping = by TermGroup; plus co-location report (which groups cluster in
   the same section / comment thread).
5. **Comments are first-class, not annotations on the side.** They're indexed
   and retrievable as their own chunks *and* linked to their anchor text, with
   thread + author + resolved status. Retrieval returns "text + the discussion
   about it".
6. **Revisions default policy is explicit**: default view = "accepted" text for
   summarizing/search, but deleted text and the ins/del authorship are retained
   and searchable (a deleted exclusion is exactly what a regulated reviewer
   needs to find). Policy is a config, recorded on the record.
7. **Storage**: content-addressed JSON (same as form-extract store) is
   canonical. Retrieval index (SQLite FTS5 + optional embeddings) is a
   rebuildable derived artifact, not source of truth. Embeddings optional and
   pluggable; keyword+term-group+summary routing may be enough at this scale.
8. **Shared core**: extract `store`, provenance, LLM-client protocol, eval
   harness scaffolding into a tiny shared package (`docextract-core`?) vs.
   copy-then-diverge. I lean: copy for now, extract once the second consumer
   proves the seam — but open to being argued into extracting up front.
9. **Reproducibility contract**: pinned parser version, node ids stable across
   re-parses of same bytes, chunk ids content-derived, all LLM calls archived,
   run record lists every cache hit/miss.

## Fixtures (already generated)

`fixtures/` via `tools/make_fixtures.py`: `program_review_v3.docx` (headings,
numbered exclusions, merged-cell table, header/footer, 5 comments incl. two on
the same range, tracked del+ins), `binder_summary.docx` (same concepts in
different wording -> term-group test), `edge_cases.docx` (range comment across
runs, comment in table cell, nested list). Known gap: python-docx can't write
threaded replies or resolved flags — need hand-built XML fixtures for
commentsExtended.

## Open questions to argue

- Is a hand-rolled OOXML walker worth it vs python-docx + patches?
- Chunk granularity: what is the right unit for section summaries?
- Should term matching live in this package or be a separate consumer of the
  store? (Depends on whether groups/terms are per-run input or persistent.)
- Eval plan: what's the golden set and what are the metrics (comment-anchor
  accuracy, term recall, summary faithfulness, cost per doc, cache hit rate on
  revised docs)?
- Phasing: what's Phase 0/1 vs later?
