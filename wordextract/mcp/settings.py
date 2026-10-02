"""What the tools are pointed at: one store path, one default view, one default term list.

Pure on purpose: no ``mcp`` import, so the settings are importable and testable whether
or not the optional extra is installed (the third-party framework stays quarantined in
``tools.py`` and ``server.py``).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..model import View
from ..pipeline import DEFAULT_STORE

__all__ = ["Settings"]


@dataclass(frozen=True)
class Settings:
    """Where the read-only tools read from: the three things a tool call can default.

    ``store`` is opened read-only and must already exist -- the store raises rather
    than reading a typo as an empty one, so a bad path fails at startup, never as a
    silently empty answer. ``view`` is ``get_chunk``'s default; the row and its
    citation still name the view actually read, so a default never implies one.
    ``term_list`` is the default for the three tools that take one: None is decision 3
    -- the store's only list, an error when it holds zero or several.
    """

    store: Path = DEFAULT_STORE
    view: View = View.ACCEPTED
    term_list: str | None = None
