"""``python -m wordextract.mcp`` -- serve the read-only tools with the default settings.

The same server as ``python -m wordextract serve``, whose flags set the three
:class:`~wordextract.mcp.settings.Settings` fields this takes as their defaults.
"""
from __future__ import annotations

import sys

from .server import serve
from .settings import Settings

if __name__ == "__main__":
    sys.exit(serve(Settings()))
