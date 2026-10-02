"""Version constants and the reproducibility key (design D10).

``HashedInputs`` is exactly D10 part (A): the inputs that participate in every
cache key. ``view_id`` is always a member, so different view policies can never
collide on a key.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from docextract_core import sha256_json

from .stem import STEM_ALGORITHM_VERSION

TEXTMODEL_VERSION = "2"
SPEC_PARSER_VERSION = "1"
HEADING_RULESET_VERSION = "2"
CHUNKER_VERSION = "1"
#: The matcher's own version, with the vendored stemmer's version nested inside it (6b): a
#: new ``stem.py`` must invalidate every key that stemmed anything, so its identity is part
#: of the matcher's -- the mirror of per-group algorithm choice living in ``term_list_hash``.
MATCHER_VERSION = f"5+{STEM_ALGORITHM_VERSION}"
SUMMARIZER_VERSION = "1"
OUTPUT_SCHEMA_VERSION = "1"
#: The version of the rendering **format** (Phase 2, Turn 1, bumped in Turn 2): the wrapper
#: syntax, the comment line, the manifest line. Turn 2 changed the manifest's derivation --
#: entries now come from ``pending.py`` and ``text_excerpt`` is truncated to
#: ``pending.EXCERPT_LIMIT`` with ``pending.TRUNCATION_MARKER`` -- so the format version
#: moves with it, even where the corpus bytes are unchanged. Bump it when the format
#: changes; it is not part of a summary key (the bytes are), so the bump is a signal to the
#: ledger and to a reviewer, not a key member.
RENDER_VERSION = "2"
#: The version of the ``pending_changes`` derivation itself (Phase 2, Turn 2): which
#: revisions a chunk's entries list, in what order, with what excerpt truncation. Like
#: ``RENDER_VERSION`` it is a ledger/reviewer signal, not a key member -- a summary's key is
#: taken over the rendered bytes, so the derivation reaches a key only through them.
PENDING_VERSION = "1"
#: The version of the retrieval and ranking derivation (Phase 2, Turn 3): how a query is
#: resolved to a term group, which sources a chunk is labeled with, and in what order the
#: results come back. Like ``PENDING_VERSION`` it is a ledger/reviewer signal, not a key
#: member: nothing is stored under it, and a summary's key is taken over the rendered
#: bytes, which ranking never touches.
RANK_VERSION = "1"
#: The roll-up derivation's version and its **key** membership (Phase 2, Turn 4): the fan-in,
#: the grouping, the rendering of a parent's input from its children, and the tri-state union
#: that becomes ``has_pending``. Unlike ``PENDING_VERSION``/``RANK_VERSION`` this one is a
#: key member -- ``store.rollup_key`` hashes it -- because a roll-up is stored under a key
#: this derivation computes, so changing the derivation must change every roll-up key above
#: the change. Bump it whenever a roll-up's inputs, grouping or flags are derived differently.
ROLLUP_VERSION = "1"


@dataclass(frozen=True)
class HashedInputs:
    source_content_hash: str
    spec_parser_version: str
    textmodel_version: str
    view_id: str
    heading_ruleset_version: str
    chunker_version: str
    chunker_params_hash: str
    term_list_hash: str
    matcher_version: str
    summarizer_version: str
    model_id: str
    model_params_hash: str
    prompt_hash: str
    output_schema_version: str


def compute_key(inputs: HashedInputs) -> str:
    """sha256 over the canonical JSON of every hashed input."""
    return sha256_json(asdict(inputs))
