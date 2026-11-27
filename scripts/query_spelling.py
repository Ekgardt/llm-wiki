"""Words a query misspells, matched by the indexed words one or two edits away.

FTS5 matches whole tokens, so "bookhself" finds nothing although "bookshelf" is
indexed. A word the index already holds is left alone; a word it does not hold
gains the indexed terms closest to it, the way MiniSearch's fuzzy match serves
Obsidian Omnisearch. The vocabulary is the artifact's own (`fts5vocab`), and the
typed word is stemmed by the artifact's own tokenizer, so both sides of the
comparison are in the form the index stores.
"""
from __future__ import annotations

import bisect
import os
import sqlite3
import string
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

# Edits allowed by word length, as in Lucene's fuzzy AUTO and MiniSearch's
# fractional fuzziness: none below five letters, where one edit makes another
# word, one below nine, two from nine.
SHORTEST_CORRECTED_WORD = 5
TWO_EDITS_FROM = 9
# Corrections kept per word: the closest, then the most common.
CORRECTIONS_PER_WORD = 3
# Letters a word drops before matching every indexed form of it, and the
# shortest root that is still matched by prefix.
ENDING_LETTERS = 2
SHORTEST_STEM = 4
# Quotation marks a word may carry besides ASCII punctuation: guillemets and curly quotes.
_QUOTES = "\u00ab\u00bb\u201c\u201d\u2018\u2019"

_VOCABULARY = "temp.llm_wiki_vocabulary"
_STEMMER = "temp.llm_wiki_stemmer"
_STEMS = "temp.llm_wiki_stems"
# Sorts after every letter a term can hold: the end of a prefix's range.
_AFTER_EVERY_LETTER = "\U0010ffff"


def allowed_edits(word: str) -> int:
    if len(word) < SHORTEST_CORRECTED_WORD:
        return 0
    if len(word) < TWO_EDITS_FROM:
        return 1
    return 2


def _alignment_row(
    left_char: str, left_prev: str, right: str, rows: tuple[list[int], list[int]], i: int
) -> list[int]:
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
    return word.strip(string.punctuation + _QUOTES).casefold()


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
    connection.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {_STEMS} "
        "USING fts5vocab(temp, llm_wiki_stemmer, 'row')"
    )
    connection.execute(f"DELETE FROM {_STEMMER}")
    connection.execute(f"INSERT INTO {_STEMMER}(word) VALUES (?)", [word])
    row = connection.execute(f"SELECT term FROM {_STEMS} LIMIT 1").fetchone()
    return str(row[0]) if row else word


def _create_vocabulary(connection: sqlite3.Connection) -> None:
    connection.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {_VOCABULARY} USING fts5vocab(main, chunks, 'row')"
    )


@dataclass(frozen=True)
class Vocabulary:
    """An artifact's indexed terms, sorted so terms sharing a prefix share its alignment rows."""

    terms: tuple[str, ...]
    documents: dict[str, int]


# The vocabulary of the artifact read last, keyed by its file and modification
# time. A generation is immutable after activation and one is active at a time,
# so one entry holds every search's vocabulary without reading it again.
_LOADED: dict[tuple[str, int], Vocabulary] = {}


def _artifact_identity(connection: sqlite3.Connection) -> tuple[str, int] | None:
    path = next(
        (str(row[2]) for row in connection.execute("PRAGMA database_list") if row[1] == "main"), ""
    )
    if not path or not os.path.exists(path):
        return None
    return path, os.stat(path).st_mtime_ns


def _read_vocabulary(connection: sqlite3.Connection) -> Vocabulary:
    _create_vocabulary(connection)
    documents = {
        str(term): int(doc)
        for term, doc in connection.execute(f"SELECT term, doc FROM {_VOCABULARY}")
    }
    return Vocabulary(tuple(sorted(documents)), documents)


def vocabulary(connection: sqlite3.Connection) -> Vocabulary:
    """The artifact's vocabulary, read once per immutable file.

    An in-memory artifact has no file to remember, so it is read each time.
    """
    identity = _artifact_identity(connection)
    if identity is None:
        return _read_vocabulary(connection)
    loaded = _LOADED.get(identity)
    if loaded is None:
        loaded = _read_vocabulary(connection)
        _LOADED.clear()
        _LOADED[identity] = loaded
    return loaded


def _common_prefix(left: str, right: str) -> int:
    shorter = min(len(left), len(right))
    pairs = enumerate(zip(left[:shorter], right[:shorter], strict=True))
    return next((index for index, (a, b) in pairs if a != b), shorter)


def _extend(rows: list[list[int]], term: str, word: str, bound: int) -> int | None:
    """Align the rest of `term` against `word`; the prefix length past which nothing can match."""
    for i in range(len(rows), len(term) + 1):
        before = rows[i - 2] if i > 1 else []
        left_prev = term[i - 2] if i > 1 else ""
        rows.append(_alignment_row(term[i - 1], left_prev, word, (before, rows[i - 1]), i))
        if min(rows[i]) > bound:
            return i
    return None


def _within(terms: Sequence[str], word: str, bound: int) -> dict[str, int]:
    """Every term within `bound` edits of `word`, walking the sorted terms as a trie.

    A prefix whose alignment row already exceeds the bound rules out every term
    under it, so those are skipped by bisection instead of being compared.
    """
    found: dict[str, int] = {}
    rows = [list(range(len(word) + 1))]
    previous, index = "", 0
    while index < len(terms):
        term = terms[index]
        del rows[_common_prefix(previous, term) + 1 :]
        dead_end = _extend(rows, term, word, bound)
        previous = term
        if dead_end is not None:
            index = bisect.bisect_left(terms, term[:dead_end] + _AFTER_EVERY_LETTER, index + 1)
            continue
        if rows[-1][-1] <= bound:
            found[term] = rows[-1][-1]
        index += 1
    return found


def _closest(left: dict[str, int], right: dict[str, int], bound: int) -> dict[str, int]:
    return {
        term: min(left.get(term, bound + 1), right.get(term, bound + 1))
        for term in left.keys() | right.keys()
    }


def _nearest(connection: sqlite3.Connection, word: str, bound: int, tokenizer: str) -> list[str]:
    stem = _stemmed(connection, word, tokenizer)
    terms = vocabulary(connection)
    distances = _closest(
        _within(terms.terms, word, bound), _within(terms.terms, stem, bound), bound
    )
    scored = sorted(
        (distance, -terms.documents[term], term)
        for term, distance in distances.items()
        if len(stem) - bound <= len(term) <= len(word) + bound
    )
    return [term for _, _, term in scored[:CORRECTIONS_PER_WORD]]


def _cyrillic(word: str) -> bool:
    return all(unicodedata.name(letter, "").startswith("CYRILLIC") for letter in word)


def light_stem(word: str) -> str:
    """The word without its last two letters, keeping at least four."""
    return word[: max(SHORTEST_STEM, len(word) - ENDING_LETTERS)]


def cyrillic_stems(words: Sequence[str]) -> list[str]:
    """The light stem of each Cyrillic word of five letters or more.

    The tokenizer's Porter stemmer is English only, so a Russian noun in the
    genitive plural never met the same noun in the nominative. Two letters cover
    most Russian case and number endings, and four letters are always kept, so a
    stem stays a word's root rather than a syllable.
    Measured 2026-11-21 on 50 Russian questions over this repository's Russian
    documents, lexical lane alone: MRR@10 0.369 to 0.499, hit@5 0.50 to 0.68. The
    Snowball Russian stemmer scored 0.476, so it does not earn a dependency.
    """
    stems = {
        light_stem(word)
        for word in map(_bare, words)
        if len(word) >= SHORTEST_CORRECTED_WORD and _cyrillic(word)
    }
    return sorted(stems)


def _containing(connection: sqlite3.Connection, word: str, tokenizer: str) -> list[str]:
    _create_vocabulary(connection)
    stem = _stemmed(connection, word, tokenizer)
    rows = connection.execute(
        f"SELECT term FROM {_VOCABULARY} WHERE length(term) > ? AND instr(term, ?) "
        "ORDER BY doc DESC, term LIMIT ?",
        [len(stem), stem, CORRECTIONS_PER_WORD],
    ).fetchall()
    return [str(row[0]) for row in rows]


def glued_words(connection: sqlite3.Connection, words: Sequence[str], tokenizer: str) -> list[str]:
    """Indexed words that hold a query word the index does not hold as a word of its own.

    The tokenizer keeps "AutoModelForSequenceClassification" as one token, so
    "SequenceClassification" never matched it. Measured 2026-11-21 on 131 such
    identifiers of this repository's research notes, queried by all but their
    first part: hit@10 0.05 to 0.86 with no index of its own; an FTS5 trigram
    index reached 0.98 but doubled the size of the artifact.
    """
    found: list[str] = []
    for word in map(_bare, words):
        if len(word) < SHORTEST_CORRECTED_WORD or not word.isalnum():
            continue
        if not _indexed(connection, word):
            found.extend(_containing(connection, word, tokenizer))
    return found


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
