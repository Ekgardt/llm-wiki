"""A Claims ledger stays readable when a blank line and a section follow it.

Compile's update appends `\\n\\n## Update (…)` after the ledger, and three copies
of the ledger pattern required exactly one line break: the page became unreadable
and the whole generation build stopped. Research:
`docs/research/2026-09-14-a-blank-line-before-the-next-section.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from tests.a_blank_line_before_the_next_section_helpers import (
    _record,
    pipeline,  # noqa: F401
)
from tests.claims_helpers import ledger_page  # noqa: E402

UPDATE = {"body_markdown": "The service moved.", "evidence": [{"claim": "moved"}]}


def _updated(page: bytes) -> bytes:
    from llm_wiki import compile_pipeline_apply

    update = compile_pipeline_apply.update_section(UPDATE, ["daily:2026-01-02"], "2026-09-14T00:00:00Z")
    return page.rstrip() + update


def test_a_ledger_before_a_compile_update_still_parses(pipeline):  # noqa: F811
    from claims import parse_claim_ledger

    page = _updated(ledger_page(_record(pipeline)))

    assert len(parse_claim_ledger(page)["claims"]) == 1


def test_new_claims_after_an_update_join_the_one_ledger(pipeline):  # noqa: F811
    from claims import parse_claim_ledger
    from llm_wiki import compile_pipeline_claims

    record = _record(pipeline)
    page = _updated(ledger_page(record))
    again = compile_pipeline_claims.with_claim_ledger(page, [{**record, "id": record["id"] + "-b"}])

    assert again.count(b"## Claims") == 1
    assert parse_claim_ledger(again) is not None


def test_stray_text_after_the_fence_is_still_refused(pipeline):  # noqa: F811
    from claims import parse_claim_ledger

    page = ledger_page(_record(pipeline)) + b"stray words\n"

    with pytest.raises(ValueError, match="Claims ledger must be one fenced"):
        parse_claim_ledger(page)
