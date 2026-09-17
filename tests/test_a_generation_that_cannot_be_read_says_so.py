"""Audit 3, B26: an unreadable generation is not an absent one.

`_active_evidence_graph` used to answer `None` for "this repository has no
generation", for "it has one that cannot be opened" and for a plain `TypeError`
inside the opening path. Research:
`docs/research/2026-09-17-graph-a-generation-that-cannot-be-read-is-not-a-generation-that-is-absent.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import code_graph  # noqa: E402

SOURCE = b"def caller():\n    callee()\n"


def _repository_with_a_generation(tmp_path, monkeypatch):
    """A real published generation for a real checkout, as the product opens it."""
    from generation_catalog import GenerationCatalog
    from repository_scope import resolve_repository_scope

    from tests.test_evidence_graph_recovery import _publish, _rich_graph_records

    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "app.py").write_bytes(SOURCE)
    catalog = GenerationCatalog(tmp_path / "state")
    scope = resolve_repository_scope(repository)
    _publish(
        catalog,
        "active",
        graph_records=_rich_graph_records(),
        repository_scope=scope.as_dict(),
    )
    catalog.register("active")
    catalog.activate("active", expected_active=None)
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(catalog.state_root))
    return repository, Path(catalog.catalog_path)


def _report(answer: dict) -> tuple:
    return answer["fallback"], answer.get("fallback_reason")


def test_a_corrupt_catalog_is_not_reported_as_an_absent_generation(
    tmp_path, monkeypatch
):
    repository, catalog_path = _repository_with_a_generation(tmp_path, monkeypatch)
    before = code_graph.find_callers("callee", repository, with_report=True)

    catalog_path.write_bytes(b"this is not a database\n" * 64)
    after = code_graph.find_callers("callee", repository, with_report=True)

    assert _report(before) == (False, None)
    assert _report(after) == (True, "generation_unreadable:DatabaseError")


def test_every_live_fallback_says_why_it_fell_back(tmp_path, monkeypatch):
    repository, catalog_path = _repository_with_a_generation(tmp_path, monkeypatch)
    catalog_path.write_bytes(b"this is not a database\n" * 64)
    reasons = {
        _report(code_graph.find_callees("caller", repository, with_report=True)),
        _report(code_graph.find_dead_code(repository, with_report=True)),
        _report(code_graph.get_architecture(repository)),
        _report(code_graph.detect_communities(repository, with_report=True)),
    }

    assert reasons == {(True, "generation_unreadable:DatabaseError")}


def test_a_repository_with_no_generation_still_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "state"))
    repository = tmp_path / "plain"
    repository.mkdir()
    (repository / "app.py").write_bytes(SOURCE)

    answer = code_graph.find_callers("callee", repository, with_report=True)

    assert _report(answer) == (True, "no_generation")


def test_a_programming_error_in_the_opening_path_is_not_swallowed(monkeypatch):
    def broken(*_args, **_options):
        raise TypeError("wrong keyword")

    monkeypatch.setattr(code_graph, "_leased_active_graph", broken)

    with pytest.raises(TypeError, match="wrong keyword"):
        code_graph._active_evidence_graph(Path("."))
