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
