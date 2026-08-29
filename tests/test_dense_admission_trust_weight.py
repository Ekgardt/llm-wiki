"""The dense leg must admit candidates by the same trust weight the lexical leg uses.

`_generation_result` multiplies the lexical rank by `trust_weight`, so the
lexical backend already decides *admission* by who said it and what the page is.
The dense backend used to overwrite that with raw cosine, which left the vault's
rule -- answer from the compiled pages, read the commentary after -- governing
only the order of candidates already in the pool, never which got into it.

Measured on this vault on 2026-08-29: `docs/` carried 1,409 chunks against
`knowledge/notes`' 620, the questions are Russian and the decision pages are
English, so same-language commentary took every high cosine. The gold page for
eight of ten stand questions ranked 117-310 by raw cosine, outside the 120-row
over-fetch, and `hit@5` fell from 0.7 to 0.3 with `applied@5` from 0.857 to
0.2857. No downstream weight can reach a candidate the backend never returned.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import search_memory  # noqa: E402

_COLUMNS = (
    "chunk_id TEXT, chunk_order INTEGER, source_id TEXT, source_path TEXT, "
    "source_sha256 TEXT, heading_ancestry TEXT, type TEXT, project TEXT, "
    "authority TEXT, confidence TEXT, status TEXT, valid_from TEXT, "
    "valid_to TEXT, language TEXT, title TEXT, content TEXT, rank REAL"
)

_COMMENTARY = "docs/research/2026-08-29-commentary.md"
_DECISION = "knowledge/notes/some-decision.md"


def _row(order: int, path: str, page_type: str) -> tuple:
    return (
        f"chunk-{order}",
        order,
        f"source:{path}",
        path,
        "0" * 64,
        json.dumps([]),
        page_type,
        "",
        "ai-derived",
        "high",
        "active",
        "2026-08-01",
        None,
        "en",
        Path(path).stem,
        "body text",
        0.0,
    )


@pytest.fixture()
def connection() -> sqlite3.Connection:
    handle = sqlite3.connect(":memory:")
    handle.row_factory = sqlite3.Row
    handle.execute(f"CREATE TABLE chunks ({_COLUMNS})")
    handle.executemany(
        f"INSERT INTO chunks VALUES ({','.join('?' * 17)})",
        (_row(0, _COMMENTARY, "doc"), _row(1, _DECISION, "decision")),
    )
    handle.commit()
    return handle


def _scored(connection: sqlite3.Connection, similarities: list[float]) -> list[dict]:
    return search_memory._vector_scored_rows(
        connection,
        similarities,
        "generation-test",
        scope="all",
        since=None,
        as_of=None,
        project=None,
        deadline=None,
        cancelled=None,
    )


def test_commentary_with_the_higher_cosine_ranks_below_the_decision_page(
    connection: sqlite3.Connection,
) -> None:
    """The exact shape measured on the vault: commentary wins on raw cosine.

    0.875 against 0.828 is the real gap between the audit register's best chunk
    and the gold page's on the `lsp-owner-lease` question. Weighted, 0.828 x 1.25
    beats 0.875 x 0.8, which is what puts the decision page back in the pool.
    """
    ranked = _scored(connection, [0.875, 0.828])

    assert [row["path"] for row in ranked] == [_DECISION, _COMMENTARY]


def test_the_dense_score_carries_the_trust_weight(
    connection: sqlite3.Connection,
) -> None:
    """Not merely reordered -- the weight is on the score the caller reads."""
    by_path = {row["path"]: row for row in _scored(connection, [0.875, 0.828])}

    assert by_path[_COMMENTARY]["score"] == pytest.approx(0.875 * 0.8, abs=5e-5)
    assert by_path[_DECISION]["score"] == pytest.approx(0.828 * 1.25, abs=5e-5)


def test_a_large_cosine_gap_still_wins(connection: sqlite3.Connection) -> None:
    """The weight tilts admission; it does not overrule a real difference.

    Guards against the opposite defect: a page type must not be able to bury
    genuinely better matches, or the vault would answer knowledge questions with
    decision pages about something else.
    """
    ranked = _scored(connection, [0.95, 0.40])

    assert ranked[0]["path"] == _COMMENTARY
