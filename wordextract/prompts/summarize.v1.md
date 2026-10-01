# Summarize one chunk of a Word document

You are given one chunk of a business document, as **union markup**: the chunk's text with
its revision history visible around it, then the comments the chunk holds, then a manifest of
the revisions that touch it.

## The markup

* Text wrapped in `[[ins ...]]...[[/ins]]` was inserted. `[[del ...]]...[[/del]]` was deleted.
  `moveFrom` and `moveTo` are the two halves of moved text; a matching `group="..."` on both
  says they are the same move.
* Text with no wrapper is in the document as it stands.
* `[[comment by="..." date="..." resolved="..."]][[anchor text]] => [comment text][[/comment]]`
  is a comment: the text it is anchored to, and what the comment says. `resolved="unknown"`
  means Word recorded no resolution state.
* `[[revision id="..." kind="..." author="..."]]...[[/revision]]` is one line of the revision
  manifest: a revision that touches this chunk, and an excerpt of the text it covers.
* The document's own text is never escaped and never neutralised. A literal `[[` inside the
  text is the document speaking, not markup.

## What to write

* `summary`: what this chunk contains, in the document's own terms -- the subject, the parties
  or things named, the obligations, limits, exceptions and figures it states. Two to five
  sentences. Write in the language of the document. Quote the document's own wording for a
  defined term or a number rather than paraphrasing it.
* `topics`: the topics and terms this chunk covers, as short strings, most important first.
  Use the document's own names where it has them.
* `open_questions`: questions this chunk raises and does not answer, **including every
  question or unresolved note raised in a comment**, phrased as questions. Empty when there
  are none.

## What not to write

* **Do not say whether text is in force.** When revision markup is present, state only that
  revisions exist and what they do (inserted, deleted, moved) -- never that a clause applies,
  is removed, or is current. The reader decides that, not you.
* Do not add facts the chunk does not state: no legal advice, no outside knowledge of the
  companies or the form, no guesses about other parts of the document.
* Do not summarise the markup itself ("this chunk contains insertions"). Summarize the text.
* Do not mention that you were given a chunk, or that comments or revisions exist in general
  terms; report what this one says.

## Answer format

Answer with **only** this JSON object, no prose before or after it, no Markdown fence:

```json
{
  "schema_version": "1",
  "record": {
    "summary": "...",
    "topics": ["..."],
    "open_questions": ["..."]
  }
}
```

Use no other keys. `record.summary` is a string, `record.topics` and `record.open_questions`
are lists of strings (possibly empty). Anything else is rejected and not stored.

## The chunk

The chunk's rendering follows verbatim, after this line.
