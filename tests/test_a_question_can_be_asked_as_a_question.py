"""FTS5 puts an implicit AND between bare terms, and it cost us whole answers.

`MATCH 'one two three'` is `one AND two AND three` — the SQLite documentation
says so in as many words. So a chunk had to contain every word of the question,
and a question phrased as a question matched nothing at all.

Measured on the LongMemEval stand 2026-09-03: "What day of the week do I take a
cocktail-making class?" retrieved **zero** candidates from a vault where
"cocktail class" retrieved three, and three of fifty questions reached the model
with an empty evidence manifest for exactly this reason. The model then said,
correctly, that it had been given nothing.

Joining with OR does not loosen relevance. bm25 splits a query into its
component phrases and scores a row by how many it carries, so a chunk holding
every term still outranks one holding a single term; the difference is that the
one-term chunk is reachable instead of discarded.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import search_memory  # noqa: E402


def test_terms_are_joined_so_that_one_missing_word_is_not_fatal() -> None:
    assert search_memory._fts_query("cocktail class") == '"cocktail" OR "class"'


def test_the_function_words_of_a_question_are_not_matched_on() -> None:
    built = search_memory._fts_query("What day of the week do I take a cocktail class?")

    assert '"what"' not in built
    assert '"the"' not in built
    assert '"cocktail"' in built
    assert '"class?"' in built


def test_a_question_of_nothing_but_function_words_still_searches() -> None:
    """Dropping every term would turn a query into a syntax error, not a search."""
    built = search_memory._fts_query("what is it")

    assert built == '"what" OR "is" OR "it"'


def test_a_quote_in_the_query_cannot_break_out_of_its_phrase() -> None:
    assert search_memory._fts_query('say "hi"') == '"say" OR """hi"""'


def test_an_empty_query_builds_an_empty_expression() -> None:
    assert search_memory._fts_query("   ") == ""
