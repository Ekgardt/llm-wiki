"""A Russian word finds the page that holds it in another case or number.

The index's Porter stemmer is English only, so "заметок" never matched
"заметки". Every Cyrillic word of five letters or more is also matched by its
stem, the word without its last two letters, as an FTS5 prefix.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import query_spelling  # noqa: E402
import search_memory  # noqa: E402


def _found(query: str, *texts: str) -> list[str]:
    connection = sqlite3.connect(":memory:")
    connection.executescript(search_memory._GENERATION_FTS_DDL)
    for order, text in enumerate(texts):
        connection.execute(
            "INSERT INTO chunks(chunk_id, chunk_order, title, content, keys) VALUES (?, ?, '', ?, '')",
            [f"c{order}", order, text],
        )
    expression = search_memory._spelled_fts_query(connection, query)
    return [str(row[0]) for row in connection.execute("SELECT content FROM chunks WHERE chunks MATCH ?", [expression])]


def test_another_case_of_a_russian_word_finds_the_page() -> None:
    assert _found("сколько заметок в хранилище", "Заметки хранятся в папке notes.", "Погода") == [
        "Заметки хранятся в папке notes."
    ]


def test_a_russian_stem_keeps_its_root() -> None:
    assert query_spelling.cyrillic_stems(["Заметок,", "поиска", "слово"]) == ["замет", "поис", "слов"]


def test_short_russian_words_and_latin_words_gain_no_stem() -> None:
    assert query_spelling.cyrillic_stems(["дом", "кот", "bookshelf", "API-ключ"]) == []
