# `fixtures/terms/` — term registries

**Synthetic only.** Real "like terms" groups (the domain vocabulary this
tool exists to flag) are **blocked on the user**: the term list is input, not something
this repo can derive, and an invented one would read as a real one. So the only registry
committed here is `synthetic.example.json`, hand-typed to exercise the registry format
and the matcher, with `tags` that say `category` and nothing about any real permit.

`LabelsProvenance` does not apply here: a registry carries no labels. It is a codec
record (`TermRegistry`, `schema_version` envelope) with `TermGroup` entries whose
`canonical`/`synonyms` the matcher reads and whose `stemming`/`rules`/`tags` it does not
(`stemming` is 6b's; tags are opaque by design, D6).

The file is `dump_registry` output plus a trailing newline, and its groups are re-typed as
literals in `tests/test_terms.py`, which asserts both that the file parses back to those
groups and that its hash is the same. Comparisons are made on parsed content
(`json.loads`), not bytes, so a CRLF working tree does not matter — but an edit here that
changes the term list fails a test rather than silently redefining hits.
Real term lists live outside the repo, in a content-addressed store
(`wordextract.terms.registry_store`) keyed by `term_list_hash` — not committed.
