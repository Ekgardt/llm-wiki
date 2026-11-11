"""One checkout carries two kinds of generation, and each answers its own questions.

Third audit, findings G-M3, G-M9 and G-L10. Research:
`docs/research/2026-09-17-a-question-is-answered-by-its-own-kind-of-generation.md`.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
for directory in (TESTS.parent / "scripts", TESTS):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))


from tests.a_question_is_answered_by_its_own_kind_of_generation_helpers import (  # noqa: E402
    _active_memory_generation,
    _rewrite_manifest,
    _scope_of,
)
from tests.generation_catalog_helpers import _catalog, _publish  # noqa: E402
from tests.repository_index_helpers import (  # noqa: E402,F401
    ALPHA,
    _git,
    _repository,
    vault,  # noqa: E402,F401
)


def _name_source_manifest(directory: Path, code_roots: list[str]) -> None:
    """A generation of the shape built before the manifest named its roots (G-M9)."""
    from reliable_memory import canonical_json_bytes

    payload = canonical_json_bytes(
        {"policy": {"code_roots": code_roots, "daily_paths": []}}
    )
    (directory / "source-manifest.json").write_bytes(payload)
    artifact = {
        "path": "source-manifest.json",
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    path = directory / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["artifacts"] = sorted(
        [*manifest["artifacts"], artifact], key=lambda item: item["path"]
    )
    path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )


def _registered_code_generation(catalog, name: str, root: Path, named: bool) -> None:
    _publish(catalog, name)
    directory = catalog.generations_path / name
    _rewrite_manifest(directory, {"repository_scope": _scope_of(root)})
    if named:
        _rewrite_manifest(directory, {"code_roots": ["scripts"]})
    else:
        _name_source_manifest(directory, ["scripts"])
    catalog.register(name)


def test_a_memory_question_is_not_answered_from_a_code_generation(tmp_path):
    """With an empty pointer the scoped fallback must not hand back code."""
    from repository_scope import resolve_repository_scope

    catalog = _catalog(tmp_path)
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "-q")
    _registered_code_generation(catalog, "gen-code", root, named=True)
    scope = resolve_repository_scope(root)

    identifier, _manifest = catalog.code_generation_for_repository(scope)

    assert (catalog.get_active_for_repository(scope), identifier) == (None, "gen-code")


def test_a_generation_built_before_the_field_still_says_it_holds_code(tmp_path):
    """G-M9: the snapshot policy has carried the roots all along."""
    import prune_generations
    from repository_scope import resolve_repository_scope

    catalog = _catalog(tmp_path)
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "-q")
    _registered_code_generation(catalog, "gen-old", root, named=False)

    identifier, _manifest = catalog.code_generation_for_repository(
        resolve_repository_scope(root)
    )

    assert (identifier, prune_generations.memory_publications(catalog)) == (
        "gen-old",
        set(),
    )


def _opened_generation_id(catalog, root: Path) -> str | None:
    from evidence_graph import EvidenceGraph
    from repository_scope import resolve_repository_scope

    graph = EvidenceGraph.open_code_for_repository(
        catalog, resolve_repository_scope(root)
    )
    if graph is None:
        return None
    with graph:
        return graph.database_path.parent.name


def _two_indexed_generations(root: Path, state: Path) -> tuple[str, str]:
    import repository_indexing

    first = repository_indexing.index_repository(root, roots=["scripts"], state_root=state)
    (root / "scripts/beta.py").write_text(ALPHA, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "second")
    second = repository_indexing.index_repository(root, roots=["scripts"], state_root=state)
    return first["generation_id"], second["generation_id"]


def test_a_code_reader_walks_on_to_the_generation_retention_kept(vault, tmp_path):  # noqa: F811
    """G-L10: the second kept code generation is a reader's fallback, not dead weight."""
    from llm_wiki import generation_catalog_store

    _root, state = vault
    repository = _repository(tmp_path / "repo", {"scripts/alpha.py": ALPHA})
    first, second = _two_indexed_generations(repository, state)
    catalog = generation_catalog_store.GenerationCatalog(state)
    damaged = catalog.generations_path / second / "evidence.sqlite3"
    damaged.write_bytes(damaged.read_bytes() + b"torn")

    opened = _opened_generation_id(catalog, repository)

    assert (opened, first != second) == (first, True)


def test_a_damaged_code_generation_is_not_answered_from_the_memory_pointer(
    vault, tmp_path  # noqa: F811
):
    """G-M3 B: no usable code index is an empty answer, never the memory generation."""
    import code_graph
    import repository_indexing
    from llm_wiki import generation_catalog_store

    _root, state = vault
    repository = _repository(tmp_path / "repo", {"scripts/alpha.py": ALPHA})
    receipt = repository_indexing.index_repository(
        repository, roots=["scripts"], state_root=state
    )
    catalog = generation_catalog_store.GenerationCatalog(state)
    _active_memory_generation(catalog, repository, "gen-memory")
    damaged = catalog.generations_path / receipt["generation_id"] / "evidence.sqlite3"
    damaged.write_bytes(damaged.read_bytes() + b"torn")

    opened = code_graph.active_evidence_graph(repository, read_only=True)

    assert opened is None
