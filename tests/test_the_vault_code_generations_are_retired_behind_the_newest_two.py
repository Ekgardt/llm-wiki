"""The vault's code generations are retired behind the newest two; its memory is left alone.

The pruner never judges a generation that holds code, and retention skipped the
vault's checkout, so nothing collected the vault's code generations. Research:
`docs/research/2026-09-17-the-vault-code-generations-have-a-collector.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
for directory in (TESTS.parent / "scripts", TESTS):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from test_prune_generations import _publish  # noqa: E402
from test_repository_index import ALPHA, _repository  # noqa: E402
from test_repository_refresh import _isolated_reader_cache, adopted_vault  # noqa: E402,F401
from test_repository_retention import _generation_ids  # noqa: E402


def _edited(root: Path, name: str) -> None:
    (root / "scripts" / f"{name}.py").write_text(f"def {name}():\n    return 1\n", encoding="utf-8")


def _three_code_generations(root: Path, state: Path) -> list[str]:
    import repository_index

    _repository(root, {"scripts/alpha.py": ALPHA})
    first = repository_index.index_repository(root, roots=["scripts"], state_root=state)["generation_id"]
    _edited(root, "beta")
    second = repository_index.refresh_repository(root, state_root=state)["generation_id"]
    _edited(root, "gamma")
    third = repository_index.refresh_repository(root, state_root=state)["generation_id"]
    return [first, second, third]


def _memory_publication(root: Path, state: Path) -> str:
    import generation_catalog
    from repository_scope import resolve_repository_scope

    catalog = generation_catalog.GenerationCatalog(state)
    _publish(catalog, "gen-memory", repository_scope=resolve_repository_scope(root).as_dict())
    catalog.register("gen-memory")
    return "gen-memory"


def test_the_vault_keeps_its_newest_two_code_generations_and_its_memory(adopted_vault):  # noqa: F811
    import repository_retention

    root, state = adopted_vault
    first, second, third = _three_code_generations(root, state)
    memory = _memory_publication(root, state)

    answer = repository_retention.retire_repositories(state_root=state)

    assert [(entry["kept"], entry["retire"]) for entry in answer["checkouts"]] == [([third, second], [first])]
    assert _generation_ids(state) == {second, third, memory}


def test_the_vault_hint_table_is_not_an_orphan_while_a_code_generation_lives(adopted_vault):  # noqa: F811
    import code_hints
    import repository_index
    import repository_retention
    from repository_scope import resolve_repository_scope

    root, state = adopted_vault
    _repository(root, {"scripts/alpha.py": ALPHA})
    repository_index.index_repository(root, roots=["scripts"], state_root=state)
    checkout_id = resolve_repository_scope(root).checkout_id
    written = code_hints.hints_path(state, checkout_id).exists()

    answer = repository_retention.retire_repositories(state_root=state)

    assert (written, answer["orphan_hints_removed"], code_hints.hints_path(state, checkout_id).exists()) == (
        True,
        [],
        True,
    )
