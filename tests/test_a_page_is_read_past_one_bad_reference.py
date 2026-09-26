"""A page that mentions `daily:` in prose is read, the mention named (audit 2026-09-26 B-17).

docs/research/2026-09-26-a-page-is-read-past-one-bad-reference.md
"""
from __future__ import annotations

import evidence_resolver


def test_prose_that_mentions_the_prefix_is_named_not_fatal() -> None:
    candidates = evidence_resolver.evidence_candidates("A `daily:` reference names a day; see mydaily:notes too.")

    assert candidates == ["evidence reference is not canonical: daily:"]


def test_the_page_is_returned_with_the_mention_named(tmp_path, monkeypatch) -> None:
    import mcp_server
    import memory_state

    notes = tmp_path / "knowledge" / "notes"
    notes.mkdir(parents=True)
    (notes / "page.md").write_text("Evidence lines start with `daily:` and a date.\n", encoding="utf-8")
    monkeypatch.setattr(memory_state, "ROOT", tmp_path)

    page = mcp_server._read_page("page")

    assert (page["slug"], page["evidence"]) == ("page", [{"reference": None, "error": "not_an_evidence_reference"}])
