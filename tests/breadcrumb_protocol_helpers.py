"""Helpers shared by test_breadcrumb_protocol.py and the test files importing them."""
from __future__ import annotations

from datetime import UTC, datetime

import breadcrumb_protocol as protocol


def _encoded(content: bytes, intent_id: str = "a" * 64):
    moment = datetime(2026, 9, 29, tzinfo=UTC)
    anchor = protocol.make_anchor(
        intent_id, content, occurred_at=moment, accepted_at=moment, time_origin="host",
    )
    parts = protocol.encode_parts(intent_id, content)
    manifest = protocol.make_manifest(anchor, parts)
    return anchor, parts, manifest
