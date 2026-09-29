"""Content-addressed archives for raw inputs and LLM I/O (no rewrite on hit)."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .hashing import content_hash, sha256_text
from .jsonio import read_json, write_json


@dataclass
class RawArchiveRecord:
    content_hash: str
    size: int
    mime: str
    original_filename: str
    ingested_at: str
    source_system: str = "local"
    storage_ref: str = ""


@dataclass
class LLMCall:
    call_id: str
    purpose: str
    model: str
    params: dict[str, Any]
    prompt_hash: str
    prompt_ref: str
    response_ref: str
    tokens: int | None = None
    latency_ms: int | None = None


def archive_raw(
    root: str | Path,
    path: str | Path,
    *,
    mime: str = "",
    source_system: str = "local",
) -> RawArchiveRecord:
    """Copy ``path`` under ``<root>/raw/<content_hash><suffix>``; idempotent."""
    root = Path(root)
    path = Path(path)
    raw_dir = root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    chash = content_hash(path)
    index_path = raw_dir / "index.json"
    index = read_json(index_path, {})
    if chash in index:
        return RawArchiveRecord(**index[chash])
    dest = raw_dir / f"{chash}{path.suffix.lower()}"
    if not dest.exists():
        dest.write_bytes(path.read_bytes())
    record = RawArchiveRecord(
        content_hash=chash,
        size=path.stat().st_size,
        mime=mime or path.suffix.lower().lstrip("."),
        original_filename=path.name,
        ingested_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        source_system=source_system,
        storage_ref=str(dest.relative_to(root)),
    )
    index[chash] = {**record.__dict__}
    write_json(index_path, index)
    return record


def archive_llm_call(
    root: str | Path,
    *,
    purpose: str,
    model: str,
    params: dict[str, Any],
    prompt: str,
    response: str,
    tokens: int | None = None,
    latency_ms: int | None = None,
) -> LLMCall:
    """Write prompt/response under content-addressed paths; existing files are left alone."""
    root = Path(root)
    llm_dir = root / "llm"
    prompt_dir = llm_dir / "prompts"
    response_dir = llm_dir / "responses"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    response_dir.mkdir(parents=True, exist_ok=True)
    prompt_hash = sha256_text(prompt)
    response_hash = sha256_text(response)
    prompt_path = prompt_dir / f"{prompt_hash}.txt"
    response_path = response_dir / f"{response_hash}.txt"
    if not prompt_path.exists():
        prompt_path.write_text(prompt, encoding="utf-8")
    if not response_path.exists():
        response_path.write_text(response, encoding="utf-8")
    return LLMCall(
        call_id=uuid.uuid4().hex,
        purpose=purpose,
        model=model,
        params=dict(params),
        prompt_hash=prompt_hash,
        prompt_ref=str(prompt_path.relative_to(root)),
        response_ref=str(response_path.relative_to(root)),
        tokens=tokens,
        latency_ms=latency_ms,
    )
