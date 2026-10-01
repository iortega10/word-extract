# Open inputs: things only the user can supply

Nothing here blocks building. Each item changes what Phase 1 / Phase 2 can *claim*, not
what can be built, and each has a defined fallback so work continues without it. Update
this file when an input arrives or a fallback changes.

Last updated: 2026-09-30 (during Turn 6b validation).

## 1. Real "like terms" groups — NOT AVAILABLE, check back later

- **Status:** the user does not currently have access to the real term lists. Revisit.
- **What it is:** the domain term groups (regulated or not) (canonical form plus
  curated synonyms, and which groups should opt in to stemming) that the matcher exists to
  flag.
- **Why it matters:** the matcher is validated only against a clearly synthetic registry
  (`fixtures/terms/synthetic.example.json`). Real lists are what expose problems in overlap
  precedence, hyphen readings, stem overreach and location handling.
- **Needed from the user:** even 5-10 real groups would help. Especially welcome:
  - groups where variants name *different* things (universe / university / universal, the
    classic stem-overreach risk), so we can decide which groups must NOT opt in to stemming;
  - hyphenated compounds (hold-harmless, loss-control, non-compliance).
- **Where real lists may come from.** Not from a former employer's curated vocabularies, which
  are typically confidential. Build them from **public sources** (public standards,
  model laws, public regulations, standard definitions), or
  use a list the owner is explicitly permitted to use. Keep them outside the repository.
- **Fallback until then:** synthetic registry only; stemming stays per-group opt-in and off
  by default; no real term list is ever invented or committed.
- **Affects:** Turn 6c-6e spot-checks, Turn 9 L2 scoring (recall gate), Phase 3 candidate
  review. Real lists live outside the repo in a `registry_store` keyed by `term_list_hash`.

## 2. A Word-saved document — OPEN

- **What it is:** a `.docx` saved by actual Word (or Word online) with a comment reply, a
  resolved comment, and a tracked change. Easiest path: open
  `fixtures/samples/review_sample.docx` in Word, reply to
  two comments, resolve one, turn on Track Changes, edit a clause, save into
  `fixtures/real/` (contents are gitignored).
- **Why it matters:** every fixture so far is repo-authored or generator output. Nothing is
  checked against real Word output. Specifically unverified: `w14:paraId` identity,
  `commentsExtended` threading, numbering label counting, heading/numbering conventions,
  strict namespaces, deleted paragraph marks.
- **Fallback:** those paths stay recorded as *unverified* (`@spec_derived`,
  `producer_verified_coverage`), as the design allows.

## 3. Human labels for the underwriting sample — OPEN

- **What it is:** a hand-labeled sidecar for
  `fixtures/samples/review_sample.docx` (headings: 6 with
  the Title; list items; comment anchors) and a **must-find term list** drawn from real
  documents, with `labels_provenance: human`.
- **Why it matters:** L2 recall = 1.0 and the "every false positive classified" gate can
  only run against human labels. Never generate or edit them to match output.
- **Fallback:** L2 reports "not evaluated" and Phase 1 closes conditionally.

## 4. Phase 2 inputs (not needed yet)

- A real query set with expected citations, the summarizer model and access, and human
  grading for L3. See `phase2-build-spec.md`, "Blocked on the user".
