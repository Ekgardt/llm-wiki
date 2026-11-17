from __future__ import annotations

import importlib

import pytest

OWNED_NAMES = [
    ("llm_wiki.generation_artifact_scan", "ArtifactScan"),
    ("llm_wiki.generation_artifact_scan", "validate_artifact_databases"),
    ("llm_wiki.generation_catalog_store", "GenerationCatalog"),
    ("llm_wiki.generation_descriptors", "hash_descriptor"),
    ("llm_wiki.generation_descriptors", "is_link_or_reparse"),
    ("llm_wiki.generation_descriptors", "open_read_descriptor"),
    ("llm_wiki.generation_descriptors", "windows_long_path"),
    ("llm_wiki.generation_descriptors", "windows_read_descriptor"),
    ("llm_wiki.generation_descriptors", "windows_read_handle"),
    ("llm_wiki.generation_fields", "checked_generation_id"),
    ("llm_wiki.generation_limits", "BUSY_MS"),
    ("llm_wiki.generation_limits", "CLEANUP_CATALOG_FENCE_SECONDS"),
    ("llm_wiki.generation_limits", "COMMIT_BUSY_FLOOR_MS"),
    ("llm_wiki.generation_limits", "HASH_CHUNK_BYTES"),
    ("llm_wiki.generation_limits", "MAX_ARTIFACT_BYTES"),
    ("llm_wiki.generation_limits", "MAX_ARTIFACTS"),
    ("llm_wiki.generation_limits", "MAX_CATALOG_BYTES"),
    ("llm_wiki.generation_limits", "MAX_GENERATION_BYTES"),
    ("llm_wiki.generation_limits", "MAX_GENERATION_CHILDREN"),
    ("llm_wiki.generation_limits", "MAX_MANIFEST_BYTES"),
    ("llm_wiki.generation_limits", "RETAINED_ANCESTOR_GENERATIONS"),
    ("llm_wiki.generation_limits", "UNBOUNDED_BUSY_MS"),
    ("llm_wiki.generation_lineage", "ABANDONED_AFTER_SECONDS"),
    ("llm_wiki.generation_lineage", "untouched_for"),
    ("llm_wiki.generation_seal", "GenerationSealCapability"),
    ("llm_wiki.generation_seal", "HeldFile"),
    ("llm_wiki.generation_seal", "bounded_scandir"),
    ("llm_wiki.generation_seal", "listed_generation_files"),
    ("llm_wiki.generation_validation", "validate_generation"),
]


@pytest.mark.parametrize(("owner", "name"), OWNED_NAMES)
def test_the_catalog_facade_exposes_the_owning_module_object(owner: str, name: str) -> None:
    facade = importlib.import_module("generation_catalog")
    assert getattr(facade, name) is getattr(importlib.import_module(owner), name)


def test_the_catalog_facade_activates_through_the_store_catalog() -> None:
    facade = importlib.import_module("generation_catalog")
    assert callable(facade.GenerationCatalog.activate)
    assert facade.RETAINED_ANCESTOR_GENERATIONS >= 1
