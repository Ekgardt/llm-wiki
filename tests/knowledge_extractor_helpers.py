"""Helpers shared by test_knowledge_extractor.py and the test files importing them."""
from __future__ import annotations

import hashlib


def _source(path: str, content: bytes, *, kind: str = "concept"):
    from corpus_snapshot import CapturedSource, SourceMetadata, SourceRecord

    return CapturedSource(
        SourceRecord(
            f"source:{path}",
            path,
            hashlib.sha256(content).hexdigest(),
            len(content),
            "text/markdown",
            "markdown",
            None,
        ),
        SourceMetadata(kind),
        content,
    )
