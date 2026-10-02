# Phase 2 known gaps

What Phase 2 (summaries, `pending_changes`, ranking, roll-ups, the query API and MCP tools)
does **not** do or cannot claim. Each entry says what is missing and what a user must not
infer from it. Phase 1's gaps (parse-level, with `known_gaps` ids) are in `phase1-gaps.md`;
those still apply and are not repeated here.

## Revisions and `pending_changes`

- **Unattributed revisions.** `pending_changes` is derived from the chunk's union stacks, so
  a revision that is on no body-chunk stack never appears in any chunk. Three cases:
  a paragraph-mark revision (`paragraph_mark_revision`: a record with no stack),
  tracked formatting (`unrecorded_revision_kind`: not a record at all), and a tracked change
  in a header, footer, footnote or endnote (chunks cover the body part only). The document row
  exposes `unattributed_revisions` and `unattributed_gaps` so this is never silent, but the
  header/footer case carries no gap id naming the part.
- **`has_pending` is false per chunk, not per document.** A chunk can read false while the
  document has unattributed revisions; read both.
- **Excerpts are truncated** to `EXCERPT_LIMIT` characters with an explicit marker; the
  spans still cover the whole revision.

## Summaries and roll-ups

- **Summaries are model prose, never facts.** Term hits, comment authors, dates and
  `pending_changes` come from the parse. A summary may omit or misstate anything; it is a
  routing aid and a cache, keyed on the exact rendered input.
- **No provider is wired.** The canned client is the only client in the repository; a real
  one is named on the command line (`module:attribute`) and configured outside the repo.
  Nothing here has been run against a real model, so summary quality is unmeasured.
- **Prompt injection is not neutralised.** The rendering passes the document's own text and
  comments through unescaped (only attribute values are escaped), so a document can contain
  text that looks like markup or an instruction. The prompts tell the model to treat it as
  content, which is a mitigation, not a guarantee.
- **A stored rejection counts as a cache hit.** A refused answer is not retried until it is
  evicted (`store.rejections.evict(key)`); there is no command for it yet.
- **Roll-ups are non-authoritative.** They restate their children and feed routing only. A
  section with one chunk still gets a roll-up call that restates one summary.
- **Cost.** One `summarize` pass makes a call per chunk plus one per section with content and
  one for the document, and there is no flag to skip the roll-ups. A schema bump evicts and
  rebuilds stored artifacts, so existing summaries are re-called once after an upgrade.
- **Original-view runs are not summarized.** Summaries are per accepted-view chunk; a run
  ingested in another view is refused, not mapped.

## Retrieval

- **Three sources, fixed tiering, no score.** Term, then text, then summary. There is no
  relevance number and no embedding source (Phase 3).
- **Term tier needs an exact registry form.** A query that is not a group's canonical or
  synonym form has no term tier; there is no stemming or fuzzy resolution of the query.
- **The text tier reads one hyphen form.** `hold-harmless` does not text-match
  "hold harmless"; the term tier covers registered groups under both readings.
- **Text matches stay inside a paragraph**, as the matcher's do; a phrase split across two
  paragraphs is missed by design (`tools/crossparagraph_report.py` measures how often).
- **Only the body part is chunked.** Text in headers, footers, footnotes and endnotes is
  reachable through term hits but not through the text or summary tiers.

## Query API

- **`get_chunk` reads only views a run ingested.** Accepted and original chunk ids differ, so
  there is no mapping from one to the other; a store ingested in the accepted view cannot
  return a chunk's original-view text. Deleted text is still visible in the chunk's union
  `markup`, its `pending_changes` and `get_revisions`.
- **`compare` is over chunk ids** (content per view plus occurrence). A text change moves a
  chunk between `only_in_a` / `only_in_b`; an edited, added or resolved comment on an
  unchanged chunk is reported separately in `comments_changed`. There is no sense that one
  document is a revision of another: both ids are named by the caller.

## Store and catalog

- **"Latest run" is a file mtime.** The run log carries no clock, so copying or restoring a
  store can reorder it; keys and ids never depend on it.
- **`list_documents` reads every parse blob** to compute the unattributed counts, so it costs
  time linear in the store's size.
- **Read-only opens.** A read-only store must already exist and contain the collections it
  always had; only the roll-up collections may be missing (they read as empty).

## Evaluation

- **Retrieval and summary faithfulness are unscored** until the user supplies a real query
  set with expected citations and human grades (`open-inputs.md`, the "Blocked on the user"
  list in `phase2-build-spec.md`). L3 is reported, never gated.
- **Nothing is verified against a Word-saved document** (carried from Phase 1).
- **Real term lists are unavailable**, so ranking has been exercised only against the
  synthetic registry.
