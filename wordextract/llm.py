"""Turn 1: the clients a summarizer can be driven with, and how a name becomes one.

No provider lives here. :class:`CannedClient` is the one implementation in the repo -- it
answers from a string and records what it was asked, so a test can assert that a cache hit
made no call -- and a caller with a real one hands :func:`~wordextract.summarize.summarize`
any :class:`~docextract_core.LLMClient` directly, or names it on the command line so
:func:`client` can import it.

    from wordextract.llm import client
    run = summarize(store, record, client=client("canned"), model="canned")

**A real client is named, never configured here.** ``client("mod:attr")`` imports the
attribute and uses it (calling it first when it is a class or a factory). That is the whole
seam: no key, URL or model id is read from this package, so nothing about a provider's
configuration can leak into a committed file, and a store's summary keys -- which carry the
model id -- name the model the caller chose, not one this module guessed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module
from typing import Any, Callable

from docextract_core import LLMClient, LLMResponse, to_json

from .model import SummaryOutput
from .versions import OUTPUT_SCHEMA_VERSION

#: The only client name this package owns: an answer, no network, no provider.
CANNED = "canned"

#: What ``summarize`` records as the model when a caller names none. The canned client ignores
#: it; a real client has a model id of its own and must be given it on the command line,
#: because the id is key material and a silent default would key a real call under a fiction.
DEFAULT_MODEL = CANNED

#: The answer a bare :class:`CannedClient` gives: a valid, obviously-canned summary, so a run
#: driven by it exercises the stored-record path and never looks like real content.
CANNED_ANSWER = to_json(
    SummaryOutput(
        summary="(canned client: no model was asked)",
        topics=[],
        open_questions=[],
    ),
    schema_version=OUTPUT_SCHEMA_VERSION,
)


@dataclass(frozen=True)
class CannedCall:
    """One call a :class:`CannedClient` was asked to make, recorded as it was asked."""

    prompt: str
    model: str
    params: dict[str, Any]


Answer = str | Callable[..., str]


@dataclass
class CannedClient:
    """An :class:`~docextract_core.LLMClient` that answers from ``output`` and calls nothing.

    ``output`` is the answer text, or a callable taking the same arguments as ``complete`` and
    returning the text -- the shape a test needs to answer one chunk differently from another
    (a malformed answer for the second chunk, say). ``tokens`` is what the client reports it
    used, ``None`` by default: a canned answer was not measured, and a made-up count would be
    recorded as a cost that never happened. Every call is appended to ``calls``, which is how a
    test shows a cache hit made none.
    """

    output: Answer = CANNED_ANSWER
    tokens: int | None = None
    calls: list[CannedCall] = field(default_factory=list)

    def complete(self, prompt: str, *, model: str, params: dict[str, Any]) -> LLMResponse:
        """Record the call, then answer it -- deterministically, and without a provider."""
        self.calls.append(CannedCall(prompt=prompt, model=model, params=dict(params)))
        text = self.output(prompt, model=model, params=params) if callable(self.output) else self.output
        return LLMResponse(text=text, model=model, tokens=self.tokens)


#: The clients :func:`client` resolves by name, before it tries to import one. A test can add
#: to this rather than monkeypatching an import; the shipped entry is the canned client.
CLIENTS: dict[str, Callable[[], LLMClient]] = {CANNED: CannedClient}


def client(name: str) -> LLMClient:
    """The client ``name``: a registered one, or ``"<module>:<attribute>"`` to import.

    The dotted form resolves at call time, so a real adapter can live in a package this repo
    does not depend on -- the one place a provider enters, named by whoever runs the command.
    An attribute that is a class or a factory is called to get the client; one that already
    has ``complete`` is used as it is (a module-level, pre-configured client).
    """
    if ":" in name:
        return _imported(name)
    try:
        return CLIENTS[name]()
    except KeyError:
        raise ValueError(
            f"unknown client {name!r}: expected {sorted(CLIENTS)} or '<module>:<attribute>'"
        ) from None


def _imported(name: str) -> LLMClient:
    """``"<module>:<attribute>"``: the object there, or what calling it returns."""
    module_name, _, attribute = name.partition(":")
    if not module_name or not attribute:
        raise ValueError(f"client {name!r} is not '<module>:<attribute>'")
    try:
        module = import_module(module_name)
    except ImportError as missing:
        raise ValueError(f"client {name!r}: cannot import {module_name!r}: {missing}") from missing
    try:
        resolved = getattr(module, attribute)
    except AttributeError:
        raise ValueError(f"client {name!r}: {module_name!r} has no {attribute!r}") from None
    if hasattr(resolved, "complete"):
        return resolved
    return resolved()


__all__ = [
    "CANNED",
    "CANNED_ANSWER",
    "CLIENTS",
    "CannedCall",
    "CannedClient",
    "DEFAULT_MODEL",
    "client",
]
