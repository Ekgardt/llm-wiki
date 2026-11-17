"""Helpers shared by test_contract.py and the test files importing them."""

from types import SimpleNamespace

from llm_wiki import breadcrumb_reader as evidence

from tests.modules.breadcrumb_documents.contract_helpers import _source


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
    monkeypatch.setattr(bounded_io.time, "monotonic", lambda: clock.now)
    return clock
