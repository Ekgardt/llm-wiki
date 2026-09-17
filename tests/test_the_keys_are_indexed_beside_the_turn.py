"""A turn is found under the facts it states, and the reader still gets the turn.

The keys the nightly pass extracts reached a question as a separate leg, the shape
LongMemEval measured as the worse one. Research:
`docs/research/2026-09-16-the-keys-are-indexed-beside-the-turn.md`.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import corpus_snapshot  # noqa: E402
import fact_keys  # noqa: E402
import search_memory  # noqa: E402

DAILY = (
    "# Daily Session Memory — 2023-05-26\n\n"
    "## [07:31:00] session_end | answer_355c48bb\n\n"
    "_captured: 2023-05-26 07:31:00_\n\n"
    "**user:** I finally saw the production last night and loved it.\n\n"
)


def _vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "knowledge/daily").mkdir(parents=True)
    (root / "knowledge/notes").mkdir(parents=True)
    (root / "knowledge/projects").mkdir(parents=True)
    (root / "knowledge/daily/2023-05-26.md").write_text(DAILY, encoding="utf-8")
    return root


def _keyed_store(tmp_path: Path, snapshot) -> dict[str, str]:
    store = fact_keys.KeyStore(tmp_path / "keys.sqlite3")
    try:
        for turn in fact_keys.user_turns(snapshot.chunks):
            store.add(turn, ["The user attended The Glass Menagerie"])
    finally:
        store.close()
    return fact_keys.keys_by_span(tmp_path / "keys.sqlite3")


def _built(tmp_path: Path, snapshot, keys) -> Path:
    directory = tmp_path / "generation"
    directory.mkdir()
    search_memory.build_generation_fts(snapshot, directory, keys=keys)
    return directory / search_memory.GENERATION_FTS_ARTIFACT


def _searched(artifact: Path, query: str) -> list[dict]:
    """The product's own lexical leg over the artifact, not a query written for the test."""
    manifest = {"generation_id": "generation-test"}
    with sqlite3.connect(f"file:{artifact}?mode=ro", uri=True) as database:
        return search_memory._generation_fts_search(
            query, manifest, database, scope="all", limit=5, project=None, since=None, as_of=None
        )


def test_a_turn_is_found_under_its_keys_and_read_without_them(tmp_path) -> None:
    root = _vault(tmp_path)
    snapshot = corpus_snapshot.collect_corpus(root, daily_paths=("knowledge/daily/2023-05-26.md",))
    artifact = _built(tmp_path, snapshot, _keyed_store(tmp_path, snapshot))

    found = _searched(artifact, "menagerie")

    assert len(found) == 1
    assert "Glass Menagerie" not in str(found[0]["content"])
    assert "production last night" in str(found[0]["content"])


def test_a_vault_that_never_keyed_anything_finds_nothing_under_a_key(tmp_path) -> None:
    root = _vault(tmp_path)
    snapshot = corpus_snapshot.collect_corpus(root, daily_paths=("knowledge/daily/2023-05-26.md",))

    artifact = _built(tmp_path, snapshot, None)

    assert (_searched(artifact, "menagerie"), len(_searched(artifact, "production"))) == ([], 1)


def test_an_artifact_written_before_the_keys_column_is_still_valid(tmp_path) -> None:
    artifact = tmp_path / "search.sqlite3"
    legacy_ddl = search_memory._GENERATION_FTS_DDL.replace("                keys,\n", "")
    with sqlite3.connect(artifact) as database:
        database.executescript(legacy_ddl)
        database.execute(
            "INSERT INTO generation_metadata(key, value) VALUES ('schema_version', ?)",
            (search_memory.LEGACY_SEARCH_SCHEMA_VERSION,),
        )
        database.commit()
        assert search_memory._valid_fts_schema(database) is True
