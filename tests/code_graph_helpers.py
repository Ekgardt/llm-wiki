"""Helpers shared by test_code_graph.py and the test files importing them."""
from __future__ import annotations


def _activate_graph(tmp_path, repository=None):
    from llm_wiki.generation_catalog_store import GenerationCatalog
    from repository_scope import resolve_repository_scope

    from tests.evidence_graph_recovery_helpers import _publish, _rich_graph_records

    catalog = GenerationCatalog(tmp_path / "state")
    scope = resolve_repository_scope(repository or tmp_path)
    _publish(
        catalog,
        "active",
        graph_records=_rich_graph_records(),
        repository_scope=scope.as_dict(),
    )
    catalog.register("active")
    catalog.activate("active", expected_active=None)
    return catalog
