"""The eight tools: thin, validated closures over :mod:`wordextract.query` (Turn 6).

One closure per query function: validate the arguments, call the function in a worker
thread (the reads are file I/O), answer with the dataclass as JSON -- the wire shape a
client reads. MCP-awareness stops here and in ``server.py``; everything substantive
stays in the query layer.

**Read-only, by construction.** ``Backend`` opens the store with ``read_only=True``,
so the store's own gate refuses any write, and no tool ingests or summarizes (those
stay on the CLI, where the liveness and the write live). Every description states the
view semantics (views are named, never implied) and that the answer carries its
citations (document, address, union spans, view).
"""
from __future__ import annotations

import json
from typing import Any, Callable

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from docextract_core import encode

from .. import query as query_api
from ..model import View
from ..store import Store
from .settings import Settings

__all__ = ["Backend", "register_tools"]

#: What every tool carries: MCP's own read-only flag, true in the protocol itself.
_READ_ONLY = ToolAnnotations(readOnlyHint=True)


class Backend:
    """What the tools read through: one store, opened read-only.

    Opening is the whole policy: ``read_only=True`` makes every collection raise on a
    write, and a store path that does not exist fails here rather than reading as an
    empty one.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = Store(settings.store, read_only=True)

    def as_json(self, result: Any) -> str:
        """A query dataclass as canonical JSON text: encoded, sorted, UTF-8 clean."""
        return json.dumps(encode(result), ensure_ascii=False, sort_keys=True)


def register_tools(mcp: FastMCP, backend: Backend, settings: Settings) -> None:
    """Register the eight query tools on ``mcp`` (the only place closures are built).

    Each tool validates its arguments, runs one :mod:`wordextract.query` call against
    ``backend``'s read-only store off the event loop, and returns the dataclass as
    JSON. ``settings`` supplies the two defaults: ``get_chunk``'s view and the term
    list for ``find_terms``/``search``/``compare``.
    """

    async def _read(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """One store read, off the event loop: the tools are file I/O over a directory."""
        return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))

    def _view(value: str) -> View:
        """A requested view, named: unknown is an error listing the views, never a fallback."""
        try:
            return View(value)
        except ValueError:
            choices = [candidate.value for candidate in View]
            raise ValueError(f"unknown view {value!r}; the views are {choices}") from None

    @mcp.tool(annotations=_READ_ONLY)
    async def list_documents() -> str:
        """Every document in the store with its stored roll-up, in catalog order.

        Read-only: nothing here ingests or summarizes. ``views`` names the view
        readings the store holds chunks for (``accepted`` for a standard ingest --
        stated, never assumed); ``summary`` and ``has_pending`` are null
        until a roll-up is written (unknown, never a no). Rows carry their
        ``document_id``; the per-answer citations (document, address, union spans,
        view) come from the addressed tools below.
        """
        return backend.as_json(await _read(query_api.list_documents, backend.store))

    @mcp.tool(annotations=_READ_ONLY)
    async def get_outline(doc: str) -> str:
        """One document's heading tree with each section's stored roll-up.

        ``doc`` is a document id or its exact title; several documents sharing that
        title is an error naming them, never a pick. ``view`` names the reading the
        tree is stated in (the accepted one -- views are never implied), and
        ``summary``/``has_pending`` are null until a roll-up exists. Each section
        names its ``heading_id`` -- the address citations resolve through -- with the
        full citations (document, address, union spans, view) on get_chunk,
        get_comments and the hit rows.
        """
        return backend.as_json(await _read(query_api.get_outline, backend.store, doc))

    @mcp.tool(annotations=_READ_ONLY)
    async def get_chunk(
        chunk_id: str,
        view: str = settings.view.value,
        doc: str | None = None,
    ) -> str:
        """One chunk rendered in one named view: text, markup, comments, pending changes.

        ``view`` defaults to the settings' default, but the row and its citation echo
        the view actually read -- a default never implies one, and a view the store
        does not hold for the document raises naming the views it does hold. ``doc``
        disambiguates a chunk id two documents share (ambiguity is an error, never a
        pick). The row's ``citation`` is document, address, union spans and view.
        """
        return backend.as_json(
            await _read(
                query_api.get_chunk,
                backend.store,
                chunk_id,
                _view(view).value,
                doc=doc,
            )
        )

    @mcp.tool(annotations=_READ_ONLY)
    async def get_comments(
        doc: str | None = None,
        section: list[str] | None = None,
        author: str | None = None,
    ) -> str:
        """The store's comment records: one document's or every document's.

        ``doc`` is a document id or exact title (None reads every document);
        ``section`` is a full outermost-first title path -- a bare leaf that matches
        no path is the section rule, not a dropped row; ``author`` is exact. Each row
        carries its citation: a ``comment:<para_id>`` address with union spans and a
        null view, because a comment anchor is a union address rather than text in any
        one view -- the row's ``section_path`` states where it sits instead.
        """
        return backend.as_json(
            await _read(
                query_api.get_comments,
                backend.store,
                doc,
                section=section,
                author=author,
            )
        )

    @mcp.tool(annotations=_READ_ONLY)
    async def get_revisions(doc: str | None = None, section: list[str] | None = None) -> str:
        """The store's revision records: one document's or every document's.

        ``doc`` is a document id or exact title; ``section`` is a full
        outermost-first title path -- a revision outside every section contributes no
        section, so the filter excludes it rather than inventing a home. Each row
        carries its citation (document, node address, union spans, view null on a
        union address) plus every section path its text stands in, and the catalog's
        unattributed counts stay null when the parse was not readable -- unknown,
        never a zero.
        """
        return backend.as_json(
            await _read(query_api.get_revisions, backend.store, doc, section=section)
        )

    @mcp.tool(annotations=_READ_ONLY)
    async def find_terms(
        group: str,
        term_list: str | None = None,
        doc: str | None = None,
        section: list[str] | None = None,
    ) -> str:
        """Stored hits for one term group, grouped by section.

        ``group`` is matched exactly as stored; when it equals no stored group,
        ``resolved_group`` is null and ``documents`` empty -- an answer with no rows,
        not an error. ``term_list`` defaults to the settings' (decision 3: the store's
        only list). Each hit carries its citation: union addresses with view null --
        addresses are view-independent -- while the views the hit occurs in are named
        on the row, never implied.
        """
        return backend.as_json(
            await _read(
                query_api.find_terms,
                backend.store,
                group,
                term_list=settings.term_list if term_list is None else term_list,
                doc=doc,
                section=section,
            )
        )

    @mcp.tool(annotations=_READ_ONLY)
    async def search(
        query: str,
        term_list: str | None = None,
        scope: str | None = None,
        sources: list[str] | None = None,
        views: list[str] | None = None,
    ) -> str:
        """``rank``'s fixed-tier answer over the store: term, then text, then summary.

        No numeric relevance exists anywhere. ``term_list`` defaults to the settings'
        (decision 3); ``scope`` restricts the search to one document (an id or exact
        title; unknown is an error naming the state, default: every document);
        ``sources`` filters to named tiers (unknown names are rejected, never
        ignored); ``views`` names which view readings the text tier scans, defaulting
        to both body readings -- accepted and original, never accepted-only. The
        answer echoes every one of those choices so it states the question it
        answered, and each result records its citation as document, chunk and
        locations (node id, union spans).
        """
        return backend.as_json(
            await _read(
                query_api.search,
                backend.store,
                query,
                term_list=settings.term_list if term_list is None else term_list,
                scope=scope,
                sources=sources,
                views=None if views is None else [_view(view) for view in views],
            )
        )

    @mcp.tool(annotations=_READ_ONLY)
    async def compare(
        doc_a: str,
        doc_b: str,
        group: str | None = None,
        term_list: str | None = None,
    ) -> str:
        """Two documents' chunk difference, computed per view they share.

        ``doc_a``/``doc_b`` are ids or exact titles (the same document twice, or two
        sharing no stored view, is an error naming the state -- never an empty
        comparison). ``group`` optionally asks which side carries the group's hits;
        ``term_list`` defaults to the settings' (decision 3). Each view comparison
        names the view it read -- a view only one side holds is absent, never guessed
        -- and the only-in-A/only-in-B rows carry citations: document, address, union
        spans, view.
        """
        return backend.as_json(
            await _read(
                query_api.compare,
                backend.store,
                doc_a,
                doc_b,
                group=group,
                term_list=settings.term_list if term_list is None else term_list,
            )
        )
