from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import generation_fts_validation
import generation_maintenance
import pytest

GENERATIONS = Path("cache") / "evidence-graph" / "generations"
NOTE = "---\ntype: concept\n---\n# First\n\nA first page about lanterns.\n"


def _built_generation(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    state = tmp_path / "state"
    (root / "knowledge" / "notes").mkdir(parents=True)
    (root / "knowledge" / "projects").mkdir(parents=True)
    (state / "cache").mkdir(parents=True)
    (state / "run").mkdir(parents=True)
    (root / "knowledge" / "notes" / "first.md").write_text(NOTE, encoding="utf-8")
    built = generation_maintenance.run_generation_maintenance(
        root=root, state_root=state, time_budget_seconds=120.0, max_sources=10
    )
    assert built.get("generation_id"), built
    return state / GENERATIONS / str(built["generation_id"])


@pytest.fixture(scope="module")
def generation(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _built_generation(tmp_path_factory.mktemp("fts-validation"))


def _manifest(generation: Path) -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads((generation / "manifest.json").read_bytes())
    return manifest


def _search_copy(generation: Path, tmp_path: Path) -> Path:
    copied = tmp_path / "search.sqlite3"
    shutil.copyfile(generation / "search.sqlite3", copied)
    copied.chmod(0o600)
    return copied


def _altered_index_is_valid(generation: Path, tmp_path: Path, statement: str) -> bool:
    copied = _search_copy(generation, tmp_path)
    with closing(sqlite3.connect(copied)) as connection:
        connection.execute(statement)
        connection.commit()
        return generation_fts_validation.valid_generation_fts(connection, _manifest(generation))


def test_the_built_index_is_valid(generation: Path, tmp_path: Path) -> None:
    assert _altered_index_is_valid(generation, tmp_path, "SELECT 1")


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE generation_metadata SET value = 'corpus-search/v9' WHERE key = 'schema_version'",
        "DELETE FROM generation_metadata WHERE key = 'schema_version'",
        "DELETE FROM generation_metadata WHERE key = 'chunk_count'",
        "UPDATE generation_metadata SET key = 'unexpected' WHERE key = 'chunk_count'",
    ],
)
def test_an_index_that_misdescribes_itself_is_invalid(
    generation: Path, tmp_path: Path, statement: str
) -> None:
    assert not _altered_index_is_valid(generation, tmp_path, statement)


def _index_that_names_the_wrong_column(database: Path) -> None:
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE t (a INTEGER, b INTEGER)")
        connection.execute("CREATE INDEX t_a ON t (a)")
        connection.executemany("INSERT INTO t VALUES (?, ?)", [(n, -n) for n in range(50)])
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_schema SET sql = 'CREATE INDEX t_a ON t (b)' WHERE name = 't_a'"
        )
        connection.commit()


def test_a_database_that_fails_its_integrity_check_has_no_valid_schema(tmp_path: Path) -> None:
    database = tmp_path / "damaged.sqlite3"
    _index_that_names_the_wrong_column(database)
    with closing(sqlite3.connect(database)) as connection:
        assert not generation_fts_validation.valid_fts_schema(connection)


def test_a_manifest_naming_one_artifact_path_twice_has_no_descriptors(generation: Path) -> None:
    artifacts = _manifest(generation)["artifacts"]
    repeated = {"artifacts": [artifacts[0], artifacts[0]]}
    assert generation_fts_validation.artifact_descriptors(repeated) is None


def test_an_index_whose_sources_cannot_be_chunked_is_invalid(
    generation: Path, tmp_path: Path
) -> None:
    copied = _search_copy(generation, tmp_path)
    unusable: dict[str, dict[str, object]] = {
        "source:x.md": {"relative_path": "x.md", "sha256": "0" * 64, "content": "text"}
    }
    with closing(sqlite3.connect(copied)) as connection:
        assert not generation_fts_validation.valid_generation_fts(
            connection, _manifest(generation), authoritative_sources=unusable
        )


def test_a_source_whose_content_is_not_bytes_cannot_be_chunked(generation: Path) -> None:
    chunks = generation_fts_validation.expected_source_chunks(
        "source:x.md",
        {"relative_path": "x.md", "sha256": "0" * 64, "content": "text"},
        manifest=_manifest(generation),
        deadline=None,
        cancelled=None,
    )
    with pytest.raises(generation_fts_validation._UnusableAuthoritativeSource):
        next(chunks)


def test_metadata_of_an_unknown_schema_does_not_match_any_manifest() -> None:
    metadata = {"schema_version": "corpus-search/v9"}
    assert not generation_fts_validation._metadata_matches_manifest(metadata, {})


def test_a_missing_or_plain_chunks_table_declares_no_fts_arguments() -> None:
    with closing(sqlite3.connect(":memory:")) as connection:
        assert generation_fts_validation._declared_fts_arguments(connection) is None
        connection.execute("CREATE TABLE chunks (content TEXT)")
        assert generation_fts_validation._declared_fts_arguments(connection) is None


def _first_stored_row(generation: Path) -> tuple[object, ...]:
    uri = f"file:{generation / 'search.sqlite3'}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        row: tuple[object, ...] = connection.execute(
            generation_fts_validation.FTS_CHUNK_SELECT
        ).fetchone()
    return row


def test_the_first_stored_row_holds_its_invariants(generation: Path) -> None:
    assert generation_fts_validation._valid_stored_chunk(_first_stored_row(generation), 0)


@pytest.mark.parametrize(
    ("position", "value"),
    [(3, ""), (4, "not-a-digest"), (7, "start"), (12, ""), (16, None)],
)
def test_a_stored_row_with_one_broken_field_is_invalid(
    generation: Path, position: int, value: object
) -> None:
    row = list(_first_stored_row(generation))
    row[position] = value
    assert not generation_fts_validation._valid_stored_chunk(tuple(row), 0)


def _cancelled_after(checks: int) -> Callable[[], bool]:
    seen: list[bool] = []

    def cancelled() -> bool:
        seen.append(True)
        return len(seen) > checks

    return cancelled


def test_a_stop_requested_by_the_caller_is_reached() -> None:
    assert generation_fts_validation.stop_reached(None, lambda: True)
    assert not generation_fts_validation.stop_reached(None, lambda: False)


def test_a_statement_interrupted_by_a_stop_raises_a_timeout() -> None:
    long_query = (
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 10000000) "
        "SELECT COUNT(*) FROM n"
    )
    with (
        closing(sqlite3.connect(":memory:")) as connection,
        pytest.raises(TimeoutError, match="cancelled or deadline exceeded"),
        generation_fts_validation.generation_sqlite_guard(connection, None, _cancelled_after(1)),
    ):
        connection.execute(long_query).fetchone()


@pytest.mark.parametrize("manifest", [{}, {"artifacts": "search.sqlite3"}])
def test_a_manifest_without_an_artifact_list_has_no_descriptors(
    manifest: dict[str, object],
) -> None:
    assert generation_fts_validation.artifact_descriptors(manifest) is None


def _written(tmp_path: Path, payload: bytes) -> Path:
    target = tmp_path / "file.bin"
    target.write_bytes(payload)
    return target


def test_a_read_past_its_byte_ceiling_is_refused(tmp_path: Path) -> None:
    target = _written(tmp_path, b"x" * 100)
    with pytest.raises(ValueError, match="source manifest exceeds its byte ceiling"):
        generation_fts_validation._read_identity_stable_bytes(
            target,
            target.stat(),
            max_bytes=10,
            label="source manifest",
            deadline=None,
            cancelled=None,
        )


def test_a_read_of_a_replaced_file_is_refused(tmp_path: Path) -> None:
    target = _written(tmp_path, b"payload")
    other = tmp_path / "other.bin"
    other.write_bytes(b"other")
    with pytest.raises(PermissionError, match="source manifest identity changed before read"):
        generation_fts_validation._read_identity_stable_bytes(
            target,
            other.stat(),
            max_bytes=100,
            label="source manifest",
            deadline=None,
            cancelled=None,
        )


def test_a_file_that_grew_during_a_read_changed(tmp_path: Path) -> None:
    target = _written(tmp_path, b"payload")
    before = target.stat()
    target.write_bytes(b"payload grown")
    with pytest.raises(PermissionError, match="source manifest changed during read"):
        generation_fts_validation._require_same_file(before, target.stat(), "source manifest")


def _with_source_manifest(generation: Path, tmp_path: Path, payload: bytes) -> Path:
    copied = tmp_path / "state" / "generation"
    copied.mkdir(parents=True)
    (copied / "source-manifest.json").write_bytes(payload)
    return copied


def _validated_source_manifest(copied: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    validated: dict[str, Any] = generation_fts_validation.validated_source_manifest(
        copied, manifest, state_root=copied.parent, deadline=None, cancelled=None
    )
    return validated


def test_a_source_manifest_that_is_not_json_is_invalid(generation: Path, tmp_path: Path) -> None:
    copied = _with_source_manifest(generation, tmp_path, b"{broken")
    with pytest.raises(ValueError, match="generation source manifest is invalid"):
        _validated_source_manifest(copied, _manifest(generation))


def test_a_source_manifest_that_is_not_canonical_is_refused(
    generation: Path, tmp_path: Path
) -> None:
    value = json.loads((generation / "source-manifest.json").read_bytes())
    copied = _with_source_manifest(generation, tmp_path, json.dumps(value, indent=2).encode())
    with pytest.raises(ValueError, match="generation source manifest hash is invalid"):
        _validated_source_manifest(copied, _manifest(generation))


def test_the_built_source_manifest_is_valid(generation: Path, tmp_path: Path) -> None:
    payload = (generation / "source-manifest.json").read_bytes()
    copied = _with_source_manifest(generation, tmp_path, payload)
    assert _validated_source_manifest(copied, _manifest(generation)) == json.loads(payload)


def _source_database(rows: list[tuple[object, ...]]) -> sqlite3.Connection:
    database = sqlite3.connect(":memory:")
    database.execute("CREATE TABLE source (source_id, relative_path, sha256, size, content BLOB)")
    database.executemany("INSERT INTO source VALUES (?, ?, ?, ?, ?)", rows)
    return database


def _source_row(source_id: object, content: bytes) -> tuple[object, ...]:
    digest = hashlib.sha256(content).hexdigest()
    return (source_id, f"{source_id}.md", digest, len(content), content)


def _metadata_rows(rows: list[tuple[object, ...]]) -> list[tuple[str, str, str, int]]:
    with closing(_source_database(rows)) as database:
        return generation_fts_validation._source_metadata_rows(
            database, deadline=None, cancelled=None
        )


def test_valid_source_rows_are_listed_in_path_order() -> None:
    listed = _metadata_rows([_source_row("b", b"two"), _source_row("a", b"one")])
    assert [row[0] for row in listed] == ["a", "b"]


@pytest.mark.parametrize(
    "rows",
    [
        [_source_row(7, b"one")],
        [("a", "a.md", "digest", 4, b"one")],
        [("a", "a.md", 5, 3, b"one")],
    ],
)
def test_an_invalid_source_row_is_refused(rows: list[tuple[object, ...]]) -> None:
    with pytest.raises(ValueError, match="generation evidence source rows are invalid"):
        _metadata_rows(rows)


def test_a_repeated_source_id_is_refused() -> None:
    seen = {"a"}
    with pytest.raises(ValueError, match="source rows are invalid"):
        generation_fts_validation._require_admissible_source_row(
            (*_source_row("a", b"one")[:4], 3), [], seen, 10
        )


def test_a_source_row_past_the_file_ceiling_is_refused() -> None:
    with pytest.raises(ValueError, match="generation source row ceiling exceeded"):
        generation_fts_validation._require_admissible_source_row(
            ("b", "b.md", "0" * 64, 1, 1), [("a", "a.md", "0" * 64, 1)], set(), 1
        )


def _small_ceilings(name: str) -> int:
    return {"corpus.max_files": 10, "corpus.max_total_bytes": 4}[name]


def test_source_bytes_past_the_total_ceiling_are_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(generation_fts_validation, "setting_value", _small_ceilings)
    with pytest.raises(ValueError, match="source bytes exceed their ceiling"):
        _metadata_rows([_source_row("a", b"one"), _source_row("b", b"two")])


def _verified(rows: list[tuple[object, ...]], metadata: list[tuple[str, str, str, int]]) -> None:
    with closing(_source_database(rows)) as database:
        generation_fts_validation._verified_source_contents(
            database, metadata, deadline=None, cancelled=None
        )


def test_a_listed_source_without_content_is_refused() -> None:
    with pytest.raises(ValueError, match="source content is missing"):
        _verified([], [("a", "a.md", "0" * 64, 3)])


def test_a_source_whose_content_no_longer_hashes_is_refused() -> None:
    with pytest.raises(ValueError, match="source content is invalid"):
        _verified([_source_row("a", b"one")], [("a", "a.md", "0" * 64, 3)])
