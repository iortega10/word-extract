"""Turn 7: the content-addressed store, the run record, and idempotent re-ingest.

D8 is one sentence with three consequences: artifacts are **canonical JSON**, they are
**content-addressed**, and the resolved node tree is among them -- "reproduce this hit"
has to work with the ``.docx`` no longer on disk. D10 splits a run's inputs in two. The
**hashed** ones (A) are the whole of a cache key; the **recorded** ones (B -- git revs,
dependency versions, run id) are written into the run record and hashed into nothing,
because two machines that disagree about their checkouts must still agree about what is
stored.

Five artifacts per run, each addressed by the *subset* of ``HashedInputs`` its own
computation actually reads, so an input that cannot change an artifact cannot invalidate
it:

===========  =========================================================================
``raw``      the source bytes, under core's :func:`~docextract_core.archive_raw`:
             addressed by ``source_content_hash`` itself, which is core's own addressing
             and not a key of ours.
``parse``    the union streams (the address space) and the resolved node tree
             (``ParseResult``): the source, plus the two versions that read it.
``chunks``   the chunks of one view: also the view policy, the heading rules the section
             tree is drawn with, and the chunker with its parameters -- none of which the
             walker saw.
``hits``     the matcher's record: the view, the term list and the matcher -- and **not**
             the heading rules, which matching never consults, nor the chunker, which it
             never runs. ``term_list_hash`` appears here and nowhere else, which is the
             point: a new term list against an unchanged document re-parses and
             re-chunks nothing.
``terms``    the term list itself, as ``term_list_hash``-named canonical JSON (6d's
             :func:`~wordextract.terms.registry_store`). Not derived from the document at
             all, but stored with it, because the offline closure needs to find it.
===========  =========================================================================

Phase 2's summaries are **not** a sixth run artifact. One document has one per chunk, and the
inputs that can invalidate one are narrower than a run's: the term list, the matcher and the
chunker parameters cannot change what a model reads. Their key is :func:`summary_key`, over the
*rendered input* rather than over ``HashedInputs``, and they live in two collections of their
own (``summaries/``, ``rejections/``).

**A record is named by its own key.** Each artifact record carries the ``key`` it was
stored under, and that field *is* the file name (``<key>.json``), so ``id_of`` and
``key_of`` are the same function and a re-save is a no-op by construction. The
``index.json`` in each artifact directory maps key to record, which is what makes the
lookup a lookup rather than a directory scan; it is deterministic because core's
``write_json`` sorts its keys.

**No-op re-ingest.** Identical bytes with identical inputs rewrite nothing: the raw
archive returns early on its content hash, and every other artifact is already stored
under its recomputed key and is returned exactly as it stands -- not decoded and
re-encoded, which would be a rewrite even when the value is equal. The run record is
written *every* time and gets a fresh run id: it is the log of the run, not a cache entry
(D10 B), so the no-op claim is about artifact bytes and never about ``runs/``.

**Verification by recomputation.** A hit is not a promise; the run record carries, per
artifact, the key recomputed from this run's own hashed inputs (D10 B). A stored record is
only ever reached through such a recomputed key, so a hit is verified by construction.

**Determinism.** Nothing in an artifact is a clock, a dict/set iteration order or a run
id. ``to_json`` sorts keys recursively and the codec sorts sets and frozensets, and every
collection the walker produces is already in document order. The one wall-clock field in
the tree, ``raw/index.json``'s ``ingested_at``, belongs to core's archive, is recorded
only, and is not rewritten by a re-ingest -- which is why the byte-level replay test
compares the artifacts and not core's index.

**Nothing here matches or dedupes.** ``hits`` is exactly what
:func:`~wordextract.terms.match_document` returns; :func:`~wordextract.terms.dedupe_hits`
and :func:`~wordextract.terms.dedupe_moves` are query-time folds over that record (6b,
6d), and folding them in here would quietly change what the store says was found.
"""
from __future__ import annotations

import platform
import uuid
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from docextract_core import (
    CORE_VERSION,
    CodecError,
    Collection,
    archive_raw,
    content_hash,
    git_revision,
    read_json,
    sha256_json,
)

from . import opc
from .chunker import DEFAULT_PARAMS, ChunkParams, chunk, params_hash
from .model import (
    Artifact,
    ArtifactCache,
    CacheStatus,
    ChunksArtifact,
    HitsArtifact,
    ParseArtifact,
    ParseResult,
    RunRecord,
    SummaryArtifact,
    SummaryRejection,
    View,
)
from .terms import (
    TermRegistry,
    compile_registry,
    match_document,
    registry_store,
    save_registry,
    term_list_hash,
)
from .versions import (
    CHUNKER_VERSION,
    HEADING_RULESET_VERSION,
    MATCHER_VERSION,
    OUTPUT_SCHEMA_VERSION,
    SPEC_PARSER_VERSION,
    SUMMARIZER_VERSION,
    TEXTMODEL_VERSION,
    HashedInputs,
)
from .walker import walk_document

#: Every artifact a run produces, in the order the run record reports them.
ARTIFACT_KINDS = ("raw", "parse", "chunks", "hits", "terms")

#: The body part's ``part_id`` prefix (Turn 1: ``<relationship type local name>:<ordinal>``).
_BODY_PREFIX = "officeDocument:"

#: Core's own raw layout -- ``<root>/raw/<content_hash><suffix>`` behind ``raw/index.json``
#: (``docextract_core.archive``). Read here for one reason only: ``archive_raw`` is
#: idempotent but does not say whether it found the bytes or copied them, and the run
#: record reports which.
_RAW_INDEX = ("raw", "index.json")


def _key(kind: str, inputs: dict[str, str]) -> str:
    """An artifact key: canonical JSON over its kind and the hashed inputs it reads.

    The kind is part of the hashed object so that two artifacts whose input subsets
    happen to coincide can never collide on a key.
    """
    return sha256_json({"kind": kind, **inputs})


def parse_key(inputs: HashedInputs) -> str:
    """``ParseResult``'s key: the source and the two versions that produced the walk."""
    return _key(
        "parse",
        {
            "source_content_hash": inputs.source_content_hash,
            "spec_parser_version": inputs.spec_parser_version,
            "textmodel_version": inputs.textmodel_version,
            "heading_ruleset_version": inputs.heading_ruleset_version,
        },
    )


def chunks_key(inputs: HashedInputs) -> str:
    """Chunks' key: the parse they were cut from, the view, and the chunker with its params.

    The parse key is a **part of this key**, not a restatement of some of its inputs: the
    chunks are derived *from the stored parse*, so anything that changes the parse -- the
    walker, the text model, the heading rules -- must change them too. Keying on a subset of
    those inputs left chunks cut from an old parse standing next to a new one.
    """
    return _key(
        "chunks",
        {
            "parse_key": parse_key(inputs),
            "view_id": inputs.view_id,
            "chunker_version": inputs.chunker_version,
            "chunker_params_hash": inputs.chunker_params_hash,
        },
    )


def hits_key(inputs: HashedInputs) -> str:
    """Hits' key: the parse they were matched in, the view, the term list and the matcher.

    The parse key is part of it for the reason it is part of the chunks': a hit names a node
    (``node_id``), and a node's id embeds its kind -- heading or paragraph -- which the
    heading rules decide. So a change to the heading rules reaches the hits through the node
    ids even though matching itself never consults them.
    """
    return _key(
        "hits",
        {
            "parse_key": parse_key(inputs),
            "view_id": inputs.view_id,
            "term_list_hash": inputs.term_list_hash,
            "matcher_version": inputs.matcher_version,
        },
    )


#: The ``view_id`` a summary key carries. A summary's input is the union **with** revision
#: markup -- :func:`~wordextract.render.render_union_markup`'s rendering, comment context and
#: manifest included -- which is not one of the accepted view's :class:`View` values, so the
#: key names it with a literal of its own rather than borrowing a view's.
SUMMARY_VIEW_ID = "union-markup"


def summary_key(
    input_hash: str,
    *,
    model_id: str,
    model_params_hash: str,
    prompt_hash: str,
    summarizer_version: str = SUMMARIZER_VERSION,
    output_schema_version: str = OUTPUT_SCHEMA_VERSION,
) -> str:
    """One chunk's summary key: the rendered input the model read, and what read it.

    A **subset** of the run's ``HashedInputs``, deliberately: the source bytes, the term list,
    the matcher and the chunker parameters cannot change what a summary says, so keying on
    them would re-summarize a whole document because one term list grew (D7). What can change
    it does: ``input_hash`` is the sha256 of the rendered input (see
    :func:`~wordextract.summarize.input_hash`), which covers a tracked deletion that leaves the
    accepted view's ``content_hash`` untouched -- revision 1 summed ``content_hash`` and
    ``context_hash`` and would have served a stale summary for one.

    ``view_id`` is the literal :data:`SUMMARY_VIEW_ID` rather than one of the accepted
    :class:`View` values: the rendering is wider than any view, and naming a view here would
    claim a summary was one view's. It stays a key member so that a second rendering exposed
    under this id can never collide with the first.
    """
    return _key(
        "summary",
        {
            "input_hash": input_hash,
            "view_id": SUMMARY_VIEW_ID,
            "summarizer_version": summarizer_version,
            "model_id": model_id,
            "model_params_hash": model_params_hash,
            "prompt_hash": prompt_hash,
            "output_schema_version": output_schema_version,
        },
    )


def artifact_keys(inputs: HashedInputs) -> dict[str, str]:
    """Every artifact's key from one run's hashed inputs, by artifact kind."""
    return {
        "raw": inputs.source_content_hash,
        "parse": parse_key(inputs),
        "chunks": chunks_key(inputs),
        "hits": hits_key(inputs),
        "terms": inputs.term_list_hash,
    }


def body_part_id(parsed: ParseResult) -> str:
    """The part :func:`~wordextract.chunker.chunk` is built from: the body.

    Read off the stored streams rather than off the package, which is what lets the
    chunks be re-derived offline from the parse artifact alone. Empty when nothing
    streamed -- an empty package has no body and therefore no chunks.
    """
    for stream in parsed.union_streams:
        if stream.part_id.startswith(_BODY_PREFIX):
            return stream.part_id
    return ""


def hashed_inputs(
    source_content_hash: str,
    *,
    view: View,
    params: ChunkParams,
    term_list: str,
) -> HashedInputs:
    """D10 (A) for one deterministic run, with the summary group left empty.

    Phase 1 produces no summary, so the summarizer/model/prompt fields are empty rather
    than filled with a plausible-looking stand-in: ``""`` is the honest "this run used no
    model", and nothing keys on them yet.
    """
    return HashedInputs(
        source_content_hash=source_content_hash,
        spec_parser_version=SPEC_PARSER_VERSION,
        textmodel_version=TEXTMODEL_VERSION,
        view_id=view.value,
        heading_ruleset_version=HEADING_RULESET_VERSION,
        chunker_version=CHUNKER_VERSION,
        chunker_params_hash=params_hash(params),
        term_list_hash=term_list,
        matcher_version=MATCHER_VERSION,
        summarizer_version="",
        model_id="",
        model_params_hash="",
        prompt_hash="",
        output_schema_version=OUTPUT_SCHEMA_VERSION,
    )


def recorded_inputs(extra: dict[str, str] | None = None) -> dict[str, str]:
    """D10 (B): what a run writes down and never hashes.

    Core's and the package's git revisions, the interpreter, the dependency versions --
    so a reader of a run can tell two environments apart without either having been part
    of a key, which is exactly the property that makes the cache safe to share. A
    revision that cannot be read (no checkout, no installed distribution) is recorded as
    the empty string: unknown is recorded as unknown, never guessed.
    """
    return {
        "core_git_rev": git_revision() or "",
        "package_git_rev": git_revision(Path(__file__).resolve().parent) or "",
        "python_version": platform.python_version(),
        "lxml_version": _distribution_version("lxml"),
        "core_distribution_version": _distribution_version("docextract-core"),
        "core_version": CORE_VERSION,
        **(extra or {}),
    }


def _distribution_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return ""


def find_stored(collection: Collection, key: str):
    """The record stored under ``key``, or None -- an unreadable one counts as missing.

    A record written under an older schema (the codec rejects it rather than decode it with
    new fields silently defaulted) is no longer a record this code can use. It is evicted so
    the run rebuilds and rewrites it, instead of every later ingest into that store failing
    on a file that was fine when it was written.

    Public because the summarizer's cache check is the same check: a summary written under an
    older schema is missing, not a hit (Phase 2, Turn 1).
    """
    try:
        return collection.find(key)
    except CodecError:
        collection.evict(key)
        return None


def _keyed(record: Artifact) -> str:
    """``id_of``/``key_of`` for every artifact collection: a record is its own key."""
    return record.key


def _run_id(record: RunRecord) -> str:
    return record.run_id


class Store:
    """One store directory: the artifacts, the term lists, and the run log.

    ::

        raw/      <content_hash><suffix>          core's archive, plus its index.json
        parse/    <key>.json  + index.json
        chunks/   <key>.json  + index.json
        hits/     <key>.json  + index.json
        terms/    <term_list_hash>.json + index.json
        summaries/ <key>.json + index.json         one per chunk (Phase 2, Turn 1)
        rejections/ <key>.json + index.json        the summary key's failures (same key space)
        runs/     <run_id>.json                   the log; no index, no key

    The term lists sit inside the store rather than beside it because the offline
    closure is ``UnionStream`` + the pinned views projection + the registry (spec review,
    Turn 3): reproducing a hit with the ``.docx`` gone needs all three from stored data.

    ``summaries`` and ``rejections`` share one key space on purpose: at most one of the two
    exists under a key, so "was this chunk summarized by this call?" is one lookup, and the
    rejection is what a reviewer reads when the answer is no. Which of the two is present is
    decided by :func:`~wordextract.summarize.summarize`, not enforced here -- a store holds
    files, and the invariant lives where the files are written.

    ``read_only=True`` (Turn 0a) is the mode a query layer opens a store in: nothing is
    created and nothing is written -- every collection raises
    :class:`~docextract_core.ReadOnlyError` when asked. Every directory therefore has to
    exist already, so a typo in a store path fails at open rather than reading as an empty
    store, and an open never creates a directory that merely opening asked for.
    """

    def __init__(self, root: str | Path, *, read_only: bool = False) -> None:
        self.root = Path(root)
        self.read_only = read_only
        self.parse = Collection(
            self.root / "parse", ParseArtifact, id_of=_keyed, key_of=_keyed, read_only=read_only
        )
        self.chunks = Collection(
            self.root / "chunks", ChunksArtifact, id_of=_keyed, key_of=_keyed, read_only=read_only
        )
        self.hits = Collection(
            self.root / "hits", HitsArtifact, id_of=_keyed, key_of=_keyed, read_only=read_only
        )
        self.terms = registry_store(self.root / "terms", read_only=read_only)
        self.summaries = Collection(
            self.root / "summaries",
            SummaryArtifact,
            id_of=_keyed,
            key_of=_keyed,
            read_only=read_only,
        )
        self.rejections = Collection(
            self.root / "rejections",
            SummaryRejection,
            id_of=_keyed,
            key_of=_keyed,
            read_only=read_only,
        )
        self.runs = Collection(
            self.root / "runs", RunRecord, id_of=_run_id, read_only=read_only
        )

    def record_run(self, record: RunRecord) -> RunRecord:
        """Write ``runs/<run_id>.json`` and return ``record``.

        Not a cache: a run is always recorded, and a caller reusing a run id overwrites
        that run's own file rather than minting a second one.
        """
        self.runs.save(record)
        return record

    def load_run(self, run_id: str) -> RunRecord | None:
        """The recorded run with this id, or None."""
        return self.runs.load(run_id)

    def run_ids(self) -> list[str]:
        """Every run the store has recorded, sorted."""
        return self.runs.list()

    def raw_index(self) -> dict[str, dict[str, str]]:
        """Core's raw archive index, keyed by content hash (empty when nothing archived)."""
        return read_json(self.root.joinpath(*_RAW_INDEX), {})


def ingest(
    store: Store,
    source: str | Path,
    *,
    registry: TermRegistry,
    view: View = View.ACCEPTED,
    params: ChunkParams = DEFAULT_PARAMS,
    recorded: dict[str, str] | None = None,
    run_id: str | None = None,
) -> RunRecord:
    """One deterministic run over one document: store what is missing, hit what is there.

    ``view`` is the view the **chunks** are: their bytes, and therefore their ids, differ
    per view, so it is part of the chunk key. The hits stay what
    :func:`~wordextract.terms.match_document` returns -- every view a hit can be in
    (D6), never one view's slice of it -- so the run's ``view_id`` does not narrow them.

    Returns the D10 run record. Every artifact is reported ``hit`` when the store already
    held it under the key recomputed from this run's inputs, and nothing was rewritten;
    the record itself is written either way, with a fresh run id (D10 B).
    """
    source = Path(source)
    inputs = hashed_inputs(
        content_hash(source),
        view=view,
        params=params,
        term_list=term_list_hash(registry),
    )
    keys = artifact_keys(inputs)

    raw_seen = inputs.source_content_hash in store.raw_index()
    archive_raw(store.root, source)
    statuses: dict[str, CacheStatus] = {
        "raw": CacheStatus.HIT if raw_seen else CacheStatus.MISS
    }

    stored_parse = find_stored(store.parse, keys["parse"])
    if stored_parse is None:
        parsed = walk_document(opc.Package(source))
        store.parse.save(ParseArtifact(key=keys["parse"], parsed=parsed))
        statuses["parse"] = CacheStatus.MISS
    else:
        parsed = stored_parse.parsed
        statuses["parse"] = CacheStatus.HIT

    stored_chunks = find_stored(store.chunks, keys["chunks"])
    if stored_chunks is None:
        store.chunks.save(
            ChunksArtifact(
                key=keys["chunks"],
                chunks=chunk(parsed, body_part_id(parsed), view=view, params=params),
            )
        )
        statuses["chunks"] = CacheStatus.MISS
    else:
        statuses["chunks"] = CacheStatus.HIT

    stored_hits = find_stored(store.hits, keys["hits"])
    if stored_hits is None:
        store.hits.save(
            HitsArtifact(key=keys["hits"], hits=match_document(compile_registry(registry), parsed))
        )
        statuses["hits"] = CacheStatus.MISS
    else:
        statuses["hits"] = CacheStatus.HIT

    find_stored(store.terms, keys["terms"])  # an unreadable term list is evicted, then rewritten
    _, term_written = save_registry(store.terms, registry)
    statuses["terms"] = CacheStatus.MISS if term_written else CacheStatus.HIT

    return store.record_run(
        RunRecord(
            run_id=run_id or uuid.uuid4().hex,
            hashed_inputs=inputs,
            recorded_inputs=recorded_inputs(recorded),
            artifact_cache=[
                ArtifactCache(
                    artifact_id=kind,
                    status=statuses[kind],
                    recomputed_key=keys[kind],
                )
                for kind in ARTIFACT_KINDS
            ],
        )
    )


__all__ = [
    "ARTIFACT_KINDS",
    "SUMMARY_VIEW_ID",
    "Store",
    "artifact_keys",
    "body_part_id",
    "chunks_key",
    "find_stored",
    "hashed_inputs",
    "hits_key",
    "ingest",
    "parse_key",
    "recorded_inputs",
    "summary_key",
]
