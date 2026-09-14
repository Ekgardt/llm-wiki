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

from tests.test_claims import ledger_page, pipeline, raw_claim, source_bytes  # noqa: E402,F401

UPDATE = {"body_markdown": "The service moved.", "evidence": [{"claim": "moved"}]}


def _record(pipeline) -> dict:
    block = pipeline.split_blocks(source_bytes())[0]
    return pipeline.normalize(pipeline.verify_literal(pipeline.extract(block, raw_claim())[0])).record


def _updated(page: bytes) -> bytes:
    import compile_memory

    update = compile_memory._update_section(UPDATE, ["daily:2026-01-02"], "2026-09-14T00:00:00Z")
    return page.rstrip() + update


def test_a_ledger_before_a_compile_update_still_parses(pipeline):
    from claims import parse_claim_ledger

    page = _updated(ledger_page(_record(pipeline)))

    assert len(parse_claim_ledger(page)["claims"]) == 1


def test_new_claims_after_an_update_join_the_one_ledger(pipeline):
    import compile_memory
    from claims import parse_claim_ledger

    record = _record(pipeline)
    page = _updated(ledger_page(record))
    again = compile_memory._with_claim_ledger(page, [{**record, "id": record["id"] + "-b"}])

    assert again.count(b"## Claims") == 1
    assert parse_claim_ledger(again) is not None


def test_stray_text_after_the_fence_is_still_refused(pipeline):
    from claims import parse_claim_ledger

    page = ledger_page(_record(pipeline)) + b"stray words\n"

    with pytest.raises(ValueError, match="Claims ledger must be one fenced"):
        parse_claim_ledger(page)
