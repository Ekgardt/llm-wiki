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

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from tests.dense_admission_trust_weight_helpers import (  # noqa: E402
    _COMMENTARY,
    _DECISION,
    _scored,
    connection,  # noqa: F401
)


def test_commentary_with_the_higher_cosine_ranks_below_the_decision_page(
    connection: sqlite3.Connection,  # noqa: F811
) -> None:
    """The exact shape measured on the vault: commentary wins on raw cosine.

    0.875 against 0.828 is the real gap between the audit register's best chunk
    and the gold page's on the `lsp-owner-lease` question. Weighted, 0.828 x 1.25
    beats 0.875 x 0.8, which is what puts the decision page back in the pool.
    """
    ranked = _scored(connection, [0.875, 0.828])

    assert [row["path"] for row in ranked] == [_DECISION, _COMMENTARY]


def test_the_dense_score_carries_the_trust_weight(
    connection: sqlite3.Connection,  # noqa: F811
) -> None:
    """Not merely reordered -- the weight is on the score the caller reads."""
    by_path = {row["path"]: row for row in _scored(connection, [0.875, 0.828])}

    assert by_path[_COMMENTARY]["score"] == pytest.approx(0.875 * 0.8, abs=5e-5)
    assert by_path[_DECISION]["score"] == pytest.approx(0.828 * 1.25, abs=5e-5)


def test_a_large_cosine_gap_still_wins(connection: sqlite3.Connection) -> None:  # noqa: F811
    """The weight tilts admission; it does not overrule a real difference.

    Guards against the opposite defect: a page type must not be able to bury
    genuinely better matches, or the vault would answer knowledge questions with
    decision pages about something else.
    """
    ranked = _scored(connection, [0.95, 0.40])

    assert ranked[0]["path"] == _COMMENTARY
