"""Words a query misspells, matched by the indexed words one or two edits away.

FTS5 matches whole tokens, so "bookhself" finds nothing although "bookshelf" is
indexed. A word the index already holds is left alone; a word it does not hold
gains the indexed terms closest to it, the way MiniSearch's fuzzy match serves
Obsidian Omnisearch. The vocabulary is the artifact's own (`fts5vocab`), and the
typed word is stemmed by the artifact's own tokenizer, so both sides of the
comparison are in the form the index stores.
"""
from __future__ import annotations

import sqlite3
import string
from collections.abc import Sequence

# Edits allowed by word length, as in Lucene's fuzzy AUTO and MiniSearch's
# fractional fuzziness: none below five letters, where one edit makes another
# word, one below nine, two from nine.
SHORTEST_CORRECTED_WORD = 5
TWO_EDITS_FROM = 9
# Corrections kept per word: the closest, then the most common.
CORRECTIONS_PER_WORD = 3
# Letters a Russian word drops before matching every indexed form of it, and the
# shortest root that is still matched by prefix.
RUSSIAN_ENDING = 2
SHORTEST_STEM = 4

_VOCABULARY = "temp.llm_wiki_vocabulary"
_STEMMER = "temp.llm_wiki_stemmer"
_STEMS = "temp.llm_wiki_stems"


def allowed_edits(word: str) -> int:
    if len(word) < SHORTEST_CORRECTED_WORD:
        return 0
    if len(word) < TWO_EDITS_FROM:
        return 1
    return 2


def _alignment_row(left_char: str, left_prev: str, right: str, rows: tuple[list[int], list[int]], i: int) -> list[int]:
    before, previous = rows
    current = [i]
    for j, right_char in enumerate(right, 1):
        cost = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (left_char != right_char))
        if j > 1 and left_char == right[j - 2] and left_prev == right_char:
            cost = min(cost, before[j - 2] + 1)
        current.append(cost)
    return current


def edit_distance(left: str, right: str, bound: int) -> int:
    """Optimal string alignment distance, or `bound + 1` as soon as it must exceed `bound`."""
    if abs(len(left) - len(right)) > bound:
        return bound + 1
    before: list[int] = []
    previous = list(range(len(right) + 1))
    for i, left_char in enumerate(left, 1):
        left_prev = left[i - 2] if i > 1 else ""
        current = _alignment_row(left_char, left_prev, right, (before, previous), i)
        if min(current) > bound:
            return bound + 1
        before, previous = previous, current
    return previous[-1]


def _bare(word: str) -> str:
    return word.strip(string.punctuation + "«»“”‘’").casefold()


def _indexed(connection: sqlite3.Connection, word: str) -> bool:
    quoted = '"' + word.replace('"', '""') + '"'
    return connection.execute(
        "SELECT 1 FROM chunks WHERE chunks MATCH ? LIMIT 1", [quoted]
    ).fetchone() is not None


def _stemmed(connection: sqlite3.Connection, word: str, tokenizer: str) -> str:
    """The word as the artifact's tokenizer stores it."""
    connection.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {_STEMMER} USING fts5(word, tokenize = '{tokenizer}')"
    )
    connection.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS {_STEMS} USING fts5vocab(temp, llm_wiki_stemmer, 'row')")
    connection.execute(f"DELETE FROM {_STEMMER}")
    connection.execute(f"INSERT INTO {_STEMMER}(word) VALUES (?)", [word])
    row = connection.execute(f"SELECT term FROM {_STEMS} LIMIT 1").fetchone()
    return str(row[0]) if row else word


def _nearest(connection: sqlite3.Connection, word: str, bound: int, tokenizer: str) -> list[str]:
    connection.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS {_VOCABULARY} USING fts5vocab(main, chunks, 'row')")
    stem = _stemmed(connection, word, tokenizer)
    rows = connection.execute(
        f"SELECT term, doc FROM {_VOCABULARY} WHERE length(term) BETWEEN ? AND ?",
        [len(stem) - bound, len(word) + bound],
    ).fetchall()
    scored = sorted(
        (min(edit_distance(word, term, bound), edit_distance(stem, term, bound)), -int(doc), str(term))
        for term, doc in rows
    )
    return [term for distance, _, term in scored if distance <= bound][:CORRECTIONS_PER_WORD]


def _cyrillic(word: str) -> bool:
    return all("а" <= letter <= "я" or letter == "ё" for letter in word)


def cyrillic_stems(words: Sequence[str]) -> list[str]:
    """Each Cyrillic word of five letters or more without its last two letters.

    The tokenizer's Porter stemmer is English only, so "заметок" never met
    "заметки". Two letters cover most Russian case and number endings, and four
    letters are always kept, so a stem stays a word's root rather than a syllable.
    Measured 2026-11-21 on 50 Russian questions over this repository's Russian
    documents, lexical lane alone: MRR@10 0.369 to 0.499, hit@5 0.50 to 0.68. The
    Snowball Russian stemmer scored 0.476, so it does not earn a dependency.
    """
    stems = {
        word[: max(SHORTEST_STEM, len(word) - RUSSIAN_ENDING)]
        for word in map(_bare, words)
        if len(word) >= SHORTEST_CORRECTED_WORD and _cyrillic(word)
    }
    return sorted(stems)


def corrections(connection: sqlite3.Connection, words: Sequence[str], tokenizer: str) -> list[str]:
    """Indexed terms near each alphabetic word of the query the index does not hold.

    `tokenizer` is the artifact's own, so the typed word is stemmed as its terms were.
    """
    found: list[str] = []
    for word in map(_bare, words):
        bound = allowed_edits(word)
        if not bound or not word.isalpha() or _indexed(connection, word):
            continue
        found.extend(_nearest(connection, word, bound, tokenizer))
    return found
