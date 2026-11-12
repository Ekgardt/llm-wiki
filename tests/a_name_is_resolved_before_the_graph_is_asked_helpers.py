"""Helpers shared by test_a_name_is_resolved_before_the_graph_is_asked.py and the test files importing them."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

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
    import repository_indexing

    patch.setenv("LLM_WIKI_ROOT", str(vault))
    patch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    evidence_reader_cache.clear()
    repository = _repository(base / "repo")
    answer = repository_indexing.index_repository(repository, state_root=state)
    assert answer["status"] == "indexed", answer
    yield repository, answer
    evidence_reader_cache.clear()
    patch.undo()
