# Text model spec (design D2): union stream, views, revisions

Status: Phase 0 deliverable, amended in Turn 0.5 (§1 span rule, terminators and
fragments; §5 two hits plus query-time dedupe; §6 example A offsets; §7 view
projection and gap-closing in the `textmodel_version` bundle plus paragraph-mark
revisions not honored; §8 the Phase 1 decisions merged in).
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
- its literal text — written once on the stream (`UnionStream.text[start:end]`),
  never duplicated per span;
- an **ancestor stack**: `[revision_id, ...]`, outer to inner.

**Span rule.** A span is a **maximal run of equal ancestor stack**. Nothing else
splits one: `w:tab`, `w:br` (every type), `w:sym`, and a literal `\n` / `\r`
inside a `w:t` are content *inside* their span, never boundaries.

**Terminators.** Each paragraph contributes exactly one `"\n"` elementary span
(empty stack) at the end of its content, so the part's spans tile the part's
union exactly — every offset belongs to exactly one span (§6 fixes the offsets
accordingly). The terminator belongs to the union and to the whole-part view
projection (§7), but a paragraph `Node`'s `spans` and the per-paragraph literals
in the fixture sidecars **exclude the paragraph's own terminator**, and comment
anchor ranges clamp to exclude terminators.

An address is `Span(part_id, start, end)`. `source_ref` names the originating
OPC/XML location; offsets are the union coordinate.

The union is **only an address space**, not content. Offsets are stable across
all views within one `textmodel_version` (§7).

**Fragments.** Text that is not in host document order (a text-box body) lives in
its own fragment: `Span.fragment_id` is the owning fragment (host node id +
ordinal) and the host node carries `host_node_id`, so host document order is
undisturbed and a box is never counted twice (known gap, §8).

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
- a move renders as **two hits, one per location**, each carrying the shared
  `move_group_id` and its own `present_in`, and the term index dedupes the pair
  **at query time** (D4); the two hits are one *group*, never one hit with two
  locations. OOXML never pairs an individual `w:del` with its `w:ins`; no "net
  change" is synthesized (D4).

## 6. Worked examples

Offsets below are illustrative. `[…rN:kind…]` denotes a run carrying revision
mark `rN` of that kind. Stacks are outer→inner.

### A. `right of [del recovery][ins subrogation]`

Union stream (one paragraph):

| offsets  | text          | stack        |
|----------|---------------|--------------|
| `0–9`    | `right of `   | `[]`         |
| `9–17`   | `recovery`    | `[r1:del]`   |
| `17–28`  | `subrogation` | `[r2:ins]`   |

Union text = `right of recoverysubrogation` (no separator between the two
revision runs; the false adjacency below is `recoverysubrogation`). The
paragraph's `"\n"` terminator would follow at `28–29`: it is elementary span
content in the union, but never part of `Node.spans` (§1).

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
- Two hits, one per location, joined by `mg1`, deduped at query time.

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

How field results interact with **views**: a result carries whatever revision
ancestry encloses it, no special masking (§8). Whether a "locked/dirty" field is
masked at all stays **unspecified** — see §8.

## 7. Versions

`textmodel_version` bundles: the union/ancestor semantics above (**the span rule**
and the one-`"\n"`-per-paragraph terminators included), the del-family /
ins-family membership, the revision-application order that fixes ancestor-stack
ordering, **the view projection** (applying the §3 mask to the union, terminators
kept, whole part, to produce each view string and its offset map), and **the
gap-closing rule** (§4: retained spans concatenated in paragraph order, elided
spans removed entirely, never across a paragraph boundary). Any change to span
splitting, terminators, projection or gap-closing moves every offset and every
view string, so it is a `textmodel_version` bump, not a silent edit.

**Not honored: paragraph-mark revisions.** `w:pPr/w:rPr/w:del|ins` neither adds,
removes nor merges a terminator: the break is retained in both views, the
affected nodes are flagged, and a loud `known_gap: paragraph_mark_revision` is
recorded (§8). Offsets are stable across views **within** one
`textmodel_version`; the union is **not** the cross-version diff mechanism —
that is what `Node.id` identity (D3) is for.

Matching unit (D6): the paragraph, per view. `superseded` is a mask only in
Phase 1.

## 8. Decisions (frozen) and what is still unspecified

Phase 0 left these open on purpose. They are now **decided** for Phase 1 and must
not be improvised by a small-context builder; each is asserted by fixture
(`fixtures/model/*.expected.json`, `labels_provenance: "spec"`) whose `clauses`
tag the section or example it encodes.

- **Union text.** One `"\n"` terminator span per paragraph (§1). `w:tab` =
  `"\t"`. Every `w:br` type and any literal `\n` / `\r` inside a `w:t` =
  `U+000B` (content inside the paragraph; a phrase can cross a page break,
  documented). `w:noBreakHyphen` = `U+2011`, `w:softHyphen` = `U+00AD`, `w:sym`
  = its character. `w:instrText` is never content. `w:t` and `w:delText` are both
  content.
- **Fields.** Excluded via a `fldChar` depth counter (nested fields): text is
  content only when **every** open field has passed its `separate`, so a nested
  field's result that sits inside an outer field's instruction is instruction, not
  content (`{ IF { MERGEFIELD name } ... }` yields only the outer result); a
  `w:fldSimple`'s instruction is its attribute (§6 example E). Results carry
  whatever revision ancestry encloses them; no special masking.
- **Comment ranges** clamp to exclude terminators. A multi-paragraph
  `anchor_text` contains `"\n"` (tests expect it). A `commentReference` with no
  matching range yields `anchor = None`, recorded in `known_gaps`, never silent.
- **Per-kind span rule** (asserted by test): paragraph, heading, list_item, cell
  = exactly one contiguous span; table, row, block-sdt = empty spans with
  `child_ids`. `NodeKind.SDT` is block-level only; inline sdt is transparent (no
  node, no boundary, no ancestor).
- **Matching unit** is always a `w:p`. Table cells never fuse; each cell
  paragraph is an ordinary paragraph; cell/row/table order is document order.
  Footnotes, endnotes, headers and footers are separate parts and **no view spans
  parts**; read every part reachable via `headerReference`, deduplicated by part.
- **Deleted/inserted paragraph mark** (`w:pPr/w:rPr/w:del|ins`): **not honored**
  (§7) — the break is retained in both views, the affected nodes are flagged, and
  a loud `known_gap: paragraph_mark_revision` is recorded. Fixture included.
- **Text boxes**: exactly one body per anchored drawing, preferring `mc:Choice`,
  then `mc:Fallback`, then a bare `w:txbxContent`. Content lives in its own
  fragment (`Span.fragment_id` = host node id + ordinal), so host document order
  is undisturbed. Recorded as a known gap.
- **Gap-closing keeps no placeholder** (§4). Word itself renders `right of ` +
  accepted `subrogation` as `right ofsubrogation` when the space sat inside the
  deleted run; tests document that miss, and a `w:br` inside a deleted run.
- **Offline-reproduction closure** (Turn 7) is `UnionStream` + the pinned views
  projection + the term registry. "From the stored node tree alone" is wrong.

Still **unspecified** — do not infer, and do not let a fixture or a test pretend
otherwise:

- **Moves across paragraph boundaries** — how source and destination paragraphs
  interact with the within-paragraph gap rule (§4).
- **Locked/dirty fields** — whether such a field result is masked at all (§6
  example E).

Anything unspecified in D2 stays unspecified here.
