# Text model spec (design D2): union stream, views, revisions

Status: Phase 0 deliverable. Precise enough to implement the Phase 1 walker from.
Source of truth is `word-extraction-design.md` D2 (with D3/D4/D5/D6 for the
consequences). Where this document is silent, it is silent **on purpose**: the
unspecified list at the end is the gap, made visible rather than guessed.

This file describes deterministic text addressing and view projection only. It
says nothing about how a `.docx` is read (OPC/`lxml`, D1) or how text is chunked
(D3).

## 1. Per-part union stream (the address space)

Text lives in **parts** (`word/document.xml`, a header/footer part, a footnotes
part, ...). Each part has one **union stream**: an ordered sequence of
elementary spans, in document order, containing inserted **and** deleted text.

Every elementary span has:

- an offset range `[start, end)` in the part's union coordinate space;
- its literal text;
- an **ancestor stack**: `[revision_id, ...]`, outer to inner.

An address is `Span(part_id, start, end)`. `source_ref` names the originating
OPC/XML location; offsets are the union coordinate.

The union is **only an address space**, not content. Offsets are stable across
all views within one `textmodel_version` (§7).

## 2. Ancestor stacks

The ancestor stack of a span is the ordered list of revision marks that enclose
it, outermost first. `w:del` nested inside `w:ins` yields `[ins_id, del_id]`;
`w:ins` nested inside `w:del` yields `[del_id, ins_id]`. Stacks mix `ins` and
`del` marks freely. The order in which nested/overlapping revisions are applied
is part of `textmodel_version` (§7).

## 3. Views (masks over the union)

A view is a **mask**, never the content (guiding principle; D5).

Define two families of revision kinds:

- **del-family** = `{del, moveFrom}`
- **ins-family** = `{ins, moveTo}`

For an elementary span `s` with stack `A(s)`:

- `accepted(s)` ⇔ no member of `A(s)` is del-family;
- `original(s)` ⇔ no member of `A(s)` is ins-family;
- `superseded(s)` ⇔ `s` is in the union and is neither accepted nor original
  (equivalently: `A(s)` contains both a del-family and an ins-family member —
  text that was inserted and then deleted).

`superseded` is mask + fixture only in Phase 1; it is **not** a matcher target
(D6).

## 4. Gap-closing rule

After applying a mask, the retained spans of a paragraph are **concatenated in
order with the elided spans removed entirely** (no placeholder):

- Gap-closing happens **within a paragraph only**.
- It **never** closes across a paragraph boundary. The retained text of
  paragraph *P* and paragraph *Q* are never concatenated into one matchable
  string; a match never spans two paragraphs. (Cross-paragraph matching is
  deferred with the miss measured — D6.)

Consequences: the union must never be matched directly. Matching the raw union
invents adjacencies no single view asserts (see §6 example A).

`w:br` / `w:tab` are content **within** a paragraph and do **not** create a
boundary.

## 5. Moves

Same-family treatment as revisions:

- a `w:moveFrom` mark is del-family, a `w:moveTo` mark is ins-family;
- both members of one move share a `move_group_id`;
- therefore a moved run is excluded from `accepted` at its source and included
  at its destination, and vice versa for `original`;
- a move renders as **one hit with two locations**, deduped in the term index
  (D4). OOXML never pairs an individual `w:del` with its `w:ins`; no "net
  change" is synthesized (D4).

## 6. Worked examples

Offsets below are illustrative. `[…rN:kind…]` denotes a run carrying revision
mark `rN` of that kind. Stacks are outer→inner.

### A. `right of [del recovery][ins subrogation]`

Union stream (one paragraph):

| offsets  | text          | stack        |
|----------|---------------|--------------|
| `0–8`    | `right of `   | `[]`         |
| `8–16`   | `recovery`    | `[r1:del]`   |
| `16–27`  | `subrogation` | `[r2:ins]`   |

Union text = `right of recoverysubrogation` (no separator between the two
revision runs; the false adjacency below is `recoverysubrogation`).

- `accepted` = `right of ` + `subrogation` → gap closed → `right of subrogation`.
- `original` = `right of ` + `recovery` → `right of recovery`.
- `superseded` = ∅.

A raw-union match would find the false adjacency `recoverysubrogation`. This
is why matching happens on views. Under D6 this yields **two hits in one
group**: `right of recovery` (`present_in={original}`) and
`right of subrogation` (`present_in={accepted}`).

### B. `w:del` nested inside a `w:ins`

`w:ins r2` encloses `Coverage ` + `w:del r3 "is excluded"` + `applies worldwide.`

| offsets  | text                 | stack           |
|----------|----------------------|-----------------|
| `0–9`    | `Coverage `          | `[r2:ins]`      |
| `9–20`   | `is excluded`        | `[r2:ins, r3:del]` |
| `20–38`  | `applies worldwide.` | `[r2:ins]`      |

- `accepted` = `Coverage ` + `applies worldwide.` → `Coverage applies worldwide.`
- `original` = ∅ (no span lacks an ins-family ancestor).
- `superseded` = `is excluded` (both ancestors present).

### C. A move

`w:moveFrom r5` at the source; `w:moveTo r6` at the destination; both
`move_group_id = mg1`.

| location    | text         | stack         |
|-------------|--------------|---------------|
| source      | `Section 4 ` | `[r5:moveFrom]` |
| destination | `Section 4 ` | `[r6:moveTo]`   |

- `accepted`: source excluded (del-family), destination included.
- `original`: source included, destination excluded.
- One hit, two locations, joined by `mg1`, deduped at rank time.

### D. A comment range that starts in a deleted run

`w:commentRangeStart` lies before `w:del r1 "excluded in all cases"`;
`w:commentRangeEnd` lies after it. The comment anchor is a **union**
coordinate range, so the anchor is an ordinary non-empty range:

- `Comment.anchor` = `Span(part, start, end)` over the deleted text;
- `Comment.anchor_text` = the **union** text of that range
  (`excluded in all cases`).

The accepted-view rendering of that same span is empty — but the comment still
points at a real, non-empty range. A comment references a *location*, not a
view; any view-specific rendering is a separate projection of the same span.

### E. Text inside `w:hyperlink` and a field result

- **`w:hyperlink`**: its runs sit at their document position in the union, with
  whatever revision ancestors enclose them. They are ordinary content and appear
  in every view the mask permits. (This is exactly the content `python-docx`
  loses via `CT_P.r_lst` — D1.)
- **Complex field** (runs between `fldChar begin` and `fldChar end`): the runs
  between `begin` and `separate` are the *field instruction* and are **not
  content** — they are excluded from the union. The runs between `separate` and
  `end` are the *field result* and are content — included in the union.
- **`w:fldSimple`**: its `w:instr` attribute is the instruction (excluded); its
  child runs are the result (included).

How field results interact with **views** (e.g. whether a result ever carries
ins-family ancestry, or whether a "locked/dirty" field is masked) is
**unspecified** — see §8.

## 7. Versions

`textmodel_version` bundles: the union/ancestor semantics above, the
del-family/ins-family membership, and the revision-application order that fixes
ancestor-stack ordering. Offsets are stable across views **within** one
`textmodel_version`; the union is **not** the cross-version diff mechanism —
that is what `Node.id` identity (D3) is for.

Matching unit (D6): the paragraph, per view. `superseded` is a mask only in
Phase 1.

## 8. Explicitly unspecified

These are **not** defined here and must not be inferred. They are gated by
nothing today (design Open risk #2) and are called out so the gap is visible:

- **Table cells** — is a cell boundary a gap-closing boundary like a paragraph
  boundary? Does a match cross a `w:tc` boundary? Ordering of cell streams
  within a row/table.
- **Footnotes / endnotes** — they live in separate parts; how their streams
  order relative to the body for matching, and whether any view spans parts.
- **Fields** — whether field-instruction text can ever enter the union, and
  whether field results carry ins-family ancestry or are masked.
- **Structured document tags (`w:sdt`)** — whether the sdt wrapper contributes
  ancestors, and what boundary (if any) it creates.
- **Moves across paragraph boundaries** — how source and destination paragraphs
  interact with the within-paragraph gap rule.

Anything unspecified in D2 stays unspecified here.
