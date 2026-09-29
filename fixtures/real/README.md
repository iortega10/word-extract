# `fixtures/real/` — placeholder for real producer documents (blocked on the user)

**Status: blocked on the user. Nothing here yet, and nothing fake has been added.**

The synthetic fixtures one directory up (`program_review_v3.docx`, `binder_summary.docx`,
`edge_cases.docx`, `spec_threaded.docx`) are hand-built and know their own answers — we
wrote them, so verifying the parser against them is partly circular. To close the loop we
need at least one document that a **real person authored in Word**, not a script, and that
exhibits the producer quirks the spec relies on:

- a comment **with a reply** (threaded; `word/commentsExtended.xml` / `w15:paraIdParent`),
  ideally one marked resolved and one not;
- at least one **tracked change** (`w:ins` and/or `w:del`), preferably a deletion and an
  insertion inside the same sentence;
- whatever else the source already contains (numbering, tables, headers) — do not
  hand-edit it to fit; a real file that exercise only some cases still beats a synthetic
  one.

## TODO

- [ ] **User action:** drop a genuine Word-authored `.docx` (with a comment reply and a
      tracked change) into this directory. Redact client names / premium figures first.
- [ ] Record where it came from and that we have permission to use it (keep the source
      note out of this tracked README — see the conventions below).
- [ ] Add a smoke-test path that runs over `fixtures/real/*.docx` as an explicit opt-in
      (see the eval harness) once a file exists.

Until a real file lands, the Phase 0 exit criterion "validated against a real producer
document" is **not met by design** — the design doc records this as an open item, and it
should stay visibly unmet rather than being papered over with a mock.

## Conventions

- **Contents of this directory are git-ignored** except this README
  (`fixtures/real/*` with `!fixtures/real/README.md`). Real documents are third-party
  material and routinely carry PII and commercial terms — do not commit them.
- **No `.expected.json` sidecars are shipped for real documents.** The sidecars next to
  the synthetic fixtures are ground truth only because the generator *wrote* those
  documents. Labels for a real file come from a spec, a human, or a model run and must
  carry the matching `LabelsProvenance` (see `wordextract/model.py`) — never a
  hand-written `"generator"` sidecar for a file this repo did not generate.
- **Name by source, not by date:** e.g. `chubb-cgl-form-reply-and-edit.docx`.
