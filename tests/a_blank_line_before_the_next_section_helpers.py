"""Helpers shared by test_a_blank_line_before_the_next_section.py and the test files importing them."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.claims_helpers import raw_claim, source_bytes


@pytest.fixture
def pipeline(tmp_path: Path):
    from claims import ClaimPipeline
    from evidence_resolver import EvidenceResolver

    daily = tmp_path / "knowledge/daily/2026-01-02.md"
    daily.parent.mkdir(parents=True)
    daily.write_bytes(source_bytes())
    return ClaimPipeline(EvidenceResolver(tmp_path))


def _record(pipeline) -> dict:
    block = pipeline.split_blocks(source_bytes())[0]
    return pipeline.normalize(pipeline.verify_literal(pipeline.extract(block, raw_claim())[0])).record
