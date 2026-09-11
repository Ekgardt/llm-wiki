"""Issue #24, B3: argument bindings hop by hop over a real generation.

One small foreign repository is indexed once into a state root of its own,
and the walk is read through the same reader the MCP server uses. The answer
is argument binding, never data-flow analysis; the note says so and the tests
pin it (docs/research/2026-09-11-argument-bindings-and-route-calls.md).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

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
    import memory_state
    import repository_index

    patch.setattr(memory_state, "ROOT", root, raising=False)
    patch.setattr(memory_state, "STATE_ROOT", state, raising=False)
    evidence_reader_cache.clear()
    repository = _make_repository(base / "repo")
    answer = repository_index.index_repository(repository, state_root=state)
    assert answer["status"] == "indexed", answer
    yield repository
    evidence_reader_cache.clear()
    patch.undo()


def _flows(repository: Path, symbol: str, depth: int | None = None) -> dict:
    from code_graph import find_argument_flows

    return find_argument_flows(symbol, repository, max_depth=depth)


def _hops(answer: dict) -> list[tuple[int, str, str]]:
    return [(row["depth"], row["to"].rsplit(".", 1)[-1], row["bindings"]) for row in answer["flows"]]


def test_one_hop_names_the_argument_and_the_parameter_it_binds(indexed_flows):
    answer = _flows(indexed_flows, "keep", depth=1)

    assert _hops(answer) == [(1, "store", "payload->record,position->index")]
    assert (answer["symbol_resolved"], answer["depth_applied"], "argument binding" in answer["note"]) == (
        True,
        1,
        True,
    )


def test_a_second_hop_follows_the_binding_further(indexed_flows):
    answer = _flows(indexed_flows, "receive", depth=2)

    assert _hops(answer) == [
        (1, "keep", "message->payload"),
        (2, "store", "payload->record,position->index"),
    ]


def test_the_walk_says_how_far_it_went_and_whether_more_lies_past_it(indexed_flows):
    shallow = _flows(indexed_flows, "receive", depth=1)
    deep = _flows(indexed_flows, "receive", depth=8)

    assert (shallow["depth_applied"], shallow["depth_frontier_open"]) == (1, True)
    assert (deep["depth_applied"], deep["depth_frontier_open"]) == (8, False)


def _hops_of(answer: dict) -> list[tuple[int, str, str]]:
    return [(row["depth"], row["to"].rsplit(".", 1)[-1], row["relation"]) for row in answer["hops"]]


def _service(repository: Path, symbol: str, depth: int | None = None) -> dict:
    from code_graph import find_service_paths

    return find_service_paths(symbol, repository, max_depth=depth)


def test_a_client_call_crosses_the_route_into_the_handler(indexed_flows):
    answer = _service(indexed_flows, "fetch", depth=1)

    assert _hops_of(answer) == [
        (1, "GET /items", "http_calls"),
        (1, "list_items", "handled_by"),
    ]
    assert (answer["routes_crossed"], "route is crossed" in answer["note"]) == (1, True)


def test_the_walk_continues_inside_the_service_it_reached(indexed_flows):
    answer = _service(indexed_flows, "fetch", depth=2)
    reached = {name for _, name, _ in _hops_of(answer)}

    assert {"GET /items", "list_items", "keep"} <= reached


def test_an_unknown_symbol_answers_nothing_and_says_it_resolved_nothing(indexed_flows):
    answer = _flows(indexed_flows, "no_such_symbol")

    assert (answer["flows"], answer["symbol_resolved"], answer["resolved_symbol_nodes"]) == (
        [],
        False,
        0,
    )


ORDERS_SERVICE = (
    "from fastapi import APIRouter\n"
    "\n"
    "router = APIRouter()\n"
    "\n"
    "\n"
    '@router.get("/orders")\n'
    "def list_orders():\n"
    "    return []\n"
)

ORDERS_CLIENT = (
    "import requests\n"
    "\n"
    "\n"
    "def order_report():\n"
    '    return requests.get("https://orders.example/orders")\n'
)


def _indexed_repository(base: Path, name: str, files: dict[str, str], state: Path) -> Path:
    import repository_index

    root = base / name
    root.mkdir(parents=True)
    _git(root, "init", "-q", ".")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "test")
    (root / "pkg").mkdir()
    (root / "pkg/__init__.py").write_bytes(b"")
    for relative, content in files.items():
        (root / relative).write_bytes(content.encode("utf-8"))
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "initial")
    answer = repository_index.index_repository(root, state_root=state)
    assert answer["status"] == "indexed", answer
    return root


@pytest.fixture(scope="module")
def two_services(tmp_path_factory):
    """Two checkouts in one state root: the client's route lives in the other."""
    base = tmp_path_factory.mktemp("cross-repo")
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
    import memory_state

    patch.setattr(memory_state, "ROOT", root, raising=False)
    patch.setattr(memory_state, "STATE_ROOT", state, raising=False)
    evidence_reader_cache.clear()
    _indexed_repository(base, "service-repo", {"pkg/api.py": ORDERS_SERVICE}, state)
    client = _indexed_repository(base, "client-repo", {"pkg/report.py": ORDERS_CLIENT}, state)
    yield client
    evidence_reader_cache.clear()
    patch.undo()


def test_a_route_of_another_checkout_is_named_with_the_repository_it_lives_in(two_services):
    answer = _service(two_services, "order_report", depth=1)
    foreign = [row for row in answer["hops"] if row["relation"] == "handled_by_repository"]

    assert [(row["to"], row["route"]) for row in foreign] == [
        ("pkg.api.list_orders", "GET /orders")
    ]
    assert (answer["repositories_crossed"], foreign[0]["file"]) == (1, "pkg/api.py")


def test_a_foreign_hop_is_not_walked_further(two_services):
    """The other generation is not opened, so the hop names its repository and stops."""
    answer = _service(two_services, "order_report", depth=3)
    depths = {row["depth"] for row in answer["hops"] if row["relation"] == "handled_by_repository"}

    assert (depths, answer["depth_applied"]) == ({1}, 3)
