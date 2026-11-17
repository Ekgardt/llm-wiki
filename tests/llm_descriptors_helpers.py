"""Helpers shared by test_llm_descriptors.py and the test files importing them."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from reliable_memory import canonical_json_bytes


def _write_dlp_policy(
    path: Path,
    *,
    literals: tuple[str, ...] = (),
    allow_fingerprints: tuple[str, ...] = (),
) -> None:
    payload = {
        "version": 1,
        "literals": list(literals),
        "allow_fingerprints": list(allow_fingerprints),
    }
    policy = {
        **payload,
        "sha256": hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
    }
    path.write_text(json.dumps(policy), encoding="utf-8")
