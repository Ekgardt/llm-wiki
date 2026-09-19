"""Audit 3, B3: the two flow walks are bounded in time, not only by seeds.

`find_argument_flows` and `find_service_paths` took no deadline, so the MCP
dispatcher discarded its own (`del deadline`) and a walk kept one of four worker
slots after its caller had given up. Research:
`docs/research/2026-09-18-graph-a-flow-walk-stops-when-its-caller-has.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import code_graph  # noqa: E402

SOURCE = b"def caller():\n    callee()\n"


def _repository_with_a_generation(tmp_path, monkeypatch) -> Path:
    from generation_catalog import GenerationCatalog
    from repository_scope import resolve_repository_scope

    from tests.test_evidence_graph_recovery import _publish, _rich_graph_records

    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "app.py").write_bytes(SOURCE)
    catalog = GenerationCatalog(tmp_path / "state")
    _publish(
        catalog,
        "active",
        graph_records=_rich_graph_records(),
        repository_scope=resolve_repository_scope(repository).as_dict(),
    )
    catalog.register("active")
    catalog.activate("active", expected_active=None)
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(catalog.state_root))
    return repository


def _cancel_after_the_seeds(monkeypatch):
    """A caller that gives up once the generation is open and the seeds are in."""
    state = {"give_up": False}
    resolve = code_graph._dependency_seed_nodes

    def watched(graph, symbol):
        seeds = resolve(graph, symbol)
        state["give_up"] = True
        return seeds

    monkeypatch.setattr(code_graph, "_dependency_seed_nodes", watched)
    return lambda: state["give_up"]


def test_an_argument_flow_walk_stops_when_its_caller_gives_up(tmp_path, monkeypatch):
    repository = _repository_with_a_generation(tmp_path, monkeypatch)
    cancelled = _cancel_after_the_seeds(monkeypatch)

    with pytest.raises(TimeoutError, match="cancelled"):
        code_graph.find_argument_flows("callee", repository, cancelled=cancelled)


def test_a_service_path_walk_stops_when_its_caller_gives_up(tmp_path, monkeypatch):
    repository = _repository_with_a_generation(tmp_path, monkeypatch)
    cancelled = _cancel_after_the_seeds(monkeypatch)

    with pytest.raises(TimeoutError, match="cancelled"):
        code_graph.find_service_paths("callee", repository, cancelled=cancelled)


def test_an_expired_deadline_stops_both_walks(tmp_path, monkeypatch):
    repository = _repository_with_a_generation(tmp_path, monkeypatch)
    expired = -1.0

    with pytest.raises(TimeoutError, match="deadline"):
        code_graph.find_argument_flows("callee", repository, deadline=expired)
    with pytest.raises(TimeoutError, match="deadline"):
        code_graph.find_service_paths("callee", repository, deadline=expired)
