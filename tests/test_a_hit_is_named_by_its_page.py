"""A hit is named by its page, and an agent row says each thing once (audit 2026-09-26 C-10).

docs/research/2026-09-26-a-hit-is-named-by-its-page.md
"""
from __future__ import annotations

import json
import re

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _row(ancestry: list[str], content: str) -> dict[str, object]:
    fields = dict.fromkeys(
        ("authority", "confidence", "project", "status", "type", "valid_from", "valid_to",
         "language", "source_id", "byte_start", "byte_end")
    )
    return {**fields, "heading_ancestry": json.dumps(ancestry), "rank": -5.0,
            "source_path": "knowledge/notes/x.md", "title": ancestry[-1] if ancestry else None,
            "content": content, "chunk_order": 0, "chunk_id": "c" * 64,
            "source_sha256": "a" * 64, "span_sha256": "b" * 64}


def test_a_section_hit_carries_the_page_title() -> None:
    import generation_search

    result = generation_search.generation_result(
        _row(["The Vault Updates Its Own Code", "Related"], "## Related\n- [[a]] — why.\n"), "g"
    )

    assert result["title"] == "The Vault Updates Its Own Code"


def test_a_page_without_headings_is_named_by_its_file() -> None:
    import generation_search

    assert generation_search.generation_result(_row([], "plain text\n"), "g")["title"] == "x"


def test_no_two_fields_of_an_agent_row_carry_one_identifier() -> None:
    import generation_search
    import mcp_server

    row = mcp_server.agent_row(generation_search.generation_result(_row(["Page", "Part"], "text\n"), "g"))
    identifiers = [value for value in row.values() if isinstance(value, str) and _HEX64.match(value)]

    assert len(identifiers) == len(set(identifiers))
