"""The hint table an index build exports, and its repair on refresh (#24, C1)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from tests import test_repository_index as _index_tests  # noqa: E402
from tests.test_repository_index import ALPHA, _repository  # noqa: E402

vault = _index_tests.vault


@pytest.fixture(autouse=True)
def _isolated_reader_cache():
    import evidence_reader_cache

    evidence_reader_cache.clear()
    yield
    evidence_reader_cache.clear()


def _checkout_id(repository: Path) -> str:
    from repository_scope import resolve_repository_scope

    return resolve_repository_scope(repository).checkout_id


def test_an_index_build_exports_the_symbols_its_generation_holds(vault, tmp_path):
    import code_hints
    import repository_index

    _root, state = vault
    repository = _repository(tmp_path / "repo", {"pkg/alpha.py": ALPHA})

    receipt = repository_index.index_repository(repository, state_root=state)
    answer = code_hints.lookup_symbol(state, _checkout_id(repository), "helper")

    assert (receipt["hints"]["status"], receipt["hints"]["generation_id"]) == ("written", receipt["generation_id"])
    assert (answer["total"], answer["rows"][0][:4]) == (1, ("pkg.alpha.helper", "function", "pkg/alpha.py", 1))
    assert answer["rows"][0][4] >= 1  # `caller` calls it: one resolved inbound edge


def test_a_fresh_refresh_repairs_a_missing_table_and_keeps_a_current_one(vault, tmp_path):
    import code_hints
    import repository_index

    _root, state = vault
    repository = _repository(tmp_path / "repo", {"pkg/alpha.py": ALPHA})
    built = repository_index.index_repository(repository, state_root=state)

    current = repository_index.refresh_repository(repository, state_root=state)["hints"]
    code_hints.remove_hints(state, _checkout_id(repository))
    repaired = repository_index.refresh_repository(repository, state_root=state)["hints"]

    assert (current["status"], repaired["status"]) == ("current", "written")
    assert repaired["generation_id"] == built["generation_id"]


def test_a_malformed_checkout_id_names_no_file(tmp_path):
    import code_hints

    with pytest.raises(ValueError):
        code_hints.hints_path(tmp_path, "checkout:../../etc")
    assert code_hints.remove_hints(tmp_path, "not-a-checkout") is False


def test_a_foreign_or_stale_table_answers_nothing(tmp_path):
    import code_hints

    checkout_id = "checkout:" + "1" * 64
    other = {**_meta(checkout_id), "checkout_id": "checkout:" + "2" * 64}
    code_hints.write_hints(tmp_path, {**other, "checkout_id": checkout_id, "schema_version": "code-hints/v0"}, [])
    assert code_hints.lookup_symbol(tmp_path, checkout_id, "helper") is None


def _meta(checkout_id: str) -> dict[str, str]:
    return {
        "schema_version": "code-hints/v1",
        "generation_id": "generation-x",
        "repository_id": "repository:" + "0" * 64,
        "checkout_id": checkout_id,
        "checkout_root": "/nowhere",
        "git_commit": "",
        "symbols": "0",
        "written_at": "0",
    }
