"""LLM client boundary: the protocol a summarizer depends on, plus its response."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class LLMResponse:
    text: str
    model: str
    tokens: int | None = None
    latency_ms: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class LLMClient(Protocol):
    def complete(self, prompt: str, *, model: str, params: dict[str, Any]) -> LLMResponse:
        ...
