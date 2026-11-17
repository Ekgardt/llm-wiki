"""Helpers shared by test_the_keys_are_indexed_beside_the_turn.py and the test files importing them."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import generation_fts_validation
import generation_search


def _built(tmp_path: Path, snapshot, keys) -> Path:
    directory = tmp_path / "generation"
    directory.mkdir()
    generation_search.build_generation_fts(snapshot, directory, keys=keys)
    return directory / generation_fts_validation.GENERATION_FTS_ARTIFACT


def _searched(artifact: Path, query: str) -> list[dict]:
    """The product's own lexical leg over the artifact, not a query written for the test."""
    manifest = {"generation_id": "generation-test"}
    with sqlite3.connect(f"file:{artifact}?mode=ro", uri=True) as database:
        return generation_search.generation_fts_search(
            query, manifest, database, scope="all", limit=5, project=None, since=None, as_of=None
        )
