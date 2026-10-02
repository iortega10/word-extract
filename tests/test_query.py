"""Turn 5: the query layer -- eight ways to read what a run stored, and nothing else.

The claims here are about *reading*, not about the parser, chunker or matcher, which have
their own tests:

* **pure over the store.** Each function answers from the catalog and the artifacts a run
  wrote, so the same call on a read-only store returns equal dataclasses, touches no byte
  and no mtime, and answers the same way twice (the results are dataclasses with a
  ``Citation``, so equality *is* the determinism check).
* **unknown stays unknown.** ``summary``/``has_pending`` are None beside a roll-up no run
  wrote -- never False, which would read as "nothing pending"; an unanchored comment keeps
  ``node_id=None`` with empty spans and section (); a paragraph-mark revision nothing is
  stacked on keeps its document's ``unattributed_revisions=1`` while ``has_pending`` is
  False, because Turn 2's catalog count and manifest flag answer two different questions.
* **lookups raise rather than pick.** An ambiguous chunk id, an unknown view, document or
  tier, a compare of one document with itself or of two documents sharing no stored view,
  and a store holding several term lists with none asked for are errors that name the
  state (rule 10).
* **filters are honest.** ``section`` is a full outermost-first title path, so a bare leaf
  title matching nothing is the prefix rule working rather than a dropped row; ``author``
  is exact equality; ``search`` echoes the term list, scope, sources and views it ran
  under, so the answer states the question it answered.

Every summary comes from :class:`~wordextract.llm.CannedClient`, so nothing here touches a
network. ``local-private/review_sample.docx`` is machine-local and git-ignored: the test
that reads it skips when the file is absent rather than failing a fresh clone.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from wordextract.llm import CannedClient
from wordextract.model import MatchType, RevisionKind, ThreadingStatus, View
from wordextract.pipeline import load_terms, run, stored_chunks
from wordextract.query import (
    compare,
    find_terms,
    get_chunk,
    get_comments,
    get_outline,
    get_revisions,
    list_documents,
    search,
)
from wordextract.rank import SOURCES
from wordextract.store import Store
from wordextract.summarize import summarize
from wordextract.terms import term_list_hash

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
REGISTRY = FIXTURES / "terms" / "synthetic.example.json"
#: Machine-local and git-ignored: real input that must never be committed (see CLAUDE.md).
LOCAL_SAMPLE = ROOT / "local-private" / "review_sample.docx"

V2 = "program_review_v2.docx"
V3 = "program_review_v3.docx"

#: One shared store's documents: the v2/v3 pair, the hits fixture, and the fixtures whose
#: threading, revision and gap unknowns must survive the read.
DOCS = (
    FIXTURES / "ledger_summary.docx",
    FIXTURES / V2,
    FIXTURES / V3,
    FIXTURES / "spec_threaded.docx",
    FIXTURES / "model" / "threading_edge_cases.docx",
    FIXTURES / "model" / "unanchored_comment.docx",
    FIXTURES / "model" / "deleted_paragraph_mark.docx",
    FIXTURES / "model" / "unrecorded_revision_kinds.docx",
)

#: What a bare CannedClient answers, and therefore what every roll-up says.
CANNED_TEXT = "(canned client: no model was asked)"

#: Comments each document holds: parse records, hand-checked in the fixtures' sidecars.
COMMENT_COUNTS = {
    "ledger_summary.docx": 1,
    "deleted_paragraph_mark.docx": 0,
    V2: 5,
    V3: 5,
    "spec_threaded.docx": 2,
    "threading_edge_cases.docx": 3,
    "unanchored_comment.docx": 3,
    "unrecorded_revision_kinds.docx": 0,
}


@pytest.fixture(scope="module")
def stored(tmp_path_factory) -> SimpleNamespace:
    """One shared store: every document above ingested and rolled up, canned."""
    root = tmp_path_factory.mktemp("querystore")
    records = {}
    for path in DOCS:
        record = run(path, REGISTRY, store_root=root, run_id=path.stem)
        summarize(Store(root), record, client=CannedClient(), model="canned")
        records[path.name] = record
    return SimpleNamespace(root=root, records=records)


def _by_title(rows) -> dict:
    return {row.title: row for row in rows}


def _sections(outline) -> list:
    """Every section of an outline in preorder, children after their parent."""

    def walk(items):
        for section in items:
            yield section
            yield from walk(section.children)

    return list(walk(outline.sections))


def _snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    """Every file under ``root`` with its bytes and mtime -- what a read must not touch."""
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _hit_chunks(store: Store, title: str) -> set[str]:
    """The chunk ids ``find_terms`` places the termination group's hits in for one title."""
    result = find_terms(store, "termination", doc=title)
    assert result.resolved_group == "termination"
    assert len(result.documents) == 1
    return {
        hit.chunk_id
        for section in result.documents[0].sections
        for hit in section.hits
        if hit.chunk_id is not None
    }


def _exercise(store) -> tuple:
    """All eight queries in call order: one comparable answer, no side effects."""
    hits = find_terms(store, "termination", doc=V3)
    chunk_id = next(
        hit.chunk_id
        for section in hits.documents[0].sections
        for hit in section.hits
        if hit.chunk_id is not None
    )
    return (
        tuple(list_documents(store)),
        get_outline(store, V3),
        get_chunk(store, chunk_id, "accepted", doc=V3),
        tuple(get_comments(store)),
        get_revisions(store),
        hits,
        search(store, "termination"),
        compare(store, V2, V3),
    )


# --- list_documents / get_outline ------------------------------------------------------


def test_the_catalog_row_is_the_stored_counts_with_the_document_roll_up(stored):
    store = Store(stored.root)
    rows = list_documents(store)
    assert [row.title for row in rows] == [path.name for path in sorted(DOCS, key=lambda p: p.name)]
    assert {row.views for row in rows} == {("accepted",)}
    by_title = _by_title(rows)

    v3 = by_title[V3]
    assert (v3.comment_count, v3.unattributed_revisions, v3.unattributed_gaps) == (5, 0, [])
    assert v3.summary == CANNED_TEXT and v3.has_pending is True
    assert by_title["spec_threaded.docx"].comment_count == 2
    assert by_title["spec_threaded.docx"].has_pending is True

    #: Two different questions, both answered: the paragraph-mark revision no chunk
    #: stacks under is the catalog's unattributed count, while no chunk carries a
    #: revision entry, so the manifest flag is False rather than True.
    deleted = by_title["deleted_paragraph_mark.docx"]
    assert deleted.comment_count == 0
    assert deleted.unattributed_revisions == 1
    assert deleted.unattributed_gaps == ["paragraph_mark_revision"]
    assert deleted.has_pending is False
    assert deleted.summary == CANNED_TEXT

    unknown = by_title["unrecorded_revision_kinds.docx"]
    assert unknown.unattributed_revisions == 0
    assert unknown.unattributed_gaps == ["unrecorded_revision_kind"]
    assert unknown.has_pending is False


def test_a_rollup_no_run_wrote_reads_as_unknown_rather_than_as_nothing(tmp_path):
    root = tmp_path / "bare"
    run(FIXTURES / V3, REGISTRY, store_root=root, run_id="ingest-only")
    store = Store(root)
    rows = list_documents(store)
    assert [row.summary for row in rows] == [None]
    assert [row.has_pending for row in rows] == [None]

    outline = get_outline(store, V3)
    assert outline.summary is None and outline.has_pending is None
    sections = _sections(outline)
    assert sections
    assert [(s.summary, s.has_pending) for s in sections] == [(None, None)] * len(sections)


def test_the_outline_names_sections_as_the_accepted_view_does_with_their_rollups(stored):
    store = Store(stored.root)
    outline = get_outline(store, V3)
    assert outline.title == V3 and outline.view is View.ACCEPTED
    assert outline.summary == CANNED_TEXT and outline.has_pending is True

    review = next(s for s in outline.sections if s.title.startswith("Program Review"))
    assert [child.title for child in review.children] == [
        "1. Coverage Terms",
        "2. Exclusions",
        "3. Fee and Rate Schedule",
        "4. Notes",
    ]
    assert {child.title: child.has_pending for child in review.children} == {
        "1. Coverage Terms": False,
        "2. Exclusions": True,
        "3. Fee and Rate Schedule": False,
        "4. Notes": False,
    }
    assert all(child.summary == CANNED_TEXT for child in review.children)

    threaded = get_outline(store, "spec_threaded.docx")
    assert [s.title for s in threaded.sections] == ["Spec-Annotated Threaded Comments"]
    assert [child.title for child in threaded.sections[0].children] == ["Threaded Review"]


# --- get_chunk -------------------------------------------------------------------------


def test_a_chunk_row_carries_both_renderings_and_the_comments_markup_cites(stored):
    store = Store(stored.root)
    chunks = stored_chunks(stored.records[V3], store_root=stored.root)

    first = chunks[0]
    row = get_chunk(store, first.id, "accepted", doc=V3)
    assert row.document_id == _by_title(list_documents(store))[V3].document_id
    assert row.chunk_id == first.id and row.view is View.ACCEPTED
    assert row.section_path == ("Program Review: Consultants Service Agreement", "1. Coverage Terms")
    assert row.node_ids == tuple(first.node_ids)
    assert row.text.startswith(
        "The permit provides a Limit of Liability of $1,000,000 per engagement and $2,000"
    )
    assert len(row.comments) == 4
    #: The row's comment set and the markup's context lines are one rule (the chunker's),
    #: so the summarizer can never cite a comment the row does not list.
    assert row.markup.count("[[comment by=") == 4
    assert row.pending_changes == () and "[[revision id=" not in row.markup
    assert row.citation.chunk_id == first.id
    assert row.citation.view is View.ACCEPTED and row.citation.spans

    #: The chunk the change lives in, found by its markup rather than by assumption: the
    #: section's chunks split, and only one carries the revision stack.
    row = next(
        chunk_row
        for chunk_row in (
            get_chunk(store, chunk.id, "accepted", doc=V3) for chunk in chunks
        )
        if "[[del" in chunk_row.markup
    )
    assert row.section_path[-1] == "2. Exclusions"
    assert "excluded except for supply-delay release" in row.text
    assert "excluded in all cases" not in row.text
    assert "[[del" not in row.text and "[[ins" not in row.text
    #: The union is the summarizer's input: both sides of the change stay visible there
    #: while the accepted view shows only what survived it.
    assert "[[del" in row.markup and "excluded in all cases" in row.markup
    assert "[[ins" in row.markup
    assert len(row.pending_changes) == 2
    assert row.markup.count("[[revision id=") == 2


def test_lookups_that_could_pick_raise_naming_the_state(stored):
    store = Store(stored.root)
    shared = sorted(_hit_chunks(store, V3))[0]
    with pytest.raises(LookupError, match="several documents"):
        get_chunk(store, shared, "accepted")
    assert get_chunk(store, shared, "accepted", doc=V3).document_id
    assert get_chunk(store, shared, "accepted", doc=V2).document_id

    with pytest.raises(LookupError, match="no chunk"):
        get_chunk(store, "not-a-chunk", "accepted", doc=V3)
    with pytest.raises(LookupError, match="unknown view"):
        get_chunk(store, shared, "bogus-view", doc=V3)

    with pytest.raises(LookupError):
        get_outline(store, "nope.docx")
    with pytest.raises(LookupError):
        get_comments(store, "nope.docx")
    with pytest.raises(LookupError):
        get_revisions(store, "nope.docx")
    with pytest.raises(LookupError):
        search(store, "termination", scope="nope.docx")
    with pytest.raises(LookupError):
        compare(store, V3, "nope.docx")


# --- get_comments ----------------------------------------------------------------------


def test_comments_keep_the_threading_and_resolution_as_recorded(stored):
    store = Store(stored.root)
    rows = get_comments(store)
    assert Counter(row.title for row in rows) == Counter(COMMENT_COUNTS)

    spec = [row for row in rows if row.title == "spec_threaded.docx"]
    assert [row.comment_id for row in spec] == ["comment:00000001", "comment:00000002"]
    assert [row.author for row in spec] == ["D. Okafor", "S. Ruiz"]
    assert [row.parent_id for row in spec] == [None, "00000001"]
    assert [row.threading_status for row in spec] == [ThreadingStatus.VERIFIED] * 2
    assert [row.resolved for row in spec] == [False, True]
    assert all(
        row.section_path == ("Spec-Annotated Threaded Comments", "Threaded Review")
        for row in spec
    )

    threading = [row for row in rows if row.title == "threading_edge_cases.docx"]
    assert [row.comment_id for row in threading] == [
        "comment:00000031",
        "comment:00000032",
        "comment:00000033",
    ]
    assert [row.resolved for row in threading] == [True, None, False]
    assert [row.parent_id for row in threading] == [None, "00000031", "0000DEAD"]


def test_an_unanchored_comment_keeps_no_node_no_spans_and_no_section(stored):
    store = Store(stored.root)
    rows = get_comments(store, "unanchored_comment.docx")
    assert [row.comment_id for row in rows] == [
        "comment:5000001A",
        "comment:5000002A",
        "comment:5000003A",
    ]
    assert [row.text for row in rows] == [
        "Only this one is anchored.",
        "Named but never bracketed.",
        "Bracketed but never closed.",
    ]
    assert {row.threading_status for row in rows} == {ThreadingStatus.ABSENT}
    assert all(row.resolved is None for row in rows)
    assert all(row.section_path == () for row in rows)

    anchored, named, unclosed = rows
    assert anchored.citation.node_id is not None and anchored.citation.spans
    for row in (named, unclosed):
        assert row.citation.node_id is None
        assert row.citation.spans == ()
    #: A named section excludes all three honestly: none has a node holding its anchor.
    assert get_comments(store, "unanchored_comment.docx", section=("Anywhere",)) == []


def test_comment_filters_are_exact_author_and_full_section_paths(stored):
    store = Store(stored.root)
    okafor = get_comments(store, author="D. Okafor")
    assert [(row.title, row.comment_id) for row in okafor] == [
        ("spec_threaded.docx", "comment:00000001")
    ]
    assert get_comments(store, V3, author="D. Okafor") == []

    full = get_comments(
        store,
        V3,
        section=("Program Review: Consultants Service Agreement", "2. Exclusions"),
    )
    assert len(full) == 1
    assert full[0].author == "M. Chen"
    assert full[0].comment_id.startswith("comment:hash:")
    #: The prefix rule is outermost-first: a bare leaf title names no path from the root.
    assert get_comments(store, V3, section=("2. Exclusions",)) == []


# --- get_revisions ---------------------------------------------------------------------


def test_revisions_carry_their_text_sections_and_document_counts(stored):
    store = Store(stored.root)
    result = get_revisions(store)
    assert result.doc is None
    by_title = {doc.title: doc for doc in result.documents}
    assert [doc.title for doc in result.documents] == [
        path.name for path in sorted(DOCS, key=lambda p: p.name)
    ]

    v3 = by_title[V3]
    assert [row.revision_id for row in v3.revisions] == ["del:900", "ins:901"]
    deleted, inserted = v3.revisions
    assert deleted.kind is RevisionKind.DEL and deleted.author == "R. Alvarez"
    assert deleted.text == "excluded in all cases"
    assert inserted.kind is RevisionKind.INS
    assert inserted.text == "excluded except for supply-delay release"
    section = ("Program Review: Consultants Service Agreement", "2. Exclusions")
    assert deleted.section_paths == (section,) and inserted.section_paths == (section,)
    assert deleted.spans and deleted.citation.node_id
    assert (v3.unattributed_revisions, v3.unattributed_gaps) == (0, [])

    spec = by_title["spec_threaded.docx"]
    assert [row.text for row in spec.revisions] == ["three years", "thirty-six months"]
    assert [row.kind for row in spec.revisions] == [RevisionKind.DEL, RevisionKind.INS]
    assert all(
        row.section_paths == (("Spec-Annotated Threaded Comments", "Threaded Review"),)
        for row in spec.revisions
    )

    #: A paragraph-mark revision nothing is stacked on: empty text, no spans, no section,
    #: and the document's unattributed count says so rather than the row vanishing.
    mark_doc = by_title["deleted_paragraph_mark.docx"]
    (mark,) = mark_doc.revisions
    assert mark.revision_id == "del:9" and mark.kind is RevisionKind.DEL
    assert mark.author == "A. Ito"
    assert mark.text == "" and mark.spans == () and mark.section_paths == ()
    assert mark_doc.unattributed_revisions == 1
    assert mark_doc.unattributed_gaps == ["paragraph_mark_revision"]

    #: A kind the parser does not record is a gap, not an empty revision list pretending
    #: the document was clean.
    unrecorded = by_title["unrecorded_revision_kinds.docx"]
    assert unrecorded.revisions == ()
    assert unrecorded.unattributed_gaps == ["unrecorded_revision_kind"]
    assert unrecorded.unattributed_revisions == 0

    for title in ("ledger_summary.docx", V2, "threading_edge_cases.docx", "unanchored_comment.docx"):
        assert by_title[title].revisions == ()


def test_a_revision_section_filter_keeps_every_document_visible(stored):
    store = Store(stored.root)
    section = ("Program Review: Consultants Service Agreement", "2. Exclusions")
    result = get_revisions(store, section=section)
    assert result.doc is None
    assert {doc.title for doc in result.documents} == set(COMMENT_COUNTS)

    rows = {doc.title: [row.revision_id for row in doc.revisions] for doc in result.documents}
    assert rows[V3] == ["del:900", "ins:901"]
    assert rows["spec_threaded.docx"] == []
    assert rows["deleted_paragraph_mark.docx"] == []

    bare = get_revisions(store, section=("2. Exclusions",))
    assert all(not doc.revisions for doc in bare.documents)

    scoped = get_revisions(store, V3)
    assert scoped.doc == V3
    assert [doc.title for doc in scoped.documents] == [V3]


# --- find_terms ------------------------------------------------------------------------


def test_find_terms_resolves_canonical_synonyms_and_nothing_at_all(stored):
    store = Store(stored.root)
    direct = find_terms(store, "termination")
    assert direct.resolved_group == "termination"
    titles = [doc.title for doc in direct.documents]
    assert titles == ["ledger_summary.docx", V2, V3]

    ledger = direct.documents[0]
    assert [section.section_path for section in ledger.sections] == [
        ("Ledger Summary", "Conditions")
    ]
    assert [hit.match_type for hit in ledger.sections[0].hits] == [
        MatchType.SYNONYM,
        MatchType.EXACT,
    ]
    v3 = next(doc for doc in direct.documents if doc.title == V3)
    assert {section.section_path for section in v3.sections} == {
        ("Program Review: Consultants Service Agreement", "1. Coverage Terms")
    }
    assert {hit.chunk_id for section in v3.sections for hit in section.hits} == set(
        _hit_chunks(store, V3)
    )
    assert all(hit.views for hit in v3.sections[0].hits)

    via_synonym = find_terms(store, "waiver of termination")
    assert via_synonym.resolved_group == "termination"
    assert [doc.title for doc in via_synonym.documents] == titles
    assert [doc.sections for doc in via_synonym.documents] == [
        doc.sections for doc in direct.documents
    ]

    unresolved = find_terms(store, "terminated")
    assert unresolved.resolved_group is None
    assert unresolved.documents == ()


def test_find_terms_section_filter_is_a_full_title_path(stored):
    store = Store(stored.root)
    full = find_terms(store, "termination", section=("Ledger Summary", "Conditions"))
    assert [doc.title for doc in full.documents] == ["ledger_summary.docx"]

    bare = find_terms(store, "termination", section=("Conditions",))
    assert bare.documents == ()

    scoped = find_terms(store, "termination", doc=V3)
    assert [doc.title for doc in scoped.documents] == [V3]
    assert scoped.term_list


# --- search ----------------------------------------------------------------------------


def test_search_states_the_question_it_answered(stored):
    store = Store(stored.root)
    result = search(store, "termination")
    assert result.query == "termination"
    assert result.resolved_group == "termination"
    assert result.scope is None
    assert result.sources == SOURCES
    assert result.views == (View.ACCEPTED, View.ORIGINAL)
    assert result.term_list

    titles = {row.document_id: row.title for row in list_documents(store)}
    assert {titles[found.document_id] for found in result.results} == {
        "ledger_summary.docx",
        V2,
        V3,
    }
    assert all("term" in found.sources for found in result.results)
    assert all(found.term_group == "termination" for found in result.results)
    tiers = [found.tier for found in result.results]
    assert tiers == sorted(tiers)


def test_search_scopes_and_filters_and_echoes_what_it_ran_under(stored):
    store = Store(stored.root)
    scoped = search(store, "termination", scope=V3)
    assert scoped.scope == V3
    v3_id = _by_title(list_documents(store))[V3].document_id
    assert {found.document_id for found in scoped.results} == {v3_id}
    assert scoped.results

    filtered = search(store, "termination", sources=("term",))
    assert filtered.sources == ("term",)
    titles = {row.document_id: row.title for row in list_documents(store)}
    assert {titles[found.document_id] for found in filtered.results} == {
        "ledger_summary.docx",
        V2,
        V3,
    }
    assert all("term" in found.sources for found in filtered.results)

    accepted = search(store, "termination", views=[View.ACCEPTED])
    assert accepted.views == (View.ACCEPTED,)

    with pytest.raises(ValueError, match="unknown sources"):
        search(store, "termination", sources=("footnote",))


def test_search_reaches_the_summary_tier_and_admits_a_miss(stored):
    store = Store(stored.root)
    canned = search(store, "canned")
    assert canned.resolved_group is None
    assert canned.results
    assert any("summary" in found.sources for found in canned.results)
    tiers = [found.tier for found in canned.results]
    assert tiers == sorted(tiers)

    miss = search(store, "flibbertigibbet")
    assert miss.resolved_group is None
    assert miss.results == ()


# --- compare ---------------------------------------------------------------------------


def test_compare_splits_chunks_and_group_hits_per_side(stored):
    store = Store(stored.root)
    result = compare(store, V2, V3)
    assert result.document_a != result.document_b
    assert result.group is None and result.resolved_group is None
    assert result.term_list is None
    assert [view.view for view in result.views] == [View.ACCEPTED]

    view = result.views[0]
    assert view.unchanged_chunks > 0
    a_ids = {change.chunk_id for change in view.only_in_a}
    b_ids = {change.chunk_id for change in view.only_in_b}
    assert a_ids.isdisjoint(b_ids)
    assert view.only_in_a and view.only_in_b
    assert all(change.citation.document_id == result.document_a for change in view.only_in_a)
    assert all(change.citation.document_id == result.document_b for change in view.only_in_b)
    #: No group was asked, so the hit difference is "not compared", never "neither".
    assert view.hits_only_in_a == () and view.hits_only_in_b == ()

    # The pair's sidecar (hand-typed) says exactly two chunks changed: chunk 2's text (it is
    # in only_in_a/only_in_b) and chunk 0's comment text, whose chunk id is unchanged -- so it
    # is neither of those but is named in comments_changed.
    assert len(view.only_in_a) == 1 and len(view.only_in_b) == 1
    assert len(view.comments_changed) == 1
    assert view.comments_changed[0] not in a_ids | b_ids
    assert view.unchanged_chunks == 4  # chunks 0, 1, 3, 4 share an id; 0's comment differs

    grouped = compare(store, "ledger_summary.docx", V3, group="termination")
    assert grouped.group == "termination" and grouped.resolved_group == "termination"
    ledger_hits = _hit_chunks(store, "ledger_summary.docx")
    v3_hits = _hit_chunks(store, V3)
    assert ledger_hits and v3_hits
    view = grouped.views[0]
    #: The fixed semantics: each side names the chunks the *other* side lacks the group
    #: in, so the same chunk id carrying the group in both documents names neither side.
    assert set(view.hits_only_in_a) == ledger_hits - v3_hits
    assert set(view.hits_only_in_b) == v3_hits - ledger_hits
    assert set(view.hits_only_in_a).isdisjoint(view.hits_only_in_b)

    shared = compare(store, V2, V3, group="termination").views[0]
    assert _hit_chunks(store, V2) == v3_hits
    assert shared.hits_only_in_a == () and shared.hits_only_in_b == ()


def test_compare_refuses_one_document_and_documents_sharing_no_view(tmp_path):
    root = tmp_path / "views"
    run(FIXTURES / V3, REGISTRY, store_root=root, run_id="accepted-v3")
    run(
        FIXTURES / "ledger_summary.docx",
        REGISTRY,
        store_root=root,
        view=View.ORIGINAL,
        run_id="original-ledger",
    )
    store = Store(root)
    with pytest.raises(ValueError, match="two different documents"):
        compare(store, V3, V3)
    with pytest.raises(LookupError, match="share no stored view"):
        compare(store, V3, "ledger_summary.docx")


# --- purity: read-only, determinism ----------------------------------------------------


def test_every_query_reads_a_read_only_store_identically_and_writes_nothing(stored):
    root = stored.root
    before = _snapshot(root)
    read_only = _exercise(Store(root, read_only=True))
    assert _snapshot(root) == before
    assert read_only == _exercise(Store(root))


def test_every_query_answers_the_same_way_twice(stored):
    store = Store(stored.root, read_only=True)
    assert _exercise(store) == _exercise(store)


# --- term-list selection (rank.pick_term_list, through query) --------------------------


def test_a_store_holding_several_term_lists_is_never_chosen_for_you(tmp_path):
    root = tmp_path / "store"
    run(FIXTURES / V3, REGISTRY, store_root=root, run_id="first")
    other = tmp_path / "other-terms.json"
    other.write_text(
        REGISTRY.read_text(encoding="utf-8").replace('"permit aggregate"', '"permit cap"'),
        encoding="utf-8",
    )
    run(FIXTURES / V3, other, store_root=root, run_id="second")
    store = Store(root)

    with pytest.raises(ValueError, match="several term lists"):
        search(store, "termination")
    with pytest.raises(ValueError, match="several term lists"):
        find_terms(store, "termination")
    with pytest.raises(ValueError, match="not stored"):
        search(store, "termination", term_list="checklist-2021")

    chosen = term_list_hash(load_terms(other))
    answered = search(store, "termination", term_list=chosen)
    assert answered.term_list == chosen
    assert answered.results


# --- the machine-local sample ----------------------------------------------------------


@pytest.mark.skipif(
    not LOCAL_SAMPLE.exists(), reason="the machine-local review sample is not in this checkout"
)
def test_the_machine_local_sample_answers_every_query(tmp_path):
    root = tmp_path / "store"
    record = run(LOCAL_SAMPLE, REGISTRY, store_root=root)
    summarize(Store(root), record, client=CannedClient(), model="canned")
    store = Store(root, read_only=True)
    title = LOCAL_SAMPLE.name

    rows = list_documents(store)
    assert [row.title for row in rows] == [title]
    assert get_outline(store, title).title == title

    chunk = stored_chunks(record, store_root=root)[0]
    row = get_chunk(store, chunk.id, "accepted")
    assert row.document_id == rows[0].document_id
    assert row.text and row.markup and row.citation.spans
    assert row.markup.count("[[comment by=") == len(row.comments)

    assert isinstance(get_comments(store), list)
    assert isinstance(get_revisions(store).documents, tuple)
    assert find_terms(store, "termination").term_list
    assert search(store, "termination").scope is None
    #: One document can only answer compare by refusing: there is no second side.
    with pytest.raises(ValueError, match="two different documents"):
        compare(store, title, title)
