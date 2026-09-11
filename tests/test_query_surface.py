"""Issue #24, section B: coverage, search, exact snippet, depth walks, diff reach.

One real generation is built once per module from a small foreign repository
(indexed into a state root of its own, never the vault's), and every answer
below is read from it through the same leased reader the MCP server uses.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

CORE = (
    "def helper(value):\n"
    "    return value + 1\n"
    "\n"
    "\n"
    "def caller(value):\n"
    "    return helper(value) * 2\n"
    "\n"
    "\n"
    "def top(value):\n"
    "    return caller(value)\n"
    "\n"
    "\n"
    "def top_level():\n"
    "    return 0\n"
    "\n"
    "\n"
    "class Widget:\n"
    "    def frob(self):\n"
    "        return top(1)\n"
)
BROKEN_JS = "function ok() { return 1; }\nfunction bad( {\n  return 2\n"


def _write(path: Path, text: str) -> None:
    """Exact bytes with LF: `write_text` would give a Windows runner CRLF, and
    the byte offsets and diff lines the tests assert are those of these bytes."""
    path.write_bytes(text.encode("utf-8"))
BAD_PY = "def f(:\n    pass\n"
EMPTY_AFFECTED = {"decisions": [], "pages": [], "tests": [], "checkpoints": []}


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)


def _make_repository(root: Path) -> Path:
    root.mkdir(parents=True)
    _git(root, "init", "-q", ".")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "test")
    (root / "pkg").mkdir()
    _write(root / "pkg/__init__.py", "")
    _write(root / "pkg/core.py", CORE)
    _write(root / "pkg/broken.js", BROKEN_JS)
    _write(root / "pkg/bad.py", BAD_PY)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "initial")
    return root


@pytest.fixture(scope="module")
def indexed(tmp_path_factory):
    """A foreign repository indexed into an isolated vault state root."""
    base = tmp_path_factory.mktemp("query-surface")
    root = base / "vault"
    (root / "knowledge/notes").mkdir(parents=True)
    (root / "knowledge/projects").mkdir(parents=True)
    state = base / "state"
    state.mkdir()
    patch = pytest.MonkeyPatch()
    patch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    patch.setenv("LLM_WIKI_ROOT", str(root))
    patch.setenv("MEMORY_LLM_PROVIDER", "fake")
    import evidence_reader_cache
    import impact_analysis
    import memory_state
    import repository_index

    patch.setattr(memory_state, "ROOT", root, raising=False)
    patch.setattr(memory_state, "STATE_ROOT", state, raising=False)
    # `impact_analysis` binds STATE_ROOT at import; in the full suite it is
    # imported long before this fixture runs, so it is rebound here too.
    patch.setattr(impact_analysis, "STATE_ROOT", state, raising=False)
    evidence_reader_cache.clear()
    repository = _make_repository(base / "repo")
    answer = repository_index.index_repository(repository, state_root=state)
    assert answer["status"] == "indexed", answer
    yield repository
    evidence_reader_cache.clear()
    patch.undo()


def _deadline() -> float:
    return time.monotonic() + 10


def _picked(mapping: dict, expected: dict) -> dict:
    """The subset of `mapping` under `expected`'s keys, for one equality."""
    return {key: mapping.get(key) for key in expected}


def _pairs(rows: list, first: str, second: str) -> list:
    return [(row[first], row[second]) for row in rows]


def _values(rows: list, key: str) -> list:
    return [row[key] for row in rows]


def _with_edited_core(repository: Path, call, *arguments):
    """Run `call` while `pkg/core.py` differs from the indexed bytes."""
    target = repository / "pkg/core.py"
    # The edit changes the file's size: a Windows runner rewrote the file in
    # the same second as the index with the same size, and `git diff` saw no
    # change at all (run 34540064380, `changes: []`). It shrinks rather than
    # grows: new-side byte ranges are matched against the generation's old
    # offsets, so a grown line spills into the next symbol (audit M13).
    _write(target, CORE.replace("value + 1", "value"))
    try:
        return call(*arguments)
    finally:
        _write(target, CORE)


# --------------------------------------------------------------------------
# B1: coverage per cited path
# --------------------------------------------------------------------------


def test_coverage_of_an_indexed_foreign_file_is_one_generations_word(indexed):
    from path_coverage import coverage_for_path

    answer = coverage_for_path(indexed, "pkg/core.py", _deadline())
    expected = {
        "indexed": True,
        "freshness": "fresh",
        "language": "python",
        "parse": {"status": "ok", "language": "python", "errors": [], "errors_truncated": False},
        "observations": {},
        "nodes_exact": True,
    }
    assert _picked(answer, expected) == expected
    assert answer["nodes"] >= 6
    assert answer["generation_id"].startswith("generation-")


def test_coverage_names_the_range_tree_sitter_could_not_read(indexed):
    from path_coverage import coverage_for_path

    answer = coverage_for_path(indexed, "pkg/broken.js", _deadline())
    expected = {"indexed": True, "observations": {"parse_error": 1}}
    assert _picked(answer, expected) == expected
    assert answer["parse"]["status"] == "error"
    [error] = answer["parse"]["errors"]
    assert _picked(error, {"kind": "ERROR", "line_start": 2, "line_end": 3}) == {
        "kind": "ERROR",
        "line_start": 2,
        "line_end": 3,
    }
    assert error["byte_start"] < error["byte_end"] <= len(BROKEN_JS)


def test_coverage_names_a_python_syntax_error_line(indexed):
    from path_coverage import coverage_for_path

    answer = coverage_for_path(indexed, "pkg/bad.py", _deadline())
    [error] = answer["parse"]["errors"]
    assert _picked(error, {"kind": "SyntaxError", "line_start": 1}) == {
        "kind": "SyntaxError",
        "line_start": 1,
    }
    assert answer["observations"] == {"parse_error": 1}


def test_coverage_of_an_unknown_path_is_not_indexed_and_says_so(indexed):
    from path_coverage import coverage_for_path

    answer = coverage_for_path(indexed, "pkg/none.py", _deadline())
    expected = {
        "indexed": False,
        "parse": {"status": "not_indexed"},
        "freshness": "missing_on_disk",
        "nodes": 0,
    }
    assert _picked(answer, expected) == expected


def test_coverage_reports_a_stale_file_from_the_stored_hash(indexed):
    from path_coverage import coverage_for_path

    answer = _with_edited_core(indexed, coverage_for_path, indexed, "pkg/core.py", _deadline())
    assert _picked(answer, {"indexed": True, "freshness": "stale"}) == {
        "indexed": True,
        "freshness": "stale",
    }


def test_parse_report_refuses_what_it_cannot_parse():
    import path_coverage

    assert path_coverage._parse_report(None, b"x")["status"] == "unsupported_language"
    assert path_coverage._parse_report("cobol", b"x")["status"] == "unsupported_language"
    too_big = path_coverage._parse_report("python", b"x" * (path_coverage.MAX_PARSE_BYTES + 1))
    assert too_big == {"status": "not_parsed", "language": "python", "reason": "source over 4 MiB"}


def test_error_ranges_are_bounded_in_count():
    import path_coverage
    from code_graph import _get_parser

    parser = _get_parser("javascript")
    if parser is None:
        pytest.skip("tree-sitter javascript grammar unavailable")
    source = "".join(f"function f{i}( {{\n" for i in range(path_coverage.PARSE_ERROR_LIMIT + 5))
    errors, truncated = path_coverage._error_ranges(parser.parse(source.encode()).root_node)
    assert 1 <= len(errors) <= path_coverage.PARSE_ERROR_LIMIT
    assert isinstance(truncated, bool)


# --------------------------------------------------------------------------
# B2: ranked symbol search
# --------------------------------------------------------------------------


def test_search_ranks_exact_before_prefix_before_substring(indexed):
    from symbol_search import search_symbols

    answer = search_symbols(indexed, "top", deadline=_deadline())
    rows = _pairs(answer["results"], "qualified_name", "match")
    assert rows == [("pkg.core.top", "exact"), ("pkg.core.top_level", "prefix")]
    assert _picked(answer, {"total": 2, "has_more": False}) == {"total": 2, "has_more": False}
    top = answer["results"][0]
    expected = {"kind": "function", "path": "pkg/core.py", "line": 9}
    assert _picked(top, expected) == expected
    assert (top["in_degree"], top["out_degree"]) >= (1, 1)


def test_search_globs_and_pages_with_an_exact_total(indexed):
    from symbol_search import search_symbols

    answer = search_symbols(indexed, "*er", limit=1, deadline=_deadline())
    assert _picked(answer, {"total": 2, "has_more": True}) == {"total": 2, "has_more": True}
    assert len(answer["results"]) == 1
    assert answer["results"][0]["qualified_name"] in {"pkg.core.helper", "pkg.core.caller"}


def test_search_narrows_by_path_prefix_and_names_a_miss(indexed):
    from symbol_search import search_symbols

    everything = search_symbols(indexed, "*", path_prefix="pkg/core", deadline=_deadline())
    assert everything["total"] == 6
    assert {row["kind"] for row in everything["results"]} == {"class", "function", "method"}
    nothing = search_symbols(indexed, "*", path_prefix="elsewhere/", deadline=_deadline())
    assert _picked(nothing, {"results": [], "total": 0, "has_more": False}) == {
        "results": [],
        "total": 0,
        "has_more": False,
    }


def test_search_patterns_escape_like_wildcards():
    from evidence_graph import _like_escaped, _search_patterns

    assert _search_patterns("top_level") == ("%top\\_level%", "top_level", "top\\_level%")
    assert _search_patterns("get_*") == ("get\\_%", "get_*", "get\\_%")
    assert _like_escaped("a_b%c\\d*") == "a\\_b\\%c\\\\d*"
    with pytest.raises(ValueError):
        _search_patterns("")


# --------------------------------------------------------------------------
# B3: exact snippet by qualified name
# --------------------------------------------------------------------------


def test_snippet_by_qualified_name_is_the_stored_definition_span(indexed):
    from symbol_snippet import snippet_for_symbol

    answer = snippet_for_symbol(indexed, "pkg.core.Widget.frob", _deadline())
    assert _picked(answer, {"graph": "active_generation", "resolved_nodes": 1}) == {
        "graph": "active_generation",
        "resolved_nodes": 1,
    }
    [snippet] = answer["snippets"]
    expected = {
        "precision": "exact",
        "qualified_name": "pkg.core.Widget.frob",
        "start_line": 18,
        "end_line": 19,
        "source": "    def frob(self):\n        return top(1)",
        "truncated": False,
        "freshness": "fresh",
    }
    assert _picked(snippet, expected) == expected
    assert len(snippet["source_sha256"]) == 64


def test_snippet_accepts_a_partial_owner_and_refuses_a_wrong_one(indexed):
    from symbol_snippet import snippet_for_symbol

    partial = snippet_for_symbol(indexed, "Widget.frob", _deadline())
    assert [item["qualified_name"] for item in partial["snippets"]] == ["pkg.core.Widget.frob"]
    wrong = snippet_for_symbol(indexed, "Other.frob", _deadline())
    assert _picked(wrong, {"snippets": [], "resolved_nodes": 0}) == {
        "snippets": [],
        "resolved_nodes": 0,
    }


def test_snippet_says_when_the_working_tree_moved_on(indexed):
    from symbol_snippet import snippet_for_symbol

    answer = _with_edited_core(indexed, snippet_for_symbol, indexed, "helper", _deadline())
    [snippet] = answer["snippets"]
    assert snippet["freshness"] == "stale"
    assert "value + 1" in snippet["source"]


def test_exact_block_cuts_long_definitions_and_keeps_the_true_end():
    import symbol_snippet

    lines = [f"line {index}" for index in range(1, 301)]
    block = symbol_snippet._exact_block(lines, {"line_start": 10, "line_end": 250})
    expected = {"start_line": 10, "end_line": 250, "truncated": True}
    assert _picked(block, expected) == expected
    assert block["source"].count("\n") + 1 == symbol_snippet.MAX_SNIPPET_LINES


# --------------------------------------------------------------------------
# B4: transitive callers and callees with a depth bound
# --------------------------------------------------------------------------


def test_callers_with_depth_walk_the_calls_closure_and_report_the_reach(indexed):
    from code_graph import find_callers

    answer = find_callers("helper", indexed, with_report=True, max_depth=3)
    rows = [(row["qualified_name"], row["depth"]) for row in answer["callers"]]
    assert rows == [("pkg.core.caller", 1), ("pkg.core.top", 2), ("pkg.core.Widget.frob", 3)]
    expected = {"depth_applied": 3, "depth_frontier_open": True, "symbol_resolved": True}
    assert _picked(answer, expected) == expected
    first = answer["callers"][0]
    assert (Path(first["file"]).as_posix().endswith("pkg/core.py"), first["line"]) == (True, 5)


def test_a_deep_enough_walk_closes_its_frontier(indexed):
    from code_graph import find_callers

    deeper = find_callers("helper", indexed, with_report=True, max_depth=8)
    assert deeper["depth_frontier_open"] is False
    assert len(deeper["callers"]) == 3


def test_callees_with_depth_walk_forward(indexed):
    from code_graph import find_callees

    answer = find_callees("frob", indexed, with_report=True, max_depth=3)
    rows = [(row["callee"], row["depth"]) for row in answer["callees"]]
    assert rows == [("top", 1), ("caller", 2), ("helper", 3)]
    assert answer["depth_applied"] == 3


def test_depth_one_keeps_the_one_hop_answer_unchanged(indexed):
    from code_graph import find_callers

    plain = find_callers("helper", indexed, with_report=True)
    one = find_callers("helper", indexed, with_report=True, max_depth=1)
    assert plain == one
    assert "depth" not in plain["callers"][0]
    assert plain["callers"][0]["confidence"] == "high"
    assert "depth_applied" not in plain


def test_a_walk_from_an_unknown_symbol_says_it_resolved_nothing(indexed):
    from code_graph import find_callers

    answer = find_callers("nobody_defines_this", indexed, with_report=True, max_depth=4)
    expected = {"callers": [], "symbol_resolved": False, "depth_frontier_open": False}
    assert _picked(answer, expected) == expected


# --------------------------------------------------------------------------
# B5: what the current diff touches, transitively
# --------------------------------------------------------------------------


def _dirty_evidence(repository: Path, impact: dict) -> str:
    """What a Windows runner needs to say why `changes` came back empty."""
    import subprocess

    status = subprocess.run(
        ["git", "-C", str(repository), "status", "--porcelain=v1", "--untracked-files=all"],
        capture_output=True,
        text=True,
        check=False,
    )
    config = subprocess.run(
        ["git", "-C", str(repository), "config", "--get", "core.autocrlf"],
        capture_output=True,
        text=True,
        check=False,
    )
    return (
        f"warnings={impact.get('warnings')!r} partial={impact.get('partial')!r} "
        f"changes={impact.get('changes')!r} git_status={status.stdout!r} "
        f"git_status_err={status.stderr!r} autocrlf={config.stdout.strip()!r}"
    )


def _dirty_reach(repository: Path) -> tuple[dict, dict]:
    import impact_analysis
    from impact_symbols import affected_symbols

    impact = impact_analysis.analyze_impact(root=repository, comparison="dirty", deadline=_deadline())
    return impact, affected_symbols(repository, impact["changed_symbols"], _deadline())


def test_impact_reaches_the_code_symbols_behind_a_dirty_change(indexed):
    impact, reach = _with_edited_core(indexed, _dirty_reach, indexed)
    assert _values(impact["changed_symbols"], "name") == ["helper"], _dirty_evidence(
        indexed, impact
    )
    assert impact["affected"] == EMPTY_AFFECTED
    rows = _pairs(reach["affected_symbols"], "qualified_name", "depth")
    assert rows == [("pkg.core.caller", 1), ("pkg.core.top", 2), ("pkg.core.Widget.frob", 3)]
    assert _picked(reach, {"affected_symbols_truncated": False, "affected_symbols_depth": 8}) == {
        "affected_symbols_truncated": False,
        "affected_symbols_depth": 8,
    }


def test_no_changed_symbol_means_an_empty_reach_with_a_reason(tmp_path):
    from impact_symbols import affected_symbols

    answer = affected_symbols(tmp_path, [], _deadline())
    assert answer["affected_symbols"] == []
    assert answer["affected_symbols_note"] == "no changed symbol resolved in the generation"


# --------------------------------------------------------------------------
# MCP: the modes are reachable through get_architecture, no new tool
# --------------------------------------------------------------------------


def test_search_and_depth_are_declared_on_get_architecture():
    import mcp_server

    validate = mcp_server._validate_architecture_arguments
    accepted = [
        validate({"directory": "x", "mode": "search", "symbol": "top", "path": "pkg/", "limit": 5}),
        validate({"directory": "x", "mode": "callers", "symbol": "top", "depth": 3}),
        validate({"directory": "x", "mode": "callees", "symbol": "top", "depth": 3}),
    ]
    assert accepted == [None, None, None]
    assert "depth" in validate({"directory": "x", "mode": "snippet", "symbol": "top", "depth": 3})
    assert "symbol" in validate({"directory": "x", "mode": "search"})
    assert "search" in mcp_server.TOOL_INPUT_SCHEMAS["get_architecture"]["properties"]["mode"]["enum"]


def _architecture(directory: str, **arguments) -> dict:
    import mcp_server

    data, _ = mcp_server._tool_get_architecture({"directory": directory, **arguments}, _deadline())
    return data


def test_get_architecture_serves_search_and_snippet(indexed):
    search = _architecture(str(indexed), mode="search", symbol="top", limit=5)
    assert [row["qualified_name"] for row in search["results"]] == ["pkg.core.top", "pkg.core.top_level"]
    snippet = _architecture(str(indexed), mode="snippet", symbol="pkg.core.helper")
    assert snippet["snippets"][0]["precision"] == "exact"


def test_get_architecture_serves_depth_walks_and_symbol_reach(indexed):
    callers = _architecture(str(indexed), mode="callers", symbol="helper", depth=2)
    assert callers["depth_applied"] == 2
    assert [row["depth"] for row in callers["architecture"]["callers"]] == [1, 2]
    impact = _architecture(str(indexed), mode="impact")
    assert _picked(impact, {"affected_symbols": []}) == {"affected_symbols": []}
    assert "affected_symbols_note" in impact
