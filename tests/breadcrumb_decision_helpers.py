"""Helpers shared by test_breadcrumb_decision.py and the test files importing them."""
from __future__ import annotations

from datetime import UTC, datetime

import breadcrumb_protocol as protocol
from breadcrumb_protocol import BreadcrumbBundle
from llm_wiki.formats.canonical import canonical_json_bytes


def _bundle(prompt: str) -> BreadcrumbBundle:
    content = canonical_json_bytes({"prompt": prompt})
    moment = datetime(2026, 9, 29, 23, 59, 59, tzinfo=UTC)
    anchor = protocol.make_anchor(
        "a" * 64, content, occurred_at=moment, accepted_at=moment, time_origin="host",
    )
    parts = protocol.encode_parts("a" * 64, content)
    return BreadcrumbBundle(protocol.make_manifest(anchor, parts), anchor, parts, content)
