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

## Declared by fixtures, owned by later slices

`paragraph_mark_revision`, `inline_sdt_transparent`, `field_result_view_ancestry`,
`duplicate_content_id_churn`, `w_cr_unspecified`, `empty_parts_unverified`,
`renamed_part_unverified`, `strict_namespaces_unverified`. Each will be documented
here by the slice that reports it.
