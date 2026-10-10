from __future__ import annotations

import sqlite3
from contextlib import contextmanager

import pytest
from markdown_transaction import MarkdownChange, MarkdownCoordinator

from tests.test_compile_transactions import vault as vault


def test_ordinal_authority_does_not_scan_unrelated_transactions(vault):
    root, state = vault
    coordinator = MarkdownCoordinator(root, state)
    queries = []
    original = coordinator._connect

    @contextmanager
    def observed_connect(*args, **kwargs):
        with original(*args, **kwargs) as database:
            database.set_trace_callback(queries.append)
            yield database

    coordinator._connect = observed_connect
    assert coordinator._committed_attempt_by_ordinal("missing-proof") is None
    query, = [item for item in queries if item.startswith('SELECT id FROM "transaction"')]
    with sqlite3.connect(coordinator.database_path) as database:
        plan = database.execute("EXPLAIN QUERY PLAN " + query).fetchall()
    assert any("SEARCH transaction USING INDEX" in row[3] for row in plan), plan
    assert all("SCAN transaction" not in row[3] for row in plan), plan


@pytest.mark.parametrize("identity", ("proof", "proof_%\\", "доказательство"))
def test_ordinal_authority_matches_literal_identity_and_latest_commit(vault, identity):
    root, state = vault
    coordinator = MarkdownCoordinator(root, state)
    committed = []
    for ordinal in (1, 2):
        prepared = coordinator.prepare(
            (MarkdownChange.create(f"knowledge/notes/ordinal-{ordinal}.md", b"# Evidence\n"),),
            operation_id=f"{identity}#{ordinal}",
        )
        committed.append(coordinator.apply(prepared.id))
    assert coordinator.committed_attempt(identity).id == committed[-1].id
    assert coordinator.committed_attempt(identity + "-other") is None
