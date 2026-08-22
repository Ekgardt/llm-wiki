"""What delimits one entry in a daily log, and which entry evidence binds to.

The product writes daily entries in two shapes: a `## [HH:MM:SS]` heading from
the flush, session-end and MCP writers, and an `<!-- llm-wiki-operation:… -->`
marker from the lifecycle capture writers. Both are entries. Evidence names a
timestamp, and exactly one entry must declare it.

See `docs/research/2026-08-21-daily-entry-boundary.md`.
"""

from __future__ import annotations

import pytest


class _Source:
    """The one attribute `_evidence_block` reads from a daily snapshot."""

    def __init__(self, text: str) -> None:
        self.content = text.encode("utf-8")


def _capture(digest: str, time: str, text: str) -> str:
    marker = f"<!-- llm-wiki-operation:{digest * 8} -->"
    return f"\n{marker}\n- `[{time}] prompt | s1` {text}\n"


def _heading(time: str, text: str) -> str:
    return f"\n## [{time}] session-end | s1\n{text}\n"


def test_a_captured_entry_can_be_cited() -> None:
    import compile_memory

    log = "# Daily\n" + _capture("ab", "10:00:00", "first") + _capture(
        "cd", "10:00:01", "second"
    )
    block, offset = compile_memory._evidence_block(_Source(log), "10:00:01")
    assert b"second" in block, "the entry the timestamp names was not returned"
    assert b"first" not in block, "the entry ran past its own boundary"
    assert log.encode("utf-8")[offset:].startswith(b"<!-- llm-wiki-operation:")


def test_a_heading_entry_still_binds() -> None:
    import compile_memory

    log = "# Daily\n" + _heading("11:00:00", "durable fact")
    block, _offset = compile_memory._evidence_block(_Source(log), "11:00:00")
    assert b"durable fact" in block


def test_a_heading_entry_stops_at_the_next_captured_entry() -> None:
    """A quote from a later entry must not be attributed to the heading."""
    import compile_memory

    log = (
        "# Daily\n"
        + _heading("12:00:00", "the heading body")
        + _capture("ef", "12:00:05", "a later prompt")
    )
    block, _offset = compile_memory._evidence_block(_Source(log), "12:00:00")
    assert b"the heading body" in block
    assert b"a later prompt" not in block


def test_two_entries_declaring_one_second_are_refused() -> None:
    import compile_memory

    log = (
        "# Daily\n"
        + _heading("13:00:00", "from the flush")
        + _capture("ab", "13:00:00", "from the capture")
    )
    with pytest.raises(ValueError, match="ambiguous or missing"):
        compile_memory._evidence_block(_Source(log), "13:00:00")


def test_a_timestamp_no_entry_declares_is_refused() -> None:
    import compile_memory

    log = "# Daily\n" + _capture("ab", "14:00:00", "only entry")
    with pytest.raises(ValueError, match="ambiguous or missing"):
        compile_memory._evidence_block(_Source(log), "14:00:01")


def test_a_log_with_no_entries_is_refused() -> None:
    import compile_memory

    with pytest.raises(ValueError, match="ambiguous or missing"):
        compile_memory._evidence_block(_Source("# Daily\n\nloose prose\n"), "15:00:00")


def test_an_absent_source_is_refused() -> None:
    import compile_memory

    with pytest.raises(ValueError, match="ambiguous or missing"):
        compile_memory._evidence_block(None, "16:00:00")


def test_a_captured_bullet_is_quotable_as_one_complete_line() -> None:
    """The whole bullet, minus its marker, is what a citation must quote."""
    import compile_memory

    log = "# Daily\n" + _capture("ab", "17:00:00", "a durable fact")
    block, _offset = compile_memory._evidence_block(_Source(log), "17:00:00")
    quote = "`[17:00:00] prompt | s1` a durable fact"
    quote_bytes = quote.encode("utf-8")
    offset = compile_memory._sole_quote_offset(block, quote_bytes)
    compile_memory._require_complete_line(block, offset, quote_bytes, quote)


def test_half_a_captured_bullet_is_not_a_complete_line() -> None:
    import compile_memory

    log = "# Daily\n" + _capture("ab", "18:00:00", "a durable fact")
    block, _offset = compile_memory._evidence_block(_Source(log), "18:00:00")
    quote = "a durable fact"
    quote_bytes = quote.encode("utf-8")
    offset = compile_memory._sole_quote_offset(block, quote_bytes)
    with pytest.raises(ValueError, match="one complete source line"):
        compile_memory._require_complete_line(block, offset, quote_bytes, quote)


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """A hermetic vault holding one captured, marker-delimited daily log."""
    import compile_memory

    root = tmp_path / "vault"
    for relative in ("knowledge/daily/receipts", "knowledge/notes"):
        (root / relative).mkdir(parents=True)
    (root / "knowledge/index.md").write_bytes(b"# Index\n")
    (root / "knowledge/log.md").write_bytes(b"# Log\n")
    (root / "AGENTS.md").write_bytes(b"contract\n")
    daily = root / "knowledge/daily/2026-07-14.md"
    daily.write_text(
        "# Daily Session Memory — 2026-07-14\n" + _capture("ab", "10:00:00", "a durable fact"),
        encoding="utf-8",
    )
    for name, value in (
        ("ROOT", root),
        ("MEMORY", root / "knowledge"),
        ("DAILY_DIR", root / "knowledge/daily"),
        ("KNOWLEDGE", root / "knowledge/notes"),
        ("INDEX", root / "knowledge/index.md"),
        ("LOG", root / "knowledge/log.md"),
        ("AGENTS", root / "AGENTS.md"),
    ):
        monkeypatch.setattr(compile_memory, name, value)
    return daily


def _operation(quote: str) -> dict[str, object]:
    return {
        "action": "create",
        "category": "patterns",
        "slug": "captured-note",
        "title": "Captured Note",
        "summary": "A bounded summary.",
        "body_section": "Lesson",
        "body_markdown": "A bounded body.",
        "related": [],
        "evidence": [
            {
                "daily_date": "2026-07-14",
                "timestamp": "10:00:00",
                "quoted_text": quote,
                "claim": "Supports the note.",
            }
        ],
    }


def test_a_captured_entry_binds_end_to_end(vault) -> None:
    """The resolver must agree with the binder, or nothing captured is citable."""
    import compile_memory

    inputs = compile_memory.snapshot_compile_inputs([vault])
    quote = "`[10:00:00] prompt | s1` a durable fact"
    _normalized, bindings = compile_memory._validate_semantic_operation(
        _operation(quote), inputs
    )
    assert len(bindings) == 1
    assert bindings[0]["source_path"] == "knowledge/daily/2026-07-14.md"
    assert bindings[0]["reference"].startswith("daily:2026-07-14 sha256:")
    assert "block:10:00:00" in bindings[0]["reference"]
