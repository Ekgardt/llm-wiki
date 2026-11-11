"""Helpers shared by test_generation_maintenance.py and the test files importing them."""
from __future__ import annotations

from pathlib import Path


def _generation_directory(state_root: Path, result: dict) -> Path:
    """The built generation, naming the whole result when there is none."""
    assert result.get("generation_id"), result
    return (
        state_root / "cache" / "evidence-graph" / "generations" / result["generation_id"]
    )


def _vault(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "vault"
    state = tmp_path / "state"
    (root / "knowledge" / "notes").mkdir(parents=True)
    (root / "knowledge" / "projects").mkdir(parents=True)
    (state / "cache").mkdir(parents=True)
    (state / "run").mkdir(parents=True)
    return root, state


def _empty_generation(
    state: Path,
    generation_id: str,
    *,
    parent: str | None = None,
    root: Path | None = None,
    extractor_version: str | None = None,
):
    import generation_maintenance
    from evidence_graph_builder import build_full_generation
    from llm_wiki.generation_catalog_store import GenerationCatalog
    from repository_scope import resolve_repository_scope

    catalog = GenerationCatalog(state)
    if root is not None and extractor_version is None:
        extractor_version = generation_maintenance.maintenance_extractor_identity()
    return build_full_generation(
        catalog,
        sources=(),
        source_bytes={},
        nodes=(),
        occurrences=(),
        assertions=(),
        evidence=(),
        observations=(),
        dependencies=(),
        generation_id=generation_id,
        parent_generation_id=parent,
        expected_active=parent,
        repository_scope=None if root is None else resolve_repository_scope(root),
        **({"extractor_version": extractor_version} if extractor_version else {}),
    )
