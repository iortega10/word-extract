"""Turn 9: the one way into a run, so the CLI and the eval harness cannot drift.

:func:`~wordextract.store.ingest` *is* the run -- parse, chunks, hits, term list. What is
left over for a caller is turning a **document path and a term-list path** into that run
and reading back what it stored:

    record = run("doc.docx", "terms.json", store_root=".wordextract")
    parsed = stored_parse(record, store_root=".wordextract")
    found = stored_hits(record, store_root=".wordextract")

Two defaults live here and nowhere else: which store directory a bare command writes to,
and that a run is the ``accepted`` view unless told otherwise.

**The parse and the hits are read back, not returned.** :func:`run` gives the run record;
:func:`stored_parse`, :func:`stored_chunks` and :func:`stored_hits` open the store under the
keys the record recomputed from the run's own hashed inputs (D10 B) and decode what is *there*.
A pipeline that returned the parser's or the matcher's in-memory objects instead would make the
store untested by the one path the product uses -- and the offline reproduction claim (D8) is
about the stored artifact.
"""

from __future__ import annotations

from pathlib import Path

from docextract_core import CodecError

from .chunker import DEFAULT_PARAMS, ChunkParams
from .model import Chunk, ParseResult, RunRecord, TermHit, View
from .store import Store, ingest
from .terms import TermRegistry, load_registry_text

#: Where a bare CLI run keeps its store: one directory per project, created on demand.
DEFAULT_STORE = Path(".wordextract")


def load_terms(path: str | Path) -> TermRegistry:
    """The term list at ``path`` -- the canonical JSON ``dump_registry`` writes.

    Read as text (6d): a registry is addressed by :func:`~wordextract.terms.term_list_hash`
    over its canonical text, so loading it is decoding that text and never walking a
    directory of term lists hoping one of them matches.
    """
    return load_registry_text(Path(path).read_text(encoding="utf-8"))


def run(
    source: str | Path,
    terms: str | Path,
    *,
    store_root: str | Path = DEFAULT_STORE,
    view: View = View.ACCEPTED,
    params: ChunkParams = DEFAULT_PARAMS,
    run_id: str | None = None,
) -> RunRecord:
    """Ingest ``source`` against the term list in ``terms`` into ``store_root``.

    Idempotent by way of the store: identical bytes and identical inputs re-parse nothing
    and rewrite nothing, and the returned record says which artifacts were found. The run
    record itself is written every time under a fresh run id (D10 B).
    """
    return ingest(
        Store(store_root),
        source,
        registry=load_terms(terms),
        view=view,
        params=params,
        run_id=run_id,
    )


def stored_hits(record: RunRecord, *, store_root: str | Path = DEFAULT_STORE) -> list[TermHit]:
    """The hits ``record`` stored, decoded from the store under the record's own key.

    The key is taken from ``record.artifact_cache`` -- recomputed from the run's hashed
    inputs, never from the document -- so this reads what the run *verified*, and a store
    that holds nothing under it is an error rather than a recomputation. ``store.ingest``
    always writes the hits before it returns, so a record for a store that has since lost
    them is the only way to get here.
    """
    store = Store(store_root)
    key = _artifact_key(record, "hits")
    try:
        stored = store.hits.find(key)
    except CodecError:
        stored = None
    if stored is None:
        raise LookupError(f"store {Path(store_root)} holds no hits under {key}")
    return stored.hits


def stored_chunks(record: RunRecord, *, store_root: str | Path = DEFAULT_STORE) -> list[Chunk]:
    """The chunks ``record`` stored, decoded from the store under the record's own key.

    Same key discipline as :func:`stored_hits`: taken from ``record.artifact_cache``, so a
    store that holds nothing under it is an error rather than a re-chunk of the document.
    The summarizer needs the chunk *records* -- their ids, their node ids and their order --
    and never their text: what a model is sent is
    :func:`~wordextract.render.render_union_markup`'s rendering of each one.
    """
    store = Store(store_root)
    key = _artifact_key(record, "chunks")
    try:
        stored = store.chunks.find(key)
    except CodecError:
        stored = None
    if stored is None:
        raise LookupError(f"store {Path(store_root)} holds no chunks under {key}")
    return stored.chunks


def stored_parse(record: RunRecord, *, store_root: str | Path = DEFAULT_STORE) -> ParseResult:
    """The parse ``record`` stored, decoded from the store under the record's own key.

    Same key discipline as :func:`stored_hits`: taken from ``record.artifact_cache``, so
    this reads the parse the run *verified*, and a store that holds nothing under it is an
    error rather than a re-parse of the document. The eval harness reads hit *text* off
    these streams, so what L2 scores is the store's own view of the document.
    """
    store = Store(store_root)
    key = _artifact_key(record, "parse")
    try:
        stored = store.parse.find(key)
    except CodecError:
        stored = None
    if stored is None:
        raise LookupError(f"store {Path(store_root)} holds no parse under {key}")
    return stored.parsed


def _artifact_key(record: RunRecord, kind: str) -> str:
    for artifact in record.artifact_cache:
        if artifact.artifact_id == kind:
            return artifact.recomputed_key
    raise KeyError(f"run {record.run_id} recorded no {kind!r} artifact")


__all__ = [
    "DEFAULT_STORE",
    "load_terms",
    "run",
    "stored_chunks",
    "stored_hits",
    "stored_parse",
]
