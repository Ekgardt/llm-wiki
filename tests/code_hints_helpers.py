"""Helpers shared by test_code_hints.py and the test files importing them."""
from __future__ import annotations


def _meta(checkout_id: str) -> dict[str, str]:
    import code_hints

    return {
        "schema_version": code_hints.SCHEMA_VERSION,
        "generation_id": "generation-x",
        "repository_id": "repository:" + "0" * 64,
        "checkout_id": checkout_id,
        "checkout_root": "/nowhere",
        "git_commit": "",
        "symbols": "0",
        "written_at": "0",
    }
