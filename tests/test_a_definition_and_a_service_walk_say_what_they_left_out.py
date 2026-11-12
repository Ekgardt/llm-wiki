"""A definition list and a service walk say what they left out (law 9).

`definition_sites` showed five definitions of a shared name and said nothing of
the rest; `_service_walk` cut its rows and its frontier at FLOW_MAX_ROWS without
a mark. Both now report the cut, over a real generation. See
`docs/research/2026-09-27-a-cut-says-what-it-left-out.md`.
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

from tests.flow_and_service_edges_helpers import (  # noqa: E402
    _service,  # noqa: E402,F401
    indexed_flows,  # noqa: E402,F401
)

SHARED_NAME_MODULES = 7


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True, timeout=60)


def _shared_name_repository(root: Path) -> Path:
    root.mkdir(parents=True)
    _git(root, "init", "-q", ".")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "test")
    (root / "pkg").mkdir()
    (root / "pkg/__init__.py").write_bytes(b"")
    for index in range(SHARED_NAME_MODULES):
        (root / f"pkg/m{index}.py").write_bytes(f"def frob():\n    return {index}\n".encode())
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "initial")
    return root


@pytest.fixture(scope="module")
def shared_name(tmp_path_factory):
    base = tmp_path_factory.mktemp("shared-name")
    root = base / "vault"
    (root / "knowledge/notes").mkdir(parents=True)
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
    repository = _shared_name_repository(base / "repo")
    answer = repository_indexing.index_repository(repository, state_root=state)
    assert answer["status"] == "indexed", answer
    yield repository
    evidence_reader_cache.clear()
    patch.undo()


def test_a_definition_list_says_how_many_definitions_it_left_out(shared_name) -> None:
    from symbol_snippet import MAX_LOCATIONS, definition_report

    report = definition_report(shared_name, "frob", time.monotonic() + 30)

    assert (len(report["sites"]), report["sites_omitted"]) == (
        MAX_LOCATIONS,
        SHARED_NAME_MODULES - MAX_LOCATIONS,
    )


def test_the_architecture_answer_carries_the_definitions_left_out(shared_name) -> None:
    import mcp_server

    request = {"resolved": shared_name, "symbol": "frob", "deadline": time.monotonic() + 30}

    answer = mcp_server.architecture_definition(request)

    assert answer["definition_omitted"] == SHARED_NAME_MODULES - len(answer["definition"])


def test_a_service_walk_says_when_it_was_cut(indexed_flows, monkeypatch) -> None:  # noqa: F811 - the fixture

    monkeypatch.setattr("llm_wiki.code_graph_flows.FLOW_MAX_ROWS", 1)

    answer = _service(indexed_flows, "fetch", depth=2)

    assert (len(answer["hops"]), answer["hops_truncated"]) == (1, True)
    assert answer["hop_count"] > 1


def test_a_whole_service_walk_says_it_is_whole(indexed_flows) -> None:  # noqa: F811 - the fixture
    answer = _service(indexed_flows, "fetch", depth=1)

    assert (answer["hop_count"], answer["hops_truncated"]) == (len(answer["hops"]), False)
