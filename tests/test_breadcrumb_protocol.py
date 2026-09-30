"""Full logical evidence survives physical-record partitioning and verification."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import breadcrumb_protocol as protocol
import pytest
from reliable_memory import canonical_json_bytes, sha256_bytes


def _encoded(content: bytes, intent_id: str = "a" * 64):
    moment = datetime(2026, 9, 29, tzinfo=timezone.utc)
    anchor = protocol.make_anchor(
        intent_id, content, occurred_at=moment, accepted_at=moment, time_origin="host",
    )
    parts = protocol.encode_parts(intent_id, content)
    manifest = protocol.make_manifest(anchor, parts)
    return anchor, parts, manifest


@pytest.mark.parametrize("text", ["small", '日\\"\n' * 1000, "x" * 1_048_576, "🧠" * 300_000])
def test_every_byte_is_restored_and_every_record_fits_the_existing_transport(text):
    content = text.encode("utf-8")
    started = time.perf_counter()
    anchor, parts, manifest = _encoded(content)
    by_digest = {sha256_bytes(part): part for part in parts}

    restored = protocol.restore_input(manifest, anchor, by_digest.__getitem__)

    assert restored == content
    assert max(map(len, (anchor, manifest, *parts))) <= protocol.MAX_CAPTURE_INTENT_BYTES
    print(f"input_bytes={len(content)} parts={len(parts)} seconds={time.perf_counter() - started:.6f}")


def _many_parts(monkeypatch):
    # Reduce only the test transport to exercise multi-part failures cheaply.
    overhead = len(canonical_json_bytes(protocol._part_record("a" * 64, 0, "b" * 64, "")))
    monkeypatch.setattr(protocol, "MAX_CAPTURE_INTENT_BYTES", overhead + 512)
    return _encoded(b"payload " * 1000)


@pytest.mark.parametrize("change", ["missing", "tampered", "foreign", "reordered"])
def test_incomplete_or_conflicting_parts_cannot_be_restored(monkeypatch, change):
    anchor, parts, manifest = _many_parts(monkeypatch)
    by_digest = {sha256_bytes(part): part for part in parts}
    target = sha256_bytes(parts[-1])
    changes = {
        "missing": lambda: by_digest.pop(target),
        "tampered": lambda: by_digest.__setitem__(target, parts[-1] + b" "),
        "foreign": lambda: by_digest.__setitem__(target, parts[-1].replace(b'a' * 64, b'b' * 64)),
        "reordered": lambda: by_digest.__setitem__(target, parts[0]),
    }
    changes[change]()

    with pytest.raises((KeyError, ValueError)):
        protocol.restore_input(manifest, anchor, by_digest.__getitem__)


@pytest.mark.parametrize("parts_change", ["reordered", "duplicated", "missing"])
def test_manifest_construction_requires_a_complete_ordered_chain(monkeypatch, parts_change):
    anchor, parts, _ = _many_parts(monkeypatch)
    alternatives = {
        "reordered": tuple(reversed(parts)),
        "duplicated": (*parts, parts[-1]),
        "missing": (parts[0], *parts[2:]),
    }

    with pytest.raises((ValueError, KeyError)):
        protocol.make_manifest(anchor, alternatives[parts_change])


def test_part_binding_is_checked_even_when_the_manifest_has_the_modified_hash():
    anchor, parts, manifest = _encoded(b"payload")
    part = json.loads(parts[0])
    part["intent_id"] = "b" * 64
    forged = canonical_json_bytes(part)
    record = json.loads(manifest)
    record["last_part_sha256"] = sha256_bytes(forged)

    with pytest.raises(ValueError, match="binding"):
        protocol.restore_input(canonical_json_bytes(record), anchor, lambda _digest: forged)


@pytest.mark.parametrize("change", [{"day": "2026-09-28"}, {"occurred_at": "2026-09-29T00:00:00"}])
def test_occurrence_metadata_cannot_change_the_day_or_lose_its_timezone(change):
    anchor, _, _ = _encoded(b"payload")
    record = json.loads(anchor)
    record.update(change)

    with pytest.raises(ValueError):
        protocol.read_anchor(canonical_json_bytes(record))


def test_occurrence_identity_is_scoped_and_does_not_use_payload_content():
    scope = {"host": "claude", "session": "one", "event": "user_prompt", "occurrence": "event-one"}

    assert protocol.occurrence_identity(scope) == protocol.occurrence_identity(dict(scope))
    assert protocol.occurrence_identity(scope) != protocol.occurrence_identity({**scope, "occurrence": "event-two"})
    assert protocol.occurrence_identity(scope) != protocol.occurrence_identity({**scope, "session": "two"})


def test_a_manifest_cannot_substitute_a_different_anchor():
    anchor, parts, manifest = _encoded(b"payload")
    changed = json.loads(anchor)
    changed["time_origin"] = "acceptance"
    by_digest = {sha256_bytes(part): part for part in parts}

    with pytest.raises(ValueError, match="anchor digest"):
        protocol.restore_input(manifest, canonical_json_bytes(changed), by_digest.__getitem__)


def test_empty_input_is_refused_instead_of_fabricating_a_part():
    with pytest.raises(ValueError, match="empty"):
        protocol.encode_parts("a" * 64, b"")
