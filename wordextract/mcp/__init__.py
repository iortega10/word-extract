"""Phase 2, Turn 6: the MCP tool layer -- read-only tools over :mod:`wordextract.query`.

This package is the only place the third-party ``mcp`` framework appears, and only in
``tools.py`` and ``server.py``: it is an **optional extra** (``pip install -e
".[mcp]"``), so the query layer, the CLI and their tests run without it -- ``settings``
is pure and this module imports nothing. Start the server either way::

    python -m wordextract serve --store DIR --view VIEW --terms TERMS   # flags
    python -m wordextract.mcp                                           # defaults

Both call :func:`wordextract.mcp.server.serve`: a FastMCP named ``wordextract`` on
stdio, its eight tools reading one store through one ``Settings``. Ingesting and
summarizing stay on the CLI -- the tools only read.
"""
