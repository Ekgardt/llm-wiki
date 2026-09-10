"""The validated reader is opened once per process and reused (#24, section A).

Measured 2026-09-10: opening a generation cost 458 of a warm answer's 521 ms.
These tests pin the reuse rules from
`docs/research/2026-09-10-warm-index-answers-inside-the-loop.md`: reuse while
the catalog, the artifact and the checkout's Git state keep their identity;
re-open when the catalog changes; keep the reader but refresh the scope after
a commit; never close a reader a lease still holds.
"""

from __future__ import annotations

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import evidence_reader_cache  # noqa: E402

from tests.test_code_graph import _activate_graph  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_cache():
    evidence_reader_cache.clear()
    yield
    evidence_reader_cache.clear()


def _counting_opener(monkeypatch) -> list:
    import evidence_graph

    opened: list[str] = []
    original = evidence_graph.EvidenceGraph.open_active_for_repository.__func__

    def counted(cls, catalog, scope, **options):
        graph = original(cls, catalog, scope, **options)
        opened.append(graph.generation_id)
        return graph

    monkeypatch.setattr(
        evidence_graph.EvidenceGraph, "open_active_for_repository", classmethod(counted)
    )
    return opened


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *arguments], check=True, capture_output=True, timeout=30
    )


def _repository(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", ".")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "test")
    (root / "app.py").write_text("def caller():\n    callee()\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "initial")
    return root


def _head(root: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


def test_a_second_query_reuses_the_validated_reader(tmp_path, monkeypatch):
    import code_graph

    catalog = _activate_graph(tmp_path)
    monkeypatch.setattr(code_graph, "_generation_catalog", lambda _directory: catalog)
    opened = _counting_opener(monkeypatch)

    first = code_graph.find_callers("callee", tmp_path)
    second = code_graph.find_callers("callee", tmp_path)

    assert (first == second, opened, evidence_reader_cache.cached_entries()) == (
        True,
        ["active"],
        1,
    )


def test_a_newly_activated_generation_is_opened_and_the_old_reader_retired(
    tmp_path, monkeypatch
):
    import code_graph
    from repository_scope import resolve_repository_scope

    from tests.test_evidence_graph_recovery import _publish, _rich_graph_records

    catalog = _activate_graph(tmp_path)
    monkeypatch.setattr(code_graph, "_generation_catalog", lambda _directory: catalog)
    opened = _counting_opener(monkeypatch)
    code_graph.find_callers("callee", tmp_path)

    _publish(
        catalog,
        "next",
        graph_records=_rich_graph_records(),
        repository_scope=resolve_repository_scope(tmp_path).as_dict(),
    )
    catalog.register("next")
    catalog.activate("next", expected_active="active")
    answer = code_graph.find_callers("callee", tmp_path, with_report=True)

    assert (opened, answer["source_generation"]) == (["active", "next"], "next")


def test_a_commit_refreshes_the_scope_and_keeps_the_reader(tmp_path, monkeypatch):
    import code_graph

    repository = _repository(tmp_path / "repository")
    catalog = _activate_graph(tmp_path, repository)
    monkeypatch.setattr(code_graph, "_generation_catalog", lambda _directory: catalog)
    opened = _counting_opener(monkeypatch)
    before = code_graph._active_evidence_graph(repository)
    commit_before = before.cached_scope.git_commit
    before.close()

    _git(repository, "commit", "-q", "--allow-empty", "-m", "moved")
    after = code_graph._active_evidence_graph(repository)
    commit_after = after.cached_scope.git_commit
    after.close()

    assert (opened, commit_before != commit_after, commit_after) == (
        ["active"],
        True,
        _head(repository),
    )


def test_closing_a_lease_keeps_the_shared_reader_open(tmp_path, monkeypatch):
    import code_graph

    catalog = _activate_graph(tmp_path)
    monkeypatch.setattr(code_graph, "_generation_catalog", lambda _directory: catalog)

    lease = code_graph._active_evidence_graph(tmp_path)
    lease.close()
    again = code_graph._active_evidence_graph(tmp_path)
    rows = again.find_nodes(name="callee", max_rows=10)
    again.close()

    assert len(rows) == 1


def test_readers_are_shared_across_worker_threads(tmp_path, monkeypatch):
    import code_graph

    catalog = _activate_graph(tmp_path)
    monkeypatch.setattr(code_graph, "_generation_catalog", lambda _directory: catalog)
    opened = _counting_opener(monkeypatch)
    code_graph.find_callers("callee", tmp_path)

    with ThreadPoolExecutor(max_workers=4) as pool:
        answers = list(pool.map(lambda _: code_graph.find_callers("callee", tmp_path), range(8)))

    assert (opened, len({str(answer) for answer in answers})) == (["active"], 1)


class _FakeReader:
    def __init__(self, path: Path) -> None:
        self.database_path = path
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _scope(root: Path):
    return SimpleNamespace(
        checkout_root=str(root), git_common_dir=None, same_repository=lambda _other: True
    )


def _lease_for(key: str, tmp_path: Path, readers: dict) -> evidence_reader_cache.GraphLease:
    return evidence_reader_cache.leased_graph(
        (key, False),
        catalog_path=tmp_path / "catalog.sqlite3",
        resolve_scope=lambda: _scope(tmp_path),
        open_graph=lambda _scope: readers.setdefault(key, _FakeReader(tmp_path / key)),
    )


def test_eviction_beyond_the_bound_closes_the_oldest_unheld_reader(tmp_path, monkeypatch):
    monkeypatch.setattr(evidence_reader_cache, "MAX_ENTRIES", 1)
    readers: dict[str, _FakeReader] = {}

    _lease_for("first", tmp_path, readers).close()
    _lease_for("second", tmp_path, readers).close()

    assert (readers["first"].closed, readers["second"].closed) == (True, False)


def test_a_held_lease_defers_the_close_until_it_is_released(tmp_path, monkeypatch):
    monkeypatch.setattr(evidence_reader_cache, "MAX_ENTRIES", 1)
    readers: dict[str, _FakeReader] = {}

    held = _lease_for("first", tmp_path, readers)
    _lease_for("second", tmp_path, readers).close()
    still_open = not readers["first"].closed
    held.close()

    assert (still_open, readers["first"].closed) == (True, True)


def test_an_idle_reader_is_closed_on_the_next_access_and_a_held_one_is_not(
    tmp_path, monkeypatch
):
    readers: dict[str, _FakeReader] = {}
    _lease_for("idle", tmp_path, readers).close()
    held = _lease_for("held", tmp_path, readers)
    monkeypatch.setattr(evidence_reader_cache, "IDLE_SECONDS", 0.0)

    _lease_for("fresh", tmp_path, readers).close()
    idle_closed, held_still_open = readers["idle"].closed, not readers["held"].closed
    held.close()

    assert (idle_closed, held_still_open, readers["held"].closed) == (True, True, True)


def test_a_missing_generation_is_not_cached(tmp_path):
    answer = evidence_reader_cache.leased_graph(
        ("absent", False),
        catalog_path=tmp_path / "catalog.sqlite3",
        resolve_scope=lambda: _scope(tmp_path),
        open_graph=lambda _scope: None,
    )

    assert (answer, evidence_reader_cache.cached_entries()) == (None, 0)
