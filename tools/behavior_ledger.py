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

from wordextract import model, opc, versions  # noqa: E402
from wordextract.chunker import DEFAULT_PARAMS, chunk  # noqa: E402
from wordextract.model import View  # noqa: E402
from wordextract.store import body_part_id  # noqa: E402
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
COMPONENTS = ("parse", "views", "chunks", "matcher", "contracts")

#: Which constant a component's version string is built from -- what to bump. The
#: ``parse`` string is a composite because one ``ParseResult`` is the product of all three
#: of its constants (the walker, the text model, the heading rules).
VERSION_CONSTANTS = {
    "parse": "SPEC_PARSER_VERSION / TEXTMODEL_VERSION / HEADING_RULESET_VERSION",
    "views": "TEXTMODEL_VERSION",
    "chunks": "CHUNKER_VERSION",
    "matcher": "MATCHER_VERSION (or the stemmer's STEM_ALGORITHM_VERSION)",
    "contracts": "docextract_core.SCHEMA_VERSION",
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
    "{version_string: sha256}}. A component's fingerprint is a sha256 over its output on "
    "the committed fixture corpus (every .docx under fixtures/ except the git-ignored, "
    "machine-local fixtures/real/, plus fixtures/terms/synthetic.example.json, default "
    "chunker params). The history before this file is not reconstructed: it was started "
    "from the code that added it, so it does not read as full history. Add a line with "
    "tools/update_behavior_ledger.py in the same commit as the behavior change that "
    "caused it, and bump that component's version constant (tools/behavior_ledger.py "
    "names it) in that same commit."
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
        TermGroup(canonical="hold harmless"),
        TermGroup(canonical="non-compliance", synonyms=["noncompliance"]),
        TermGroup(
            canonical="subrogation",
            synonyms=["right of subrogation", "right of recovery"],
            stemming="porter",
        ),
        TermGroup(canonical="exclusion", stemming="porter"),
        TermGroup(canonical="insured", stemming="porter"),
        TermGroup(canonical="aggregate limit", synonyms=["aggregate"]),
    ]
)
PROBE_TEXTS = (
    "Hold-harmless and hold harmless clauses; holdharmless; hold\u2011harmless.",
    "Non-compliance, noncompliance and NON\u00adCOMPLIANCE; non compliance.",
    "The right of subrogation; rights of subrogations; right ofsubrogation; subrogation-clause.",
    "Exclusions apply; excluded; excluding; exclusion; the exclusion-zone.",
    "The insured notified the insurer about insurance; insureds.",
    "The aggregate\nlimit applies; aggregate limit; Aggregate  Limit; aggregates.",
    "waiver of right of recovery and a RIGHT OF SUBROGATION.\nSecond paragraph: subrogation.",
)


def fingerprints(fixtures_dir: str | Path | None = None) -> dict[str, str]:
    """Every component's fingerprint over the corpus. One parse per fixture, four hashes.

    The parse, its views, its chunks and the registry's hits are all taken from the *same*
    parse of each document, so the four fingerprints describe one corpus reading.
    """
    root = Path(fixtures_dir) if fixtures_dir is not None else FIXTURES
    registry = load_registry_text((root / TERM_LIST).read_text(encoding="utf-8"))
    index = compile_registry(registry)
    parses: dict[str, Any] = {}
    projections: dict[str, Any] = {}
    chunks: dict[str, Any] = {}
    stress: dict[str, Any] = {}
    hits: dict[str, Any] = {}
    for document in corpus(root):
        name = document.relative_to(root).as_posix()
        parsed = walk_document(opc.Package(document))
        parses[name] = encode(parsed)
        projections[name] = [
            encode(project(stream, view))
            for stream in parsed.union_streams
            for view in PROJECTION_VIEWS
        ]
        chunks[name] = [
            encode({field_: getattr(built, field_) for field_ in CHUNK_FIELDS})
            for built in chunk(parsed, body_part_id(parsed), params=DEFAULT_PARAMS)
        ]
        stress[name] = [
            encode({field_: getattr(built, field_) for field_ in CHUNK_FIELDS})
            for built in chunk(parsed, body_part_id(parsed), params=STRESS_PARAMS)
        ]
        hits[name] = encode(match_document(index, parsed))
    probe_index = compile_registry(PROBE_REGISTRY)
    probe = [encode(match_text(probe_index, text)) for text in PROBE_TEXTS]
    return {
        "parse": sha256_json(parses),
        "views": sha256_json(projections),
        "chunks": sha256_json({"default": chunks, "stress": stress}),
        "matcher": sha256_json({"corpus": hits, "probe": probe}),
        "contracts": contracts_fingerprint(),
    }


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
    differs from the recomputed one is a behavior change nobody bumped for. Each problem
    names the constant to bump.
    """
    ledger = load_ledger() if ledger is None else ledger
    current = fingerprints(fixtures_dir) if computed is None else computed
    problems: list[str] = []
    for component, version in version_strings().items():
        entry = ledger.get(component)
        if not isinstance(entry, Mapping) or version not in entry:
            problems.append(
                f"{component}: version {version!r} is not in the ledger -- record it "
                f"(tools/update_behavior_ledger.py) in the same commit as the change; "
                f"if the behavior changed, bump {VERSION_CONSTANTS[component]} first"
            )
            continue
        if entry[version] != current[component]:
            problems.append(
                f"{component}: version {version!r} was recorded with fingerprint "
                f"{entry[version]} but the code now computes {current[component]} -- the "
                f"behavior changed without a bump: bump {VERSION_CONSTANTS[component]} "
                f"and record the new version"
            )
    return problems


def record(
    ledger: Mapping[str, Mapping[str, str]],
    *,
    computed: Mapping[str, str] | None = None,
    fixtures_dir: str | Path | None = None,
) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Append each component's current version and fingerprint; return what changed.

    Appends only: a version already recorded with the fingerprint the code still computes
    is left alone, and a version already recorded with a *different* fingerprint is a
    :class:`ValueError` -- that is the case where the change needs a version bump, not a
    new line under the old version, which is what makes the ledger append-only.
    """
    current = fingerprints(fixtures_dir) if computed is None else computed
    updated = {name: dict(entry) for name, entry in ledger.items()}
    lines: list[str] = []
    for component, version in version_strings().items():
        entry = updated.setdefault(component, {})
        recorded = entry.get(version)
        if recorded == current[component]:
            lines.append(f"{component} {version}: unchanged ({recorded})")
        elif recorded is not None:
            raise ValueError(
                f"{component}: version {version!r} is already recorded with fingerprint "
                f"{recorded}, but the code now computes {current[component]}. Bump "
                f"{VERSION_CONSTANTS[component]} and record the new version -- an "
                f"existing version is never overwritten."
            )
        else:
            entry[version] = current[component]
            lines.append(f"{component} {version}: appended {current[component]}")
    return updated, lines


__all__ = [
    "COMPONENTS",
    "CORE_RECORDS",
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
