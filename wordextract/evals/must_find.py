"""Turn 9: L2's human must-find labels -- the loader, the format, and the scorer.

L2 is the only layer whose labels can make the *product* wrong rather than the code: a term
list that misses a phrase a human says is in the document fails at recall, and a hit nobody
claimed is a false positive until a human classifies it. So the labels here have two rules
this module enforces rather than documents:

* **they are human** -- ``labels_provenance`` must be ``"human"``. A generator or a model
  cannot state what *should* be found in a real document without the answer being circular
  (open-inputs section 3; design D11).
* **they are never edited to match output** -- nothing here writes a label. Scoring is
  one-way: the labels say what is there, the run says what was found, and a disagreement is
  a failure carrying both sides.

The format (see ``fixtures/evals/must_find.example.json``, which is documentation and is
never scored) is one file per label set::

    {
      "label_set": "...",              # names the set; a file may hold one
      "labels_provenance": "human",    # the rule above
      "term_list": "terms/real.json",  # the registry that has to *find* these terms
      "documents": {
        "real/scan.docx": {            # path, relative to the fixtures directory
          "source": "where it came from and why it may be used",
          "must_find": [{"term": "...", "note": "where it is"}],
          "expected_extra": [
            {"term": "...", "classification": "false_positive", "note": "why it is not one"}
          ]
        }
      }
    }

``must_find`` is the recall side: every term must appear among the hits, or the gate fails.
``expected_extra`` is the precision side: a hit the must-find terms do not account for is a
false positive, and it is only *classified* -- never silently tolerated -- when a human
names it here as a true positive, a real false positive, or a stem of a must-find term.

A labelled document that is not on disk (``fixtures/real`` is git-ignored, and a real
document is routinely absent) is reported as **skipped**, never scored as a miss: L2 must not
report a recall failure for a file it never read.

Scoring needs only the *text* each hit matched, so ``score`` takes
``Mapping[document, Iterable[str]]`` and stays free of parser types -- the term matcher and
the document never meet inside the scorer.

Deliberately stdlib-only, like :mod:`wordextract.evals.labels` and for the same reason: a
checker that shares code with the thing it checks cannot tell a bug from a definition.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
import json

#: A label set is one JSON file; ``*.example.json`` documents the format and is never scored.
MUST_FIND_SUFFIX = ".json"
EXAMPLE_SUFFIX = ".example.json"

#: Files other eval loaders own (``queries*.json``, ``l3_grades*.json``): must-find globs
#: ``*.json`` broadly, so it leaves their files alone rather than parsing a query set as
#: labels and failing on a key it does not know.
OTHER_LOADERS = ("queries", "l3_grades")

#: The only provenance the must-find list may carry (open-inputs section 3).
HUMAN = "human"

#: How a human may classify a hit the must-find terms do not account for.
CLASSIFICATIONS = ("true_positive", "false_positive", "stem_match")


@dataclass(frozen=True)
class MustFindTerm:
    """One phrase a human says is in the document (its ``note`` says where)."""

    term: str
    note: str | None = None


@dataclass(frozen=True)
class ExpectedExtra:
    """A hit the must-find terms do not account for, classified by a human."""

    term: str
    classification: str
    note: str | None = None


@dataclass(frozen=True)
class DocumentLabels:
    """One labelled document: what has to be found in it, and what else is expected."""

    document: str
    source: str = ""
    must_find: tuple[MustFindTerm, ...] = ()
    expected_extra: tuple[ExpectedExtra, ...] = ()


@dataclass(frozen=True)
class MustFindSet:
    """One ``must_find*.json`` file, and the registry that has to find its terms."""

    label_set: str
    labels_provenance: str
    term_list: str
    documents: tuple[DocumentLabels, ...] = ()
    path: Path | None = None

    def document(self, name: str) -> DocumentLabels | None:
        for labels in self.documents:
            if labels.document == name:
                return labels
        return None


@dataclass(frozen=True)
class L2Row:
    """One document's L2 result: what should have been found, and what was."""

    document: str
    must_find: int
    found: int
    missed: tuple[str, ...]
    extra: int
    unclassified: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.missed and not self.unclassified

    def as_dict(self) -> dict:
        return {
            "layer": "L2",
            "document": self.document,
            "must_find": self.must_find,
            "found": self.found,
            "missed": list(self.missed),
            "extra": self.extra,
            "unclassified": list(self.unclassified),
            "result": self.ok,
        }


@dataclass(frozen=True)
class L2Report:
    """The must-find score: recall over the labelled terms, and the unclassified extras."""

    label_sets: tuple[str, ...] = ()
    term_lists: tuple[str, ...] = ()
    rows: tuple[L2Row, ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()

    @property
    def documents(self) -> tuple[str, ...]:
        return tuple(row.document for row in self.rows)

    @property
    def expected(self) -> int:
        return sum(row.must_find for row in self.rows)

    @property
    def found(self) -> int:
        return sum(row.found for row in self.rows)

    @property
    def extra(self) -> int:
        return sum(row.extra for row in self.rows)

    @property
    def unclassified(self) -> tuple[str, ...]:
        return tuple(term for row in self.rows for term in row.unclassified)

    @property
    def missed(self) -> tuple[str, ...]:
        return tuple(term for row in self.rows for term in row.missed)

    @property
    def evaluated(self) -> bool:
        """A document was scored *and* it labelled something to find."""
        return bool(self.rows) and self.expected > 0

    @property
    def recall(self) -> float | None:
        """Found over expected, or ``None`` when L2 was not evaluated at all."""
        if not self.evaluated:
            return None
        return self.found / self.expected

    @property
    def metrics(self) -> dict:
        if not self.evaluated:
            return {}
        return {
            "recall": self.recall,
            "must_find": self.expected,
            "found": self.found,
            "false_positives_unclassified": len(self.unclassified),
        }

    @property
    def result(self) -> bool | None:
        """The gate (recall == 1.0) plus its second half: every false positive classified."""
        if not self.evaluated:
            return None
        return self.recall == 1.0 and not self.unclassified

    @property
    def note(self) -> str:
        """Why L2 stands where it does -- the exit criterion is reported, not implied."""
        if not self.label_sets:
            return (
                "not evaluated: no human must-find labels exist "
                "(docs/design/open-inputs.md section 3)"
            )
        if not self.rows:
            absent = ", ".join(name for name, _ in self.skipped) or "no document"
            return f"not evaluated: no labelled document is on disk ({absent})"
        if not self.expected:
            return "not evaluated: the label set labels no terms"
        if self.result:
            return (
                f"evaluated: recall {self.recall:.3f} over {self.expected} labelled terms in "
                f"{len(self.rows)} document(s), every extra hit classified"
            )
        return (
            f"failed: {len(self.missed)} missed, {len(self.unclassified)} unclassified "
            f"false positive(s)"
        )

    def table_rows(self) -> list[dict]:
        return [row.as_dict() for row in self.rows]

    def coverage(self) -> dict:
        return {
            "label_sets": list(self.label_sets),
            "term_lists": list(self.term_lists),
            "documents": len(self.rows),
            "expected_terms": self.expected,
            "found_terms": self.found,
            "extra_hits": self.extra,
            "skipped_documents": [name for name, _ in self.skipped],
            "evaluated": self.evaluated,
        }


def merge(reports: list[L2Report]) -> L2Report:
    """One report over several label sets: recall is over all terms, not an average of ratios."""
    sets: list[str] = []
    term_lists: list[str] = []
    for report in reports:
        sets.extend(name for name in report.label_sets if name not in sets)
        term_lists.extend(name for name in report.term_lists if name not in term_lists)
    return L2Report(
        label_sets=tuple(sets),
        term_lists=tuple(term_lists),
        rows=tuple(row for report in reports for row in report.rows),
        skipped=tuple(entry for report in reports for entry in report.skipped),
    )


def normalize(text: str) -> str:
    """How a hit's text is compared to a human's phrase: case-folded, whitespace collapsed.

    Nothing else: the phrase a human copied out of a document and the text the matcher read
    are the same characters, and a scorer lenient enough to ignore punctuation or extra
    words would pass a term list that finds the wrong thing.
    """
    return " ".join(text.split()).casefold()


def _require(data: dict, key: str) -> object:
    if key not in data:
        raise ValueError(f"must-find labels are missing {key!r}")
    return data[key]


def _unknown(data: dict, allowed: tuple[str, ...], what: str) -> None:
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise ValueError(f"unknown keys for {what}: {unknown}")


def _term(data: dict) -> MustFindTerm:
    _unknown(data, ("term", "note"), "a must_find entry")
    return MustFindTerm(term=str(_require(data, "term")), note=data.get("note"))


def _extra(data: dict) -> ExpectedExtra:
    _unknown(data, ("term", "classification", "note"), "an expected_extra entry")
    classification = str(_require(data, "classification"))
    if classification not in CLASSIFICATIONS:
        raise ValueError(
            f"unknown classification {classification!r}: expected one of {CLASSIFICATIONS}"
        )
    return ExpectedExtra(
        term=str(_require(data, "term")), classification=classification, note=data.get("note")
    )


def _document(name: str, data: dict) -> DocumentLabels:
    _unknown(data, ("source", "must_find", "expected_extra", "note"), f"document {name!r}")
    return DocumentLabels(
        document=name,
        source=str(data.get("source", "")),
        must_find=tuple(_term(entry) for entry in data.get("must_find", ())),
        expected_extra=tuple(_extra(entry) for entry in data.get("expected_extra", ())),
    )


def must_find_from_dict(data: dict, path: Path | None = None) -> MustFindSet:
    """One label set from its parsed JSON; an unknown key or a non-human provenance is an error."""
    provenance = str(_require(data, "labels_provenance"))
    if provenance != HUMAN:
        raise ValueError(
            f"must-find labels must be {HUMAN!r} (open-inputs section 3), not {provenance!r}"
        )
    _unknown(data, ("label_set", "labels_provenance", "term_list", "documents", "_comment"), "a must-find set")
    documents = _require(data, "documents")
    if not isinstance(documents, dict):
        raise ValueError("'documents' must map a document path to its labels")
    return MustFindSet(
        label_set=str(_require(data, "label_set")),
        labels_provenance=provenance,
        term_list=str(_require(data, "term_list")),
        documents=tuple(_document(name, entry) for name, entry in sorted(documents.items())),
        path=path,
    )


def load_must_find(path: str | Path) -> MustFindSet:
    path = Path(path)
    return must_find_from_dict(json.loads(path.read_text(encoding="utf-8")), path=path)


def iter_must_find(directory: str | Path) -> dict[str, MustFindSet]:
    """Every must-find label set in ``directory``, by set name, name-ordered.

    ``*.example.json`` is the format's documentation and is skipped by name: it carries
    invented phrases (a must-find list may only come from a real document), so scoring it
    would put a number on something no human ever labelled. The files other eval loaders
    own (:data:`OTHER_LOADERS` -- a query set, an L3 grades file) are skipped for the same
    reason in kind: they are not label sets, and parsing one as labels would fail on keys
    must-find does not know.
    """
    directory = Path(directory)
    found: dict[str, MustFindSet] = {}
    for path in sorted(directory.glob(f"*{MUST_FIND_SUFFIX}")):
        if path.name.endswith(EXAMPLE_SUFFIX) or path.name.startswith(OTHER_LOADERS):
            continue
        labels = load_must_find(path)
        if labels.label_set in found:
            raise ValueError(
                f"two must-find label sets named {labels.label_set!r}: "
                f"{found[labels.label_set].path} and {path}"
            )
        found[labels.label_set] = labels
    return found


def score(labels: MustFindSet, hits: Mapping[str, Iterable[str]]) -> L2Report:
    """Score ``labels`` against the text each hit matched, per document.

    ``hits`` maps a document path (as the labels name it) to the *texts* the run found
    there -- what the matcher matched, in output order. A document the labels name but
    ``hits`` does not is skipped on the record: a real document's absence is not a recall
    failure.
    """
    rows: list[L2Row] = []
    skipped: list[tuple[str, str]] = []
    for document in labels.documents:
        if document.document not in hits:
            skipped.append(
                (document.document, "the document is not on disk where the labels name it")
            )
            continue
        matched = [normalize(text) for text in hits[document.document]]
        wanted = [normalize(entry.term) for entry in document.must_find]
        claimed = set(wanted)
        missed = tuple(
            entry.term for entry, term in zip(document.must_find, wanted) if term not in matched
        )
        extras = sorted({text for text in matched if text not in claimed})
        classified = {normalize(entry.term) for entry in document.expected_extra}
        rows.append(
            L2Row(
                document=document.document,
                must_find=len(wanted),
                found=len(wanted) - len(missed),
                missed=missed,
                extra=len(extras),
                unclassified=tuple(text for text in extras if text not in classified),
            )
        )
    return L2Report(
        label_sets=(labels.label_set,),
        term_lists=(labels.term_list,),
        rows=tuple(rows),
        skipped=tuple(skipped),
    )
