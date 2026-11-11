"""Permanent sources retain every input byte and cannot validate partial copies."""
from __future__ import annotations

import breadcrumb_evidence as evidence
import pytest

from tests.modules.breadcrumb_documents.contract_helpers import _source as _source
from tests.modules.breadcrumb_reader.contract_helpers import _disk_source as _disk_source
from tests.modules.breadcrumb_reader.contract_helpers import (
    _fake_reader_clock as _fake_reader_clock,
)


@pytest.mark.parametrize("source_restored", [False, True])
def test_a_refused_source_can_retry_without_erasing_the_refusal(tmp_path, source_restored):
    from markdown_transaction import MarkdownChange, TransactionFailure
    from reliable_memory import canonical_json_bytes, sha256_bytes

    from tests.queue_v3_capture_links_helpers import _coordinator, _queue

    _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    relative = "knowledge/raw/sessions/2026-09-29/retry.md"
    (coordinator.vault / relative).parent.mkdir(parents=True, exist_ok=True)
    document = b"---\ntype: raw-source\n---\ncomplete evidence\n"
    operation = "breadcrumb-source:" + sha256_bytes(canonical_json_bytes({
        "path": relative, "sha256": sha256_bytes(document),
    }))
    gate = coordinator.vault / "knowledge/notes/source-gate.md"
    gate.parent.mkdir(parents=True, exist_ok=True)
    gate.write_bytes(b"accepted")
    with coordinator.writer_gate() as owner:
        refused = coordinator.prepare(
            [MarkdownChange.create(relative, document)], operation_id=operation,
            preconditions={"knowledge/notes/source-gate.md": sha256_bytes(b"accepted")},
        )
        gate.write_bytes(b"new accepted version")
        with pytest.raises(TransactionFailure, match="precondition"):
            coordinator.apply(refused.id)
        assert coordinator._record(refused.id).state == "quarantined"
        assert not (coordinator.vault / relative).exists()
        if source_restored:
            (coordinator.vault / relative).write_bytes(document)
        evidence.create_source_document(coordinator, owner, relative, document, {})
        evidence.create_source_document(coordinator, owner, relative, document, {})
    assert (coordinator.vault / relative).read_bytes() == document
    assert coordinator._record(refused.id).state == "quarantined"
    _assert_source_retry_witness(coordinator, refused.id, operation, document, source_restored)


def _assert_source_retry_witness(coordinator, refused_id, operation, document, source_restored):
    from reliable_memory import sha256_bytes

    with coordinator._connect() as database:
        committed = database.execute(
            'SELECT parent_transaction_id FROM "transaction" WHERE state=\'committed\' '
            'AND operation_id LIKE ?', (operation + "%",),
        ).fetchall()
        from transaction_lineage import resolved_quarantines
        assert refused_id in resolved_quarantines(database)
        rows = database.execute(
            'SELECT operation.before_hash, operation.after_hash FROM operation '
            'JOIN "transaction" ON operation.transaction_id="transaction".id '
            'WHERE "transaction".parent_transaction_id=? AND "transaction".state=\'committed\'',
            (refused_id,),
        ).fetchall()
    assert [row[0] for row in committed] == [refused_id]
    assert [row[1] for row in rows] == [sha256_bytes(document)]
    if source_restored:
        assert [row[0] for row in rows] == [sha256_bytes(document)]


def test_evidence_facade_keeps_identical_document_objects():
    from llm_wiki import breadcrumb_documents

    assert evidence.source_path is breadcrumb_documents.source_path
    assert evidence.source_part_path is breadcrumb_documents.source_part_path
    assert evidence.source_documents is breadcrumb_documents.source_documents
    assert evidence.render_head is breadcrumb_documents.render_head
    assert evidence.render_part is breadcrumb_documents.render_part
    assert evidence.restore_source is breadcrumb_documents.restore_source
    assert evidence.read_head is breadcrumb_documents.read_head
    assert evidence.archived_source_path is breadcrumb_documents.archived_source_path


def test_reader_facade_preserves_function_identity():
    from llm_wiki import breadcrumb_reader

    for name in ('source_target', 'read_source_document', 'read_exact_source_document', 'read_permanent_source'):
        assert getattr(evidence, name) is getattr(breadcrumb_reader, name)
