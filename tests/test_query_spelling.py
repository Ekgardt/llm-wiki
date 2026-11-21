"""A misspelled word still finds the page that spells it right.

FTS5 matches whole tokens, so one transposed letter used to drop the word from
the query entirely. Measured 2026-11-21 on LongMemEval_S, lexical lane alone:
MRR@10 0.906 clean, 0.865 with one transposition, 0.901 with the corrections.
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


def _artifact(*texts: str) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(search_memory._GENERATION_FTS_DDL)
    for order, text in enumerate(texts):
        connection.execute(
            "INSERT INTO chunks(chunk_id, chunk_order, title, content, keys) "
            "VALUES (?, ?, '', ?, '')",
            [f"c{order}", order, text],
        )
    return connection


def _found(connection: sqlite3.Connection, query: str) -> list[str]:
    return [
        str(row[0])
        for row in connection.execute(
            "SELECT content FROM chunks WHERE chunks MATCH ?",
            [search_memory._spelled_fts_query(connection, query)],
        )
    ]


def test_a_transposed_letter_still_finds_the_page() -> None:
    connection = _artifact("I bought a new bookshelf at the flea market.", "We cooked pasta.")

    assert _found(connection, "where did I buy my bookhself") == [
        "I bought a new bookshelf at the flea market."
    ]


def test_a_word_is_compared_in_the_form_the_index_stores_it() -> None:
    """Porter stores "family" as "famili"; the typed "famliy" is stemmed the same way first."""
    connection = _artifact("A trip with my family to Spain.")

    assert _found(connection, "trip famliy") == ["A trip with my family to Spain."]


def test_a_correctly_spelled_query_is_left_as_it_is() -> None:
    connection = _artifact("I bought a new bookshelf.")

    assert search_memory._spelled_fts_query(connection, "bookshelf") == '"bookshelf"'


def test_short_words_and_numbers_are_never_corrected() -> None:
    connection = _artifact("cat 2023 report")

    words = ["cta", "2024", "x-ray"]

    assert query_spelling.corrections(connection, words, search_memory.GENERATION_TOKENIZER) == []


def test_one_transposition_is_one_edit() -> None:
    assert query_spelling.edit_distance("bookhself", "bookshelf", 1) == 1


def test_the_distance_stops_counting_past_its_bound() -> None:
    assert query_spelling.edit_distance("alphabet", "zzzzzzzz", 2) == 3


def test_longer_words_allow_more_edits() -> None:
    edits = [query_spelling.allowed_edits(word) for word in ("cat", "house", "ethnicity")]

    assert edits == [0, 1, 2]


# Russian test words, written as escapes because the repository holds no Cyrillic
# text. "Notes" in the nominative and in the genitive plural, "search" in the
# genitive, "word", and the short "house" and "cat".
NOTES = "\u0437\u0430\u043c\u0435\u0442\u043a\u0438"
OF_NOTES = "\u0437\u0430\u043c\u0435\u0442\u043e\u043a"
OF_SEARCH = "\u043f\u043e\u0438\u0441\u043a\u0430"
WORD = "\u0441\u043b\u043e\u0432\u043e"
HOUSE = "\u0434\u043e\u043c"
CAT = "\u043a\u043e\u0442"


def test_another_case_of_a_russian_word_finds_the_page() -> None:
    """Porter stems English only; the Russian word is matched by its stem as a prefix."""
    connection = _artifact(f"{NOTES} notes", "weather")

    assert _found(connection, f"{OF_NOTES} vault") == [f"{NOTES} notes"]


def test_a_russian_stem_keeps_its_root() -> None:
    stems = query_spelling.cyrillic_stems([OF_NOTES.title() + ",", OF_SEARCH, WORD])

    assert stems == [OF_NOTES[:5], OF_SEARCH[:4], WORD[:4]]


def test_short_russian_words_and_latin_words_gain_no_stem() -> None:
    assert query_spelling.cyrillic_stems([HOUSE, CAT, "bookshelf", "API-" + NOTES]) == []
