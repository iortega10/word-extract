# Phase 1 known gaps

One entry per gap id. `known_gaps` on a parse result must use exactly these ids
(a test asserts the walker's ids are documented here). Entries are added as the
slice that owns them lands; the full list is checked at Turn 9.

## Owned by the union-stream walker (Turn 2a)

- **`textbox`** -- text-box (`w:txbxContent`) text is in no union stream yet. It
  belongs to its own fragment (`Span.fragment_id`), which a later slice builds, so
  until then term matching **misses text-box text**. Recorded whenever a text box is
  met, so the miss is never silent.
- **`unrecognized_container`** -- a `w:` element outside the walker's allow-list held
  text and was skipped. The text is lost from the union; the id says so. Known
  transparent containers: `hyperlink`, `fldSimple`, `smartTag`, `customXml`, `dir`,
  `bdo`, `sdt` (inline and block). An element that lands here should be triaged and
  either made transparent or documented.
- **`revision_missing_id`** -- a revision element had no `w:id`. It gets a
  deterministic `<kind>:noid<n>` id (`n` counts id-less revisions in per-part
  document order), so ids stay stable but are not Word's. Word always writes ids, so
  this only occurs in malformed or generated input.

## Owned by the node walker (Turn 2b)

- **`inline_sdt_transparent`** -- an inline content control (`w:sdt` inside a
  paragraph) is walked through with no node and no boundary: its content is ordinary
  content at its position, and what the control *was* -- a tag, an alias, a lock --
  does not survive into the union. A block-level `sdt` is unaffected: it is a node of
  its own kind. Recorded whenever an inline control is met.
- **`duplicate_content_id_churn`** -- two nodes in the same part derived the same
  content-hash id, so the later one was re-id'd with an occurrence ordinal (`:1`, ...).
  The ordinal is a position among equal-content nodes in *document order*, so inserting
  another equal-content node ahead of one **changes its id**. Word's own files trigger
  this routinely: an empty footnote/endnote part holds both a `separator` and a
  `continuationSeparator` note of identical content. Recorded whenever a duplicate
  content-derived id is actually spent.

- **`style_chain_cycle`** -- resolving a paragraph style's numbering or outline level
  (`styles.py`, along `w:basedOn`) hit a `basedOn` cycle. The chain stops there and
  whatever was found before is kept; the paragraph is not guessed at further. An
  undefined style, or a `basedOn` naming a style `styles.xml` does not define, is
  **not** a gap: there is nothing to inherit, as when Word treats the style as Normal
  (docx generators routinely omit `Normal`).

## Owned by the revision walker (Turn 2c)

- **`paragraph_mark_revision`** -- a deleted or inserted paragraph mark
  (`w:pPr/w:rPr/w:del|ins`) is **not honored**: the break is retained in every view, so
  the paragraphs on either side are neither merged nor dropped, and Word's own break or
  merge is not reproduced. The mark itself is still a fact, recorded as a revision of its
  own. Recorded whenever such a mark is met, even when its `w:id` was already seen.

## Declared by fixtures, owned by later slices

`field_result_view_ancestry`, `w_cr_unspecified`, `empty_parts_unverified`,
`renamed_part_unverified`, `strict_namespaces_unverified`. Each will be documented here
by the slice that reports it.
