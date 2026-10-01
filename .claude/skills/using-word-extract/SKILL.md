---
name: using-word-extract
description: How to run and interpret the word-extract package: parse a .docx with comments and tracked changes, flag a term list, read hits and citations, and what the results can and cannot claim. Use when asked to analyse, search, or compare Word documents with this repo.
---

# Using word-extract

Use this when the task is **to run the package on a document**, not to change it. For changing it,
see the `working-on-word-extract` skill.

## 1. Setup

```bash
pip install -e docextract-core -e ".[dev]"      # from the repo root
```

If an import fails with `No module named 'lxml'` or `'docextract_core'`, the install step was skipped.

## 2. The three inputs you need

1. **A `.docx`.** Any Word file. Tracked changes, comments and tables are all read.
2. **A term list** (`terms.json`): canonical JSON of groups. Write it with
   `wordextract.terms.dump_registry(TermRegistry(groups=[TermGroup(canonical=..., synonyms=[...],
   stemming=None|"porter")]))`. **Do not invent a real vocabulary**: ask the user. Real term lists
   are not committed and must never contain an employer's confidential vocabulary (build them from public
   sources or get permission); the only one in the repo (`fixtures/terms/synthetic.example.json`) is synthetic.
3. **A store directory** (default `.wordextract`), created on first use.

## 3. Run it

```bash
python -m wordextract ingest DOC.docx --terms terms.json --store .wordextract   # run record
python -m wordextract hits   DOC.docx --terms terms.json --store .wordextract   # stored hits
```

Re-running on identical bytes is a no-op (every artifact reports `hit`). From Python:
`walk_document(opc.Package(path))` for the structure, `chunk(...)` for retrieval units,
`match_document(compile_registry(registry), parsed)` for hits.

## 4. Reading the result correctly

- **Views are explicit.** `accepted` = tracked changes applied, `original` = rejected,
  `superseded` = inserted then deleted (never matched). A hit's `present_in` says which views hold
  it. Text deleted by a tracked change appears only in `original`. Never describe a result as simply
  "the document says X" without naming the view when a document has tracked changes.
- **Cite with `spans`/`view_spans`.** `spans` are union addresses, one per retained run, so a
  citation never highlights deleted text. Comment hits are view-less and named `comment:<id>`.
- **Moves** appear as two hits sharing a `move_group_id`; fold with `terms.dedupe_moves(hits, streams)`
  in the order the matcher returned them.
- **`match_type`**: `exact`, `synonym`, or `stem` (only for groups that opted in). Stemming merges
  words that name different things (insured / insurer / insurance), so treat `stem` hits as leads.
- **Matching is token-based** under either hyphen reading and never crosses a paragraph. A phrase
  split across two paragraphs is missed by design; `tools/crossparagraph_report.py` measures how often.

## 5. What it cannot claim (say so)

- **`parsed.known_gaps`** lists constructs not modelled. Always surface them. Common ids:
  `textbox` (text-box text is not searched), `paragraph_mark_revision`, `unrecorded_revision_kind`
  (tracked formatting), `unanchored_comment`, `revision_id_collision`. Meanings are in
  `docs/design/phase1-gaps.md`.
- **Comment threading** is `verified`, `absent` or `unknown`. `absent`/`unknown` never means
  "no replies".
- **Nothing is verified against a Word-saved document yet** (`w14:paraId` identity, threading,
  numbering labels, strict namespaces were tested only on files this repo wrote). Say so when it matters.
- The matcher has only been tested with a synthetic term list. Do not state recall or precision.
- **No summaries or search yet** (Phase 2). Nothing here calls a model.

## 6. Checking your own run

`python -m wordextract.evals` re-validates the parser against the fixture ground truth; it should exit 0.
