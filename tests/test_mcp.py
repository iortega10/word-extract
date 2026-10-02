"""Phase 2, Turn 6: the MCP tool layer, asserted against a store that exists.

The claims under test:

* ``create_mcp`` registers exactly the eight query tools Turn 6 names, with the
  arguments the spec gives them (``chunk_id``/``view``/``doc`` optional where said),
  each carrying MCP's ``readOnlyHint`` and a description that names the views and the
  citations (both required: views are never implied, answers must be checkable).
* Every tool answers with JSON -- one TextContent -- whose records carry the six-key
  ``citation`` (document, address, union spans, view) wherever the spec says they do,
  with view ``None`` on union-address citations and the rendered view on a chunk row.
* Failures are ToolErrors naming the state: unknown document, unknown view, a view the
  store does not hold, unknown tier, unknown scope, unknown term list, a document
  compared with itself -- no silent defaults anywhere.
* The backend opens the store read-only and no call -- successful or failing -- writes
  a byte or a nanosecond of mtime into the store tree.
* ``serve`` wires the flags into ``Settings``, runs on stdio, exits ``2`` when the
  store will not open read-only **or** the optional extra is missing, and the
  third-party ``mcp`` framework stays quarantined: the query layer, the CLI and the
  settings import without it. A spawned ``python -m wordextract serve`` speaks the
  real wire: initialize, a working call, a failing call.

Fixture ground truth: the v2/v3 pair, five comments each, five sections each, two
revisions in v3 only, four ``subrogation`` hits (three in v3), chunks stored in the
accepted view only -- so every answer below must *name* that state, never assume it.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import subprocess
import sys
import threading
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

pytest.importorskip(
    "mcp", reason="the optional extra is not installed (pip install -e '.[mcp]')"
)

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from wordextract.cli import build_parser, main
from wordextract.mcp.server import create_mcp, serve
from wordextract.mcp.settings import Settings
from wordextract.mcp.tools import Backend
from wordextract.model import View
from wordextract.pipeline import DEFAULT_STORE, run
from wordextract.rank import SOURCES

ROOT = Path(__file__).resolve().parents[1]
TITLES = ("program_review_v2.docx", "program_review_v3.docx")
TERMS = ROOT / "fixtures" / "terms" / "synthetic.example.json"
CITATION_KEYS = {"document_id", "node_id", "chunk_id", "comment_id", "view", "spans"}

#: name -> (required arguments, optional arguments) exactly as the spec calls them.
EXPECTED_ARGUMENTS = {
    "list_documents": (set(), set()),
    "get_outline": ({"doc"}, set()),
    "get_chunk": ({"chunk_id"}, {"view", "doc"}),
    "get_comments": (set(), {"doc", "section", "author"}),
    "get_revisions": (set(), {"doc", "section"}),
    "find_terms": ({"group"}, {"term_list", "doc", "section"}),
    "search": ({"query"}, {"term_list", "scope", "sources", "views"}),
    "compare": ({"doc_a", "doc_b"}, {"group", "term_list"}),
}


def _call(server: FastMCP, name: str, **arguments: object) -> object:
    """Call one tool and parse the JSON text it answers with (the wire shape)."""
    result = asyncio.run(server.call_tool(name, arguments))
    contents = result[0] if isinstance(result, tuple) else result
    (content,) = contents
    assert content.type == "text"
    return json.loads(content.text)


def _hits(answer: dict) -> list[dict]:
    """A ``find_terms`` answer's hit rows, flattened across documents and sections."""
    return [
        hit
        for document in answer["documents"]
        for section in document["sections"]
        for hit in section["hits"]
    ]


def _exercise(server: FastMCP, documents: list[dict]) -> dict[str, object]:
    """Call every tool once with valid arguments: one sequence, used by two tests."""
    v2, v3 = sorted(documents, key=lambda row: row["title"])
    assert [v2["title"], v3["title"]] == list(TITLES)
    answers: dict[str, object] = {
        "list_documents": _call(server, "list_documents"),
        "get_outline": _call(server, "get_outline", doc=v3["title"]),
        "find_terms": _call(server, "find_terms", group="subrogation"),
        "get_comments": _call(server, "get_comments", doc=v2["document_id"]),
        "get_revisions": _call(server, "get_revisions"),
        "search": _call(server, "search", query="subrogation"),
        "compare": _call(
            server, "compare", doc_a=v2["title"], doc_b=v3["title"], group="subrogation"
        ),
    }
    cited = next(hit for hit in _hits(answers["find_terms"]) if hit["chunk_id"])
    answers["get_chunk"] = _call(
        server,
        "get_chunk",
        chunk_id=cited["chunk_id"],
        view="accepted",
        doc=cited["document_id"],
    )
    return answers


def _tree(root: Path) -> dict[str, tuple[int, int]]:
    """Every file under ``root`` with its size and mtime: anything written shows up."""
    return {
        str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.fixture(scope="module")
def store_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The v2/v3 pair ingested into one store: two documents, one shared term list."""
    root = tmp_path_factory.mktemp("mcp-store")
    for name in ("program_review_v2", "program_review_v3"):
        run(
            ROOT / "fixtures" / f"{name}.docx",
            TERMS,
            store_root=root,
            run_id=name,
        )
    return root


@pytest.fixture(scope="module")
def server(store_root: Path) -> FastMCP:
    return create_mcp(Settings(store=store_root))


@pytest.fixture(scope="module")
def documents(server: FastMCP) -> list[dict]:
    rows = _call(server, "list_documents")
    assert {row["title"] for row in rows} == set(TITLES)
    return rows


def test_the_eight_tools_are_registered_with_their_arguments(server: FastMCP) -> None:
    assert server.name == "wordextract"
    tools = asyncio.run(server.list_tools())
    assert {tool.name for tool in tools} == set(EXPECTED_ARGUMENTS)
    for tool in tools:
        required, optional = EXPECTED_ARGUMENTS[tool.name]
        properties = set(tool.inputSchema.get("properties", []))
        assert set(tool.inputSchema.get("required", [])) == required, tool.name
        assert properties - required == optional, tool.name
        assert tool.annotations is not None, tool.name
        assert tool.annotations.readOnlyHint is True, tool.name
    chunk = next(tool for tool in tools if tool.name == "get_chunk")
    assert chunk.inputSchema["properties"]["view"]["default"] == "accepted"


def test_every_description_names_the_views_and_the_citations(server: FastMCP) -> None:
    """No tool may be read without knowing which view it answers in and where from."""
    for tool in asyncio.run(server.list_tools()):
        description = (tool.description or "").lower()
        assert "view" in description, tool.name
        assert "citation" in description, tool.name


def test_each_tool_answers_with_json_and_citations(
    server: FastMCP, documents: list[dict]
) -> None:
    answers = _exercise(server, documents)
    v2, v3 = sorted(documents, key=lambda row: row["title"])

    rows = answers["list_documents"]
    assert {row["title"] for row in rows} == set(TITLES)
    for row in rows:
        assert row["views"] == ["accepted"], "the store names the view it holds"
        assert row["comment_count"] == 5, "the hand-typed sidecar"
        assert row["summary"] is None and row["has_pending"] is None, (
            "no roll-up written: unknown, never a no"
        )

    outline = answers["get_outline"]
    assert outline["document_id"] == v3["document_id"]
    assert outline["view"] == "accepted", "an outline's sections are accepted-view text"
    assert outline["summary"] is None and outline["has_pending"] is None
    assert outline["sections"], "the fixture's heading tree"
    assert outline["sections"][0]["title"] == "Program Review: Contractors General Liability"
    assert outline["sections"][0]["heading_id"], "the address citations resolve through"

    comments = answers["get_comments"]
    assert len(comments) == 5, "the hand-typed sidecar"
    comment = comments[0]
    assert comment["comment_id"].startswith("comment:")
    assert comment["threading_status"] in {"verified", "absent", "unknown"}
    assert set(comment["citation"]) == CITATION_KEYS
    assert comment["citation"]["comment_id"] == comment["comment_id"]
    assert comment["citation"]["view"] is None, "a comment anchor is a union address"
    assert comment["citation"]["spans"], "anchors are addressable"

    revisions = answers["get_revisions"]
    assert revisions["doc"] is None, "no document asked: every readable document"
    counts = {
        row["document_id"]: len(row["revisions"]) for row in revisions["documents"]
    }
    assert counts == {v2["document_id"]: 0, v3["document_id"]: 2}, "the sidecars"
    v3_row = next(row for row in revisions["documents"] if row["document_id"] == v3["document_id"])
    assert v3_row["unattributed_revisions"] == 0, "no revision sits outside the model"
    assert v3_row["unattributed_gaps"] == [], "no unrecorded gap: unknowns stay named"
    revision = v3_row["revisions"][0]
    assert set(revision["citation"]) == CITATION_KEYS
    assert revision["citation"]["view"] is None, "revision spans are union addresses"
    assert revision["citation"]["spans"]

    terms = answers["find_terms"]
    assert terms["group"] == "subrogation" and terms["resolved_group"] == "subrogation"
    hits = _hits(terms)
    assert len(hits) == 4, "the pair's hits under the synthetic registry"
    for hit in hits:
        assert hit["chunk_id"], "a body hit: every one points at a chunk"
        assert hit["views"] == ["accepted", "original"], "views named, never implied"
        assert set(hit["citation"]) == CITATION_KEYS
        assert hit["citation"]["view"] is None, "a hit is a union address"
        assert hit["citation"]["chunk_id"] == hit["chunk_id"]
        assert hit["citation"]["spans"]

    chunk = answers["get_chunk"]
    cited = next(hit for hit in hits if hit["chunk_id"])
    assert chunk["document_id"] == cited["document_id"]
    assert chunk["chunk_id"] == cited["chunk_id"]
    assert chunk["view"] == "accepted"
    assert chunk["text"] and chunk["markup"]
    assert chunk["comments"], "the chunk the comment-anchor rule grouped"
    assert set(chunk["citation"]) == CITATION_KEYS
    assert chunk["citation"]["view"] == "accepted", "a rendered view answers with its view"

    found = answers["search"]
    assert found["query"] == "subrogation"
    assert found["term_list"] == terms["term_list"], "decision 3's one stored list"
    assert found["scope"] is None
    assert found["sources"] == list(SOURCES)
    assert found["views"] == ["accepted", "original"], "both body readings, never accepted-only"
    assert found["results"]
    for result in found["results"]:
        assert result["document_id"] and result["chunk_id"]
        assert result["sources"]
        assert isinstance(result["locations"], list), "results stay addressable"
    term_results = [result for result in found["results"] if result["term_group"]]
    assert term_results, "the term tier matched"
    assert {result["term_group"] for result in term_results} == {"subrogation"}

    diff = answers["compare"]
    assert diff["document_a"] == v2["document_id"]
    assert diff["document_b"] == v3["document_id"]
    assert diff["group"] == "subrogation" and diff["resolved_group"] == "subrogation"
    assert diff["term_list"] == terms["term_list"]
    (comparison,) = diff["views"]
    assert comparison["view"] == "accepted", "the pair shares the accepted view"
    assert comparison["unchanged_chunks"] == 4, "the v2/v3 pair's shared chunks"
    assert len(comparison["comments_changed"]) == 1, "one comment was revised in v3"
    changes = comparison["only_in_a"] + comparison["only_in_b"]
    assert len(changes) == 2, "section 2's text changed, nothing else"
    for change in changes:
        assert change["citation"]["document_id"] in {v2["document_id"], v3["document_id"]}
        assert change["section_path"][-1] == "2. Exclusions"
        assert set(change["citation"]) == CITATION_KEYS
        assert change["citation"]["view"] == "accepted", "a rendered comparison names its view"
        assert change["citation"]["spans"], "a chunk change cites its bytes"


def test_failed_lookups_and_bad_arguments_name_the_state(server: FastMCP) -> None:
    with pytest.raises(ToolError) as unknown:
        _call(server, "get_outline", doc="no such document")
    assert "Error executing tool get_outline" in str(unknown.value)
    assert "no document 'no such document' in this store" in str(unknown.value)

    with pytest.raises(ToolError) as bad_view:
        _call(server, "get_chunk", chunk_id="0" * 64, view="union")
    assert "unknown view 'union'" in str(bad_view.value)
    assert "accepted" in str(bad_view.value), "the error names the views"

    terms = _call(server, "find_terms", group="subrogation")
    cited = next(hit for hit in _hits(terms) if hit["chunk_id"])
    with pytest.raises(ToolError) as wrong_view:
        _call(server, "get_chunk", chunk_id=cited["chunk_id"], view="original")
    assert "this store's views are ['accepted']" in str(wrong_view.value), (
        "the store names the views it holds, never an empty answer"
    )

    with pytest.raises(ToolError) as bad_source:
        _call(server, "search", query="subrogation", sources=["bogus"])
    assert "bogus" in str(bad_source.value), "unknown tier names are rejected"

    with pytest.raises(ToolError) as bad_scope:
        _call(server, "search", query="subrogation", scope="nope")
    assert "no document 'nope' in this store" in str(bad_scope.value)

    with pytest.raises(ToolError) as bad_list:
        _call(server, "find_terms", group="subrogation", term_list="nope")
    assert "term list 'nope' is not stored" in str(bad_list.value)
    assert terms["term_list"] in str(bad_list.value), "the error names what is stored"

    with pytest.raises(ToolError) as same:
        _call(
            server, "compare",
            doc_a="program_review_v2.docx", doc_b="program_review_v2.docx",
        )
    assert "two different documents" in str(same.value), "never an empty comparison"

    no_rows = _call(server, "find_terms", group="no such group")
    assert no_rows["resolved_group"] is None and no_rows["documents"] == [], (
        "a query equal to no group is an answer with no rows, not an error"
    )


def test_the_backend_reads_only_and_no_call_touches_the_store(
    server: FastMCP, store_root: Path, documents: list[dict]
) -> None:
    assert Backend(Settings(store=store_root)).store.read_only is True

    before = _tree(store_root)
    _exercise(server, documents)
    with pytest.raises(ToolError):
        _call(server, "get_outline", doc="no such document")
    with pytest.raises(ToolError):
        _call(server, "get_chunk", chunk_id="0" * 64, view="union")
    assert _tree(store_root) == before, "every call, failing ones included, wrote nothing"


def test_serve_serves_on_stdio_with_the_flag_settings(
    monkeypatch: pytest.MonkeyPatch, store_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    started: dict[str, object] = {}
    recorded: dict[str, Settings] = {}

    class _RecordingBackend:
        def __init__(self, settings: Settings) -> None:
            recorded["settings"] = settings

    monkeypatch.setattr("wordextract.mcp.server.Backend", _RecordingBackend)
    monkeypatch.setattr(FastMCP, "run", lambda self, *args, **kwargs: started.update(kwargs))

    code = main(
        [
            "serve",
            "--store",
            str(store_root),
            "--view",
            "original",
            "--terms",
            "synthetic.example",
        ]
    )
    assert code == 0
    assert started == {"transport": "stdio"}, "the only transport the spec wires"
    assert recorded["settings"] == Settings(
        store=store_root, view=View.ORIGINAL, term_list="synthetic.example"
    )
    printed = capsys.readouterr()
    assert printed.out == "" and printed.err == "", "a stdio server prints no records"


def test_the_real_entrypoint_speaks_the_protocol_over_stdio(store_root: Path) -> None:
    """Spawned ``python -m wordextract serve``: initialize, a call, a failing call.

    The wire is spoken directly rather than through the ``mcp`` client SDK, whose
    stdio client does not complete a handshake in this environment (it hangs against
    any FastMCP server here, including a trivial one) -- the server's answers are
    what is under test.
    """
    messages = "\n".join(
        (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "pytest", "version": "0"},
                    },
                }
            ),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "list_documents", "arguments": {}},
                }
            ),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "get_outline", "arguments": {"doc": "no-such-doc"}},
                }
            ),
        )
    ) + "\n"
    env = dict(os.environ)
    parts = [str(ROOT), str(ROOT / "docextract-core")]
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    proc = subprocess.Popen(
        [sys.executable, "-m", "wordextract", "serve", "--store", str(store_root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=ROOT,
    )
    lines: list[str] = []
    answered = threading.Event()

    def read_answers() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line)
            try:
                ids = {m["id"] for m in map(json.loads, lines) if "id" in m}
            except ValueError:
                continue
            if {1, 2, 3} <= ids:
                answered.set()

    reader = threading.Thread(target=read_answers, daemon=True)
    reader.start()
    try:
        assert proc.stdin is not None
        proc.stdin.write(messages)
        proc.stdin.flush()
        # Answer first, EOF second: closing stdin immediately drops the queued calls, so
        # wait until every request has been answered (not a fixed sleep: CI is slow) and
        # only then close stdin. proc.communicate() is avoided on purpose: before 3.13 it
        # flushes the stdin we closed and raises "I/O operation on closed file".
        assert answered.wait(timeout=60), "".join(lines)
        proc.stdin.close()
        proc.wait(timeout=60)
        reader.join(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    out = "".join(lines)
    assert proc.returncode == 0, out

    answers = {
        message["id"]: message
        for message in (json.loads(line) for line in out.splitlines())
        if "id" in message
    }
    assert answers[1]["result"]["serverInfo"]["name"] == "wordextract"
    listed = answers[2]["result"]
    assert listed["isError"] is False, "a working store answers, it does not defer"
    rows = json.loads(listed["content"][0]["text"])
    assert {row["title"] for row in rows} == set(TITLES)
    failed = answers[3]["result"]
    assert failed["isError"] is True, "an unknown document is an error, never empty"
    assert "no document 'no-such-doc' in this store" in failed["content"][0]["text"]


def test_serve_fails_loudly_when_the_store_will_not_open_read_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "not-a-store"
    assert serve(Settings(store=missing)) == 2
    printed = capsys.readouterr()
    assert printed.out == "", "no partial answer on stdout"
    assert "cannot be opened read-only" in printed.err
    assert str(missing) in printed.err, "the path it could not open"


def test_serve_exits_2_when_the_optional_extra_is_not_installed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without ``pip install -e '.[mcp]'`` the command says so and exits 2, like any run
    that cannot start -- and it never reaches the store."""
    monkeypatch.setitem(sys.modules, "wordextract.mcp.server", None)
    assert main(["serve", "--store", "no-such-store"]) == 2
    printed = capsys.readouterr()
    assert printed.out == "", "a failure prints to stderr, never a partial answer"
    assert "optional extra" in printed.err and "pip install" in printed.err


def test_serve_flags_and_settings_default_to_the_documented_values() -> None:
    args = build_parser().parse_args(["serve"])
    assert args.command == "serve"
    assert (args.store, args.view, args.terms) == (str(DEFAULT_STORE), "accepted", None)
    defaults = Settings()
    assert defaults.store == DEFAULT_STORE == Path(".wordextract")
    assert defaults.view is View.ACCEPTED
    assert defaults.term_list is None
    with pytest.raises(FrozenInstanceError):
        defaults.store = Path("elsewhere")


def test_the_query_layer_cli_and_settings_never_import_the_mcp_framework(
    tmp_path: Path,
) -> None:
    """The optional extra is optional: none of it loads for a command that does not serve."""
    code = (
        "import sys;"
        "import wordextract.cli, wordextract.query, wordextract.mcp.settings;"
        "wordextract.cli.build_parser();"
        "loaded = [name for name in sys.modules if name.split('.')[0] == 'mcp'];"
        "assert not loaded, loaded;"
        "print('clean')"
    )
    env = dict(os.environ)
    parts = [str(ROOT), str(ROOT / "docextract-core")]
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "clean"


def test_the_module_entrypoint_imports_without_running_a_transport() -> None:
    """``python -m wordextract.mcp`` shares the CLI's server; importing it binds nothing."""
    module = importlib.import_module("wordextract.mcp.__main__")
    assert callable(module.serve)
