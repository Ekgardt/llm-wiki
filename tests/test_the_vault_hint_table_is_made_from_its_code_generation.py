"""The vault's hint table is a projection of its code generation, not of its memory.

On the vault the active pointer names the memory generation of the same checkout,
which holds no symbols; the table was empty and rewritten on every refresh.
Research: `docs/research/2026-09-17-a-hint-table-is-made-from-the-code-generation.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
for directory in (TESTS.parent / "scripts", TESTS):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from test_repository_index import ALPHA, _repository  # noqa: E402
from test_repository_refresh import _isolated_reader_cache, adopted_vault  # noqa: E402,F401


def _vault_with_memory_and_code(root: Path, state: Path) -> tuple[str, str]:
    """An active memory generation first, then the code generation of the same checkout."""
    import doctor
    import repository_index

    (root / "knowledge/notes/page.md").write_text(
        "---\ntype: concept\n---\n# Page\n\nOne-sentence summary: a page.\n", encoding="utf-8"
    )
    _repository(root, {"scripts/alpha.py": ALPHA})
    memory = doctor.run_generation_maintenance(root=root, state_root=state, time_budget_seconds=300, max_sources=50)
    code = repository_index.index_repository(root, roots=["scripts"], state_root=state)["generation_id"]
    return code, memory["generation_id"]


def _hint_meta(root: Path, state: Path) -> dict:
    import code_hints
    from repository_scope import resolve_repository_scope

    return code_hints.read_meta(code_hints.hints_path(state, resolve_repository_scope(root).checkout_id))


def test_the_vault_hints_name_the_code_generation_and_stay_current(adopted_vault):  # noqa: F811
    import repository_index

    root, state = adopted_vault
    code, memory = _vault_with_memory_and_code(root, state)
    meta = _hint_meta(root, state)

    refreshed = repository_index.refresh_repository(root, state_root=state)["hints"]

    assert code != memory
    assert (meta["generation_id"], int(meta["symbols"]) > 0) == (code, True)
    assert refreshed == {"status": "current", "generation_id": code}
