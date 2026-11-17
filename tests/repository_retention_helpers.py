"""Helpers shared by test_repository_retention.py and the test files importing them."""
from __future__ import annotations

from pathlib import Path


def _generation_ids(state: Path) -> set[str]:
    import repository_indexing

    catalog = repository_indexing.open_catalog(state, read_only=True)
    return {identifier for identifier, _at, _manifest in catalog.registered_manifests()}
