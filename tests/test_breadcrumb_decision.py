"""Receipt planning is deterministic, lossless by reference, and provider-free."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone

import breadcrumb_decision as decision
import breadcrumb_protocol as protocol
import pytest
from breadcrumb_storage import BreadcrumbBundle
from reliable_memory import MAX_CAPTURE_DECISION_BYTES, canonical_json_bytes


def _bundle(prompt: str) -> BreadcrumbBundle:
    content = canonical_json_bytes({"prompt": prompt})
    moment = datetime(2026, 9, 29, 23, 59, 59, tzinfo=timezone.utc)
    anchor = protocol.make_anchor(
        "a" * 64, content, occurred_at=moment, accepted_at=moment, time_origin="host",
    )
    parts = protocol.encode_parts("a" * 64, content)
    return BreadcrumbBundle(protocol.make_manifest(anchor, parts), anchor, parts, content)


def test_small_event_is_whole_in_the_journal_and_has_no_model_metadata():
    bundle = _bundle("complete prompt\n```\n# quoted header")
    data = decision.decision_bytes(bundle)
    record = decision.read_decision(data, bundle)
    plan = record["operation_plan"][0]

    assert record["processing"] == "deterministic"
    assert "provider" not in record
    assert "wire_output" not in record
    assert bundle.content.decode() in plan["block"]
    assert plan["path"] == "knowledge/daily/2026-09-29.md"
    assert "[23:59:59]" in plan["block"]
    assert decision.decision_bytes(bundle) == data


def test_large_event_uses_complete_source_reference_without_a_larger_receipt_cap():
    bundle = _bundle("x" * 1_048_576)
    data = decision.decision_bytes(bundle)
    record = decision.read_decision(data, bundle)

    assert len(data) <= MAX_CAPTURE_DECISION_BYTES
    assert record["complete_input_sha256"] == protocol.read_manifest(bundle.manifest)["input_sha256"]
    assert record["input_bytes"] == len(bundle.content)
    assert record["source"]["path"].endswith(".breadcrumb.md")
    assert "[Complete captured event]" in record["operation_plan"][0]["block"]


def test_receipt_refuses_content_substituted_after_publication():
    bundle = replace(_bundle("original"), content=canonical_json_bytes({"prompt": "changed"}))

    with pytest.raises(ValueError, match="input"):
        decision.decision_bytes(bundle)


def test_receipt_refuses_a_different_day_even_with_a_matching_block_digest():
    bundle = _bundle("original")
    record = json.loads(decision.decision_bytes(bundle))
    record["operation_plan"][0]["path"] = "knowledge/daily/2026-09-30.md"

    with pytest.raises(ValueError, match="conflicts"):
        decision.read_decision(canonical_json_bytes(record), bundle)


@pytest.mark.parametrize("content", [b"[1,2]", b'{\n"prompt":"injected formatting"\n}'])
def test_journal_rendering_requires_a_canonical_event_object(content):
    original = _bundle("original")
    anchor_record = protocol.read_anchor(original.anchor)
    moment = datetime.fromisoformat(anchor_record["occurred_at"])
    anchor = protocol.make_anchor(
        anchor_record["intent_id"], content, occurred_at=moment,
        accepted_at=moment, time_origin="host",
    )
    parts = protocol.encode_parts(anchor_record["intent_id"], content)
    bundle = BreadcrumbBundle(protocol.make_manifest(anchor, parts), anchor, parts, content)

    with pytest.raises(ValueError, match="canonical JSON object"):
        decision.decision_bytes(bundle)
