"""Helpers shared by test_a_question_is_answered_by_its_own_kind_of_generation.py and the test files importing them."""
from __future__ import annotations

import json
from pathlib import Path

from tests.generation_catalog_helpers import _publish_v2


def _scope_of(root: Path) -> dict:
    from repository_scope import resolve_repository_scope

    return resolve_repository_scope(root).as_dict()


def _rewrite_manifest(directory: Path, fields: dict) -> None:
    path = directory / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest.update(fields)
    path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )


def _active_memory_generation(catalog, root: Path, name: str) -> None:
    """A real memory generation of this checkout, activated as the vault's is."""
    _publish_v2(catalog, name)
    _rewrite_manifest(catalog.generations_path / name, {"repository_scope": _scope_of(root)})
    catalog.register(name)
    catalog.activate(name, expected_active=None)
