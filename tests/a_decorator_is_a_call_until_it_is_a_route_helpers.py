"""Helpers shared by test_a_decorator_is_a_call_until_it_is_a_route.py and the test files importing them."""
from __future__ import annotations

import hashlib

from corpus_snapshot import CapturedSource, SourceMetadata, SourceRecord


def _source(path: str, content: bytes) -> CapturedSource:
    return CapturedSource(
        SourceRecord(
            logical_id=f"source:{path}",
            relative_path=path,
            sha256=hashlib.sha256(content).hexdigest(),
            size=len(content),
            media_type="text/x-python",
            language="python",
            git_oid=None,
        ),
        SourceMetadata(type="code", language="python"),
        content,
    )


def _call_target_names(result) -> list[str]:
    by_id = {node["node_id"]: node["metadata"].get("name") for node in result.nodes}
    return sorted(
        by_id[item["target_node_id"]]
        for item in result.assertions
        if item["edge_type"] == "CALLS"
    )
