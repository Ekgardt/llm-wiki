"""The low findings of the third audit's generations report.

G-L1 (the row a checkout is headed by), G-L2 (a commit that changed no source),
G-L3 (one catalog lookup instead of a second capture), G-L8 (a count taken over
the rows it describes), G-L9 (a watcher a caller's callback cannot end, and a
path refused by name), G-L11 (a commit whose body is already irreversible).
Research: `docs/research/2026-09-17-small-corrections-in-the-generations-area.md`.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
for directory in (TESTS.parent / "scripts", TESTS):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from code_kernel_helpers import basic_graph_records  # noqa: E402
from test_a_question_is_answered_by_its_own_kind_of_generation import (  # noqa: E402
    _active_memory_generation,
)
from test_repository_index import ALPHA, _git, _repository, vault  # noqa: E402,F401

from tests.slow_machine import SHORT_TIMEOUT  # noqa: E402


def test_a_checkouts_row_is_headed_by_its_code_generation(vault, tmp_path):  # noqa: F811
    """G-L1: not by whichever kind happened to be registered last."""
    import generation_catalog
    import repository_index

    _root, state = vault
    repository = _repository(tmp_path / "repo", {"scripts/alpha.py": ALPHA})
    receipt = repository_index.index_repository(
        repository, roots=["scripts"], state_root=state
    )
    catalog = generation_catalog.GenerationCatalog(state)
    _active_memory_generation(catalog, repository, "gen-memory")

    rows = repository_index.list_repositories(state_root=state)["repositories"]
    row = next(item for item in rows if item["checkout_root"] == str(repository))

    assert (row["generation_id"], row["active"], row["code_roots"]) == (
        receipt["generation_id"],
        False,
        ["scripts"],
    )


def test_a_commit_that_changed_no_source_stops_saying_the_index_is_stale(
    vault, tmp_path  # noqa: F811
):
    """G-L2: the hint table records the commit its generation was confirmed at."""
    import repository_index
    from code_hints import hints_path, read_meta
    from repository_scope import resolve_repository_scope

    _root, state = vault
    repository = _repository(tmp_path / "repo", {"scripts/alpha.py": ALPHA})
    repository_index.index_repository(repository, roots=["scripts"], state_root=state)
    (repository / "README.md").write_text("# docs only\n", encoding="utf-8")
    _git(repository, "add", "-A")
    _git(repository, "commit", "-qm", "docs only")

    answer = repository_index.refresh_repository(repository, state_root=state)
    scope = resolve_repository_scope(repository)
    meta = read_meta(hints_path(state, scope.checkout_id))

    assert (answer["status"], answer["commit_moved"], meta["git_commit"]) == (
        "fresh",
        True,
        scope.git_commit,
    )


def test_asking_whether_a_checkout_is_indexed_does_not_capture_it(
    vault, tmp_path, monkeypatch  # noqa: F811
):
    """G-L3: `_indexed_already` needs one catalog lookup, not a corpus capture."""
    import repository_index

    root, state = vault
    _repository(root, {"scripts/alpha.py": ALPHA})
    repository_index.index_repository(root, roots=["scripts"], state_root=state)
    captured: list[Path] = []
    collect = repository_index._collect

    def watched(directory, roots, deadline):
        captured.append(directory)
        return collect(directory, roots, deadline)

    monkeypatch.setattr(repository_index, "_collect", watched)

    indexed = repository_index._indexed_already(root, state, time.monotonic() + 60)

    assert (indexed, captured) == (True, [])


class _RaisingCancellation:
    """A caller's callback that fails the way a caller's code can."""

    def __call__(self) -> bool:
        raise RuntimeError("a caller's callback")


def test_a_watcher_is_not_ended_by_a_cancellation_callback_that_raises():
    """G-L9: the watcher thread is the only bound on a blocking read."""
    import repository_scope

    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"],
        stdout=subprocess.PIPE,
    )
    watch = repository_scope._GitWatch(
        process, time.monotonic() + SHORT_TIMEOUT, _RaisingCancellation()
    )
    watch.start()
    returncode = process.wait(timeout=SHORT_TIMEOUT)
    watch.finish()

    assert (returncode != 0, watch.reason) == (True, ["cancelled"])


def test_a_path_the_scope_cannot_serialize_is_refused_by_name(tmp_path, monkeypatch):
    """G-L9: callers handle `RepositoryScopeUnavailable`, not a bare ValueError."""
    import repository_scope

    root = _repository(tmp_path / "repo", {"a.py": "x = 1\n"})

    def refuse(*_args, **_options):
        raise ValueError("repository scope path must be a canonical POSIX absolute path")

    monkeypatch.setattr(repository_scope, "_local_serialized_path", refuse)

    with pytest.raises(repository_scope.RepositoryScopeUnavailable):
        repository_scope.resolve_repository_scope(root)


class _StoppedClock:
    """A monotonic clock the test moves itself."""

    def __init__(self) -> None:
        self.value = 1_000.0

    def __call__(self) -> float:
        return self.value


def test_a_deadline_that_passes_inside_a_write_still_commits(tmp_path):
    """G-L11: the body of a discard removes a tree; the commit must follow it."""
    import generation_catalog

    clock = _StoppedClock()
    catalog = generation_catalog.GenerationCatalog(tmp_path / "state", monotonic=clock)

    with catalog._write_transaction(clock.value + 10) as database:
        database.execute("PRAGMA user_version = 7")
        clock.value += 100

    with catalog._readonly() as database:
        stored = database.execute("PRAGMA user_version").fetchone()[0]

    assert stored == 7


def test_a_count_of_unresolved_calls_counts_what_it_can_return(tmp_path):
    """G-L8: an observation the row query cannot return is not part of the size."""
    from evidence_graph import EvidenceGraph, create_generation_database

    records = dict(basic_graph_records())
    records["observations"] = [
        {
            "observation_id": "observation",
            "source_node_id": "caller",
            "edge_type": "CALLS",
            "target_text": "queue.vanished",
            "reason": "dynamic_dispatch",
            "extractor": "python/v1",
        }
    ]
    path = tmp_path / "evidence.sqlite3"
    create_generation_database(path, **records)

    with EvidenceGraph(path, state_root=tmp_path) as graph:
        answer = graph.unresolved_calls_naming("vanished")

    assert (answer["count"], answer["calls"]) == (0, [])
