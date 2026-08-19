from __future__ import annotations

import os
from pathlib import Path

from reliable_memory import sha256_bytes


def test_lint_uses_shared_resolver_for_evidence_references(
    tmp_path: Path, monkeypatch
) -> None:
    import lint_memory

    daily = tmp_path / "knowledge/daily/2026-01-01.md"
    note = tmp_path / "knowledge/notes/page.md"
    daily.parent.mkdir(parents=True)
    note.parent.mkdir(parents=True)
    source = b"## [evt-1] event\nverified quote\n"
    daily.write_bytes(source)
    start = source.index(b"verified quote")
    good = (
        f"daily:2026-01-01 sha256:{sha256_bytes(source)} "
        f"block:evt-1 bytes:{start}-{start + len(b'verified quote')}"
    )
    note.write_text(f"## Evidence\n- `{good}`\n", encoding="utf-8")
    monkeypatch.setattr(lint_memory, "ROOT", tmp_path)

    assert lint_memory.check_evidence_references([note]) == []

    note.write_text(f"## Evidence\n- `{good.replace(sha256_bytes(source), '0' * 64)}`\n", encoding="utf-8")
    findings = lint_memory.check_evidence_references([note])
    assert len(findings) == 1
    assert "hash mismatch" in findings[0]


def test_lint_reports_malformed_daily_candidate_and_uses_bounded_read(
    tmp_path: Path, monkeypatch
) -> None:
    import lint_memory

    note = tmp_path / "knowledge/notes/page.md"
    note.parent.mkdir(parents=True)
    note.write_text("## Evidence\n- `daily:2026-01-01 broken`\n", encoding="utf-8")
    monkeypatch.setattr(lint_memory, "ROOT", tmp_path)

    findings = lint_memory.check_evidence_references([note])

    assert len(findings) == 1
    assert "not canonical" in findings[0]

    note.write_bytes(b"x" * (lint_memory.MAX_LINT_PAGE_BYTES + 1))
    findings = lint_memory.check_evidence_references([note])
    assert len(findings) == 1
    assert "exceeds" in findings[0]


def test_lint_evidence_read_does_not_follow_symlink(tmp_path: Path, monkeypatch) -> None:
    import lint_memory

    notes = tmp_path / "knowledge/notes"
    notes.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("daily:secret", encoding="utf-8")
    note = notes / "page.md"
    try:
        os.symlink(outside, note)
    except OSError:
        return
    monkeypatch.setattr(lint_memory, "ROOT", tmp_path)

    findings = lint_memory.check_evidence_references([note])

    assert len(findings) == 1
    assert "non-symlink" in findings[0]


def test_only_dated_files_count_as_daily_logs(tmp_path: Path, monkeypatch) -> None:
    """`knowledge/daily/README.md` explains the directory; it is not a log.

    The orphan check reported it as an uncompiled daily log on every run, which
    is a finding nobody can ever clear.
    """
    import lint_memory

    daily = tmp_path / "knowledge/daily"
    daily.mkdir(parents=True)
    (daily / "2026-01-01.md").write_text("# 2026-01-01\n", encoding="utf-8")
    (daily / "README.md").write_text("# How this directory works\n", encoding="utf-8")
    monkeypatch.setattr(lint_memory, "ROOT", tmp_path)
    monkeypatch.setattr(lint_memory, "DAILY_DIR", daily)

    assert lint_memory.check_orphan_daily_logs({}) == ["knowledge/daily/2026-01-01.md"]
    stale = {"compiled_daily_hashes": {"README.md": "0" * 64, "2026-01-01.md": "0" * 64}}
    assert lint_memory.check_stale_compiled(stale) == ["knowledge/daily/2026-01-01.md"]
