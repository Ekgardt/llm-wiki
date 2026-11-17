"""Helpers shared by test_prune_generations.py and the test files importing them."""
from __future__ import annotations

import sqlite3
from contextlib import closing

from tests.generation_catalog_helpers import _publish


def _chain(catalog, names: list[str]) -> None:
    """Publish and activate a linear parent chain, oldest first."""
    previous = None
    for name in names:
        _publish(catalog, name, parent=previous)
        catalog.register(name)
        assert catalog.activate(name, expected_active=previous) is True
        previous = name


def _rows(catalog, table: str, identifier: str) -> int:
    with closing(sqlite3.connect(catalog.catalog_path)) as database:
        return database.execute(
            f"SELECT COUNT(*) FROM {table} WHERE generation_id = ?", (identifier,)
        ).fetchone()[0]


def _footprint(catalog, identifier: str) -> tuple[bool, int, int]:
    """Directory, registration and activation history, as one observation."""
    return (
        (catalog.generations_path / identifier).is_dir(),
        _rows(catalog, "generations", identifier),
        _rows(catalog, "activation_history", identifier),
    )


def _present(catalog, names: list[str]) -> list[bool]:
    return [(catalog.generations_path / name).is_dir() for name in names]
