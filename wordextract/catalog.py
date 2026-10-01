"""Turn 0d: the document catalog -- what a store holds, derived on demand and never written.

Phase 1's store can answer "give me the record under this key" and nothing else: the keys it
holds are the *inputs* of a run, so a reader who has not brought a run record cannot name a
single artifact. A query layer starts from the other end -- "what documents are in here, and
where are their chunks and hits" -- and that is a question about the store as a whole:

:func:`list_documents` answers it from the two inventories the store already keeps, and adds
nothing to them:

* the **raw archive index** -- every document whose bytes are held, by content hash, with the
  filename it was ingested under (``core.archive_raw``);
* the **run records** -- every run's hashed inputs (so: the document, the view, the term list)
  and the artifact keys recomputed from them, which is what the run verified at the time.

**One row per document, not per run.** ``document_id`` is the full ``source_content_hash`` --
the same identity the parse and chunks keys are built on -- and a document ingested twice, in
two views, against three term lists is still one row. Its ``views`` say where each view's
chunks are, and, per term list ingested in that view, where the hits are; a term list matched
in a view is reachable by ``(document_id, view, term_list)`` and nothing else has to be
searched. The chunks key is per view, the hits key per view *and* term list, so the shape
mirrors the keys: a term list cannot be attached to chunks it did not cut, and adding a term
list never moves the chunks.

**"Latest" is the run file's mtime.** The run log deliberately carries no clock (D10 B: a
timestamp would be recorded input a key must never see), so recency is read off ``runs/*.json``
and a tie is broken by run id, so the answer never depends on directory iteration order. Copying or restoring a store can reorder
mtimes, so ``latest_run_id`` (and which chunks key a re-chunked view names) is only as stable
as the files' mtimes; the keys and ids themselves never depend on them. History
enters a row in exactly two places: ``latest_run_id``, and the ``chunks`` key a view names when
the same view was chunked more than once -- the newest run wins, so the row points at the chunks
a reader wants rather than at a superseded key. Every view and term list any run ingested is
listed, not only those the latest run happened to use.

**Read-only, and honest about the gaps.** Nothing here is stored: the catalog is derived from
what is already there, so it works on a store opened ``read_only=True`` and writes no file. Two
half-known states are reported rather than rounded off: a document in the archive with no run
(an ingest whose walk failed leaves the bytes behind) is a row with no views and an empty
``latest_run_id``, and a run whose bytes are no longer archived carries the empty title. A run
record that cannot be *decoded* -- written under an older schema, the codec rejecting it rather
than defaulting new fields -- is not a run this code can use, so it is skipped outright.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from docextract_core import CodecError

from .model import RunRecord
from .store import Store


@dataclass(frozen=True)
class ViewArtifacts:
    """One view of one document: its chunks, and its hits per term list.

    ``hits`` is keyed by ``term_list_hash`` -- the term list the run was matched against,
    which is part of the hits key and of nothing else (a new term list re-parses and
    re-matches nothing; only the hits move).
    """

    view: str
    chunks: str
    hits: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DocumentEntry:
    """One document the store holds: its identity, its name, its last run, its artifacts.

    ``title`` is the filename the document was ingested under, which is a label and not an
    identity -- the same bytes under two names are still one ``document_id``, and the name
    the *archive* recorded is the one reported. ``views`` holds one entry per view ingested,
    sorted by view id.
    """

    document_id: str
    title: str
    latest_run_id: str
    views: list[ViewArtifacts] = field(default_factory=list)


def list_documents(store: Store) -> list[DocumentEntry]:
    """Every document ``store`` holds, sorted by title and then by ``document_id``.

    Rows are the union of the archived documents and the documents some run recorded, so a
    store that holds bytes (or a run) for a document lists it rather than hiding it.
    """
    archived = store.raw_index()
    rows: dict[str, dict] = {}

    def row_of(document_id: str) -> dict:
        return rows.setdefault(
            document_id,
            {
                "title": archived.get(document_id, {}).get("original_filename", ""),
                "latest_run_id": "",
                "views": {},
            },
        )

    for document_id in archived:
        row_of(document_id)

    for _written_at, run_id, record in _runs_by_age(store):
        row = row_of(record.hashed_inputs.source_content_hash)
        row["latest_run_id"] = run_id  # oldest first, so the last one wins
        keys = {artifact.artifact_id: artifact.recomputed_key for artifact in record.artifact_cache}
        view = row["views"].setdefault(record.hashed_inputs.view_id, {"chunks": "", "hits": {}})
        view["chunks"] = keys.get("chunks", view["chunks"])
        if "hits" in keys:
            view["hits"][record.hashed_inputs.term_list_hash] = keys["hits"]

    return [
        DocumentEntry(
            document_id=document_id,
            title=row["title"],
            latest_run_id=row["latest_run_id"],
            views=[
                ViewArtifacts(
                    view=view_id,
                    chunks=view["chunks"],
                    hits={term_list: view["hits"][term_list] for term_list in sorted(view["hits"])},
                )
                for view_id, view in sorted(row["views"].items())
            ],
        )
        for document_id, row in sorted(rows.items(), key=lambda item: (item[1]["title"], item[0]))
    ]


def _runs_by_age(store: Store) -> list[tuple[int, str, RunRecord]]:
    """Every readable run record, oldest-written first; a tie on the run id.

    An unreadable one (the codec rejects a blob from an older schema) is skipped, the same
    call ``store.find_stored`` makes: a record this code cannot decode is not a run it can
    report.
    """
    runs = []
    for run_id in store.run_ids():
        try:
            record = store.load_run(run_id)
        except CodecError:
            continue
        if record is None:
            continue
        runs.append((_written_at(store.runs.root, run_id), run_id, record))
    runs.sort(key=lambda run: (run[0], run[1]))
    return runs


def _written_at(root: Path, run_id: str) -> int:
    """When ``runs/<run_id>.json`` was last written, in nanoseconds; 0 when it is not there."""
    try:
        return (root / f"{run_id}.json").stat().st_mtime_ns
    except OSError:
        return 0


__all__ = ["DocumentEntry", "ViewArtifacts", "list_documents"]
