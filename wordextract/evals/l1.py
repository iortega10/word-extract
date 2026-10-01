"""Turn 9: L1 -- the exact oracle. Every sidecar fact, checked against the real parser.

L1 says the parser *perceives* what the labels say is there. For every
``<name>.expected.json`` under ``fixtures/`` it re-parses the fixture with the real walker
and compares, fact by fact, everything the label asserts:

* **comments** -- how many, whose (author, initials), the identity (``para_id``), the body
  text, and the text each one is about: the anchor's union offsets, its literal
  ``anchor_text`` and its ancestor stack;
* **revisions** -- how many, and each one's kind, author, date, move group, and the union
  text it covers (the spans whose stack names it, nested content included);
* **sections** -- the outline in document order: title and level, flattened depth-first;
* **paragraphs** -- the 0a literals: the union text, the elementary-span tiling (relative
  offsets, stacks, literal text), and the accepted/original/superseded view strings;
* **tables** -- the shape: rows, columns, and the merged cells the row shapes imply.

Then **tiling**, which no sidecar can state: every union stream tiles its text exactly (no
gap, no overlap, no empty span), and every character of every part is either inside a node's
span or is a paragraph terminator. Those facts are what make a green L1 mean something -- an
empty paragraph the walker stopped emitting would otherwise change no labelled fact at all.

Two disciplines make the score trustworthy. A fact is compared **only where the sidecar
asserts it**: an absent family is not an assertion, but an empty family is (``"revisions":
[]`` claims there are none), and within a family a label that pins no stack, no date or no
identity is skipped *on the record* -- counted per family in the coverage report and
returned as a :class:`Skip`, never silently passed. And the score is
``exact_fact_accuracy``: the fraction of compared facts that matched, **0.0 when nothing was
compared**, so an empty corpus fails the gate instead of passing it vacuously.

Nothing here writes a label, and nothing here tolerates a disagreement: a mismatch is a
failure carrying what the label said and what the parser produced (design D11).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .. import opc
from ..model import NodeKind, ParseResult, Span, UnionStream, View
from ..store import body_part_id
from ..views import project
from ..walker import TERMINATOR, walk_document
from .labels import Sidecar, iter_sidecars

#: What each family is, in the order the table reports them.
FAMILIES = {
    "comments": "comment facts: count, author, initials, identity, body, anchor and its text",
    "revisions": "revision facts: count, kind, author, date, move group, union text covered",
    "sections": "the section outline in document order: title and level",
    "paragraphs": "the 0a literals: union text, elementary-span tiling, view strings",
    "tables": "table shapes: rows, columns, merged cells",
    "tiling": "the union tiles the text: nothing lost between the nodes, nothing overlapping",
}

#: Kinds whose node carries exactly one union span (2b's per-kind span rule).
_TEXT_KINDS = frozenset({NodeKind.HEADING, NodeKind.PARA, NodeKind.LIST_ITEM, NodeKind.CELL})


@dataclass(frozen=True)
class Fact:
    """One compared fact: what the label asserted, and what the parser produced."""

    family: str
    document: str
    what: str
    want: object
    got: object

    @property
    def ok(self) -> bool:
        return self.want == self.got

    @property
    def line(self) -> str:
        return f"{self.document}: {self.what}: label {self.want!r}, parser {self.got!r}"


@dataclass(frozen=True)
class Skip:
    """A fact the label does not assert, and therefore L1 does not compare."""

    family: str
    document: str
    what: str
    why: str

    @property
    def line(self) -> str:
        return f"{self.document}: {self.what}: not asserted ({self.why})"


@dataclass
class _Checks:
    """The facts and skips of one L1 run, in the order they were checked."""

    facts: list[Fact] = field(default_factory=list)
    skips: list[Skip] = field(default_factory=list)

    def check(self, family: str, document: str, what: str, want: object, got: object) -> None:
        self.facts.append(Fact(family, document, what, _plain(want), _plain(got)))

    def skip(self, family: str, document: str, what: str, why: str) -> None:
        self.skips.append(Skip(family, document, what, why))


def _plain(value: object) -> object:
    """A fact's two sides in a comparable, stable shape (stacks are ordered, so tuples)."""
    if isinstance(value, (list, tuple)):
        return tuple(_plain(item) for item in value)
    if isinstance(value, Span):
        return (value.part_id, value.start, value.end)
    return value


@dataclass(frozen=True)
class L1Report:
    """Every fact L1 compared, every fact it skipped, and what the corpus exercised."""

    facts: tuple[Fact, ...]
    skips: tuple[Skip, ...]
    tally: dict[str, int]

    @property
    def failures(self) -> tuple[Fact, ...]:
        return tuple(fact for fact in self.facts if not fact.ok)

    @property
    def compared(self) -> int:
        return len(self.facts)

    @property
    def exact_fact_accuracy(self) -> float:
        """Matched facts over compared facts -- ``0.0`` when there is nothing to compare."""
        if not self.facts:
            return 0.0
        return (len(self.facts) - len(self.failures)) / len(self.facts)

    @property
    def metrics(self) -> dict:
        return {
            "exact_fact_accuracy": self.exact_fact_accuracy,
            "compared": self.compared,
            "failed": len(self.failures),
        }

    @property
    def result(self) -> bool:
        """The gate: every compared fact matched, over a corpus that compared something."""
        return bool(self.facts) and not self.failures

    def _per_family(self) -> dict[str, list[int]]:
        counts = {family: [0, 0, 0] for family in FAMILIES}
        for fact in self.facts:
            counts[fact.family][0] += 1
            if not fact.ok:
                counts[fact.family][1] += 1
        for skip in self.skips:
            counts[skip.family][2] += 1
        return counts

    def rows(self, examples: int = 5) -> list[dict]:
        """One row per family: how much was compared, how much failed, and what failed."""
        per_family = self._per_family()
        rows = []
        for family, what in FAMILIES.items():
            compared, failed, skipped = per_family[family]
            rows.append(
                {
                    "layer": "L1",
                    "family": family,
                    "what": what,
                    "compared": compared,
                    "skipped": skipped,
                    "failed": failed,
                    "accuracy": (compared - failed) / compared if compared else 0.0,
                    "result": bool(compared) and not failed,
                    "examples": [fact.line for fact in self.failures if fact.family == family][
                        :examples
                    ],
                }
            )
        return rows

    def coverage(self) -> dict:
        """What the corpus exercised -- the evidence that a green L1 is not vacuous."""
        per_family = self._per_family()
        return {
            **self.tally,
            "families": {
                family: {"compared": row[0], "failed": row[1], "skipped": row[2]}
                for family, row in per_family.items()
            },
            "vacuous": not self.facts,
        }

    def example_failures(self, limit: int = 20) -> list[str]:
        return [fact.line for fact in self.failures[:limit]]


def _revision_text(parsed: ParseResult, revision_id: str) -> str:
    """The union text ``revision_id`` covers: every elementary span whose stack names it.

    The union of the spans, so where one revision nests inside another both cover the inner
    text -- what "the text of this deletion" means from either one's point of view. No label
    currently pins a nested case; the flat ones are the ones asserted.
    """
    return "".join(
        stream.text[span.start : span.end]
        for stream in parsed.union_streams
        for span in stream.spans
        if revision_id in span.stack
    )


def _paragraph_slices(stream: UnionStream, terminator: str) -> list[tuple[int, int]]:
    """The union range of every paragraph in ``stream``, in order, terminator excluded.

    A paragraph terminator is a span of one terminator character with an empty stack
    (``UnionStream``'s own rule), so the paragraphs are what lies between two of them. The
    terminator is not part of a paragraph's union text: the 0a labels start a paragraph's
    offsets at its first content character.
    """
    slices: list[tuple[int, int]] = []
    start = 0
    for span in stream.spans:
        if span.stack or stream.text[span.start : span.end] != terminator:
            continue
        slices.append((start, span.start))
        start = span.end
    if start != len(stream.text):
        slices.append((start, len(stream.text)))
    return slices


def _spans_in(stream: UnionStream, start: int, end: int) -> tuple[tuple[int, int, tuple, str], ...]:
    """The stream's elementary spans inside ``[start, end)``, offsets rebased to ``start``."""
    return tuple(
        (
            span.start - start,
            span.end - start,
            tuple(span.stack),
            stream.text[span.start : span.end],
        )
        for span in stream.spans
        if span.start >= start and span.end <= end
    )


def _table_shape(rows: list[int]) -> tuple[tuple[tuple[int, int, int, int], ...], int] | None:
    """The merges a table's row widths imply, and its width -- or ``None`` when unreadable.

    The walker reads cell *elements*, not ``w:tblGrid``, so a row holding fewer cells than
    the table's widest row states that one of its cells spans: a row of one cell, in a table
    wider than one, is one cell spanning the whole width -- the shape the fixtures use. A
    short row of two or more cells is a shape the cells alone cannot state (which cell spans
    how far is not in them), so a label that asserts merges for such a table is a fact L1
    reports as unreadable rather than guessing at it.
    """
    width = max(rows, default=0)
    merges: list[tuple[int, int, int, int]] = []
    for index, cells in enumerate(rows):
        if cells == width:
            continue
        if cells == 1 and width > 1:
            merges.append((index, 0, index, width - 1))
            continue
        return None
    return tuple(merges), width


def _comment_facts(checks: _Checks, sidecar: Sidecar, parsed: ParseResult, document: str) -> None:
    if "comments" not in sidecar.families:
        return
    labels = sidecar.comments
    checks.check("comments", document, "comment count", len(labels), len(parsed.comments))
    for index, label in enumerate(labels):
        what = f"comments[{index}] (w:id={label.id!r})"
        if index >= len(parsed.comments):
            checks.skip("comments", document, what, "the parser produced no comment at this index")
            continue
        record = parsed.comments[index]
        checks.check("comments", document, f"{what}.author", label.author, record.author)
        checks.check("comments", document, f"{what}.initials", label.initials, record.initials)
        checks.check(
            "comments", document, f"{what}.anchor_text", label.anchor_text, record.anchor_text
        )
        if label.text is not None:
            checks.check("comments", document, f"{what}.text", label.text, record.text)
        else:
            checks.skip("comments", document, f"{what}.text", "the sidecar labels no body text")
        if label.para_id is not None:
            checks.check("comments", document, f"{what}.para_id", label.para_id, record.para_id)
        else:
            checks.skip(
                "comments",
                document,
                f"{what}.para_id",
                "the sidecar asserts no identity: the comment's own paragraphs carry no w14:paraId",
            )
        if label.anchor is not None:
            checks.check(
                "comments",
                document,
                f"{what}.anchor union offsets",
                (label.anchor.start, label.anchor.end),
                None if record.anchor is None else (record.anchor.start, record.anchor.end),
            )
            if label.anchor.stack:
                checks.check(
                    "comments",
                    document,
                    f"{what}.anchor stack",
                    tuple(label.anchor.stack),
                    None if record.anchor is None else tuple(record.anchor.stack),
                )
            else:
                checks.skip(
                    "comments", document, f"{what}.anchor stack", "the sidecar labels no stack"
                )
        elif label.anchor_text == "":
            checks.check(
                "comments", document, f"{what}.anchor is unanchored", None, record.anchor
            )
        else:
            checks.skip(
                "comments",
                document,
                f"{what}.anchor union offsets",
                "the sidecar pins the anchored text, not the offsets it sits at",
            )
        for name, want in (
            ("parent_id", label.parent_id),
            ("resolved", label.resolved),
            ("threading_status", label.threading_status),
        ):
            if want is None:
                checks.skip(
                    "comments",
                    document,
                    f"{what}.{name}",
                    "the sidecar states nothing here (a JSON null reads the same as absent)",
                )
                continue
            got = getattr(record, name)
            checks.check("comments", document, f"{what}.{name}", want, getattr(got, "value", got))


def _revision_facts(checks: _Checks, sidecar: Sidecar, parsed: ParseResult, document: str) -> None:
    asserted = "revisions" in sidecar.families
    labels = sidecar.revisions
    if asserted:
        checks.check("revisions", document, "revision count", len(labels), len(parsed.revisions))
    claimed: list[str] = []
    for index, label in enumerate(labels):
        what = f"revisions[{index}]"
        if label.id is not None:
            found = [r for r in parsed.revisions if r.id == label.id]
            checks.check(
                "revisions", document, f"{what}: one revision with id {label.id!r}", 1, len(found)
            )
        else:
            # No id in the label: revisions are matched to labels of the same kind in
            # document order, which is how a generator sidecar states them.
            same_kind = [r for r in parsed.revisions if r.kind.value == label.kind]
            rank = sum(
                1
                for earlier in labels[:index]
                if earlier.id is None and earlier.kind == label.kind
            )
            found = same_kind[rank : rank + 1]
            checks.check(
                "revisions",
                document,
                f"{what}: a {label.kind!r} revision (its {rank + 1}.)",
                1,
                len(found),
            )
        if len(found) != 1:
            continue
        revision = found[0]
        claimed.append(revision.id)
        checks.check("revisions", document, f"{what}.kind", label.kind, revision.kind.value)
        checks.check("revisions", document, f"{what}.author", label.author, revision.author)
        checks.check(
            "revisions",
            document,
            f"{what}.move_group_id",
            label.move_group_id,
            revision.move_group_id,
        )
        if label.date is not None:
            checks.check("revisions", document, f"{what}.date", label.date, revision.date)
        else:
            checks.skip("revisions", document, f"{what}.date", "the sidecar pins no date")
        if label.text is not None:
            checks.check(
                "revisions",
                document,
                f"{what}.union text covered",
                label.text,
                _revision_text(parsed, revision.id),
            )
        else:
            checks.skip(
                "revisions", document, f"{what}.union text covered", "the sidecar pins no text"
            )
    if asserted:
        checks.check(
            "revisions",
            document,
            "every parsed revision is one a label names",
            (),
            tuple(r.id for r in parsed.revisions if r.id not in claimed),
        )


def _section_facts(checks: _Checks, sidecar: Sidecar, parsed: ParseResult, document: str) -> None:
    if "sections" not in sidecar.families:
        return
    labels = sidecar.sections
    checks.check(
        "sections",
        document,
        "the labels are in document order",
        tuple(range(len(labels))),
        tuple(label.order for label in labels),
    )
    outline: list[tuple[str, int]] = []

    def visit(section) -> None:
        outline.append((section.title, section.level))
        for child in section.children:
            visit(child)

    for section in parsed.sections:
        visit(section)
    checks.check("sections", document, "section count", len(labels), len(outline))
    for index, label in enumerate(labels):
        if index >= len(outline):
            checks.skip(
                "sections", document, f"sections[{index}]", "the parser produced no section here"
            )
            continue
        title, level = outline[index]
        checks.check("sections", document, f"sections[{index}].title", label.text, title)
        checks.check("sections", document, f"sections[{index}].level", label.level, level)


def _paragraph_facts(checks: _Checks, sidecar: Sidecar, parsed: ParseResult, document: str) -> None:
    if "paragraphs" not in sidecar.families:
        return
    body = body_part_id(parsed)
    stream = next((s for s in parsed.union_streams if s.part_id == body), None)
    if stream is None:
        checks.skip(
            "paragraphs", document, "the paragraph tiling", "the document has no body stream"
        )
        return
    terminator = sidecar.terminator or TERMINATOR
    slices = _paragraph_slices(stream, terminator)
    labels = sidecar.paragraphs
    checks.check("paragraphs", document, "labelled paragraph count", len(labels), len(slices))
    for index, label in enumerate(labels):
        what = f"paragraphs[{index}]"
        if index >= len(slices):
            checks.skip("paragraphs", document, what, "the union has no paragraph at this index")
            continue
        start, end = slices[index]
        checks.check("paragraphs", document, f"{what}.index", label.index, index)
        checks.check("paragraphs", document, f"{what}.union text", label.union, stream.text[start:end])
        checks.check(
            "paragraphs",
            document,
            f"{what}.elementary spans (offsets, stacks, text)",
            tuple((span.start, span.end, tuple(span.stack), span.text) for span in label.spans),
            _spans_in(stream, start, end),
        )
        for view in View:
            checks.check(
                "paragraphs",
                document,
                f"{what}.{view.value} view text",
                getattr(label, view.value),
                project(stream, view, start, end).text,
            )


def _table_facts(checks: _Checks, sidecar: Sidecar, parsed: ParseResult, document: str) -> None:
    if "tables" not in sidecar.families:
        return
    by_id = {node.id: node for node in parsed.nodes}
    tables = [node for node in parsed.nodes if node.kind is NodeKind.TABLE]
    checks.check("tables", document, "table count", len(sidecar.tables), len(tables))
    for index, label in enumerate(sidecar.tables):
        what = f"tables[{index}]"
        if index >= len(tables):
            checks.skip("tables", document, what, "the parser produced no table at this index")
            continue
        table = tables[index]
        checks.check(
            "tables",
            document,
            f"{what}.rows are nodes",
            (),
            tuple(child for child in table.child_ids if child not in by_id),
        )
        widths = [len(by_id[child].child_ids) for child in table.child_ids if child in by_id]
        checks.check("tables", document, f"{what}.rows", label.rows, len(widths))
        shape = _table_shape(widths)
        if shape is None:
            checks.skip(
                "tables",
                document,
                f"{what}.columns and merges",
                "a short row of several cells does not say which cell spans how far",
            )
            continue
        merges, width = shape
        checks.check("tables", document, f"{what}.columns (widest row)", label.columns, width)
        checks.check("tables", document, f"{what}.merges (from the row shapes)", label.merges, merges)


def _tile_defect(spans, length: int) -> str | None:
    """The first way ``spans`` fails to tile ``[0, length)``: a gap, an overlap, an empty span."""
    cursor = 0
    for index, span in enumerate(spans):
        if span.start != cursor:
            return f"span {index} starts at {span.start}, not {cursor} ({span.start - cursor:+d})"
        if span.end <= span.start:
            return f"span {index} is empty at {span.start}"
        cursor = span.end
    if cursor != length:
        return f"the spans stop at {cursor} of {length}"
    return None


def _tiling_facts(checks: _Checks, parsed: ParseResult, document: str, tally: Counter) -> None:
    """The union tiles the text, and every last character is accounted for.

    Per part: the elementary spans tile ``[0, len(text))`` exactly, and every character is
    either inside a node's span or a paragraph terminator -- so a paragraph the walker
    dropped, or a node whose span drifted, shows up here even where no sidecar labels it.
    """
    tally["streams"] += len(parsed.union_streams)
    tally["nodes"] += len(parsed.nodes)
    tally["union_characters"] += sum(len(stream.text) for stream in parsed.union_streams)
    by_part: dict[str, list[tuple[int, int]]] = {stream.part_id: [] for stream in parsed.union_streams}
    for node in parsed.nodes:
        for span in node.spans:
            if span.part_id in by_part:
                by_part[span.part_id].append((span.start, span.end))
    for stream in parsed.union_streams:
        checks.check(
            "tiling",
            document,
            f"{stream.part_id}: the elementary spans tile [0, {len(stream.text)})",
            None,
            _tile_defect(stream.spans, len(stream.text)),
        )
        occupied: set[int] = set()
        for start, end in by_part[stream.part_id]:
            occupied.update(range(start, end))
        tally["characters_in_a_node"] += len(occupied)
        tally["characters_outside_a_node"] += len(stream.text) - len(occupied)
        unaccounted = [
            offset
            for offset in range(len(stream.text))
            if offset not in occupied and stream.text[offset] != TERMINATOR
        ]
        checks.check(
            "tiling",
            document,
            f"{stream.part_id}: every character is in a node span or is a terminator",
            None,
            f"not accounted for at {unaccounted[:5]} of {len(stream.text)}" if unaccounted else None,
        )
    for node in parsed.nodes:
        if node.kind not in _TEXT_KINDS:
            continue
        checks.check(
            "tiling",
            document,
            f"{node.id} ({node.kind.value}): exactly one union span",
            None,
            None if len(node.spans) == 1 else f"{len(node.spans)} spans",
        )


def score_sidecar(checks: _Checks, sidecar: Sidecar, parsed: ParseResult, document: str) -> None:
    """Every fact ``sidecar`` asserts about ``parsed``, in family order."""
    _comment_facts(checks, sidecar, parsed, document)
    _revision_facts(checks, sidecar, parsed, document)
    _section_facts(checks, sidecar, parsed, document)
    _paragraph_facts(checks, sidecar, parsed, document)
    _table_facts(checks, sidecar, parsed, document)


def score_fixtures(fixtures_dir: str | Path, *, only: str | None = None) -> L1Report:
    """Score every sidecar under ``fixtures_dir`` against a fresh parse of its fixture.

    The fixture is re-parsed from its own ``.docx``, not read out of any store, because L1 is
    a claim about the parser: a store could hold a stale parse and still agree with itself.
    """
    fixtures_dir = Path(fixtures_dir)
    checks = _Checks()
    tally: Counter = Counter()
    for name, sidecar in sorted(iter_sidecars(fixtures_dir).items()):
        if only is not None and name != only:
            continue
        if sidecar.path is None:
            continue
        fixture = sidecar.path.parent / sidecar.fixture
        parsed = walk_document(opc.Package(fixture))
        tally["sidecars"] += 1
        tally["comments"] += len(parsed.comments)
        tally["anchored_comments"] += sum(1 for c in parsed.comments if c.anchor is not None)
        tally["revisions"] += len(parsed.revisions)
        tally["sections"] += _section_count(parsed)
        tally["tables"] += sum(1 for n in parsed.nodes if n.kind is NodeKind.TABLE)
        tally["known_gaps"] += len(parsed.known_gaps)
        score_sidecar(checks, sidecar, parsed, name)
        _tiling_facts(checks, parsed, name, tally)
    return L1Report(facts=tuple(checks.facts), skips=tuple(checks.skips), tally=dict(tally))


def _section_count(parsed: ParseResult) -> int:
    count = 0
    stack = list(parsed.sections)
    while stack:
        section = stack.pop()
        count += 1
        stack.extend(section.children)
    return count
