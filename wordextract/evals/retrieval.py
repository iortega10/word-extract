"""Retrieval checks: the query layer's citations against a human's example set (Turn 7).

L1/L2 ask whether a *term list* finds what a person says is in the documents; this layer
asks the question the query layer actually answers: for an example -- a request in the
words a user would type -- do the citations a human says should appear, appear? The
candidates are ``wordextract.query.search``'s own results (the fixed tiering, one
citation per matching chunk), so the score runs over the product path rather than over a
re-implementation of it, and a citation is present or it is not: no rank cut, no
similarity, no confidence (rules 6 and 7 restated for retrieval).

The labels are human (``labels_provenance: human``) and never invented here. The
committed ``queries.example.json`` documents the format and is skipped by name -- the
rule ``must_find.example.json`` lives under -- so with no real set the harness reports
the layer as *not evaluated* with ``result`` None; the gate (``citation_recall == 1.0``)
arms itself only once real human labels exist. A real set is one of the things only the
owner can supply (docs/design/open-inputs.md).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
import json

#: A query set is one JSON file; ``*.example.json`` documents the format and is never scored.
QUERY_GLOB = "queries*.json"
EXAMPLE_SUFFIX = ".example.json"

#: The only provenance a query set may carry (open-inputs section 3): the example a human
#: chose is the point of the layer, so a generated or model-picked set is refused.
HUMAN = "human"

#: The top-level keys a query set file may carry, beyond ``_comment``.
_KEYS = ("query_set", "labels_provenance", "term_list", "examples", "_comment")


def _require(data: Mapping, key: str, what: str):
    if key not in data:
        raise ValueError(f"{what} is missing {key!r}")
    return data[key]


def _unknown(data: Mapping, keys: Iterable[str], what: str) -> None:
    extra = sorted(set(data) - set(keys))
    if extra:
        raise ValueError(f"unknown keys {extra} in {what} (allowed: {sorted(keys)})")


@dataclass(frozen=True)
class ExpectedCitation:
    """Where a human says an answer should point: a document, optionally a chunk.

    ``chunk`` is the 0-based ordinal in the accepted view's chunk list -- the same
    numbering the pair sidecar's ``changed`` entries use -- and None expects the
    document at any chunk. The address is fixture-relative, the set's own vocabulary.
    """

    document: str
    chunk: int | None = None
    note: str | None = None

    @property
    def label(self) -> str:
        return f"{self.document}#{self.chunk}" if self.chunk is not None else f"{self.document}#any"


@dataclass(frozen=True)
class Example:
    """One example request and the citations its answer must contain."""

    query: str
    expected: tuple[ExpectedCitation, ...]
    note: str | None = None


@dataclass(frozen=True)
class QuerySet:
    """A human's query set: the examples, and the term list they were asked against."""

    query_set: str
    labels_provenance: str
    term_list: str
    examples: tuple[Example, ...]
    path: Path | None = None


def citation_from_dict(data: Mapping) -> ExpectedCitation:
    """One expected citation: ``document``, optional 0-based ``chunk``."""
    if not isinstance(data, Mapping):
        raise ValueError(f"an expected citation must be an object, got {type(data).__name__}")
    _unknown(data, ("document", "chunk", "note"), "an expected citation")
    document = _require(data, "document", "an expected citation")
    if not isinstance(document, str) or not document:
        raise ValueError(f"an expected citation's document must be a non-empty string, got {document!r}")
    chunk = data.get("chunk")
    if chunk is not None:
        if not isinstance(chunk, int) or isinstance(chunk, bool):
            raise ValueError(f"an expected citation's chunk must be a 0-based ordinal, got {chunk!r}")
        if chunk < 0:
            raise ValueError(f"an expected citation's chunk must be >= 0, got {chunk}")
    note = data.get("note")
    if note is not None and not isinstance(note, str):
        raise ValueError(f"an expected citation's note must be a string, got {note!r}")
    return ExpectedCitation(document=document, chunk=chunk, note=note)


def example_from_dict(data: Mapping) -> Example:
    """One example: the query text as typed, and every citation its answer must contain."""
    if not isinstance(data, Mapping):
        raise ValueError(f"an example must be an object, got {type(data).__name__}")
    _unknown(data, ("query", "expected_citations", "note"), "an example")
    query = _require(data, "query", "an example")
    if not isinstance(query, str) or not query.strip():
        raise ValueError(f"an example's query must be a non-empty string, got {query!r}")
    citations = _require(data, "expected_citations", "an example")
    if not isinstance(citations, list):
        raise ValueError(f"an example's expected_citations must be a list, got {type(citations).__name__}")
    note = data.get("note")
    if note is not None and not isinstance(note, str):
        raise ValueError(f"an example's note must be a string, got {note!r}")
    return Example(
        query=query,
        expected=tuple(citation_from_dict(entry) for entry in citations),
        note=note,
    )


def query_set_from_dict(data: Mapping) -> QuerySet:
    """A whole query set from its JSON object; every shape error is a ``ValueError``.

    The load is the format gate: unknown keys, a non-human provenance, a missing field or
    two examples typed with the same query text all fail here rather than quietly
    scoring something the file did not say.
    """
    _unknown(data, _KEYS, "a query set")
    query_set = _require(data, "query_set", "a query set")
    provenance = _require(data, "labels_provenance", "a query set")
    if provenance != HUMAN:
        raise ValueError(
            f"a query set's labels_provenance must be {HUMAN!r}, got {provenance!r}: "
            "the example a human chose is what this layer measures"
        )
    term_list = _require(data, "term_list", "a query set")
    if not isinstance(term_list, str) or not term_list:
        raise ValueError(f"a query set's term_list must be a path relative to the fixtures, got {term_list!r}")
    examples = _require(data, "examples", "a query set")
    if not isinstance(examples, list):
        raise ValueError(f"a query set's examples must be a list, got {type(examples).__name__}")
    parsed = tuple(example_from_dict(entry) for entry in examples)
    seen: set[str] = set()
    for example in parsed:
        if example.query in seen:
            raise ValueError(f"two examples are both the query {example.query!r}; each question appears once")
        seen.add(example.query)
    return QuerySet(
        query_set=query_set,
        labels_provenance=provenance,
        term_list=term_list,
        examples=parsed,
    )


def iter_query_sets(directory: str | Path) -> dict[str, QuerySet]:
    """Every ``queries*.json`` in ``directory``, by name; ``*.example.json`` never scores.

    A directory without real sets yields ``{}`` -- with no query set the harness reports
    the layer as not evaluated rather than scoring the format example it committed.
    """
    found: dict[str, QuerySet] = {}
    for path in sorted(Path(directory).glob(QUERY_GLOB)):
        if path.name.endswith(EXAMPLE_SUFFIX):
            continue
        query_set = query_set_from_dict(json.loads(path.read_text(encoding="utf-8")))
        if query_set.query_set in found:
            raise ValueError(
                f"two query sets are both named {query_set.query_set!r} "
                f"({found[query_set.query_set].path} and {path})"
            )
        found[query_set.query_set] = QuerySet(
            query_set=query_set.query_set,
            labels_provenance=query_set.labels_provenance,
            term_list=query_set.term_list,
            examples=query_set.examples,
            path=path,
        )
    return found


@dataclass(frozen=True)
class RetrievalRow:
    """One scored example: what was expected, what appeared, what could not be resolved."""

    query: str
    expected: int
    found: int
    missed: tuple[ExpectedCitation, ...]
    unresolved: tuple[ExpectedCitation, ...]
    returned: int
    not_on_disk: int = 0

    def as_dict(self) -> dict:
        return {
            "query": self.query,
            "expected": self.expected,
            "found": self.found,
            "missed": [citation.label for citation in self.missed],
            "unresolved": [citation.label for citation in self.unresolved],
            "returned": self.returned,
            "not_on_disk": self.not_on_disk,
        }


@dataclass(frozen=True)
class RetrievalReport:
    """Every scored example across the query sets, with the skips kept on the record."""

    query_sets: tuple[str, ...] = ()
    term_lists: tuple[str, ...] = ()
    rows: tuple[RetrievalRow, ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()

    @property
    def expected(self) -> int:
        return sum(row.expected for row in self.rows)

    @property
    def found(self) -> int:
        return sum(row.found for row in self.rows)

    @property
    def evaluated(self) -> bool:
        """Something was expected: an example set that expects no citation measured nothing."""
        return bool(self.rows) and self.expected > 0

    @property
    def recall(self) -> float | None:
        return (self.found / self.expected) if self.expected else None

    @property
    def result(self) -> bool | None:
        """The gate's answer, or None when nothing was expected (never a silent pass)."""
        if not self.evaluated:
            return None
        return self.recall == 1.0

    @property
    def metrics(self) -> dict:
        if not self.evaluated:
            return {}
        return {
            "citation_recall": self.recall,
            "queries": len(self.rows),
            "expected_citations": self.expected,
            "found_citations": self.found,
        }

    @property
    def note(self) -> str:
        if not self.rows:
            if self.skipped:
                reasons = sorted({reason for _, reason in self.skipped})
                return "not evaluated: " + "; ".join(reasons)
            return "not evaluated: the query set has no example"
        if not self.evaluated:
            return "not evaluated: no example expects a citation"
        if self.result:
            return (
                f"evaluated: all {self.expected} expected citations appear "
                f"across {len(self.rows)} example(s)"
            )
        missed = sum(len(row.missed) for row in self.rows)
        return f"failed: {missed} expected citation(s) did not appear"

    def coverage(self) -> dict:
        return {
            "query_sets": list(self.query_sets),
            "term_lists": list(self.term_lists),
            "examples": len(self.rows) + len(self.skipped),
            "examples_scored": len(self.rows),
            "examples_not_on_disk": len(self.skipped),
            "citations_not_on_disk": sum(row.not_on_disk for row in self.rows),
            "evaluated": self.evaluated,
        }


def score(
    query_set: QuerySet,
    ranked: Mapping[str, Sequence[tuple[str, str]]],
    chunks: Mapping[str, Sequence[str]],
) -> RetrievalReport:
    """Score one query set over the search's own results.

    ``ranked`` maps an example's query text to the ordered ``(document, chunk_id)`` pairs
    the query layer returned for it (the harness filtered to this set's documents);
    ``chunks`` maps a document, as the set names it, to its chunk ids in accepted order --
    the ordinals an expected citation's ``chunk`` indexes. A citation whose document is
    not in ``chunks`` is skipped on the record (a real document's absence is not a recall
    failure, L2's precedent, rule 8); a citation whose ordinal the run cannot show IS a
    miss -- the human named a chunk that does not exist, which the row reports as
    ``unresolved`` and counts, so the denominator stays the human's full expectation.
    """
    rows: list[RetrievalRow] = []
    skipped: list[tuple[str, str]] = []
    for example in query_set.examples:
        on_disk = [citation for citation in example.expected if citation.document in chunks]
        absent = len(example.expected) - len(on_disk)
        if example.expected and not on_disk:
            skipped.append((example.query, "the document is not on disk where the set names it"))
            continue
        returned = ranked.get(example.query, ())
        returned_set = {(document, chunk_id) for document, chunk_id in returned}
        returned_documents = {document for document, _ in returned_set}
        found: list[ExpectedCitation] = []
        missed: list[ExpectedCitation] = []
        unresolved: list[ExpectedCitation] = []
        for citation in on_disk:
            if citation.chunk is None:
                present = citation.document in returned_documents
            else:
                ids = chunks[citation.document]
                if citation.chunk >= len(ids):
                    unresolved.append(citation)
                    missed.append(citation)
                    continue
                present = (citation.document, ids[citation.chunk]) in returned_set
            (found if present else missed).append(citation)
        rows.append(
            RetrievalRow(
                query=example.query,
                expected=len(on_disk),
                found=len(found),
                missed=tuple(missed),
                unresolved=tuple(unresolved),
                returned=len(returned_set),
                not_on_disk=absent,
            )
        )
    return RetrievalReport(
        query_sets=(query_set.query_set,),
        term_lists=(query_set.term_list,),
        rows=tuple(rows),
        skipped=tuple(skipped),
    )


def merge_reports(reports: Iterable[RetrievalReport]) -> RetrievalReport:
    """One report over every query set; no set scored yields the empty report."""
    rows: list[RetrievalRow] = []
    skipped: list[tuple[str, str]] = []
    query_sets: set[str] = set()
    term_lists: set[str] = set()
    for report in reports:
        rows.extend(report.rows)
        skipped.extend(report.skipped)
        query_sets.update(report.query_sets)
        term_lists.update(report.term_lists)
    return RetrievalReport(
        query_sets=tuple(sorted(query_sets)),
        term_lists=tuple(sorted(term_lists)),
        rows=tuple(rows),
        skipped=tuple(skipped),
    )
