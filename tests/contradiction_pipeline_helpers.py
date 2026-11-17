"""Helpers shared by test_contradiction_pipeline.py and the test files importing them."""
from __future__ import annotations

from claims import IndexedClaim, NormalizedClaim
from reliable_memory import canonical_json_bytes, sha256_bytes


def claim(
    value: object = "blue",
    *,
    claim_id: str = "new",
    subject: str = "project",
    relation: str = "has-state",
    start: str | None = "2026-01-01",
    end: str | None = None,
    authority: str = "user",
    confidence: str = "high",
    lifecycle: str = "active",
    qualifiers: list[dict[str, object]] | None = None,
) -> NormalizedClaim:
    typed = {"type": "string", "value": value}
    semantic = {
        "subject": subject,
        "relation": relation,
        "value": typed,
        "qualifiers": qualifiers or [],
        "validity": {"from": start, "to": end},
    }
    text = f"{subject} {relation} {value}"
    evidence_hash = sha256_bytes(text.encode())
    record = {
        "schema_version": "claim/v1",
        "id": claim_id,
        "fingerprint": sha256_bytes(canonical_json_bytes(semantic)),
        "text": text,
        **semantic,
        "observed_at": "2026-01-01T12:00:00Z",
        "lifecycle": lifecycle,
        "confidence": confidence,
        "authority": authority,
        "evidence": {
            "reference": (
                "daily:2026-01-01 sha256:" + "a" * 64
                + " block:12:00:00 bytes:0-1"
            ),
            "sha256": evidence_hash,
            "text": text,
        },
        "links": [],
        "extractor_version": "test/v1",
    }
    return NormalizedClaim(record)


def indexed(value: object = "red", **kwargs: object) -> IndexedClaim:
    return IndexedClaim(
        "knowledge/notes/existing.md",
        claim(value, claim_id="existing", **kwargs),
    )
