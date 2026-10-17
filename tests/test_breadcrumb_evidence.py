"""Permanent sources retain every input byte and cannot validate partial copies."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import breadcrumb_evidence as evidence
import breadcrumb_protocol as protocol
import pytest
from bounded_io import MAX_KNOWLEDGE_PAGE_BYTES


def _source(content: bytes):
    moment = datetime(2026, 9, 29, tzinfo=timezone.utc)
    anchor = protocol.make_anchor(
        "a" * 64, content, occurred_at=moment, accepted_at=moment, time_origin="host",
    )
    parts = protocol.encode_parts("a" * 64, content)
    manifest = protocol.make_manifest(anchor, parts)
    documents = dict(evidence.source_documents(manifest, anchor, parts))
    return evidence.source_path(anchor), documents


@pytest.mark.parametrize("content", [
    b"a complete prompt", ('```\n---\n# quoted heading\n"\\\u2028\u65e5' * 1000).encode(),
    b"x" * (2 * 1_048_576),
], ids=["small", "quoted-unicode", "ascii-2-mib"])
def test_permanent_markdown_reconstructs_the_complete_source_without_runtime(content):
    head, documents = _source(content)

    assert evidence.restore_source(head, documents.__getitem__) == content
    assert next(reversed(documents)) == head
    assert all(len(document) <= MAX_KNOWLEDGE_PAGE_BYTES for document in documents.values())
    assert all(document.startswith(b"---\ntype: raw-source\n") for document in documents.values())


def test_a_missing_permanent_part_cannot_prove_completion():
    head, documents = _source(b"x" * (2 * 1_048_576))
    del documents[next(iter(documents))]

    with pytest.raises(KeyError):
        evidence.restore_source(head, documents.__getitem__)


@pytest.mark.parametrize("target", ["head", "part"])
def test_editing_a_permanent_record_invalidates_the_proof(target):
    head, documents = _source(b"complete prompt")
    path = head if target == "head" else next(iter(documents))
    documents[path] += b"unverified change\n"

    with pytest.raises((ValueError, TypeError)):
        evidence.restore_source(head, documents.__getitem__)


def test_a_complete_source_moved_to_another_day_does_not_prove_the_original_path():
    head, documents = _source(b"complete prompt")
    changed = head.replace("2026-09-29", "2026-09-30")
    documents[changed] = documents.pop(head)

    with pytest.raises(ValueError, match="conflicts"):
        evidence.restore_source(changed, documents.__getitem__)


def _disk_source(root, content, *, archived=False):
    head, documents = _source(content)
    for relative, data in documents.items():
        stored = evidence.archived_source_path(relative) if archived else relative
        target = root / stored
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return head


def _fake_reader_clock(monkeypatch):
    import bounded_io

    clock = SimpleNamespace(now=10.0)
    monkeypatch.setattr(bounded_io, "time", SimpleNamespace(monotonic=lambda: clock.now))
    return clock


def test_expired_source_deadline_refuses_before_reading_any_path(tmp_path, monkeypatch):
    _fake_reader_clock(monkeypatch)

    with pytest.raises(TimeoutError, match="deadline"):
        evidence.read_permanent_source(tmp_path, "missing", deadline=9.0)


@pytest.mark.parametrize("archived", [False, True])
def test_source_parts_and_archive_share_one_deadline(tmp_path, monkeypatch, archived):
    content = b"complete event" * 100000
    head = _disk_source(tmp_path, content, archived=archived)
    clock = _fake_reader_clock(monkeypatch)
    original = evidence.read_stable_bytes
    seen = []

    def consume_budget(path, limit, **kwargs):
        seen.append(kwargs.get("deadline"))
        data = original(path, limit, **kwargs)
        clock.now += 1.0
        return data

    monkeypatch.setattr(evidence, "read_stable_bytes", consume_budget)
    with pytest.raises(TimeoutError, match="deadline"):
        evidence.read_permanent_source(tmp_path, head, deadline=12.0)
    assert seen and set(seen) == {12.0}


def test_expiry_during_final_verification_cannot_return_success(tmp_path, monkeypatch):
    head = _disk_source(tmp_path, b"complete event")
    clock = _fake_reader_clock(monkeypatch)
    restore = evidence.restore_source

    def finish_after_deadline(*args, **kwargs):
        content = restore(*args, **kwargs)
        clock.now = 12.0
        return content

    monkeypatch.setattr(evidence, "restore_source", finish_after_deadline)
    with pytest.raises(TimeoutError, match="deadline"):
        evidence.read_permanent_source(tmp_path, head, deadline=12.0)


@pytest.mark.parametrize("deadline, error", [(True, TypeError), (float("inf"), ValueError), (float("nan"), ValueError)])
def test_invalid_source_deadline_is_refused_before_access(tmp_path, deadline, error):
    with pytest.raises(error, match="deadline"):
        evidence.read_permanent_source(tmp_path, "missing", deadline=deadline)


@pytest.mark.parametrize("archived", [False, True])
def test_source_read_with_time_remaining_keeps_full_evidence(tmp_path, monkeypatch, archived):
    content = b"complete event" * 100000
    head = _disk_source(tmp_path, content, archived=archived)
    _fake_reader_clock(monkeypatch)

    assert evidence.read_permanent_source(tmp_path, head, deadline=12.0) == content


def test_a_refused_source_can_retry_without_erasing_the_refusal(tmp_path):
    from markdown_transaction import MarkdownChange, TransactionFailure
    from reliable_memory import canonical_json_bytes, sha256_bytes

    from tests.test_queue_v3_capture_links import _coordinator, _queue

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
        evidence._create_source_document(coordinator, owner, relative, document, {})
        evidence._create_source_document(coordinator, owner, relative, document, {})
    assert (coordinator.vault / relative).read_bytes() == document
    assert coordinator._record(refused.id).state == "quarantined"
    with coordinator._connect() as database:
        committed = database.execute(
            'SELECT parent_transaction_id FROM "transaction" WHERE state=\'committed\' '
            'AND operation_id LIKE ?', (operation + "%",),
        ).fetchall()
    assert [row[0] for row in committed] == [refused.id]
