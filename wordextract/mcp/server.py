"""``create_mcp`` -- the FastMCP server both entrypoints share -- and the stdio run.

Construction registers the tools and opens the store read-only; binding happens only in
:func:`serve` (``mcp.run``), so tests can build a server without starting a transport.
"""
from __future__ import annotations

import sys

from docextract_core import ReadOnlyError
from mcp.server.fastmcp import FastMCP

from .settings import Settings
from .tools import Backend, register_tools

__all__ = ["create_mcp", "serve"]

_INSTRUCTIONS = (
    "Read-only tools over a wordextract store: every answer comes from the records a "
    "run already wrote -- nothing here ingests, summarizes or writes. Views are named, "
    "never implied, and each answer carries its citations (document, address, union "
    "spans, view) so it can be checked against the store."
)


def create_mcp(settings: Settings) -> FastMCP:
    """A ``FastMCP`` with the eight query tools registered; nothing is bound yet.

    The backend opens ``settings.store`` read-only: a store that does not exist raises
    here rather than reading as an empty one (a typo fails at startup).
    """
    mcp = FastMCP("wordextract", instructions=_INSTRUCTIONS)
    register_tools(mcp, Backend(settings), settings)
    return mcp


def serve(settings: Settings) -> int:
    """Create the server and run it on stdio; 2 when the store will not open read-only.

    Both entrypoints call this: ``python -m wordextract serve`` (settings from its
    flags) and ``python -m wordextract.mcp`` (the :class:`Settings` defaults).
    """
    try:
        mcp = create_mcp(settings)
    except ReadOnlyError as exc:
        print(
            f"wordextract: store {settings.store} cannot be opened read-only: {exc}",
            file=sys.stderr,
        )
        return 2
    mcp.run(transport="stdio")
    return 0
