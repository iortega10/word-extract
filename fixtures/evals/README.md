# `fixtures/evals/` — human labels for the eval harness

L1's labels are the sidecars next to the fixtures: the generator wrote both the document and
the answer, so they can only tell us whether the parser *perceives* what was put there. The
other three layers ask questions only a person can answer, and this directory is where each
one's files live. Nothing in this repo invents a label; every loader refuses a file that is
not what it claims.

| Layer | Loader | File | Committed status |
|---|---|---|---|
| L2 must-find | `wordextract/evals/must_find.py` | `*.json` (except the names below) | **blocked on the user** — only `must_find.example.json` |
| retrieval | `wordextract/evals/retrieval.py` | `queries*.json` | **blocked on the user** — only `queries.example.json` |
| L3 grades | `wordextract/evals/l3.py` | `l3_grades*.json` | **blocked on the user** — only `l3_grades.example.json` |

`*.example.json` documents a format and is skipped by name by every loader, because its
content is invented placeholders: a must-find phrase is only ever a string a human copied out
of a real document, a query set is only ever a question a person actually asked, and a grade
is only ever a person's reading of a summary. With no real file, the harness reports the
layer as *not evaluated* (`result: null`) rather than scoring the example
(`docs/design/open-inputs.md` section 3).

The three loaders share the directory, so each leaves the others' files alone: must-find
skips `queries*.json` and `l3_grades*.json` before it reads anything (`must_find.OTHER_LOADERS`),
and each loader's own glob claims only its files.

## L2 must-find: authoring one

1. Drop the document in `fixtures/real/` (git-ignored; redact it first — see that
   directory's README) and put the real term list somewhere outside the repo.
2. Copy `must_find.example.json` to `fixtures/evals/<name>.json`.
3. Replace every placeholder. `term_list` is relative to the fixtures directory; `documents`
   is keyed by a fixture-relative document path.
4. `labels_provenance` must be `"human"` — a generator or a model would be labelling what it
   found, which is the circularity L2 exists to avoid.

A labelled document that is not on disk is **skipped**, not scored as a miss, so a label set
whose real document has not been dropped in yet is inert rather than red.

Rules the loader enforces:

- `must_find`: every phrase must appear among the run's hits, or recall < 1.0 and the gate
  fails.
- `expected_extra`: a hit the must-find phrases do not account for is a false positive until
  a human names it here, classified `true_positive`, `stem_match`, or `false_positive`.
  Unclassified extras fail the layer even when recall is perfect.
- Comparisons are case-folded and whitespace-collapsed, and nothing else: the phrase a human
  copied and the text the matcher read are the same characters.

## Retrieval: authoring a query set

Copy `queries.example.json` to `fixtures/evals/<name>.json`. Each example is one request
(`query`) plus the citations an honest answer must contain (`expected_citations`): a
fixture-relative `document`, optionally the 0-based `chunk` ordinal in the accepted view.
The candidates are `wordextract.query.search`'s own results over the set's own term list, so
a citation either appears in them or it does not — no rank cut, no score. A citation whose
document is not on disk is skipped on the record, not counted as a miss; an ordinal the run
cannot show *is* a miss (the human named a chunk that does not exist). The layer gates on
`citation_recall == 1.0` only once a real human set exists.

## L3 grades: authoring one

Copy `l3_grades.example.json` to `fixtures/evals/<name>.json`. The harness selects the
sample (about 12 chunks, evenly spaced over the fixture summaries); you grade a chunk by
recording its `document`, `chunk_id`, the summary record's `prompt_hash` and `model`, and one
label per rubric criterion. A grade under another prompt or model is **stale** and never
counts as graded; a grade for a chunk the sample did not select is **unmatched** and waits.
L3 is reported, never gated: summary faithfulness is judged, not asserted
(`docs/design/phase2-build-spec.md` Turn 7).
