"""Helpers shared by test_claim_v2_versioned_consumers.py and the test files importing them."""

import claims
from reliable_memory import canonical_json_bytes, sha256_bytes


def _record(text, version="claim/v2"):
    source = b"# 2026-10-02\n\n## [12:00:00] Source\n" + text.encode() + b"\n"
    start = source.index(text.encode())
    semantic = {"subject": "project", "relation": "has-state", "value": {"type": "string", "value": "available"}, "qualifiers": [], "validity": {"from": "2026-10-02", "to": None}}
    record = {"schema_version": version, "id": "claim-version-test", "fingerprint": sha256_bytes(canonical_json_bytes(semantic)), "text": text, **semantic, "observed_at": "2026-10-02T12:00:00Z", "lifecycle": "active", "confidence": "medium", "authority": "ai-derived", "evidence": {"reference": f"daily:2026-10-02 sha256:{sha256_bytes(source)} block:12:00:00 bytes:{start}-{start+len(text.encode())}", "sha256": sha256_bytes(text.encode()), "text": text}, "links": [], "extractor_version": "test/v2"}
    return record, source


def _page(records, version="claim-ledger/v2"):
    return b"---\ntype: concept\n---\n# Version\n\n## Claims\n```json\n" + claims.claim_json_bytes({"schema_version": version, "claims": records}) + b"\n```\n"
