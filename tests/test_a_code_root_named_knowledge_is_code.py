"""`knowledge/` is this vault's noun, not a fact about every repository (G-M5).

Another repository's tracked top-level `knowledge/` directory is one of its code
roots, so a module there is code: functions, calls, callers. Research:
`docs/research/2026-09-17-a-question-is-answered-by-its-own-kind-of-generation.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
for directory in (TESTS.parent / "scripts", TESTS):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from test_repository_index import ALPHA, _repository, vault  # noqa: E402,F401


def test_a_module_under_a_repositorys_knowledge_root_is_extracted_as_code(
    vault, tmp_path  # noqa: F811
):
    import repository_index
    from code_graph import find_callers

    _root, state = vault
    repository = _repository(tmp_path / "repo", {"knowledge/alpha.py": ALPHA})

    receipt = repository_index.index_repository(
        repository, roots=["knowledge"], state_root=state
    )
    answer = find_callers("helper", repository, with_report=True)

    assert (
        answer["source_generation"],
        [row["qualified_name"] for row in answer["callers"]],
    ) == (receipt["generation_id"], ["knowledge.alpha.caller"])
