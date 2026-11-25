"""A misspelled word still finds the page that spells it right.

FTS5 matches whole tokens, so one transposed letter used to drop the word from
the query entirely. Measured 2026-11-21 on LongMemEval_S, lexical lane alone:
MRR@10 0.906 clean, 0.865 with one transposition, 0.901 with the corrections.
"""

from __future__ import annotations

import os
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


def test_the_tail_of_a_glued_identifier_finds_the_page() -> None:
    connection = _artifact(
        "Load it with AutoModelForSequenceClassification from transformers.", "We cooked pasta."
    )

    assert _found(connection, "SequenceClassification") == [
        "Load it with AutoModelForSequenceClassification from transformers."
    ]


def test_a_correction_the_stemmer_would_cut_again_still_finds_the_page() -> None:
    """Porter stores "advertise" as "advertis", and stems "advertis" on to "adverti"."""
    connection = _artifact("We advertise the release.", "We cooked pasta.")

    assert _found(connection, "advertsie") == ["We advertise the release."]


def test_a_glued_term_the_stemmer_would_cut_again_still_finds_the_page() -> None:
    connection = _artifact("Messages go to AWSSimpleQueueService.", "We cooked pasta.")

    assert _found(connection, "SimpleQueueService") == ["Messages go to AWSSimpleQueueService."]


def test_a_word_the_index_holds_gains_no_glued_words() -> None:
    connection = _artifact("A bookshelf and a bookshelfmaker.")

    found = query_spelling.glued_words(
        connection, ["bookshelf"], search_memory.GENERATION_TOKENIZER
    )

    assert found == []


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


NOTES = "заметки"
OF_NOTES = "заметок"
OF_SEARCH = "поиска"
WORD = "слово"
HOUSE = "дом"
CAT = "кот"


def test_another_case_of_a_russian_word_finds_the_page() -> None:
    """Porter stems English only; the Russian word is matched by its stem as a prefix."""
    connection = _artifact(f"{NOTES} notes", "weather")

    assert _found(connection, f"{OF_NOTES} vault") == [f"{NOTES} notes"]


def test_a_russian_stem_keeps_its_root() -> None:
    stems = query_spelling.cyrillic_stems([OF_NOTES.title() + ",", OF_SEARCH, WORD])

    assert stems == [OF_NOTES[:5], OF_SEARCH[:4], WORD[:4]]


def test_short_russian_words_and_latin_words_gain_no_stem() -> None:
    assert query_spelling.cyrillic_stems([HOUSE, CAT, "bookshelf", "API-" + NOTES]) == []


def test_the_pruned_walk_finds_every_term_a_full_comparison_finds() -> None:
    """Skipping a prefix whose alignment already exceeds the bound loses no term."""
    import random

    generator = random.Random(11)
    terms = sorted({"".join(generator.choices("abcde", k=generator.randint(3, 9))) for _ in range(3000)})

    for word in ("abcdeab", "eeddcc", "aabbccdde", "bacedb"):
        for bound in (1, 2):
            expected = {
                term: distance
                for term in terms
                if (distance := query_spelling.edit_distance(word, term, bound)) <= bound
            }
            assert query_spelling._within(terms, word, bound) == expected


def test_a_file_artifact_reads_its_vocabulary_once_until_it_changes(tmp_path: Path) -> None:
    path = tmp_path / "search.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(search_memory._GENERATION_FTS_DDL)
    connection.execute(
        "INSERT INTO chunks(chunk_id, chunk_order, title, content, keys) VALUES ('c0', 0, '', 'bookshelf', '')"
    )
    connection.commit()

    first = query_spelling.vocabulary(connection)
    assert query_spelling.vocabulary(connection) is first

    connection.execute(
        "INSERT INTO chunks(chunk_id, chunk_order, title, content, keys) VALUES ('c1', 1, '', 'lantern', '')"
    )
    connection.commit()
    os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1_000_000))

    assert "lantern" in query_spelling.vocabulary(connection).terms
