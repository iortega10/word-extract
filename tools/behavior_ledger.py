"""Turn 9: the behavior ledger -- a version bump as a failing test, not a habit.

Store keys are built from version constants (``SPEC_PARSER_VERSION``,
``TEXTMODEL_VERSION``, ``HEADING_RULESET_VERSION``, ``CHUNKER_VERSION``,
``MATCHER_VERSION``, the vendored stemmer's version nested inside it, the codec's
``SCHEMA_VERSION``). A behavior change that ships without bumping its constant lets an
old store serve stale artifacts, and the build spec records that this project has already
done exactly that (the walker was fixed repeatedly while ``SPEC_PARSER_VERSION`` stayed
``"1"``; two contract changes shipped without a schema bump). This module makes the
discipline a test.

``tests/ledger/behavior_ledger.json`` is an **append-only** map
``{component: {version_string: fingerprint}}``. A fingerprint is a sha256 over a
canonical serialization of that component's output on the fixture corpus -- every
committed ``.docx`` under ``fixtures/``, the synthetic registry, default chunker params:

* **parse**, version ``SPEC_PARSER_VERSION|TEXTMODEL_VERSION|HEADING_RULESET_VERSION``:
  each fixture's full ``ParseResult``, through the strict codec's ``encode``;
* **views**, ``TEXTMODEL_VERSION``: every part's accepted/original/superseded
  projection -- text and offset runs -- for each fixture;
* **chunks**, ``CHUNKER_VERSION``: each fixture's body chunk ids, hashes, section paths
  and node ids;
* **matcher**, ``MATCHER_VERSION``: ``match_document`` hits over the corpus with the
  synthetic registry, unfolded and in output order;
* **contracts**, the codec's ``SCHEMA_VERSION``: every dataclass in
  :mod:`wordextract.model` and the core's own records -- name, field names, types and
  defaults. A field added, removed, renamed or retyped changes this fingerprint, which is
  exactly the contract change a schema bump is for.
* **render**, ``RENDER_VERSION``: each fixture's every body chunk in the union-markup format and
  in each view's plain text -- the two renderings the summarizer is written against;
* **summary_key**, ``SUMMARIZER_VERSION`` (and ``OUTPUT_SCHEMA_VERSION``): the key
  :func:`wordextract.store.summary_key` computes for each body chunk, under fixed model, params
  and prompt literals. It measures the *key's* construction, not a call: the prompt file is not
  read, because a committed fingerprint cannot depend on the working tree's prompt content.
* **pending**, ``PENDING_VERSION``: ``pending_changes`` over each fixture's default body cuts
  -- the entries a summary record and the manifest carry -- plus ``document_pending``'s
  document-level count and gap ids, so both halves of Turn 2's derivation are fingerprinted.
* **rank**, ``RANK_VERSION``: Turn 3's retrieval over each fixture's default cuts -- a fixed
  query set run through ``wordextract.rank.rank`` with the synthetic registry and canned
  (literal, never modeled) summaries, so resolution, tiering, dedupe and ordering are all
  under one fingerprint.
* **rollup**, ``ROLLUP_VERSION``: Turn 4's roll-up machinery over each fixture -- the
  section tree from ``wordextract.summarize.rollup_tree``, every canned roll-up executed
  bottom-up through ``_roll_tree`` (fan-in grouping included: ``ROLLUP_FAN_IN`` is also a
  key member), each record's real ``rollup_key`` over probe literals and the three
  derivations ``_rollup_fields`` computes. No call, no provider, no prompt file read --
  the children are the canned summaries, whose ``has_pending`` probe cycles True/None/False
  so the tri-state union's branches are all under the fingerprint.

:func:`check` is the test's half (it reports every disagreement) and :func:`record` is
:mod:`update_behavior_ledger`'s (it appends, and refuses to overwrite). The ledger starts
from the code that adds it: the history before this file is **not** reconstructed (build
spec, Turn 9), and the ``_comment`` header says so.

**``fixtures/real/`` is excluded from the corpus** even though the spec's shorthand is
"all ``fixtures/**/*.docx``": those documents are git-ignored, machine-local and opt-in
(``fixtures/real/README.md``), so a fingerprint that folded them in would differ per
machine and could not be a committed constant. The corpus is the fixtures the repository
ships.
"""
from __future__ import annotations

import json
import sys
from dataclasses import MISSING, Field, fields, is_dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

# The tool is run from the repo root, where neither package is on the path outside pytest
# (the root ``pyproject`` supplies both via ``pythonpath``). Put them there, as the
# ``python -m`` entry points rely on an editable install of the core to do.
_ROOT = Path(__file__).resolve().parent.parent
for _path in (_ROOT, _ROOT / "docextract-core"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from docextract_core import SCHEMA_VERSION, encode, sha256_json  # noqa: E402
from docextract_core import LLMCall, LLMResponse, RawArchiveRecord  # noqa: E402

from wordextract import model, opc, summarize, versions  # noqa: E402
# Aliased: this module defines its own ``render`` (the ledger's text form).
from wordextract import render as rendering  # noqa: E402
from wordextract.chunker import DEFAULT_PARAMS, chunk  # noqa: E402
from wordextract.model import View  # noqa: E402
from wordextract.pending import document_pending, pending_changes  # noqa: E402
from wordextract.rank import (  # noqa: E402
    DocumentInput,
    SummaryRef,
    rank as rank_results,
)
from wordextract.store import body_part_id, rollup_key, summary_key  # noqa: E402
from wordextract.chunker import ChunkParams  # noqa: E402
from wordextract.model import TermGroup  # noqa: E402
from wordextract.terms import (  # noqa: E402
    TermRegistry,
    compile_registry,
    load_registry_text,
    match_document,
    match_text,
)
from wordextract.views import project  # noqa: E402
from wordextract.walker import walk_document  # noqa: E402

#: The component names, in the ledger's own order. Each is a stage whose output is a
#: function of the version constants, so changing what it emits must change a key.
COMPONENTS = (
    "parse",
    "views",
    "chunks",
    "matcher",
    "contracts",
    "render",
    "summary_key",
    "pending",
    "rank",
    "rollup",
)

#: Which constant a component's version string is built from -- what to bump. The
#: ``parse`` string is a composite because one ``ParseResult`` is the product of all three
#: of its constants (the walker, the text model, the heading rules).
VERSION_CONSTANTS = {
    "parse": "SPEC_PARSER_VERSION / TEXTMODEL_VERSION / HEADING_RULESET_VERSION",
    "views": "TEXTMODEL_VERSION",
    "chunks": "CHUNKER_VERSION",
    "matcher": "MATCHER_VERSION (or the stemmer's STEM_ALGORITHM_VERSION)",
    "contracts": "docextract_core.SCHEMA_VERSION",
    "render": "RENDER_VERSION",
    "summary_key": "SUMMARIZER_VERSION (or OUTPUT_SCHEMA_VERSION)",
    "pending": "PENDING_VERSION",
    "rank": "RANK_VERSION",
    "rollup": "ROLLUP_VERSION",
}

#: The corpus. ``fixtures/real`` is machine-local (see the module docstring), so it is
#: the one directory under ``fixtures/`` that is not a fixture.
FIXTURES = _ROOT / "fixtures"
_LOCAL_ONLY = "real"
TERM_LIST = Path("terms") / "synthetic.example.json"

#: The views a projection is fingerprinted in, explicitly ordered: ``superseded`` is a
#: mask and fixture only (D6), but what it projects is still behavior.
PROJECTION_VIEWS = (View.ACCEPTED, View.ORIGINAL, View.SUPERSEDED)

#: What a chunk's fingerprint covers: its identity, its two hashes, and the structure it
#: was placed in. Its text is already in ``content_hash``, and its heading decision fields
#: are the parse's (the ``parse`` component's).
CHUNK_FIELDS = ("id", "content_hash", "context_hash", "section_path", "node_ids")

#: Records that cross the docextract-core boundary, whose shape is a contract too.
CORE_RECORDS = (LLMCall, LLMResponse, RawArchiveRecord)

LEDGER_PATH = _ROOT / "tests" / "ledger" / "behavior_ledger.json"

_HEADER = (
    "Append-only behavior fingerprints (build spec, Turn 9): {component: "
    "{key: sha256}}. A component's fingerprint is a sha256 over its output on a corpus of "
    "committed fixtures (tests/ledger/corpus.json lists each corpus version; "
    "fixtures/real/ is git-ignored and never included; the synthetic term registry and "
    "default chunker params are fixed). A key is '<version string>|corpus:<N>': the code "
    "version and the corpus version it was fingerprinted on. Adding a fixture adds a new "
    "corpus version and NEW LINES under the SAME component versions -- it is not a behavior "
    "change and bumps nothing; the tool refuses to record a new corpus if any older "
    "corpus's fingerprint moved (that is a behavior change that needs a bump). 'contracts' "
    "does not depend on the corpus and is keyed by the schema version alone. The history "
    "before this file is not reconstructed. Record lines with "
    "tools/update_behavior_ledger.py in the same commit as the change that caused them."
)


def corpus(fixtures_dir: str | Path | None = None) -> list[Path]:
    """Every committed fixture document, in a machine-independent order."""
    root = Path(fixtures_dir) if fixtures_dir is not None else FIXTURES
    documents = (
        path
        for path in root.rglob("*.docx")
        if _LOCAL_ONLY not in path.relative_to(root).parts
    )
    return sorted(documents, key=lambda path: path.relative_to(root).as_posix())


CORPUS_PATH = _ROOT / "tests" / "ledger" / "corpus.json"

#: The components whose fingerprint is over the corpus. ``contracts`` is over the record
#: definitions alone, so it is keyed by the schema version and nothing else.
CORPUS_DEPENDENT = ("parse", "views", "chunks", "matcher", "render", "summary_key", "pending", "rank", "rollup")

_CORPUS_HEADER = (
    "The ledger's fixture corpora, oldest first: each version lists the fixtures (paths "
    "relative to fixtures/) its fingerprints were taken over. Growing the committed fixtures "
    "adds a version here (tools/update_behavior_ledger.py --new-corpus); it never bumps a "
    "component version."
)


def discovered(fixtures_dir: str | Path | None = None) -> list[str]:
    """The committed fixture documents, as sorted paths relative to the fixtures directory."""
    root = Path(fixtures_dir) if fixtures_dir is not None else FIXTURES
    return [path.relative_to(root).as_posix() for path in corpus(root)]


def load_corpora(path: str | Path | None = None) -> list[dict]:
    """The corpus versions on disk, oldest first; ``[]`` when there is no manifest yet."""
    manifest = Path(path) if path is not None else CORPUS_PATH
    if not manifest.is_file():
        return []
    loaded = json.loads(manifest.read_text(encoding="utf-8"))
    return [
        {"version": int(entry["version"]), "files": list(entry["files"])}
        for entry in loaded.get("corpora", [])
    ]


def write_corpora(corpora: list[dict], path: str | Path | None = None) -> Path:
    manifest = Path(path) if path is not None else CORPUS_PATH
    manifest.parent.mkdir(parents=True, exist_ok=True)
    body = {"_comment": _CORPUS_HEADER, "corpora": corpora}
    with manifest.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(body, indent=2, sort_keys=True) + "\n")
    return manifest


def latest_corpus(corpora: list[dict]) -> int:
    return max((entry["version"] for entry in corpora), default=0)


def entry_key(component: str, version: str, corpus_version: int) -> str:
    """The ledger key for a component: its version, plus the corpus it was taken over."""
    if component in CORPUS_DEPENDENT:
        return f"{version}|corpus:{corpus_version}"
    return version


def version_strings() -> dict[str, str]:
    """Each component's current version string, read off the modules at call time.

    Read dynamically -- not imported by value -- so a test that monkeypatches a constant
    (or a branch that bumps one) is seen here without a reimport.
    """
    return {
        "parse": "|".join(
            (
                versions.SPEC_PARSER_VERSION,
                versions.TEXTMODEL_VERSION,
                versions.HEADING_RULESET_VERSION,
            )
        ),
        "views": versions.TEXTMODEL_VERSION,
        "chunks": versions.CHUNKER_VERSION,
        "matcher": versions.MATCHER_VERSION,
        "contracts": SCHEMA_VERSION,
        "render": rendering.RENDER_VERSION,
        "summary_key": versions.SUMMARIZER_VERSION,
        "pending": versions.PENDING_VERSION,
        "rank": versions.RANK_VERSION,
        "rollup": versions.ROLLUP_VERSION,
    }


def _field_shape(field_: Field) -> list[Any]:
    """One field as ``[name, type, default]``; a factory is named, never called."""
    if field_.default is not MISSING:
        default: Any = encode(field_.default)
    elif field_.default_factory is not MISSING:
        factory = field_.default_factory
        default = {"factory": getattr(factory, "__qualname__", type(factory).__name__)}
    else:
        default = None
    return [field_.name, str(field_.type), default]


def contract_records() -> tuple[type, ...]:
    """Every dataclass whose shape is part of the persisted contract, sorted by name."""
    return tuple(
        sorted(
            (
                obj
                for obj in vars(model).values()
                if isinstance(obj, type) and is_dataclass(obj)
            ),
            key=lambda cls: cls.__name__,
        )
    ) + tuple(sorted(CORE_RECORDS, key=lambda cls: cls.__name__))


def contracts_fingerprint(records: Iterable[type] | None = None) -> str:
    """The shape of every record: its name, and its fields' names, types and defaults."""
    shapes = {
        cls.__qualname__: [_field_shape(field_) for field_ in fields(cls)]
        for cls in sorted(
            list(records) if records is not None else contract_records(),
            key=lambda cls: cls.__qualname__,
        )
    }
    return sha256_json(shapes)


#: Chunking is also fingerprinted under parameters small enough to force every path -- a
#: split, a merge-up, a list-run boundary -- on the tiny fixtures, which never reach the
#: default size cap. Without it a change to the packing rules would move no fingerprint.
STRESS_PARAMS = ChunkParams(size_cap=60, min_size=15, list_run=2)

#: The matcher is also fingerprinted on a fixed probe, because the fixture corpus yields only
#: a handful of hits against the synthetic registry and says nothing about hyphen readings,
#: stemming, synonym precedence or fused seams. The probe is part of the *definition* of the
#: matcher fingerprint: changing it changes every matcher fingerprint, so it is changed
#: deliberately and re-recorded, never edited to make a diff go away.
PROBE_REGISTRY = TermRegistry(
    groups=[
        TermGroup(canonical="hand delivery"),
        TermGroup(canonical="non-compliance", synonyms=["noncompliance"]),
        TermGroup(
            canonical="termination",
            synonyms=["right of termination", "right of transfer"],
            stemming="porter",
        ),
        TermGroup(canonical="exclusion", stemming="porter"),
        TermGroup(canonical="universe", stemming="porter"),
        TermGroup(canonical="aggregate limit", synonyms=["aggregate"]),
    ]
)
PROBE_TEXTS = (
    "Hand-delivery and hand delivery clauses; handdelivery; hand\u2011delivery.",
    "Non-compliance, noncompliance and NON\u00adCOMPLIANCE; non compliance.",
    "The right of termination; rights of terminations; right oftermination; termination-clause.",
    "Exclusions apply; excluded; excluding; exclusion; the exclusion-zone.",
    "The universe notified the university about universal; universes.",
    "The aggregate\nlimit applies; aggregate limit; Aggregate  Limit; aggregates.",
    "waiver of right of transfer and a RIGHT OF TERMINATION.\nSecond paragraph: termination.",
)

#: The views a chunk's plain text is fingerprinted in: the two a summarizer could be handed
#: (``superseded`` is a mask, and a chunk's own bytes are already in ``content_hash``).
RENDER_VIEWS = (View.ACCEPTED, View.ORIGINAL)

#: The summarizer inputs a ``summary_key`` fingerprint is taken under. Literals, because the
#: component measures the *key*, not a call: the prompt file is not read (a committed
#: fingerprint cannot depend on the working tree's prompt text), and the model is nobody.
PROBE_SUMMARY_INPUTS = {
    "model_id": "ledger-probe",
    "model_params_hash": "probe-params",
    "prompt_hash": "probe-prompt",
}


#: Turn 3's fixed query set, chosen so every path the ranking takes is under one
#: fingerprint: a canonical form that resolves and merges term+text on the same chunk,
#: a **synonym** that resolves and produces term-only results (the hit's chunk whose view
#: text lacks the query's words), an unregistered word that is text-tier only, and
#: ``canned``, which matches only the canned summaries' own text -- the summary tier,
#: guaranteed to fire even where no fixture text carries a term. Part of the *definition*
#: of the rank fingerprint: changing it changes every rank fingerprint, deliberately
#: re-recorded, never edited to make a diff go away.
RANK_QUERIES = (
    "termination",
    "right of transfer",
    "partner",
    "canned",
)


def _canned_summary(built) -> SummaryRef:
    """One chunk's canned summary: a literal of the section path, never a model's answer."""
    path = " > ".join(built.section_path) if built.section_path else "the document"
    return SummaryRef(
        chunk_id=built.id,
        text=f"Canned ledger summary of {path}",
        topics=tuple(built.section_path) or ("document",),
    )


def _canned_rollup(name: str, parsed, chunks) -> dict[str, Any]:
    """One fixture's roll-up fingerprint: its tree plus every canned roll-up over it.

    No call, no provider, no prompt file: each execution derives a real
    :class:`wordextract.model.RollupArtifact` under a real :func:`wordextract.store.rollup_key`
    over probe literals -- ``PROBE_SUMMARY_INPUTS``'s three names are rollup_key's three, so
    the key's construction is measured while the committed fingerprint stays independent of
    the working tree's prompt content, exactly as ``summary_key``'s is. What moves this
    fingerprint is the machinery: ``summarize.rollup_tree``'s structure, the bottom-up walk
    and fan-in grouping of ``summarize._roll_tree`` (``ROLLUP_FAN_IN`` is itself a key
    member, so a changed fan-in re-keys every roll-up even where no fixture exceeds one
    batch), and ``summarize._rollup_fields``' three derivations, recorded per execution.

    The children are the canned summaries, and their ``has_pending`` probe cycles
    True/None/False by chunk position so the tri-state union's three branches are all
    exercised across the corpus. ``document_id`` is the fixture's own name: the canned
    document unit's id appears in no key (by design), only in provenance.
    """
    built_by_id = {built.id: built for built in chunks}
    canned = {chunk_id: _canned_summary(built) for chunk_id, built in built_by_id.items()}
    pending_probe = (True, None, False)

    def child_of_chunk(chunk_id: str):
        built = built_by_id[chunk_id]
        summary = canned[chunk_id]
        return summarize._Child(
            key=summary_key(
                summarize.input_hash(rendering.render_union_markup(parsed, built)),
                **PROBE_SUMMARY_INPUTS,
            ),
            label=f"chunk:{chunk_id}",
            text=summary.text,
            has_pending=pending_probe[list(built_by_id).index(chunk_id) % 3],
            chunk_ids=(chunk_id,),
        )

    executions: list[dict[str, Any]] = []

    def execute(children, target_kind: str, target_id: str):
        fields = summarize._rollup_fields(children)
        key = rollup_key([child.key for child in children], **PROBE_SUMMARY_INPUTS)
        record = model.RollupArtifact(
            key=key,
            summary=f"Canned ledger roll-up of {target_kind}:{target_id}",
            topics=["ledger", target_kind],
            open_questions=[],
            child_keys=fields["child_keys"],
            child_chunk_ids=fields["child_chunk_ids"],
            has_pending=fields["has_pending"],
            target_kind=target_kind,
            target_id=target_id,
            document_id=name,
            model_id=PROBE_SUMMARY_INPUTS["model_id"],
            prompt_hash=PROBE_SUMMARY_INPUTS["prompt_hash"],
            params_hash=PROBE_SUMMARY_INPUTS["model_params_hash"],
            prompt_ref="",
            response_ref="",
        )
        executions.append(
            {
                "target": f"{target_kind}:{target_id}",
                "labels": [child.label for child in children],
                "key": key,
                **fields,
            }
        )
        outcome = summarize.RollupOutcome(
            target_kind=target_kind,
            target_id=target_id,
            key=key,
            called=True,
            rollup=record,
            rejection=None,
        )
        return summarize._Rolled(
            child=summarize._child_of(record), record=record, outcome=outcome
        )

    tree = summarize.rollup_tree(parsed, chunks, document_id=name)
    if tree.children:
        summarize._roll_tree(tree, child_of_chunk=child_of_chunk, execute=execute)
    return {"document_id": name, "executions": executions}


def _document_pieces(root: Path, name: str) -> dict[str, Any]:
    """Everything one fixture contributes to the corpus fingerprints, parsed once."""
    registry = _registry(root)
    index = compile_registry(registry)
    parsed = walk_document(opc.Package(root / name))
    body = body_part_id(parsed)
    default = list(chunk(parsed, body, params=DEFAULT_PARAMS))
    hits = match_document(index, parsed)
    return {
        "parse": encode(parsed),
        "views": [
            encode(project(stream, view))
            for stream in parsed.union_streams
            for view in PROJECTION_VIEWS
        ],
        "chunks": [
            encode({field_: getattr(built, field_) for field_ in CHUNK_FIELDS})
            for built in default
        ],
        "stress": [
            encode({field_: getattr(built, field_) for field_ in CHUNK_FIELDS})
            for built in chunk(parsed, body, params=STRESS_PARAMS)
        ],
        "matcher": encode(hits),
        "render": encode(
            [
                {
                    "id": built.id,
                    "markup": rendering.render_union_markup(parsed, built),
                    "text": {
                        view.value: rendering.chunk_text(parsed, built, view)
                        for view in RENDER_VIEWS
                    },
                }
                for built in default
            ]
        ),
        "summary_key": [
            summary_key(
                summarize.input_hash(rendering.render_union_markup(parsed, built)),
                **PROBE_SUMMARY_INPUTS,
            )
            for built in default
        ],
        "pending": encode(
            {
                "chunks": [
                    {"chunk_id": built.id, "entries": pending_changes(parsed, built)}
                    for built in default
                ],
                "document": document_pending(parsed, default),
            }
        ),
        "rank": encode(
            [
                {
                    "query": query,
                    "results": rank_results(
                        query,
                        (
                            DocumentInput(
                                document_id=name,
                                parsed=parsed,
                                chunks=tuple(default),
                                hits=hits,
                                summaries=tuple(
                                    _canned_summary(built) for built in default
                                ),
                            ),
                        ),
                        registry=registry,
                        views=RENDER_VIEWS,
                    ),
                }
                for query in RANK_QUERIES
            ]
        ),
        "rollup": _canned_rollup(name, parsed, default),
    }


def _registry(root: Path) -> TermRegistry:
    return load_registry_text((root / TERM_LIST).read_text(encoding="utf-8"))


def _registry_index(root: Path):
    return compile_registry(_registry(root))


def _assemble(pieces: Mapping[str, dict[str, Any]], names: list[str], root: Path) -> dict[str, str]:
    """One corpus's fingerprints plus the contracts fingerprint, from parsed pieces."""
    probe_index = compile_registry(PROBE_REGISTRY)
    probe = [encode(match_text(probe_index, text)) for text in PROBE_TEXTS]
    return {
        "parse": sha256_json({n: pieces[n]["parse"] for n in names}),
        "views": sha256_json({n: pieces[n]["views"] for n in names}),
        "chunks": sha256_json(
            {
                "default": {n: pieces[n]["chunks"] for n in names},
                "stress": {n: pieces[n]["stress"] for n in names},
            }
        ),
        "matcher": sha256_json({"corpus": {n: pieces[n]["matcher"] for n in names}, "probe": probe}),
        "contracts": contracts_fingerprint(),
        "render": sha256_json({n: pieces[n]["render"] for n in names}),
        "summary_key": sha256_json({n: pieces[n]["summary_key"] for n in names}),
        "pending": sha256_json({n: pieces[n]["pending"] for n in names}),
        "rank": sha256_json({n: pieces[n]["rank"] for n in names}),
        "rollup": sha256_json({n: pieces[n]["rollup"] for n in names}),
    }


def all_fingerprints(
    fixtures_dir: str | Path | None = None,
    corpus_versions: list[int] | None = None,
    corpora: list[dict] | None = None,
) -> dict[int, dict[str, str]]:
    """Fingerprints for each requested corpus version, parsing every fixture **once**.

    A fixture appearing in several corpus versions is parsed a single time and its pieces
    are reused, so checking every corpus costs one pass over the union of the files.
    """
    root = Path(fixtures_dir) if fixtures_dir is not None else FIXTURES
    corpora = load_corpora() if corpora is None else corpora
    if not corpora:  # no manifest yet: the discovered fixtures are corpus 1
        corpora = [{"version": 1, "files": discovered(root)}]
    wanted = corpus_versions if corpus_versions is not None else [latest_corpus(corpora)]
    by_version = {entry["version"]: entry["files"] for entry in corpora}
    needed = sorted({name for version in wanted for name in by_version[version]})
    pieces = {name: _document_pieces(root, name) for name in needed}
    return {version: _assemble(pieces, by_version[version], root) for version in wanted}


def fingerprints(
    fixtures_dir: str | Path | None = None, corpus_version: int | None = None
) -> dict[str, str]:
    """Every component's fingerprint over one corpus (the latest by default)."""
    corpora = load_corpora()
    if not corpora:
        corpora = [{"version": 1, "files": discovered(fixtures_dir)}]
    version = latest_corpus(corpora) if corpus_version is None else corpus_version
    return all_fingerprints(fixtures_dir, [version], corpora)[version]


def load_ledger(path: str | Path | None = None) -> dict[str, dict[str, str]]:
    """The ledger as it stands on disk; ``{}`` when the file does not exist yet."""
    ledger_path = Path(path) if path is not None else LEDGER_PATH
    if not ledger_path.is_file():
        return {}
    return _read_ledger(ledger_path.read_text(encoding="utf-8"))


def _read_ledger(text: str) -> dict[str, dict[str, str]]:
    loaded = json.loads(text)
    if not isinstance(loaded, dict):
        raise ValueError("the ledger must be a JSON object")
    return {
        component: dict(entry)
        for component, entry in loaded.items()
        if not component.startswith("_")
    }


def render(ledger: Mapping[str, Mapping[str, str]]) -> str:
    """The ledger's canonical file text: sorted keys, the header first, one trailing \\n."""
    body = {"_comment": _HEADER, **{name: dict(entry) for name, entry in ledger.items()}}
    return json.dumps(body, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_ledger(ledger: Mapping[str, Mapping[str, str]], path: str | Path | None = None) -> Path:
    ledger_path = Path(path) if path is not None else LEDGER_PATH
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    # `newline="\n"`: the ledger's bytes are then the same on every platform, which is the
    # property the file exists to carry.
    with ledger_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(render(ledger))
    return ledger_path


def check(
    ledger: Mapping[str, Mapping[str, str]] | None = None,
    *,
    computed: Mapping[str, str] | None = None,
    fixtures_dir: str | Path | None = None,
) -> list[str]:
    """Every way the ledger disagrees with the code: ``[]`` means the guard is satisfied.

    Both halves of the discipline are here: a component whose *current* version string is
    missing from the ledger is a bump nobody recorded, and one whose recorded fingerprint
    differs from the recomputed one is a behavior change nobody bumped for. Every corpus
    version the ledger holds an entry for is checked, so a behavior change cannot hide in
    the same commit as a new fixture. Each problem names the constant to bump.

    ``computed`` (one set of fingerprints) checks the latest corpus only.
    """
    ledger = load_ledger() if ledger is None else ledger
    corpora = load_corpora() or [
        {"version": 1, "files": discovered(fixtures_dir)}
    ]
    latest = latest_corpus(corpora)
    problems: list[str] = []
    if not load_corpora():
        problems.append(
            "corpus: tests/ledger/corpus.json is missing -- run "
            "tools/update_behavior_ledger.py to write corpus version 1"
        )
    if load_corpora() and sorted(discovered(fixtures_dir)) != sorted(
        next(e["files"] for e in corpora if e["version"] == latest)
    ):
        problems.append(
            "corpus: the committed fixtures differ from corpus "
            f"{latest} in tests/ledger/corpus.json -- add a corpus version with "
            "tools/update_behavior_ledger.py --new-corpus (growing the fixtures bumps no "
            "component version)"
        )
    if computed is None:
        # one pass over every corpus version the ledger has a line for under a current version
        current_versions = version_strings()
        wanted = sorted(
            {
                entry["version"]
                for entry in corpora
                for component, version in current_versions.items()
                if entry_key(component, version, entry["version"]) in (ledger.get(component) or {})
            }
            | {latest}
        )
        by_corpus = all_fingerprints(fixtures_dir, wanted, corpora)
    else:
        by_corpus = {latest: dict(computed)}
    for component, version in version_strings().items():
        entry = ledger.get(component)
        entry = entry if isinstance(entry, Mapping) else {}
        key = entry_key(component, version, latest)
        if key not in entry:
            problems.append(
                f"{component}: {key!r} is not in the ledger -- record it "
                f"(tools/update_behavior_ledger.py) in the same commit as the change; "
                f"if the behavior changed, bump {VERSION_CONSTANTS[component]} first"
            )
        for corpus_version, current in sorted(by_corpus.items()):
            recorded_key = entry_key(component, version, corpus_version)
            if recorded_key in entry and entry[recorded_key] != current[component]:
                problems.append(
                    f"{component}: {recorded_key!r} was recorded with fingerprint "
                    f"{entry[recorded_key]} but the code now computes {current[component]} "
                    f"-- the behavior changed without a bump: bump "
                    f"{VERSION_CONSTANTS[component]} and record the new version"
                )
    return problems


def record(
    ledger: Mapping[str, Mapping[str, str]],
    *,
    computed: Mapping[str, str] | None = None,
    fixtures_dir: str | Path | None = None,
) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Append each component's current version and fingerprint for every corpus version.

    Appends only: a key already recorded with the fingerprint the code still computes is
    left alone, and one recorded with a *different* fingerprint is a :class:`ValueError` --
    that is the case where the change needs a version bump, not a new line under the old
    version. Because every corpus version is recomputed, adding a fixture (a new corpus
    version) is refused if it also hid a behavior change in an older corpus.
    """
    corpora = load_corpora() or [{"version": 1, "files": discovered(fixtures_dir)}]
    latest = latest_corpus(corpora)
    if computed is None:
        by_corpus = all_fingerprints(
            fixtures_dir, [entry["version"] for entry in corpora], corpora
        )
    else:
        by_corpus = {latest: dict(computed)}
    updated = {name: dict(entry) for name, entry in ledger.items()}
    lines: list[str] = []
    for component, version in version_strings().items():
        entry = updated.setdefault(component, {})
        for corpus_version, current in sorted(by_corpus.items()):
            key = entry_key(component, version, corpus_version)
            recorded = entry.get(key)
            if recorded == current[component]:
                lines.append(f"{component} {key}: unchanged ({recorded})")
            elif recorded is not None:
                raise ValueError(
                    f"{component}: {key!r} is already recorded with fingerprint "
                    f"{recorded}, but the code now computes {current[component]}. Bump "
                    f"{VERSION_CONSTANTS[component]} and record the new version -- an "
                    f"existing key is never overwritten."
                )
            else:
                entry[key] = current[component]
                lines.append(f"{component} {key}: appended {current[component]}")
            if component not in CORPUS_DEPENDENT:
                break  # corpus-independent: one line is the whole fact
    return updated, lines


def add_corpus_version(
    fixtures_dir: str | Path | None = None, path: str | Path | None = None
) -> tuple[list[dict], bool]:
    """Append a corpus version listing today's fixtures; ``False`` when nothing changed."""
    corpora = load_corpora(path)
    files = discovered(fixtures_dir)
    if corpora and sorted(corpora[-1]["files"]) == sorted(files):
        return corpora, False
    corpora = [*corpora, {"version": latest_corpus(corpora) + 1, "files": files}]
    return corpora, True


__all__ = [
    "COMPONENTS",
    "CORE_RECORDS",
    "CORPUS_DEPENDENT",
    "CORPUS_PATH",
    "add_corpus_version",
    "all_fingerprints",
    "discovered",
    "entry_key",
    "latest_corpus",
    "load_corpora",
    "write_corpora",
    "FIXTURES",
    "LEDGER_PATH",
    "TERM_LIST",
    "VERSION_CONSTANTS",
    "check",
    "contract_records",
    "contracts_fingerprint",
    "corpus",
    "fingerprints",
    "load_ledger",
    "record",
    "render",
    "version_strings",
    "write_ledger",
]
