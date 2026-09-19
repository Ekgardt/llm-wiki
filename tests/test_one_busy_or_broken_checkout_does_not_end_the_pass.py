"""One busy or broken checkout is a named row of the nightly pass, not its end.

Research: `docs/research/2026-09-17-one-checkout-does-not-end-the-pass.md`.
"""

from __future__ import annotations

import shutil
import sys
import threading
from contextlib import closing
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
for directory in (TESTS.parent / "scripts", TESTS):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from test_repository_index import ALPHA, _repository  # noqa: E402
from test_repository_refresh import _isolated_reader_cache, adopted_vault  # noqa: E402,F401

from tests.slow_machine import SHORT_TIMEOUT  # noqa: E402

BUSY_FILES = 40


class _Writer:
    """An agent that never stops writing in its checkout."""

    def __init__(self, repository: Path) -> None:
        self._targets = [repository / "pkg" / f"busy_{number}.py" for number in range(BUSY_FILES)]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._write, daemon=True)

    def _write(self) -> None:
        turn = 0
        while not self._stop.is_set():
            turn += 1
            target = self._targets[turn % BUSY_FILES]
            target.write_text(f"def busy():\n    return {turn}\n", encoding="utf-8")

    def __enter__(self) -> _Writer:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=SHORT_TIMEOUT)


def _busy_repository(path: Path) -> Path:
    files = {f"pkg/busy_{number}.py": "def busy():\n    return 0\n" for number in range(BUSY_FILES)}
    return _repository(path, files)


def _checkout_name(row: dict) -> str:
    """A refused row names `checkout_root`, a refreshed one `directory`."""
    return Path(str(row.get("checkout_root") or row["directory"])).name


def _rows_by_name(answer: dict) -> dict[str, tuple]:
    return {_checkout_name(row): (row.get("status"), row.get("reason")) for row in answer["repositories"]}


def test_a_checkout_being_written_is_refused_by_name_and_the_others_are_refreshed(adopted_vault, tmp_path):  # noqa: F811
    import repository_index

    _root, state = adopted_vault
    busy = _busy_repository(tmp_path / "busy")
    quiet = _repository(tmp_path / "quiet", {"pkg/alpha.py": ALPHA})
    for repository in (busy, quiet):
        repository_index.index_repository(repository, state_root=state)
    (quiet / "pkg" / "beta.py").write_text("def beta():\n    return 1\n", encoding="utf-8")

    with _Writer(busy):
        answer = repository_index.refresh_all_repositories(state_root=state, budget_seconds=120)

    assert _rows_by_name(answer) == {
        "busy": ("refused", "repository_changed_during_capture"),
        "quiet": ("refreshed", None),
    }


def test_a_registration_whose_tree_is_gone_is_refused_by_name(adopted_vault, tmp_path):  # noqa: F811
    import repository_index

    _root, state = adopted_vault
    repository = _repository(tmp_path / "gone", {"pkg/alpha.py": ALPHA})
    generation = repository_index.index_repository(repository, state_root=state)["generation_id"]
    shutil.rmtree(state / "cache" / "evidence-graph" / "generations" / generation)

    answer = repository_index.refresh_all_repositories(state_root=state, budget_seconds=120)

    assert _rows_by_name(answer) == {"gone": ("refused", "repository_index_unreadable")}


def test_a_git_read_that_outlives_its_bound_is_a_named_refusal(tmp_path):
    import repository_index

    repository = _repository(tmp_path / "slow", {"pkg/alpha.py": ALPHA})

    with pytest.raises(repository_index.RepositoryIndexRefused) as refused:
        repository_index._git_text(repository, "status", timeout=1e-9)

    assert refused.value.reason == "repository_git_probe_timed_out"


def _taken_away(registry, repository_id: str) -> None:
    """Another owner's reclaim: the row this process held is no longer there."""
    import repository_index

    with closing(registry._connect()) as database:
        database.execute(
            "DELETE FROM maintenance_owners WHERE role=? AND scope=?",
            (repository_index.REFRESH_ROLE, repository_index.refresh_scope(repository_id)),
        )
        database.commit()


def test_a_fence_taken_during_the_work_is_a_refused_fence(adopted_vault):  # noqa: F811
    import repository_index

    _root, state = adopted_vault
    _coordinator, registry = repository_index._refresh_fence(state)

    def work(_bound: float, _stop) -> dict:
        _taken_away(registry, "repository-under-test")
        return {"status": "refreshed"}

    outcome = repository_index.run_fenced("repository-under-test", state, None, None, work)

    assert outcome == {
        repository_index.FENCE_REFUSED: True,
        "status": "refresh_owned_elsewhere",
        "reason": "fence_lost",
    }
