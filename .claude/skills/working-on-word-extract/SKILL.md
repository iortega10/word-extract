---
name: working-on-word-extract
description: Contributor and validation workflow for the word-extract repo: how a build turn is validated, the version/schema/ledger discipline, fixture and label rules, and the commit conventions. Use when changing code, validating another model's work, or committing in this repo.
---

# Working on word-extract

Read `CLAUDE.md` first (rules and status), then the relevant spec in `docs/design/`.

## The loop

A turn is built from a spec in `docs/design/phase*-build-spec.md` (usually by hearth, run by the
owner). Then validate, report, fix, commit:

1. `git status` and `git log`: **check the repo actually changed** before saying a turn is done.
2. `python -m pytest -q` (about 40 s). Green is the start, not the finish.
3. **Probe with crafted input** the tests do not cover: build a tiny `.docx` with zipfile (minimal
   `[Content_Types].xml`, `_rels/.rels`, `word/document.xml`, optional comments/styles parts) and
   exercise the behavior directly. Compare against an **independent reference** where one exists (the
   stemmer was checked against NLTK over 55k words; the term probes against hand-computed offsets).
4. Look for the recurring defect classes: silent drops (an unknown container losing text), stale cache
   keys (a key missing an input it depends on), vacuous passes (an empty corpus scoring 1.0),
   contract changes without a schema bump, view/union confusion (matching the raw union), and
   overconfident reports (a verdict from thin evidence).
5. Report findings with severity and a proposed fix. **Wait for the owner's go-ahead** unless the
   fix is clearly in scope of what they already approved.
6. Fix with a test that fails before the fix. Hand-type expected values from the spec, never from output.
7. Commit with **explicit paths**; separate hearth's work from your fixes when possible.

## Discipline that tests enforce

| Change | You must also |
|---|---|
| A component's **output** changes (walker, headings, views, chunker, matcher, stem) | bump its constant in `wordextract/versions.py` (or `stem.STEM_ALGORITHM_VERSION`) and record a ledger line: `python tools/update_behavior_ledger.py` |
| A **record field** is added/changed (`model.py`, core records) | bump `SCHEMA_VERSION` and `MIN_SUPPORTED_VERSION` in `docextract-core/docextract_core/codec.py`; regenerate stored example files (e.g. `fixtures/terms/synthetic.example.json`) |
| A **fixture** is added | `python tools/update_behavior_ledger.py --new-corpus`; bump nothing |
| A **known gap** is added | give it an id, document it in `docs/design/phase1-gaps.md` (a test checks they match) |
| A **store artifact** is derived from another | include the upstream key in its key (see `store.py`) |

`python tools/update_behavior_ledger.py --check` must exit 0. Verify a guard can fail: mutate the
behavior in place (monkeypatch the name the code looks up *at call time*) and confirm the check fires.

## Fixtures and labels

- Generators: `tools/make_fixtures.py`, `make_revision_fixtures.py`, `make_spec_fixtures.py`. Output is
  byte-reproducible; a test regenerates and compares.
- Sidecar facts are **hand-typed in generator source** with the spec clause they encode. `labels.py`
  must not import any parser code. L2 labels must have `labels_provenance: human`; never author them.
- If an existing literal contradicts the spec, fix it only with a spec citation and say so loudly.

## Environment notes

- Windows with Git Bash and PowerShell. Shell heredocs can mangle backslash escapes in files you write;
  use the Write/Edit tools for source and test files.
- The CLI needs the packages importable (`pip install -e`, or `PYTHONPATH="docextract-core;."`).
- Keep scratch work out of the repo (the session scratchpad); `.hearth/`, `.exec-mcp/`, `.scratch/` are ignored.
- Real documents go in `fixtures/real/` (ignored). Do not commit them or real term lists.

## Phase 2 pointers

Spec: `docs/design/phase2-build-spec.md` rev 2. Hard parts to remember: the summary key is a hash of the
**rendered input** (not `compute_key`, not a view's `content_hash`); `pending_changes` is derived from
union stacks (a `Revision` has no span or text); the store must open read-only without creating
anything; the v2/v3 fixture pair isolates exactly two edits.
