from __future__ import annotations

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
