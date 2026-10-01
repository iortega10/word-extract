# `fixtures/evals/` — L2 must-find labels (human only)

L1's labels are the sidecars next to the fixtures: the generator wrote both the document and
the answer, so they can only tell us whether the parser *perceives* what was put there. L2
asks the question the sidecars cannot — *does a term list find what a person says is in the
document* — so its labels come from a human reading a real document, and nothing in this repo
invents one. See `wordextract/evals/must_find.py` for the loader and the scorer.

**Status: blocked on the user. No label set is committed here** — only
`must_find.example.json`, which documents the format and is skipped by name
(`iter_must_find`), because its phrases are invented placeholders. With no label set, the
harness reports L2 as *not evaluated* and Phase 1 closes conditionally
(`docs/design/open-inputs.md` section 3).

## Authoring one

1. Drop the document in `fixtures/real/` (git-ignored; redact it first — see that
   directory's README) and put the real term list somewhere outside the repo.
2. Copy `must_find.example.json` to `fixtures/evals/<name>.json`.
3. Replace every placeholder. `term_list` is relative to the fixtures directory; `documents`
   is keyed by a fixture-relative document path.
4. `labels_provenance` must be `"human"` — a generator or a model would be labelling what it
   found, which is the circularity L2 exists to avoid.

A labelled document that is not on disk is **skipped**, not scored as a miss, so a label set
whose real document has not been dropped in yet is inert rather than red.

## Rules the loader enforces

- `must_find`: every phrase must appear among the run's hits, or recall < 1.0 and the gate
  fails.
- `expected_extra`: a hit the must-find phrases do not account for is a false positive until
  a human names it here, classified `true_positive`, `stem_match`, or `false_positive`.
  Unclassified extras fail the layer even when recall is perfect.
- Comparisons are case-folded and whitespace-collapsed, and nothing else: the phrase a human
  copied and the text the matcher read are the same characters.
