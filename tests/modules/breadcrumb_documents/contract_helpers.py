"""Helpers shared by test_contract.py and the test files importing them."""

from datetime import UTC, datetime

import breadcrumb_protocol as protocol
from llm_wiki import breadcrumb_documents as evidence


def _source(content: bytes):
    moment = datetime(2026, 9, 29, tzinfo=UTC)
    anchor = protocol.make_anchor(
        "a" * 64, content, occurred_at=moment, accepted_at=moment, time_origin="host",
    )
    parts = protocol.encode_parts("a" * 64, content)
    manifest = protocol.make_manifest(anchor, parts)
    documents = dict(evidence.source_documents(manifest, anchor, parts))
    return evidence.source_path(anchor), documents
