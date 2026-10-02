"""Checked runtime staging is a hint for an occurrence, never accepted evidence."""
from __future__ import annotations

import secrets
import time

import breadcrumb_storage as storage
import pytest
from reliable_memory import _write_staged_bytes, canonical_json_bytes

from tests.slow_machine import LONG_TIMEOUT
from tests.test_breadcrumb_adoption import _orphan
from tests.test_breadcrumb_storage import MOMENT, SCOPE
from tests.test_queue_v3_capture_links import _coordinator, _queue


def _stage(target, content):
    path = target.with_name(f".{target.name}.{secrets.token_hex(16)}.tmp")
    _write_staged_bytes(path, content, 0o600)
    return path


def test_a_staged_sibling_does_not_invalidate_a_verified_occurrence(tmp_path, monkeypatch):
    _queue, _coordinator, identity = _orphan(tmp_path, monkeypatch, "index_capture_intent_pending")
    anchor = storage.anchor_path(tmp_path, identity)
    staged = _stage(anchor, anchor.read_bytes())
    before = staged.read_bytes()
    assert storage.inspect_pending_sources(tmp_path, set(), deadline=time.monotonic() + LONG_TIMEOUT) == {"complete": 1, "incomplete": 0}
    assert staged.read_bytes() == before


def test_a_complete_staged_part_without_a_manifest_stays_incomplete(tmp_path):
    from integration_adapter import _ensure_capture_intent_directories

    _queue(tmp_path)
    _coordinator(tmp_path)
    identity = storage.protocol.occurrence_identity(SCOPE)
    content = canonical_json_bytes({"prompt": "only staged evidence"})
    _ensure_capture_intent_directories(tmp_path, identity)
    anchor = storage.protocol.make_anchor(identity, content, occurred_at=MOMENT, accepted_at=MOMENT, time_origin="host")
    storage._publish(tmp_path, storage.anchor_path(tmp_path, identity), anchor)
    part = storage.protocol.encode_parts(identity, content)[0]
    staged = _stage(storage.part_path(tmp_path, identity, storage.sha256_bytes(part)), part)
    before = staged.read_bytes()
    assert storage.inspect_pending_sources(tmp_path, set(), deadline=time.monotonic() + LONG_TIMEOUT) == {"complete": 0, "incomplete": 1}
    assert staged.read_bytes() == before


def test_an_unknown_dotfile_is_still_refused(tmp_path, monkeypatch):
    _queue, _coordinator, identity = _orphan(tmp_path, monkeypatch, "index_capture_intent_pending")
    path = storage.anchor_path(tmp_path, identity).with_name(".unknown.tmp")
    path.write_bytes(b"not an admitted runtime staging name")
    with pytest.raises(ValueError):
        storage.inspect_pending_sources(tmp_path, set(), deadline=time.monotonic() + LONG_TIMEOUT)
    assert path.read_bytes() == b"not an admitted runtime staging name"
