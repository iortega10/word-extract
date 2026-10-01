"""Turn 1: one summary per accepted-view chunk, cached on the rendered input.

    from wordextract.llm import client
    run = summarize(Store(".wordextract"), record, client=client("canned"), model="canned")

**One summary per chunk, keyed on what the model read.** Not per document, not per view:
:func:`~wordextract.store.summary_key` is over the sha256 of
:func:`~wordextract.render.render_union_markup`'s rendering of *that chunk* -- union text with
revision markup, comment context and revision manifest -- plus the model, its parameters, the
prompt file and the output schema. Nothing about the source, the term list, the matcher or the
chunker is in it, so adding a term list or re-running the chunker re-summarizes nothing, and a
word that exists only as a tracked deletion *does* change the key (the accepted view's
``content_hash`` would not have).

**A hit costs nothing.** Each chunk is looked up before any call, so a second ``summarize`` of
an unchanged document makes no call at all and leaves every stored artifact byte for byte as it
was (the pass's own run record is still written: it is the log of the pass, not a cache entry).
Every chunk that *is* called is archived with core's
:func:`~docextract_core.archive_llm_call` -- the prompt and the response text under the store --
and the summary records those two refs, so a stored summary can be replayed against the
archived answer.

**A rejected answer is recorded, never decoded.** An answer that is not the requested JSON
(or is built on a different output schema) is stored as a
:class:`~wordextract.model.SummaryRejection` under the key the summary would have taken, with
the error text and the archived response's ref. No partial summary is ever stored: a summary
that is missing a field reads like a complete one, which is how a dropped requirement goes
unnoticed. A stored rejection counts as a **hit** -- the call was made and paid for, and it is
deterministic-keyed like everything else -- so buying a retry means evicting it:
``store.rejections.evict(key)``.

**The run is recorded.** The pass writes its own :class:`~wordextract.model.RunRecord`, with one
``ArtifactCache`` entry per chunk under ``artifact_id = "summary:<chunk_id>"`` (the five Phase 1
kinds are untouched; ``ARTIFACT_KINDS`` is still the static tuple of them). The summarizer's
own inputs -- model, parameters, prompt, output schema -- are filled into that record's
``hashed_inputs`` where the ingest run left them empty, because a reader of the record needs to
know which call produced these summaries.

No provider lives here: the model is any :class:`~docextract_core.LLMClient`
(:mod:`wordextract.llm` has the canned one and the name lookup the CLI uses).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from importlib.resources import files as _files
from typing import Any, Mapping

from docextract_core import (
    CodecError,
    LLMClient,
    ReadOnlyError,
    archive_llm_call,
    from_json,
    sha256_json,
    sha256_text,
)

from .model import (
    ArtifactCache,
    CacheStatus,
    RunRecord,
    SummaryArtifact,
    SummaryOutput,
    SummaryRejection,
    View,
)
from .pipeline import stored_chunks, stored_parse
from .render import render_union_markup
from .store import Store, find_stored, summary_key
from .versions import OUTPUT_SCHEMA_VERSION, SUMMARIZER_VERSION

#: The versioned prompt, by file name. A new prompt is a **new file** (never an edit in
#: place): a summary names the prompt it was written against by that file's content hash, so
#: the file a stored summary used has to still be there to replay it.
PROMPT = "summarize.v1.md"

_PROMPTS = "prompts"

#: The parameters a summary is produced with when a caller passes none. They are a key member
#: (``model_params_hash``), so changing one re-summarizes rather than reuses.
DEFAULT_MODEL_PARAMS: dict[str, Any] = {"temperature": 0, "max_tokens": 700}

#: What decoding a model's answer may raise: the strict codec's own error (an unknown key, a
#: wrong type, an envelope from a different output schema), ``TypeError`` from the record's
#: constructor (a missing field -- ``decode`` only checks keys it is given), and ``ValueError``
#: (malformed JSON). Each is a rejection; none of them is ever a default.
_BAD_ANSWER = (CodecError, TypeError, ValueError)


@dataclass(frozen=True)
class SummaryOutcome:
    """One chunk's summarization: what the store holds under its key, and how it got there.

    ``called`` is False when nothing was sent to a model -- a cache hit, either on a summary
    or on a recorded rejection. Exactly one of ``summary`` and ``rejection`` is set, and it is
    the record that was *stored* (a hit returns the stored record, not a copy of a new one).
    """

    chunk_id: str
    key: str
    called: bool
    summary: SummaryArtifact | None = None
    rejection: SummaryRejection | None = None

    @property
    def stored(self) -> SummaryArtifact | SummaryRejection | None:
        """Whichever of the two is stored under the key."""
        return self.summary if self.summary is not None else self.rejection


@dataclass(frozen=True)
class SummaryRun:
    """One summary pass: its own run record, and one outcome per accepted-view chunk."""

    record: RunRecord
    outcomes: list[SummaryOutcome]

    @property
    def called(self) -> int:
        """How many chunks were sent to a model."""
        return sum(1 for outcome in self.outcomes if outcome.called)

    @property
    def hits(self) -> int:
        """How many chunks the store already answered for."""
        return sum(1 for outcome in self.outcomes if not outcome.called)

    @property
    def rejected(self) -> int:
        """How many chunks the store holds a rejection for, stored now or earlier."""
        return sum(1 for outcome in self.outcomes if outcome.rejection is not None)


def prompt_text() -> str:
    """The prompt file's text, read out of the package's own data."""
    return _files("wordextract").joinpath(_PROMPTS, PROMPT).read_text(encoding="utf-8")


def prompt_hash() -> str:
    """The prompt file's content hash: the key member naming the prompt that was used.

    Not the hash of the prompt that was *sent*: that is this file plus one chunk's rendering,
    and the rendering is already the key's ``input_hash``. The file is the versioned part, and
    the same file serves every chunk.
    """
    return sha256_text(prompt_text())


def params_hash(params: Mapping[str, Any]) -> str:
    """sha256 of the model parameters' canonical JSON (the key's ``model_params_hash``)."""
    return sha256_json(dict(params))


def input_hash(rendering: str) -> str:
    """sha256 of the exact rendered input a model reads (the key's ``input_hash``).

    The rendering is :func:`~wordextract.render.render_union_markup`'s output, so deleted text
    and comments are covered: a change to either changes the key, where the accepted view's
    ``content_hash`` alone would not.
    """
    return sha256_text(rendering)


def build_prompt(rendering: str) -> str:
    """The prompt a model is sent for one chunk's rendering.

    The prompt file is the instruction prefix and the rendering is appended to it verbatim,
    never interpolated through a template: braces and percent signs in a document are the
    document's own text, and no part of a document may be read as a format string.
    """
    return f"{prompt_text()}{rendering}\n"


def parse_output(text: str) -> SummaryOutput:
    """``text`` as the answer the prompt asked for, decoded strictly; anything else raises.

    The output schema version is bounded on **both** sides by
    :data:`~wordextract.versions.OUTPUT_SCHEMA_VERSION`: an answer written against a different
    output schema is rejected rather than decoded with a field defaulted -- the same rule the
    store's own records live under.
    """
    return from_json(
        SummaryOutput,
        text,
        schema_version=OUTPUT_SCHEMA_VERSION,
        min_version=OUTPUT_SCHEMA_VERSION,
    )


def summarize(
    store: Store,
    record: RunRecord,
    *,
    client: LLMClient,
    model: str,
    params: Mapping[str, Any] | None = None,
    run_id: str | None = None,
) -> SummaryRun:
    """Summarize every accepted-view chunk of ``record``, calling ``client`` for the misses.

    ``record`` is the ingest run whose chunks are summarized: the chunks and the parse are read
    back from ``store`` under the keys that record verified, never re-derived from the
    document. A summary pass writes (it archives every call it makes and records its own run),
    so a store opened ``read_only=True`` is refused here rather than after a call has been paid
    for and a file written behind a reader's back.
    """
    if record.hashed_inputs.view_id != View.ACCEPTED.value:
        raise ValueError(
            f"summaries are per accepted-view chunk; this run was ingested in view "
            f"{record.hashed_inputs.view_id!r} (its chunk ids are not the accepted view's)"
        )
    if store.read_only:
        raise ReadOnlyError(f"{store.root} is read-only: summarizing archives calls and writes summaries")
    parameters = dict(DEFAULT_MODEL_PARAMS if params is None else params)
    prompt_digest = prompt_hash()
    params_digest = params_hash(parameters)
    parsed = stored_parse(record, store_root=store.root)
    outcomes = [
        _summarize_chunk(
            store,
            chunk,
            render_union_markup(parsed, chunk),
            client=client,
            model=model,
            parameters=parameters,
            prompt_digest=prompt_digest,
            params_digest=params_digest,
            document_id=record.hashed_inputs.source_content_hash,
        )
        for chunk in stored_chunks(record, store_root=store.root)
    ]
    return SummaryRun(
        record=store.record_run(
            RunRecord(
                run_id=run_id or uuid.uuid4().hex,
                hashed_inputs=replace(
                    record.hashed_inputs,
                    summarizer_version=SUMMARIZER_VERSION,
                    model_id=model,
                    model_params_hash=params_digest,
                    prompt_hash=prompt_digest,
                    output_schema_version=OUTPUT_SCHEMA_VERSION,
                ),
                recorded_inputs=record.recorded_inputs,
                artifact_cache=[
                    ArtifactCache(
                        artifact_id=f"summary:{outcome.chunk_id}",
                        status=CacheStatus.MISS if outcome.called else CacheStatus.HIT,
                        recomputed_key=outcome.key,
                    )
                    for outcome in outcomes
                ],
            )
        ),
        outcomes=outcomes,
    )


def _summarize_chunk(
    store: Store,
    chunk,
    rendering: str,
    *,
    client: LLMClient,
    model: str,
    parameters: dict[str, Any],
    prompt_digest: str,
    params_digest: str,
    document_id: str,
) -> SummaryOutcome:
    """One chunk: the stored record under its key, or the call that produced one."""
    key = summary_key(
        input_hash(rendering),
        model_id=model,
        model_params_hash=params_digest,
        prompt_hash=prompt_digest,
    )
    stored = find_stored(store.summaries, key) or find_stored(store.rejections, key)
    if stored is not None:
        return SummaryOutcome(
            chunk_id=chunk.id,
            key=key,
            called=False,
            summary=stored if isinstance(stored, SummaryArtifact) else None,
            rejection=stored if isinstance(stored, SummaryRejection) else None,
        )

    prompt = build_prompt(rendering)
    response = client.complete(prompt, model=model, params=parameters)
    call = archive_llm_call(
        store.root,
        purpose="summary",
        model=model,
        params=parameters,
        prompt=prompt,
        response=response.text,
        tokens=response.tokens,
    )
    provenance = {
        "chunk_id": chunk.id,
        "document_id": document_id,
        "model_id": model,
        "prompt_hash": prompt_digest,
        "params_hash": params_digest,
        "prompt_ref": call.prompt_ref,
        "response_ref": call.response_ref,
    }
    try:
        answer = parse_output(response.text)
    except _BAD_ANSWER as failure:
        rejection = SummaryRejection(key=key, error=_error(failure), **provenance)
        _store_one(store, key, rejection)
        return SummaryOutcome(chunk_id=chunk.id, key=key, called=True, rejection=rejection)

    summary = SummaryArtifact(
        key=key,
        summary=answer.summary,
        topics=answer.topics,
        open_questions=answer.open_questions,
        tokens=response.tokens,
        **provenance,
    )
    _store_one(store, key, summary)
    return SummaryOutcome(chunk_id=chunk.id, key=key, called=True, summary=summary)


def _store_one(store: Store, key: str, record: SummaryArtifact | SummaryRejection) -> None:
    """Store ``record`` under ``key``, evicting whatever sibling was there first.

    At most one of a summary and a rejection exists under a key. ``Collection.save`` is
    idempotent and refuses to overwrite, and ``evict`` also unlinks a ``<key>.json`` whose
    index entry was lost, so evicting both first is what keeps a stale file from outliving the
    record that replaced it.
    """
    store.summaries.evict(key)
    store.rejections.evict(key)
    collection = store.summaries if isinstance(record, SummaryArtifact) else store.rejections
    collection.save(record)


def _error(failure: BaseException) -> str:
    """A rejection's error text: what failed, named, and what it said."""
    return f"{type(failure).__name__}: {failure}"


__all__ = [
    "DEFAULT_MODEL_PARAMS",
    "PROMPT",
    "SummaryOutcome",
    "SummaryRun",
    "build_prompt",
    "input_hash",
    "params_hash",
    "parse_output",
    "prompt_hash",
    "prompt_text",
    "summarize",
]
