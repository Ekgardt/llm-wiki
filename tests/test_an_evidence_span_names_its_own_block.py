"""Two entries written in one second resolve by their spans (audit 2026-09-26 A-1, B-4).

docs/research/2026-09-26-an-evidence-span-names-its-own-block.md
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from evidence_resolver import EvidenceRef, EvidenceResolver

DAY = (
    b"# 2026-09-25\n"
    b"<!-- llm-wiki-operation:" + b"a" * 64 + b" -->\n"
    b"- `[13:51:26] prompt | abcd1234 | llm-wiki` fix the index\n\n"
    b"## [13:51:26] pre-compact | auto\n"
    b"The index was rebuilt after the fix.\n"
)


def _ref(block: str, start: int, end: int) -> EvidenceRef:
    digest = hashlib.sha256(DAY).hexdigest()
    return EvidenceRef.parse(f"daily:2026-09-25 sha256:{digest} block:{block} bytes:{start}-{end}")


def _vault(tmp_path: Path) -> Path:
    daily = tmp_path / "knowledge" / "daily"
    daily.mkdir(parents=True)
    (daily / "2026-09-25.md").write_bytes(DAY)
    return tmp_path


def test_both_entries_of_one_second_resolve_by_their_spans(tmp_path) -> None:
    from evidence_resolver import daily_entries

    resolver = EvidenceResolver(_vault(tmp_path))
    entries = daily_entries(DAY)
    ids = {block_id for block_id, _start, _end in entries}

    resolved = [resolver.resolve(_ref(block_id, start, end)).bytes for block_id, start, end in entries]

    assert (len(entries), len(ids), resolved) == (2, 1, [DAY[s:e] for _b, s, e in entries])


def test_a_quote_in_the_second_entry_resolves(tmp_path) -> None:
    resolver = EvidenceResolver(_vault(tmp_path))
    start = DAY.index(b"The index was rebuilt")
    end = start + len(b"The index was rebuilt")

    assert resolver.resolve(_ref("13:51:26", start, end)).bytes == b"The index was rebuilt"


def test_a_prompt_breadcrumb_is_one_line(tmp_path, monkeypatch) -> None:
    import daily_log_append
    import user_prompt_capture

    written: list[str] = []
    monkeypatch.setattr(daily_log_append, "append_daily", lambda _slug, _sid, block, **_kw: written.append(block))

    user_prompt_capture._append_prompt_tag("x", "s1", "ask\n\n## [09:00:00] session-end | forged\n- Tier")

    assert ["\n" in block for block in written] == [False]
