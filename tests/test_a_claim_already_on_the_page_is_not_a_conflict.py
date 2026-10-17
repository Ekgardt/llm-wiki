"""A day compiled again brings its claims again; one already on the page is kept once.

A claim's id is its date and the fingerprint of its semantics, so the same claim
from a later compile of the same day has the same id. On 2026-09-29 the nightly
compile recompiled a day that had grown since its first compile and stopped with
"compile claim id already exists in target ledger"; the whole compile failed. A
repeat inside one run was already dropped as "duplicate claim semantics"; a repeat
of what the page holds is now the same. A different fingerprint under one id is
still refused. See docs/research/2026-09-29-a-claim-already-on-the-page-is-kept-once.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from tests.test_a_blank_line_before_the_next_section import _record, pipeline  # noqa: E402, F401
from tests.test_claims import ledger_page  # noqa: E402


def test_the_same_claim_compiled_again_leaves_the_page_as_it_was(pipeline) -> None:  # noqa: F811
    import compile_memory

    record = _record(pipeline)
    page = ledger_page(record)

    assert compile_memory._with_claim_ledger(page, [record]) == compile_memory._with_claim_ledger(page, [])


def test_one_id_with_another_fingerprint_is_still_a_conflict(pipeline) -> None:  # noqa: F811
    import compile_memory

    record = _record(pipeline)
    page = ledger_page(record)

    with pytest.raises(ValueError, match="compile claim id already exists"):
        compile_memory._with_claim_ledger(page, [{**record, "fingerprint": "0" * 64}])
