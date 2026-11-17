from __future__ import annotations

import importlib
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

helpers = importlib.import_module("tests.retrieval_v2_helpers")
lexical = importlib.import_module("retrieval_v2_sqlite_lexical")
contract = importlib.import_module("retrieval_v2_contract")

SCOPE = contract.QueryScope(projects=("alpha",), temporal_mode="current", as_of=None)
OTHER_SCOPE = contract.QueryScope(projects=("beta",), temporal_mode="current", as_of=None)


def _candidates() -> list[Any]:
    return [
        helpers.lexical_candidate("one", "catalog entry"),
        helpers.lexical_candidate("two", "queue entry"),
    ]


def _adapter(root: Path, configuration: str = "L0", **options: Any) -> Any:
    return lexical.SQLiteLexicalAdapter(_candidates(), root, configuration, **options)


class ListSegmenter:
    def __init__(self, tokens: list[str]) -> None:
        self.tokens = tokens

    def cut(self, value: str, HMM: bool) -> Iterator[str]:
        return iter(self.tokens)


class BrokenIterationSegmenter:
    def cut(self, value: str, HMM: bool) -> Iterator[str]:
        yield "first"
        raise RuntimeError("dictionary vanished")


@pytest.mark.parametrize(
    ("configuration", "deadline_seconds", "message"),
    [
        ("L9", 5.0, "unknown lexical configuration: L9"),
        ("L0", 0.0, "lexical deadline must be positive"),
    ],
)
def test_the_adapter_refuses_an_unknown_configuration_or_a_spent_deadline(
    tmp_path: Path, configuration: str, deadline_seconds: float, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _adapter(tmp_path, configuration, deadline_seconds=deadline_seconds)
    assert list(tmp_path.iterdir()) == []


def test_a_cache_root_that_becomes_a_reparse_point_after_creation_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = iter([True])
    monkeypatch.setattr(lexical, "is_reparse_point", lambda path: next(answers))
    root = tmp_path / "cache"

    with pytest.raises(ValueError, match="must not be a symlink or reparse point"):
        _adapter(root)
    assert root.is_dir()


def test_a_segmenter_without_a_cut_function_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="test segmenter has no callable cut function"):
        _adapter(tmp_path, "L3", test_segmenter=SimpleNamespace(cut="not callable"))


def test_a_segmenter_failing_mid_iteration_fails_the_build_and_removes_the_index(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="segmentation failed: dictionary vanished"):
        _adapter(tmp_path, "L3", test_segmenter=BrokenIterationSegmenter())
    assert (tmp_path / "lexical-L3.sqlite3").exists() is False


def test_empty_segments_are_dropped_from_the_indexed_text(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path, "L3", test_segmenter=ListSegmenter(["", "catalog", ""]))
    try:
        assert adapter._segment("anything") == "catalog"
    finally:
        adapter.close()


@pytest.mark.parametrize(("query", "expected"), [("ab", None), ('a"bc', '"a""bc"')])
def test_a_trigram_query_needs_three_characters_and_doubles_its_quotes(
    query: str, expected: str | None
) -> None:
    assert lexical._trigram_match_query(query) == expected


def test_without_a_descriptor_filesystem_the_index_is_opened_by_its_verified_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hide_descriptor_filesystems(tmp_path, monkeypatch)
    adapter = _adapter(tmp_path / "cache")
    try:
        ranked = adapter.rank("catalog", SCOPE, _candidates(), limit=5, language="EN")
    finally:
        adapter.close()
    assert [item.evidence_id for item in ranked] == ["one"]
    assert adapter.path.exists()


def test_a_descriptor_uri_sqlite_cannot_open_falls_back_to_a_clean_owned_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_connect = sqlite3.connect
    refused: list[str] = []

    def connect(database: str, **options: Any) -> sqlite3.Connection:
        if "/fd/" in database:
            refused.append(database)
            raise sqlite3.OperationalError("unable to open database file")
        connection: sqlite3.Connection = real_connect(database, **options)
        return connection

    monkeypatch.setattr(lexical.sqlite3, "connect", connect)
    adapter = _adapter(tmp_path)
    try:
        ranked = adapter.rank("queue", SCOPE, _candidates(), limit=5, language="EN")
    finally:
        adapter.close()
    assert len(refused) == 1
    assert [item.evidence_id for item in ranked] == ["two"]


def test_a_replaced_index_file_is_detected_as_a_changed_path(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    adapter.path.unlink()
    adapter.path.write_bytes(b"")

    with pytest.raises(PermissionError, match="lexical index path changed during index operation"):
        adapter._check_owned_identity()
    adapter.close()
    assert adapter.path.read_bytes() == b""


def test_a_query_without_any_indexable_term_ranks_nothing(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    try:
        assert adapter.rank("!!! ???", SCOPE, _candidates(), limit=5, language="EN") == []
    finally:
        adapter.close()


def test_a_scope_with_no_eligible_candidate_ranks_nothing(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    try:
        assert adapter.rank("catalog", OTHER_SCOPE, _candidates(), limit=5, language="EN") == []
    finally:
        adapter.close()


def test_a_ranking_universe_with_duplicate_normalized_ids_is_refused_and_removed(
    tmp_path: Path,
) -> None:
    adapter = _adapter(tmp_path)
    duplicated = [*_candidates(), helpers.lexical_candidate("ONE", "catalog")]

    with pytest.raises(ValueError, match="duplicate normalized id"):
        adapter.rank("catalog", SCOPE, duplicated, limit=5, language="EN")
    assert adapter.path.exists() is False


class FailingConnection:
    def __init__(self, on_execute: Any) -> None:
        self.on_execute = on_execute

    def execute(self, sql: str, parameters: list[object]) -> None:
        self.on_execute()
        raise sqlite3.OperationalError("interrupted")


def _swap_connection(adapter: Any, on_execute: Any) -> sqlite3.Connection:
    real: sqlite3.Connection = adapter._connection
    adapter._connection = FailingConnection(on_execute)
    return real


def test_a_failed_fts_query_names_its_index(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    real = _swap_connection(adapter, lambda: None)

    with pytest.raises(ValueError, match="FTS5 query failed for unicode"):
        adapter._bm25_rows("unicode", '"catalog"', {"one"}, 5)
    real.close()
    adapter._connection = None
    adapter.close()


def test_a_query_interrupted_by_the_deadline_is_reported_as_a_timeout(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)

    def expire() -> None:
        adapter._deadline = time.perf_counter() - 1

    real = _swap_connection(adapter, expire)

    with pytest.raises(TimeoutError, match="lexical benchmark absolute deadline exceeded"):
        adapter._bm25_rows("unicode", '"catalog"', {"one"}, 5)
    real.close()
    adapter._connection = None
    adapter.close()


def test_a_build_that_starts_past_its_deadline_leaves_no_index(tmp_path: Path) -> None:
    with pytest.raises(TimeoutError, match="lexical benchmark absolute deadline exceeded"):
        _adapter(tmp_path, deadline=time.perf_counter() - 1)
    assert [path.name for path in tmp_path.iterdir()] == []


def test_a_descriptor_opened_read_only_is_closed_and_the_owned_file_is_reopened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_connect = sqlite3.connect
    read_only: list[str] = []

    def connect(database: str, **options: Any) -> sqlite3.Connection:
        if "/fd/" in database:
            read_only.append(database)
            database = database.replace("mode=rw", "mode=ro")
        connection: sqlite3.Connection = real_connect(database, **options)
        return connection

    monkeypatch.setattr(lexical.sqlite3, "connect", connect)
    adapter = _adapter(tmp_path)
    try:
        ranked = adapter.rank("catalog", SCOPE, _candidates(), limit=5, language="EN")
    finally:
        adapter.close()
    assert len(read_only) == 1
    assert [item.evidence_id for item in ranked] == ["one"]


def _hide_descriptor_filesystems(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    descriptor_bases = {"/proc/self/fd", "/dev/fd"}
    absent = tmp_path / "absent"

    def path_without_descriptor_filesystems(value: str | Path) -> Path:
        if str(value) in descriptor_bases:
            return absent
        return Path(value)

    monkeypatch.setattr(lexical, "Path", path_without_descriptor_filesystems)


def test_an_index_replaced_while_it_is_being_opened_is_refused_and_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hide_descriptor_filesystems(tmp_path, monkeypatch)
    real_connect = sqlite3.connect
    opened: list[sqlite3.Connection] = []

    def connect_then_replace(database: str, **options: Any) -> sqlite3.Connection:
        connection: sqlite3.Connection = real_connect(database, **options)
        opened.append(connection)
        index = tmp_path / "cache" / "lexical-L0.sqlite3"
        index.unlink()
        index.write_bytes(b"foreign")
        return connection

    monkeypatch.setattr(lexical.sqlite3, "connect", connect_then_replace)

    with pytest.raises(PermissionError, match="lexical index path changed during index operation"):
        _adapter(tmp_path / "cache")
    assert (tmp_path / "cache" / "lexical-L0.sqlite3").read_bytes() == b"foreign"
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")
