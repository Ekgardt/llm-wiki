"""Audit 3, A5-A7: names are resolved, cut and sliced before the reader is asked.

Research: `docs/research/2026-09-17-a-name-is-resolved-before-the-graph-is-asked.md`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

COMMON_NAME_FILES = 25
SAME_NAME_METHODS = 520

CORE = (
    "def helper():\n"
    "    return 1\n"
    "\n"
    "\n"
    "def _private():\n"
    "    return helper()\n"
    "\n"
    "\n"
    "def top():\n"
    "    return _private()\n"
)


def _holder(index: int) -> str:
    return f"class Holder{index}:\n    def close(self):\n        return helper()\n\n\n"


def _wide_module() -> str:
    body = "".join(_holder(index) for index in range(SAME_NAME_METHODS))
    return "def helper():\n    return 1\n\n\n" + body


def _route(index: int) -> str:
    return f'@app.get("/r{index}")\ndef route_{index}():\n    return {index}\n\n\n'


def _routes_module() -> str:
    body = "".join(_route(index) for index in range(SAME_NAME_METHODS))
    return "from fastapi import FastAPI\n\napp = FastAPI()\n\n\n" + body


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)


def _repository(root: Path) -> Path:
    package = root / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_bytes(b"")
    (package / "core.py").write_bytes(CORE.encode("utf-8"))
    (package / "wide.py").write_bytes(_wide_module().encode("utf-8"))
    (package / "routes.py").write_bytes(_routes_module().encode("utf-8"))
    for index in range(COMMON_NAME_FILES):
        text = "def main():\n    return 0\n"
        (package / f"entry_{index:02d}.py").write_bytes(text.encode("utf-8"))
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "test")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "initial")
    return root


@pytest.fixture(scope="module")
def indexed(built):
    return built[0]


@pytest.fixture(scope="module")
def index_answer(built):
    return built[1]


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """The indexed repository and what the index build answered."""
    base = tmp_path_factory.mktemp("names-before-graph")
    vault = base / "vault"
    (vault / "knowledge/notes").mkdir(parents=True)
    (vault / "knowledge/projects").mkdir(parents=True)
    state = base / "state"
    state.mkdir()
    patch = pytest.MonkeyPatch()
    patch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    patch.setenv("LLM_WIKI_ROOT", str(vault))
    import evidence_reader_cache
    import memory_state
    import repository_index

    patch.setattr(memory_state, "ROOT", vault, raising=False)
    patch.setattr(memory_state, "STATE_ROOT", state, raising=False)
    evidence_reader_cache.clear()
    repository = _repository(base / "repo")
    answer = repository_index.index_repository(repository, state_root=state)
    assert answer["status"] == "indexed", answer
    yield repository, answer
    evidence_reader_cache.clear()
    patch.undo()


def test_more_routes_than_one_reader_call_takes_still_get_a_hint_file(index_answer):
    hints = index_answer["hints"]
    assert (hints["status"], hints.get("routes")) == ("written", SAME_NAME_METHODS)


def _cut(answer: dict) -> tuple:
    return (
        answer["symbol_resolved"],
        answer["resolved_symbol_nodes"],
        answer.get("matching_symbol_nodes"),
        answer.get("symbol_nodes_truncated"),
    )


def test_a_common_name_is_cut_to_the_seed_limit_and_says_so(indexed):
    import code_graph

    answers = (
        code_graph.find_dependencies("main", indexed, with_report=True),
        code_graph.find_argument_flows("main", indexed),
        code_graph.find_service_paths("main", indexed),
        code_graph.find_callers("main", indexed, with_report=True, max_depth=2),
    )
    assert [_cut(answer) for answer in answers] == [(True, 20, COMMON_NAME_FILES, True)] * 4


def test_a_rare_name_carries_no_cut_fields(indexed):
    import code_graph

    answer = code_graph.find_dependencies("top", indexed, with_report=True)
    assert _cut(answer) == (True, 1, None, None)


def test_a_path_is_found_between_two_names_even_private_ones(indexed):
    import code_graph

    answer = code_graph.find_paths("top", "helper", indexed, with_report=True)
    private = code_graph.find_paths("_private", "helper", indexed, with_report=True)
    found = (
        [path["depth"] for path in answer["paths"]],
        [path["depth"] for path in private["paths"]],
        answer["source_resolved_nodes"],
        answer["target_resolved_nodes"],
    )
    assert found == ([2], [1], 1, 2)


def test_more_same_name_nodes_than_one_reader_call_takes_are_all_asked(indexed):
    import code_graph

    callees = code_graph.find_callees("close", indexed, with_report=True)
    callers = code_graph.find_callers("helper", indexed, with_report=True)
    counted = (len(callees["callees"]), len(callers["callers"]))
    assert counted == (SAME_NAME_METHODS, SAME_NAME_METHODS + 1)
