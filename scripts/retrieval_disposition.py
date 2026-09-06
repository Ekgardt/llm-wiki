#!/usr/bin/env python3
"""What the memory has learned about which pages carry answers.

The retrieval telemetry has logged 14 806 impressions — which candidate was
*shown*, at which rank, for which query — and its `outcome` column has been null
on every single row. We recorded half a fact fourteen thousand times.

The RAG literature treats that missing half as the hard problem: these systems
hand back a synthesised answer instead of links, so the engagement data that
would say which document helped is never produced, and where feedback does exist
it is clicks, biased by position and selection, needing propensity models and
counterfactual learning-to-rank to be usable at all.

**We are not in that situation.** A grounded answer here names its evidence.
Every published claim carries citation ids, and those ids are exactly the spans
that carried the answer past the gates. That is not a click; it is a verified
statement, by the system that used the evidence, that this page did the work.

This module reads that signal back. Nothing here trains, fits or estimates: it
counts and it decays, which is what the biology is too — a cell that has met a
challenge does not store what it met, it changes how readily it responds
afterwards.

See `docs/research/2026-09-06-a-signal-stronger-than-a-click.md`.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from retrieval_telemetry import TELEMETRY_DB, hash_query  # noqa: E402

# A vault answers from a small set of pages, and a disposition built from more
# than this is not a disposition, it is the whole corpus.
MAX_DISPOSITION_PAGES = 500

# What one page may gain from having carried answers before. Bounded on purpose:
# the failure mode is a page that was useful once becoming permanently
# privileged, and a ceiling is the guard against it.
MAX_DISPOSITION_BOOST = 0.25

# How many citations it takes to reach that ceiling. Low, because in a personal
# vault a page that carried three answers has earned the whole of a small boost.
BOOST_SATURATION = 3


def _read_only(path: Path) -> sqlite3.Connection | None:
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def _counts_statement(digest: str | None) -> tuple[str, tuple]:
    select = "SELECT candidate_id, COUNT(*) FROM retrieval_events WHERE outcome = 'cited'"
    tail = " GROUP BY candidate_id ORDER BY COUNT(*) DESC LIMIT ?"
    if digest is None:
        return select + tail, (MAX_DISPOSITION_PAGES,)
    return select + " AND query_sha256 = ?" + tail, (digest, MAX_DISPOSITION_PAGES)


def cited_counts(query: str | None = None, *, db_path: Path | None = None) -> dict[str, int]:
    """How often each page was named as the evidence that carried an answer.

    With `query`, only answers to that exact question count: narrow, and as
    strong as evidence gets. Without it, every answer counts: weak, and it
    applies everywhere. Those are deliberately different signals.
    """
    database = _read_only(Path(db_path or TELEMETRY_DB))
    if database is None:
        return {}
    try:
        return _counted(database, _digest_of(query))
    finally:
        database.close()


def _digest_of(query: str | None) -> str | None:
    if query is None:
        return None
    return hash_query(query)


def _counted(database: sqlite3.Connection, digest: str | None) -> dict[str, int]:
    statement, parameters = _counts_statement(digest)
    try:
        rows = database.execute(statement, parameters).fetchall()
    except sqlite3.Error:
        return {}
    return {str(row[0]): int(row[1]) for row in rows}


def _boost_for(count: int) -> float:
    if count <= 0:
        return 0.0
    return MAX_DISPOSITION_BOOST * min(count, BOOST_SATURATION) / BOOST_SATURATION


def disposition(query: str | None = None, *, db_path: Path | None = None) -> dict[str, float]:
    """A bounded multiplier per page, from what has carried answers before.

    Returned as a factor above one, so a caller multiplies an existing score and
    the ranking it already computed still decides. A page nothing knows about
    gets nothing rather than a penalty: absence of evidence is not evidence.
    """
    counts = cited_counts(query, db_path=db_path)
    return {page: 1.0 + _boost_for(count) for page, count in counts.items()}
