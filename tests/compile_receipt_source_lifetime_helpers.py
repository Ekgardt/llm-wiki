"""Helpers shared by test_compile_receipt_source_lifetime.py and the test files importing them."""

from dataclasses import replace

import compile_receipts
import pytest
from markdown_transaction import MarkdownCoordinator
from reliable_memory import sha256_bytes


@pytest.fixture
def selection(tmp_path):
    (tmp_path / "knowledge/daily").mkdir(parents=True)
    return compile_receipts.receipt_predicate(MarkdownCoordinator(tmp_path, tmp_path))


def _daily(selection, name, body):
    logical = f"knowledge/daily/{name}.md"
    (selection.vault / logical).write_bytes(body)
    return logical, sha256_bytes(body)


def _part_snapshots(selection, logical):
    with selection._source_parts(logical) as parts:
        return tuple(replace(part, original_content=part.original_content[:]) for part in parts)
