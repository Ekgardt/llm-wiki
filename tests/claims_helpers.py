"""Helpers shared by test_claims.py and the test files importing them."""
from __future__ import annotations

import hashlib
import json


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def source_bytes() -> bytes:
    return (
        "# 2026-01-02\n"
        "## [03:04:05] session\n"
        "Service A DEPENDS-ON Café.\n"
        "## [04:05:06] session\n"
        "Second block.\n"
    ).encode()


def raw_claim(source: bytes | None = None) -> dict[str, object]:
    source = source or source_bytes()
    literal = "Service A DEPENDS-ON Café.".encode()
    start = source.index(literal)
    return {
        "schema_version": "claim-extraction/v1",
        "claims": [
            {
                "id": "claim:2026-01-02-03:04:05:0",
                "text": literal.decode(),
                "subject": " Service A ",
                "relation": "DEPENDS-ON",
                "value": {"type": "entity", "value": " Café "},
                "qualifiers": [
                    {"key": " Environment ", "value": {"type": "string", "value": " PROD "}}
                ],
                "validity": {"from": None, "to": None},
                "lifecycle": "active",
                "confidence": "high",
                "authority": "user",
                "evidence": {
                    "reference": (
                        f"daily:2026-01-02 sha256:{sha(source)} block:03:04:05 "
                        f"bytes:{start}-{start + len(literal)}"
                    ),
                    "sha256": sha(literal),
                    "text": literal.decode(),
                },
                "links": [],
                "extractor_version": "extractor/v1",
            }
        ],
    }


def ledger_page(claim: dict[str, object]) -> bytes:
    ledger = {"schema_version": "claim-ledger/v1", "claims": [claim]}
    encoded = json.dumps(ledger, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"---\ntype: concept\n---\n# Service\n\n## Claims\n```json\n{encoded}\n```\n".encode()
