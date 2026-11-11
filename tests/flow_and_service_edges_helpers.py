"""Helpers shared by test_flow_and_service_edges.py and the test files importing them."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

CORE = (
    "def store(record, index):\n"
    "    return (record, index)\n"
    "\n"
    "\n"
    "def keep(payload, position):\n"
    "    return store(payload, index=position)\n"
    "\n"
    "\n"
    "def receive(message):\n"
    "    return keep(message, 0)\n"
)


SERVICE = (
    "from fastapi import APIRouter\n"
    "\n"
    "from pkg.core import keep\n"
    "\n"
    "router = APIRouter()\n"
    "\n"
    "\n"
    '@router.get("/items")\n'
    "def list_items(limit):\n"
    "    return keep(limit, 0)\n"
)


CLIENT = (
    "import requests\n"
    "\n"
    "\n"
    "def fetch(cursor):\n"
    '    return requests.get("https://service.example/items", params=cursor)\n'
)


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)


def _make_repository(root: Path) -> Path:
    root.mkdir(parents=True)
    _git(root, "init", "-q", ".")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "test")
    (root / "pkg").mkdir()
    (root / "pkg/__init__.py").write_bytes(b"")
    (root / "pkg/core.py").write_bytes(CORE.encode("utf-8"))
    (root / "pkg/service.py").write_bytes(SERVICE.encode("utf-8"))
    (root / "pkg/client.py").write_bytes(CLIENT.encode("utf-8"))
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "initial")
    return root


@pytest.fixture(scope="module")
def indexed_flows(tmp_path_factory):
    base = tmp_path_factory.mktemp("flow-edges")
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
    import repository_indexing

    patch.setenv("LLM_WIKI_ROOT", str(root))
    patch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    evidence_reader_cache.clear()
    repository = _make_repository(base / "repo")
    answer = repository_indexing.index_repository(repository, state_root=state)
    assert answer["status"] == "indexed", answer
    yield repository
    evidence_reader_cache.clear()
    patch.undo()


def _service(repository: Path, symbol: str, depth: int | None = None) -> dict:
    from code_graph import find_service_paths

    return find_service_paths(symbol, repository, max_depth=depth)
