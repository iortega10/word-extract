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

- **`revision_id_collision`** -- two revisions of the same kind shared one `w:id` but
  carried a different author or date. Word repeats one id across the runs of *one*
  revision, so a same-author-and-date repeat is one record; a different author or date
  is a different revision, and folding it into the first would silently drop its
  attribution. The second is recorded under `<kind>:<w:id>~<n>` (`n` counts the
  colliding variants of that id) and this gap. Ancestor stacks use the disambiguated id.
- **`unrecorded_revision_kind`** -- the part carries a tracked *formatting or structure*
  change: `w:rPrChange`, `w:pPrChange`, `w:sectPrChange`, `w:tblPrChange`,
  `w:trPrChange`, `w:tcPrChange`, `w:tblGridChange`, `w:numberingChange`, `w:cellIns` /
  `w:cellDel` / `w:cellMerge`, or a row-level `w:ins` / `w:del` (`w:trPr`). None changes
  text, so none is a `Revision` record and none affects a view; the gap says the
  document has them, so "who changed what" is known to be incomplete. A deleted row's
  *text* is still captured whenever its runs sit inside `w:del`.

## Owned by the comment walker (Turn 2d)

- **`unanchored_comment`** -- a comment could not be anchored to a span of union text.
  Its `w:commentRangeStart` / `w:commentRangeEnd` pair has to open and close in the
  **same** part (a range is one part's `Span`), so a range that never closes, one whose
  end marker is in another part, or a bare `w:commentReference` with no range at all
  leaves the comment with `anchor = None` and **no `anchor_text`**. A marker naming a
  `w:comment` body the comments part does not hold -- a range whose comment was dropped --
  is reported the same way, since a marker that anchors nothing is the same loss. The
  comment is still a record: its author, date and body text are known, only the text it
  was made about is not.

## Owned by the threading reader (Turn 2e)

- **`dangling_comment_parent`** -- a `commentsExtended` row names a `w15:paraIdParent`
  that no comment in the document carries. The part states the parent, so `parent_id`
  keeps it; the gap says the thread is broken and the parent cannot be resolved. The
  comparison is exact (case-sensitive), like the join itself: Word writes both sides in
  upper case, and a producer that does not gets `UNKNOWN`, never a wrong parent.

## Owned by the chunker (Turn 5)

These two are the chunker's own limitations, and **not** `known_gaps` ids: the chunker
reports no gaps -- it has no such channel, and the walker has already reported every fact
they are about -- so they are written here without the bold id form above, which is
reserved for an id a parse result can report. They carry an id and a sentence all the
same, because the id is what a reviewer greps for.

- `comment_body_text` -- the contract's `Comment` record carries no **body text**: the
  anchors, the anchored text and the facts (author, initials, date, resolution, parent),
  never the comment's own words. The chunker folds those facts into `context_hash`, so a
  comment whose identity is Word's own `w14:paraId` and whose body is rewritten is, to
  every summary key, the same comment: the edited prose re-summarizes nothing. Only a
  comment with no `paraId` folds its body in, and it does so *through* the content-hash
  fallback identity (2b) rather than as a fact of its own -- which is the whole shape of
  the limitation.
- `comment_off_chunk` -- a comment can be anchored where **no chunk holds text**, and
  then it folds into no chunk's `context_hash`. Three ways it happens: the anchor is in a
  heading's own text (a heading is its section's `heading_id`, never one of its members);
  the anchor is on a paragraph's terminator, which is a boundary rather than block
  content; or the anchor is in a part other than the one the chunks were built from --
  the chunker chunks the part it is handed, so a comment in the footnotes folds into no
  body chunk. The comment is still a `Comment` record with all its facts, and
  `unanchored_comment` above covers the ranges that anchor to nothing at all: what is
  lost is only the fold-in, a fact of the document that no chunk's summary key can see.

## Declared by fixtures, owned by later slices

`field_result_view_ancestry`, `w_cr_unspecified`, `empty_parts_unverified`,
`renamed_part_unverified`, `strict_namespaces_unverified`. Each will be documented here
by the slice that reports it.
