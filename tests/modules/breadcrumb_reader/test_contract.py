"""Original complete-source reader assertions at their actual owner."""
from types import SimpleNamespace

import pytest
from llm_wiki import breadcrumb_reader as evidence

from tests.modules.breadcrumb_documents.test_contract import _source


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
