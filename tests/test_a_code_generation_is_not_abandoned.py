"""A code generation, never activated by design, is neither collected nor mistaken for memory.

The nightly prune removed the vault's code generation as abandoned, and the refresh had
been asking the vault's memory generation for code roots it never has. Research:
`docs/research/2026-09-15-a-code-generation-is-not-abandoned.md`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
for directory in (TESTS.parent / "scripts", TESTS):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from test_an_abandoned_publication_is_collected import _age  # noqa: E402
from test_generation_catalog import _scope_at_commit  # noqa: E402
from test_prune_generations import _catalog, _chain, _footprint, _publish  # noqa: E402
from test_repository_index import ALPHA, _repository, vault  # noqa: E402,F401


def _with_code(directory: Path, scope: dict) -> None:
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update({"repository_scope": scope, "code_roots": ["scripts"]})
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")), encoding="utf-8")


def test_a_day_old_code_generation_survives_the_prune_that_removes_an_abandoned_publication(tmp_path):
    import prune_generations

    catalog = _catalog(tmp_path)
    _chain(catalog, ["gen-1", "gen-2"])
    for name in ("gen-abandoned", "gen-code"):
        _publish(catalog, name, parent="gen-2")
    _with_code(catalog.generations_path / "gen-code", _scope_at_commit(tmp_path, "a" * 40).as_dict())
    for name in ("gen-abandoned", "gen-code"):
        catalog.register(name)
        _age(catalog.generations_path / name)

    prune_generations.prune_generations(state_root=catalog.state_root, apply=True)

    assert (_footprint(catalog, "gen-abandoned")[:2], _footprint(catalog, "gen-code")[:2]) == ((False, 0), (True, 1))


def test_detection_reads_the_code_generation_when_a_memory_one_is_newer(vault):  # noqa: F811 - the fixture
    import generation_catalog
    import repository_index
    from repository_scope import resolve_repository_scope

    root, state = vault
    _repository(root, {"scripts/alpha.py": ALPHA})
    receipt = repository_index.index_repository(root, roots=["scripts"], state_root=state)
    catalog = generation_catalog.GenerationCatalog(state)
    _publish(catalog, "gen-memory", repository_scope=resolve_repository_scope(root).as_dict())
    catalog.register("gen-memory")

    detected = repository_index.detect_repository_changes(root, state_root=state)

    assert (detected["generation_id"], detected["code_roots"], detected["stale"]) == (receipt["generation_id"], ["scripts"], False)
