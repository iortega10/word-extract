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

**Pending revisions travel with the record.** Each new summary record carries
``pending_changes``: the chunk's :func:`~wordextract.pending.pending_changes`, computed at
write time from the same parse the manifest is rendered from, so "which revisions does this
summary describe?" is answered by the record itself (D5 -- whatever the prose omitted sits
beside it). A **hit** returns the stored record untouched -- one written before Turn 2 keeps
its ``None``, *not computed*, and is never back-filled on read -- and
``record.has_pending`` is the tri-state flag (True, False, or None). Rejections carry no
pending list: the model's answer was refused, so there is no record to amend.

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

**Roll-ups (Turn 4).** After the chunks, the same pass rolls the chunk summaries up: one
roll-up per section that holds content, then one for the document, bottom-up through
:func:`rollup_tree`. A roll-up's children are its sections' and chunks' summaries in document
order -- at most ``ROLLUP_FAN_IN`` per call, longer lists grouped into intermediate roll-ups
that become the children of the level above. Its key is
:func:`~wordextract.store.rollup_key`: the ordered child keys, the roll-up prompt and the model
-- no target id -- so two documents with the same content share a roll-up, and a re-summarized
chunk re-keys exactly the family above it. Every record is ``non_authoritative=True``: a
roll-up repeats what its children say and is a routing aid, never a replacement for them.
``has_pending`` is the tri-state union of the children's flags, computed here, never
model-written. Hits, rejections and their stored/rejection pairs follow the chunk rules, under
``rollups/`` and ``rollup_rejections/``. The run record adds one entry per roll-up **target**
(section or document) under ``artifact_id = "rollup:<id>"``; ``SummaryRun.rollups`` holds every
roll-up outcome of the pass, the fan-in intermediates included.

No provider lives here: the model is any :class:`~docextract_core.LLMClient`
(:mod:`wordextract.llm` has the canned one and the name lookup the CLI uses).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from importlib.resources import files as _files
from typing import Any, Callable, Mapping

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
    RollupArtifact,
    RollupRejection,
    RunRecord,
    SummaryArtifact,
    SummaryOutput,
    SummaryRejection,
    View,
)
from .pending import pending_changes
from .pipeline import stored_chunks, stored_parse
from .render import render_union_markup
from .store import Store, find_stored, rollup_batches, rollup_key, summary_key
from .versions import OUTPUT_SCHEMA_VERSION, SUMMARIZER_VERSION

#: The versioned prompt, by file name. A new prompt is a **new file** (never an edit in
#: place): a summary names the prompt it was written against by that file's content hash, so
#: the file a stored summary used has to still be there to replay it.
PROMPT = "summarize.v1.md"

#: The versioned roll-up prompt, by file name. The same rule as ``PROMPT`` applies, and the
#: file's content hash is ``rollup_key``'s ``prompt_hash`` member: editing it in place would
#: silently re-key every roll-up, so a changed roll-up prompt is a **new file**.
ROLLUP_PROMPT = "rollup.v1.md"

_PROMPTS = "prompts"

#: A child whose answer was rejected, rendered for its parent's prompt: the prose the parent
#: reads says the child has no summary, in the same words the chunk path's docstring uses for
#: its own rejections -- unknown stays unknown, never an empty string that reads as "nothing".
NO_SUMMARY_TEXT = "(no summary: the model's answer was rejected)"

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
class RollupOutcome:
    """One roll-up record: what the store holds under its key, and how it got there.

    The mirror of :class:`SummaryOutcome` over a section's or the document's synthesis.
    ``target_kind``/``target_id`` name the roll-up's *target* (``section`` + a heading node
    id, or ``document`` + the source hash); an intermediate level of a fan-in hierarchy
    reports the same target as its final record -- it belongs to that target's family, and
    ``SummaryRun.rollups`` lists it too. ``called`` is False on a cache hit, including a hit
    on a stored rejection.
    """

    target_kind: str
    target_id: str
    key: str
    called: bool
    rollup: RollupArtifact | None = None
    rejection: RollupRejection | None = None

    @property
    def stored(self) -> RollupArtifact | RollupRejection | None:
        """Whichever of the two is stored under the key."""
        return self.rollup if self.rollup is not None else self.rejection


@dataclass(frozen=True)
class SummaryRun:
    """One summary pass: its run record, one outcome per chunk, one per roll-up record.

    ``rollups`` holds **every** roll-up outcome of the pass in execution order -- the
    intermediate levels of a fan-in hierarchy included, not only the per-target records the
    run record's ``artifact_cache`` names -- because a reader checking why a roll-up was
    called needs the whole family, and a reader of the family's final record needs to know
    the levels below it were hits or calls too. Defaults to empty so a caller that only
    wants chunk outcomes is unaffected.
    """

    record: RunRecord
    outcomes: list[SummaryOutcome]
    rollups: list[RollupOutcome] = field(default_factory=list)

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


def rollup_prompt_text() -> str:
    """The roll-up prompt file's text, read out of the package's own data."""
    return _files("wordextract").joinpath(_PROMPTS, ROLLUP_PROMPT).read_text(encoding="utf-8")


def rollup_prompt_hash() -> str:
    """The roll-up prompt file's content hash: the key member naming that prompt.

    The same distinction as :func:`prompt_hash`: not the hash of what was sent (that is the
    file plus one parent's rendering), but of the versioned file that serves every roll-up --
    so editing the prompt in place re-keys every roll-up, which is why the file is versioned
    rather than edited.
    """
    return sha256_text(rollup_prompt_text())


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


def build_rollup_prompt(rendering: str) -> str:
    """The prompt a model is sent for one roll-up's rendering of its children.

    Verbatim concatenation for the reason :func:`build_prompt` gives: a child summary's text
    is document prose and never a format string.
    """
    return f"{rollup_prompt_text()}{rendering}\n"


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
    chunks = stored_chunks(record, store_root=store.root)
    document_id = record.hashed_inputs.source_content_hash
    outcomes = [
        _summarize_chunk(
            store,
            chunk,
            render_union_markup(parsed, chunk),
            parsed=parsed,
            client=client,
            model=model,
            parameters=parameters,
            prompt_digest=prompt_digest,
            params_digest=params_digest,
            document_id=document_id,
        )
        for chunk in chunks
    ]
    rollup_outcomes, rollup_entries = _summarize_rollups(
        store,
        parsed=parsed,
        chunks=chunks,
        document_id=document_id,
        outcomes=outcomes,
        client=client,
        model=model,
        parameters=parameters,
        params_digest=params_digest,
    )
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
                ]
                + rollup_entries,
            )
        ),
        outcomes=outcomes,
        rollups=rollup_outcomes,
    )


def _summarize_chunk(
    store: Store,
    chunk,
    rendering: str,
    *,
    parsed,
    client: LLMClient,
    model: str,
    parameters: dict[str, Any],
    prompt_digest: str,
    params_digest: str,
    document_id: str,
) -> SummaryOutcome:
    """One chunk: the stored record under its key, or the call that produced one.

    ``parsed`` is only read on a miss -- a hit returns the stored record byte for byte, and
    a record written before Turn 2 keeps ``pending_changes=None`` (*not computed*) rather
    than being back-filled here, so "when was this written?" stays legible.
    """
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
        pending_changes=pending_changes(parsed, chunk),
        **provenance,
    )
    _store_one(store, key, summary)
    return SummaryOutcome(chunk_id=chunk.id, key=key, called=True, summary=summary)


@dataclass(frozen=True)
class _Unit:
    """One node of the roll-up tree: a leaf chunk, or a group with children to roll up.

    ``kind`` is ``"chunk"``, ``"section"`` or ``"document"``; ``id`` is the chunk id, the
    section's heading node id, or the document's ``source_content_hash``. Children are in
    document order, and only *non-empty* groups appear: a section with no chunk in its whole
    subtree has nothing to summarize and gets no record -- its absence is the tree's, not a
    silent drop, and a reader sees it by comparing the sections with the ``rollup:``
    entries of the run record.
    """

    kind: str
    id: str
    children: tuple[_Unit, ...] = ()


def _sections(sections):
    """Depth-first over a section forest: every section, parents before their children."""
    for section in sections:
        yield section
        yield from _sections(section.children)


def _first_index(unit: _Unit, order: dict[str, int]) -> int:
    """A unit's first chunk's index in the chunk list -- its position in document order.

    ``order`` maps chunk id to index. A non-leaf unit is non-empty by construction, so the
    minimum over its leaves is its subtree's first chunk: a heading's section starts where
    its first content starts, whether that content is its own or a deeper heading's.
    """
    if unit.kind == "chunk":
        return order[unit.id]
    return min(_first_index(child, order) for child in unit.children)


def rollup_tree(parsed, chunks, document_id: str) -> _Unit:
    """The document's roll-up tree: every section that holds content, under the document.

    Sections are a forest and ``Section.node_ids`` is direct membership -- the heading node
    itself is ``heading_id``, not a member -- so a chunk maps to exactly one section: the
    owner of its **first** node, where the heading node counts as its own section's. A
    nesting choice can therefore never put one chunk in two roll-ups, and a chunk whose
    first node is in no section (preamble text before the first heading) belongs to the
    document alone.

    Positions come from the **chunk list** order only: loose chunks and sections are
    interleaved by each one's first chunk, so every node's children read in document order
    regardless of how the forest nests, and a section's position is where its first content
    is -- its heading's own chunk included.
    """
    if not chunks:
        return _Unit("document", document_id, ())
    owner = {}
    for section in _sections(parsed.sections):
        owner[section.heading_id] = section
        for node_id in section.node_ids:
            owner[node_id] = section
    order = {chunk.id: index for index, chunk in enumerate(chunks)}
    direct: dict[str, list[str]] = {}
    loose: list[str] = []
    for chunk in chunks:
        section = owner.get(chunk.node_ids[0]) if chunk.node_ids else None
        if section is None:
            loose.append(chunk.id)
        else:
            direct.setdefault(section.heading_id, []).append(chunk.id)

    def section_unit(section) -> _Unit | None:
        kids = [
            unit
            for unit in (section_unit(child) for child in section.children)
            if unit is not None
        ]
        positioned = [
            (order[chunk_id], _Unit("chunk", chunk_id))
            for chunk_id in direct.get(section.heading_id, [])
        ]
        positioned += [(_first_index(unit, order), unit) for unit in kids]
        if not positioned:
            return None
        positioned.sort(key=lambda pair: pair[0])
        return _Unit("section", section.heading_id, tuple(unit for _, unit in positioned))

    top = [
        unit
        for unit in (section_unit(section) for section in parsed.sections)
        if unit is not None
    ]
    positioned = [(order[chunk_id], _Unit("chunk", chunk_id)) for chunk_id in loose]
    positioned += [(_first_index(unit, order), unit) for unit in top]
    positioned.sort(key=lambda pair: pair[0])
    return _Unit("document", document_id, tuple(unit for _, unit in positioned))


@dataclass(frozen=True)
class _Child:
    """What one child contributes to its parent's roll-up: its key, its prose, its flag.

    ``label`` is how the parent's prompt names it (``chunk:<id>``, ``section:<id>``,
    ``document:<id>``); ``text`` is the summary itself, or :data:`NO_SUMMARY_TEXT` when the
    child's answer was rejected -- never a placeholder that could read as content;
    ``chunk_ids`` flattens the chunks under it, in document order, so a parent's
    ``child_chunk_ids`` covers whole subtrees without a second walk.
    """

    key: str
    label: str
    text: str
    has_pending: bool | None
    chunk_ids: tuple[str, ...]


@dataclass(frozen=True)
class _Rolled:
    """One executed roll-up: the child it becomes for its parent, and the record behind it."""

    child: _Child
    record: RollupArtifact | RollupRejection
    outcome: RollupOutcome


def _chunk_child(outcome: SummaryOutcome) -> _Child:
    """A chunk's summary outcome as its parent's child descriptor."""
    label = f"chunk:{outcome.chunk_id}"
    if outcome.summary is not None:
        return _Child(
            key=outcome.key,
            label=label,
            text=outcome.summary.summary,
            has_pending=outcome.summary.has_pending,
            chunk_ids=(outcome.chunk_id,),
        )
    return _Child(
        key=outcome.key,
        label=label,
        text=NO_SUMMARY_TEXT,
        has_pending=None,
        chunk_ids=(outcome.chunk_id,),
    )


def _child_of(record: RollupArtifact | RollupRejection) -> _Child:
    """A stored roll-up (or roll-up rejection) as its parent's child descriptor."""
    label = f"{record.target_kind}:{record.target_id}"
    if isinstance(record, RollupArtifact):
        return _Child(
            key=record.key,
            label=label,
            text=record.summary,
            has_pending=record.has_pending,
            chunk_ids=tuple(record.child_chunk_ids),
        )
    return _Child(
        key=record.key,
        label=label,
        text=NO_SUMMARY_TEXT,
        has_pending=None,
        chunk_ids=tuple(record.child_chunk_ids),
    )


def _union(flags) -> bool | None:
    """The tri-state union of children's ``has_pending`` flags (Turn 2's rule, propagated).

    True when any child has a pending revision -- the parent's whole subtree then does;
    None when any child's state is unknown (a rejected answer, or a record written before
    the flag existed); False only when every child is known to be clear. False from no
    children would be a lie about knowledge, so an empty input would be None -- and is
    never produced, because a roll-up always has children.
    """
    values = list(flags)
    if any(flag is True for flag in values):
        return True
    if any(flag is None for flag in values):
        return None
    return False


def _render_rollup_input(children: list[_Child]) -> str:
    """The children's prose for one roll-up's prompt, in document order.

    Each child under a ``### <label>`` line: the label is the only place the parent learns
    *what* a child is, and it carries ids rather than titles, so nothing a heading's
    wording could change reaches the model's input through here.
    """
    return "".join(f"### {child.label}\n{child.text}\n" for child in children)


def _rollup_fields(children: list[_Child]) -> dict[str, Any]:
    """A roll-up record's derivations from its children -- computed here, never model-written.

    ``child_keys`` restates the key's input, ``child_chunk_ids`` flattens the children in
    document order, ``has_pending`` is :func:`_union`. One function so the behavior
    ledger's canned roll-up derives the same three fields from its canned children: a
    change to any of them moves the ledger's ``rollup`` fingerprint or a test fails.
    """
    return {
        "child_keys": [child.key for child in children],
        "child_chunk_ids": [
            chunk_id for child in children for chunk_id in child.chunk_ids
        ],
        "has_pending": _union(child.has_pending for child in children),
    }


def _rolled(
    target_kind: str, target_id: str, record: RollupArtifact | RollupRejection, *, called: bool
) -> _Rolled:
    """Assemble the outcome and child descriptor around a stored-or-new record."""
    outcome = RollupOutcome(
        target_kind=target_kind,
        target_id=target_id,
        key=record.key,
        called=called,
        rollup=record if isinstance(record, RollupArtifact) else None,
        rejection=record if isinstance(record, RollupRejection) else None,
    )
    return _Rolled(child=_child_of(record), record=record, outcome=outcome)


def _roll_tree(
    unit: _Unit,
    *,
    child_of_chunk: Callable[[str], _Child],
    execute: Callable[[list[_Child], str, str], _Rolled],
) -> tuple[_Child, list[RollupOutcome], list[RollupOutcome]]:
    """Execute one subtree bottom-up: children first, then this unit's roll-up.

    Returns the child this unit becomes for its parent, every outcome the subtree produced,
    and the subtree's **targets** -- one outcome per section's final roll-up plus (at the
    root) the document's. A unit with more children than ``rollup_batches`` keeps is rolled
    up in batches first and those intermediate records become the children of the next
    level, until the final record has at most one batch's worth of children of its own; the
    intermediates report the same target as the final record, because they are that target's
    family.

    The recursion is shared with the behavior ledger's canned roll-up: the tree walk, the
    grouping and the fan-in rule are the sensitive parts, and they run identically there.
    """
    if unit.kind == "chunk":
        return child_of_chunk(unit.id), [], []
    outcomes: list[RollupOutcome] = []
    targets: list[RollupOutcome] = []
    children: list[_Child] = []
    for child_unit in unit.children:
        child, subtree, subtree_targets = _roll_tree(
            child_unit, child_of_chunk=child_of_chunk, execute=execute
        )
        children.append(child)
        outcomes.extend(subtree)
        targets.extend(subtree_targets)
    while len(rollup_batches(children)) > 1:
        next_children = []
        for group in rollup_batches(children):
            rolled = execute(group, unit.kind, unit.id)
            outcomes.append(rolled.outcome)
            next_children.append(rolled.child)
        children = next_children
    final = execute(children, unit.kind, unit.id)
    outcomes.append(final.outcome)
    targets.append(final.outcome)
    return final.child, outcomes, targets


def _roll_up_one(
    store: Store,
    children: list[_Child],
    target_kind: str,
    target_id: str,
    *,
    client: LLMClient,
    model: str,
    parameters: dict[str, Any],
    prompt_digest: str,
    params_digest: str,
    document_id: str,
) -> _Rolled:
    """One roll-up record: the stored record under its key, or the call that produced one.

    ``_summarize_chunk``'s shape with the rendering replaced: the key is computed over the
    children's keys before anything is looked up (a hit is verified by recomputation, like
    every other artifact), the miss path archives its call, and the answer is parsed
    strictly -- a malformed answer is a ``RollupRejection`` under the same key, never a
    partial roll-up. ``prompt_digest`` here is :func:`rollup_prompt_hash`, not the chunk
    prompt's: one digest per prompt file, each naming the prompt its own keys read.
    """
    key = rollup_key(
        [child.key for child in children],
        model_id=model,
        model_params_hash=params_digest,
        prompt_hash=prompt_digest,
    )
    stored = find_stored(store.rollups, key) or find_stored(store.rollup_rejections, key)
    if stored is not None:
        return _rolled(target_kind, target_id, stored, called=False)

    rendering = _render_rollup_input(children)
    prompt = build_rollup_prompt(rendering)
    response = client.complete(prompt, model=model, params=parameters)
    call = archive_llm_call(
        store.root,
        purpose="rollup",
        model=model,
        params=parameters,
        prompt=prompt,
        response=response.text,
        tokens=response.tokens,
    )
    provenance = {
        "target_kind": target_kind,
        "target_id": target_id,
        "document_id": document_id,
        "model_id": model,
        "prompt_hash": prompt_digest,
        "params_hash": params_digest,
        "prompt_ref": call.prompt_ref,
        "response_ref": call.response_ref,
    }
    fields = _rollup_fields(children)
    try:
        answer = parse_output(response.text)
    except _BAD_ANSWER as failure:
        rejection = RollupRejection(
            key=key,
            error=_error(failure),
            child_chunk_ids=fields["child_chunk_ids"],
            **provenance,
        )
        _store_one(store, key, rejection)
        return _rolled(target_kind, target_id, rejection, called=True)

    rollup = RollupArtifact(
        key=key,
        summary=answer.summary,
        topics=answer.topics,
        open_questions=answer.open_questions,
        non_authoritative=True,
        tokens=response.tokens,
        **fields,
        **provenance,
    )
    _store_one(store, key, rollup)
    return _rolled(target_kind, target_id, rollup, called=True)


def _summarize_rollups(
    store: Store,
    *,
    parsed,
    chunks,
    document_id: str,
    outcomes: list[SummaryOutcome],
    client: LLMClient,
    model: str,
    parameters: dict[str, Any],
    params_digest: str,
) -> tuple[list[RollupOutcome], list[ArtifactCache]]:
    """Every roll-up of this pass, and the run record's entries for the targets.

    The tree is walked after all chunk outcomes exist, so each roll-up reads summaries that
    are already stored -- a roll-up never holds text the store does not. Returns ``(all
    outcomes in execution order, entries for the final per-target records only)``: the run
    record names one ``rollup:<id>`` per section with content and one for the document,
    while the intermediates of a fan-in hierarchy stay in ``SummaryRun.rollups`` where a
    reader can still see them. No chunks (or none in any section) means no roll-ups and no
    entries, not a roll-up of nothing.
    """
    if not chunks:
        return [], []
    tree = rollup_tree(parsed, chunks, document_id)
    if not tree.children:
        return [], []
    by_id = {outcome.chunk_id: outcome for outcome in outcomes}
    rollup_digest = rollup_prompt_hash()

    def execute(children: list[_Child], target_kind: str, target_id: str) -> _Rolled:
        return _roll_up_one(
            store,
            children,
            target_kind,
            target_id,
            client=client,
            model=model,
            parameters=parameters,
            prompt_digest=rollup_digest,
            params_digest=params_digest,
            document_id=document_id,
        )

    _, all_outcomes, targets = _roll_tree(
        tree,
        child_of_chunk=lambda chunk_id: _chunk_child(by_id[chunk_id]),
        execute=execute,
    )
    entries = [
        ArtifactCache(
            artifact_id=f"rollup:{target.target_id}",
            status=CacheStatus.MISS if target.called else CacheStatus.HIT,
            recomputed_key=target.key,
        )
        for target in targets
    ]
    return all_outcomes, entries


def _store_one(
    store: Store, key: str, record: SummaryArtifact | SummaryRejection | RollupArtifact | RollupRejection
) -> None:
    """Store ``record`` under ``key``, evicting whatever sibling was there first.

    Both key spaces, one rule: at most one of a summary and a rejection exists under a key
    (likewise one of a roll-up and a roll-up rejection), ``Collection.save`` is idempotent
    and refuses to overwrite, and ``evict`` also unlinks a ``<key>.json`` whose index entry
    was lost, so evicting both first is what keeps a stale file from outliving the record
    that replaced it.
    """
    if isinstance(record, (RollupArtifact, RollupRejection)):
        good, bad = store.rollups, store.rollup_rejections
    else:
        good, bad = store.summaries, store.rejections
    good.evict(key)
    bad.evict(key)
    collection = good if isinstance(record, (SummaryArtifact, RollupArtifact)) else bad
    collection.save(record)


def _error(failure: BaseException) -> str:
    """A rejection's error text: what failed, named, and what it said."""
    return f"{type(failure).__name__}: {failure}"


__all__ = [
    "DEFAULT_MODEL_PARAMS",
    "PROMPT",
    "RollupOutcome",
    "SummaryOutcome",
    "SummaryRun",
    "build_prompt",
    "input_hash",
    "params_hash",
    "parse_output",
    "prompt_hash",
    "prompt_text",
    "rollup_prompt_hash",
    "rollup_prompt_text",
    "rollup_tree",
    "summarize",
]
