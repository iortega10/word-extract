"""L3: summary faithfulness over a sampled rubric -- human-graded, never gated (Turn 7).

L1 and L2 measure extraction; this layer asks whether a stored summary still says what
its chunk says. A model cannot grade that honestly here (the label it would write is the
circularity L3 exists to see through), so grading is **human-only**: the harness selects
the sample, records each chunk's ``prompt_hash`` and ``model``, and lays a grades file's
answers over it. Nothing is invented: an ungraded chunk is ungraded, a grade written
under another prompt or model is *stale* and must be regraded, and a grade for a chunk
outside the sample is *unmatched* and waits for a sample that names it.

The committed ``l3_grades.example.json`` documents the rubric and the fields a grader
records; ``iter_grades`` skips every ``*.example.json`` by name, so with no real grades
the layer reports ``result: None`` -- and per the spec L3 is reported, never a gate:
summary faithfulness is judged, not asserted (docs/design/phase2-build-spec.md Turn 7).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
import json

#: The sample band the spec asks for (about 10-15 chunks), even over the pool in order.
SAMPLE_SIZE = 12

#: A grades file is one JSON file; ``*.example.json`` documents the format and is never scored.
GRADE_GLOB = "l3_grades*.json"
EXAMPLE_SUFFIX = ".example.json"

#: The only provenance grading may carry: a human grades the summary (open-inputs section 3).
HUMAN = "human"

#: The keys a grades file may carry, beyond ``_comment``.
_KEYS = ("gradeset", "labels_provenance", "rubric", "grades", "_comment")


def _require(data: Mapping, key: str, what: str):
    if key not in data:
        raise ValueError(f"{what} is missing {key!r}")
    return data[key]


def _unknown(data: Mapping, keys: Iterable[str], what: str) -> None:
    extra = sorted(set(data) - set(keys))
    if extra:
        raise ValueError(f"unknown keys {extra} in {what} (allowed: {sorted(keys)})")


@dataclass(frozen=True)
class Sample:
    """One summarized chunk the report may grade: its address and the pass that wrote it.

    Identity is ``(document, chunk_id)`` -- stable when the prompt moves -- while
    ``prompt_hash`` and ``model`` pin *what text* was graded: a grade recorded under
    another prompt or model no longer speaks about the sample the store shows.
    """

    document: str
    chunk_id: str
    prompt_hash: str
    model: str


@dataclass(frozen=True)
class Criterion:
    """One rubric criterion: the question, and the labels a grade may carry for it."""

    id: str
    question: str
    grades: tuple[str, ...]


@dataclass(frozen=True)
class Grade:
    """One grader's entry for one sampled chunk."""

    document: str
    chunk_id: str
    prompt_hash: str
    model: str
    criteria: dict[str, str]
    note: str | None = None


@dataclass(frozen=True)
class GradesSet:
    """A grades file: its rubric and every entry it carries."""

    gradeset: str
    labels_provenance: str
    rubric: tuple[Criterion, ...]
    grades: tuple[Grade, ...]
    path: Path | None = None


def criterion_from_dict(data: Mapping) -> Criterion:
    """One rubric criterion: ``id``, the ``question`` a grader answers, allowed ``grades``."""
    if not isinstance(data, Mapping):
        raise ValueError(f"a criterion must be an object, got {type(data).__name__}")
    _unknown(data, ("id", "question", "grades"), "a criterion")
    identifier = _require(data, "id", "a criterion")
    question = _require(data, "question", "a criterion")
    grades = _require(data, "grades", "a criterion")
    if not isinstance(identifier, str) or not identifier:
        raise ValueError(f"a criterion's id must be a non-empty string, got {identifier!r}")
    if not isinstance(question, str) or not question.strip():
        raise ValueError(f"criterion {identifier!r} needs a non-empty question")
    if not isinstance(grades, list) or not grades or not all(isinstance(g, str) and g for g in grades):
        raise ValueError(f"criterion {identifier!r} needs a non-empty list of grade labels")
    return Criterion(id=identifier, question=question, grades=tuple(grades))


def grade_from_dict(data: Mapping, rubric: Mapping[str, Criterion]) -> Grade:
    """One grade entry, validated against the rubric it grades under.

    An unknown criterion or a label outside the criterion's own grades is a format error
    (``ValueError``): a typo'd criterion must not become an ungraded chunk later.
    """
    if not isinstance(data, Mapping):
        raise ValueError(f"a grade must be an object, got {type(data).__name__}")
    _unknown(data, ("document", "chunk_id", "prompt_hash", "model", "criteria", "note"), "a grade")
    document = _require(data, "document", "a grade")
    chunk_id = _require(data, "chunk_id", "a grade")
    prompt_hash = _require(data, "prompt_hash", "a grade")
    model = _require(data, "model", "a grade")
    criteria = _require(data, "criteria", "a grade")
    for field_name, value in (("document", document), ("chunk_id", chunk_id), ("prompt_hash", prompt_hash), ("model", model)):
        if not isinstance(value, str) or not value:
            raise ValueError(f"a grade's {field_name} must be a non-empty string, got {value!r}")
    if not isinstance(criteria, Mapping):
        raise ValueError(f"a grade's criteria must be an object, got {type(criteria).__name__}")
    for identifier, label in criteria.items():
        if identifier not in rubric:
            raise ValueError(
                f"a grade names criterion {identifier!r}; the rubric has {[c.id for c in rubric.values()]}"
            )
        if label not in rubric[identifier].grades:
            raise ValueError(
                f"criterion {identifier!r} graded {label!r}; the rubric allows {list(rubric[identifier].grades)}"
            )
    note = data.get("note")
    if note is not None and not isinstance(note, str):
        raise ValueError(f"a grade's note must be a string, got {note!r}")
    return Grade(
        document=document,
        chunk_id=chunk_id,
        prompt_hash=prompt_hash,
        model=model,
        criteria=dict(criteria),
        note=note,
    )


def gradeset_from_dict(data: Mapping) -> GradesSet:
    """A whole grades file from its JSON object; every shape error is a ``ValueError``."""
    _unknown(data, _KEYS, "a grades file")
    gradeset = _require(data, "gradeset", "a grades file")
    provenance = _require(data, "labels_provenance", "a grades file")
    if provenance != HUMAN:
        raise ValueError(
            f"a grades file's labels_provenance must be {HUMAN!r}, got {provenance!r}: "
            "summary faithfulness is judged by a person, never by a model grading a model"
        )
    rubric_object = _require(data, "rubric", "a grades file")
    if not isinstance(rubric_object, Mapping):
        raise ValueError(f"a grades file's rubric must be an object, got {type(rubric_object).__name__}")
    _unknown(rubric_object, ("criteria",), "a rubric")
    criteria = _require(rubric_object, "criteria", "a rubric")
    if not isinstance(criteria, list):
        raise ValueError(f"a rubric's criteria must be a list, got {type(criteria).__name__}")
    parsed = tuple(criterion_from_dict(entry) for entry in criteria)
    if not parsed:
        raise ValueError("a rubric with no criterion grades nothing; name at least one")
    by_id: dict[str, Criterion] = {}
    for criterion in parsed:
        if criterion.id in by_id:
            raise ValueError(f"two rubric criteria are both named {criterion.id!r}")
        by_id[criterion.id] = criterion
    grades = _require(data, "grades", "a grades file")
    if not isinstance(grades, list):
        raise ValueError(f"a grades file's grades must be a list, got {type(grades).__name__}")
    entries = tuple(grade_from_dict(entry, by_id) for entry in grades)
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        if (entry.document, entry.chunk_id) in seen:
            raise ValueError(
                f"two grades are both for {entry.document}#{entry.chunk_id}; one chunk, one entry"
            )
        seen.add((entry.document, entry.chunk_id))
    return GradesSet(
        gradeset=gradeset,
        labels_provenance=provenance,
        rubric=parsed,
        grades=entries,
    )


def iter_grades(directory: str | Path) -> dict[str, GradesSet]:
    """Every ``l3_grades*.json`` in ``directory``, by name; ``*.example.json`` never scores.

    A directory without real grades yields ``{}``: with nothing to lay over the sample the
    report says *not graded*, and never that the summaries were faithful.
    """
    found: dict[str, GradesSet] = {}
    for path in sorted(Path(directory).glob(GRADE_GLOB)):
        if path.name.endswith(EXAMPLE_SUFFIX):
            continue
        gradeset = gradeset_from_dict(json.loads(path.read_text(encoding="utf-8")))
        if gradeset.gradeset in found:
            raise ValueError(
                f"two grades files are both named {gradeset.gradeset!r} "
                f"({found[gradeset.gradeset].path} and {path})"
            )
        found[gradeset.gradeset] = GradesSet(
            gradeset=gradeset.gradeset,
            labels_provenance=gradeset.labels_provenance,
            rubric=gradeset.rubric,
            grades=gradeset.grades,
            path=path,
        )
    return found


def select(samples: Sequence[Sample], *, n: int = SAMPLE_SIZE) -> tuple[Sample, ...]:
    """``n`` samples evenly spaced over the pool in its order, never any other ``n``.

    Deterministic over the order the pass produced the summaries in, so the same run
    selects the same chunks to grade tomorrow and a regrade is comparable. Fewer
    candidates than ``n`` grades every one rather than inventing slots.
    """
    pool = list(samples)
    if n <= 0 or not pool:
        return ()
    if len(pool) <= n:
        return tuple(pool)
    if n == 1:
        return (pool[0],)
    step = (len(pool) - 1) / (n - 1)
    indices = [int(round(i * step)) for i in range(n)]
    return tuple(pool[index] for index in indices)


@dataclass(frozen=True)
class L3Row:
    """One sampled chunk's grade state: graded, partial, stale or ungraded."""

    document: str
    chunk_id: str
    prompt_hash: str
    model: str
    status: str
    gradeset: str | None = None
    criteria: dict[str, str] = field(default_factory=dict)
    missing_criteria: tuple[str, ...] = ()
    recorded: dict | None = None

    def as_dict(self) -> dict:
        return {
            "document": self.document,
            "chunk_id": self.chunk_id,
            "prompt_hash": self.prompt_hash,
            "model": self.model,
            "status": self.status,
            "gradeset": self.gradeset,
            "criteria": dict(self.criteria),
            "missing_criteria": list(self.missing_criteria),
            "recorded": dict(self.recorded) if self.recorded else None,
        }


@dataclass(frozen=True)
class L3Report:
    """The sample, the grade states, and what could not be laid over it."""

    sample: tuple[Sample, ...] = ()
    gradesets: tuple[str, ...] = ()
    rubric: tuple[Criterion, ...] = ()
    rows: tuple[L3Row, ...] = ()
    unmatched: tuple[dict, ...] = ()
    shadowed: int = 0

    @property
    def graded(self) -> int:
        return sum(1 for row in self.rows if row.status == "graded")

    @property
    def partial(self) -> int:
        return sum(1 for row in self.rows if row.status == "partial")

    @property
    def stale(self) -> int:
        return sum(1 for row in self.rows if row.status == "stale")

    @property
    def ungraded(self) -> int:
        return sum(1 for row in self.rows if row.status == "ungraded")

    @property
    def evaluated(self) -> bool:
        """The rubric was applied to at least one sampled chunk under the current prompt."""
        return (self.graded + self.partial) > 0

    @property
    def result(self) -> bool | None:
        """Always None: L3 is reported, never gated (phase2-build-spec Turn 7)."""
        return None

    @property
    def metrics(self) -> dict:
        if not self.sample:
            return {}
        return {
            "samples": len(self.sample),
            "graded": self.graded,
            "partial": self.partial,
            "stale": self.stale,
            "ungraded": self.ungraded,
        }

    @property
    def note(self) -> str:
        if not self.sample:
            return "not graded: no summary was sampled (no fixture summary pass ran)"
        if not self.gradesets:
            return (
                "not graded: no human L3 grades exist "
                "(fixtures/evals/l3_grades.example.json documents the rubric)"
            )
        if not self.evaluated:
            return (
                f"not graded: the sample has no fresh grade "
                f"({self.stale} stale, {self.ungraded} ungraded)"
            )
        return (
            f"graded: {self.graded} of {len(self.sample)} sampled chunks complete; "
            f"{self.partial} partial, {self.stale} stale, {self.ungraded} ungraded"
        )

    def coverage(self) -> dict:
        return {
            "grade_sets": list(self.gradesets),
            "rubric": [criterion.id for criterion in self.rubric],
            "samples": len(self.sample),
            "graded": self.graded,
            "unmatched": len(self.unmatched),
            "shadowed": self.shadowed,
            "evaluated": self.evaluated,
        }


def build_report(
    candidates: Sequence[Sample],
    gradesets: Mapping[str, GradesSet],
    *,
    n: int = SAMPLE_SIZE,
) -> L3Report:
    """Lay the grades over the sample; every ungraded or stale slot stays on the record.

    The first name-ordered grades file's rubric is the report's (its criteria are the
    dimensions the sample is judged by); each entry is judged by the rubric of the file
    it came from. A grade whose ``prompt_hash`` or ``model`` differs from the sample's is
    *stale* -- the text it graded may no longer be what the store shows -- so it never
    counts as graded and the note asks for a regrade. Grades naming a chunk outside the
    sample are *unmatched*: the sample is what may be graded, and the rest wait.
    """
    sample = select(candidates, n=n)
    names = sorted(gradesets)
    entries: dict[tuple[str, str], tuple[str, Grade]] = {}
    shadowed = 0
    for name in names:
        for grade in gradesets[name].grades:
            key = (grade.document, grade.chunk_id)
            if key in entries:
                shadowed += 1
                continue
            entries[key] = (name, grade)
    rows: list[L3Row] = []
    for slot in sample:
        found = entries.get((slot.document, slot.chunk_id))
        if found is None:
            rows.append(
                L3Row(
                    document=slot.document,
                    chunk_id=slot.chunk_id,
                    prompt_hash=slot.prompt_hash,
                    model=slot.model,
                    status="ungraded",
                )
            )
            continue
        name, grade = found
        if grade.prompt_hash != slot.prompt_hash or grade.model != slot.model:
            rows.append(
                L3Row(
                    document=slot.document,
                    chunk_id=slot.chunk_id,
                    prompt_hash=slot.prompt_hash,
                    model=slot.model,
                    status="stale",
                    gradeset=name,
                    criteria=dict(grade.criteria),
                    recorded={"prompt_hash": grade.prompt_hash, "model": grade.model},
                )
            )
            continue
        rubric_ids = tuple(criterion.id for criterion in gradesets[name].rubric)
        missing = tuple(identifier for identifier in rubric_ids if identifier not in grade.criteria)
        rows.append(
            L3Row(
                document=slot.document,
                chunk_id=slot.chunk_id,
                prompt_hash=slot.prompt_hash,
                model=slot.model,
                status="partial" if missing else "graded",
                gradeset=name,
                criteria=dict(grade.criteria),
                missing_criteria=missing,
            )
        )
    sampled = {(slot.document, slot.chunk_id) for slot in sample}
    unmatched = tuple(
        {"gradeset": name, "document": document, "chunk_id": chunk_id}
        for (document, chunk_id), (name, _) in entries.items()
        if (document, chunk_id) not in sampled
    )
    governing = gradesets[names[0]].rubric if names else ()
    return L3Report(
        sample=sample,
        gradesets=tuple(names),
        rubric=governing,
        rows=tuple(rows),
        unmatched=unmatched,
        shadowed=shadowed,
    )
