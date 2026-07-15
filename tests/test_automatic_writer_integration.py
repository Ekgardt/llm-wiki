from __future__ import annotations

import concurrent.futures
import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _vault(tmp_path: Path) -> tuple[Path, Path]:
    vault = tmp_path / "vault"
    state = tmp_path / "state"
    (vault / "knowledge" / "daily").mkdir(parents=True)
    (vault / "knowledge" / "notes").mkdir()
    (vault / "knowledge" / "projects").mkdir()
    (vault / "knowledge" / "inbox").mkdir()
    return vault, state


def test_repository_scanner_finds_no_unapproved_covered_writers():
    from check_knowledge_writers import discover_repository_writers

    findings = discover_repository_writers(ROOT)
    assert findings, "scanner must discover the coordinator's target apply"
    offenders = [finding for finding in findings if not finding.approved]
    assert offenders == [], "\n" + "\n".join(str(item) for item in offenders)


def test_scanner_keeps_runtime_writes_outside_boundary():
    from check_knowledge_writers import scan_source

    source = 'Path("cache/access.jsonl").write_text("x")\n'
    assert scan_source(Path("scripts/example.py"), source) == []


def test_append_knowledge_is_idempotent_and_records_expected_hash(tmp_path, monkeypatch):
    import markdown_transaction

    vault, state = _vault(tmp_path)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    path = vault / "knowledge" / "daily" / "2026-07-15.md"

    first = markdown_transaction.append_knowledge("daily:event-1", path, b"one\n")
    second = markdown_transaction.append_knowledge("daily:event-1", path, b"one\n")

    assert first.id == second.id
    assert path.read_bytes() == b"one\n"
    assert first.preconditions["knowledge/daily/2026-07-15.md"] == "absent"


def test_concurrent_daily_and_project_jsonl_appends_never_interleave(tmp_path, monkeypatch):
    import markdown_transaction

    vault, state = _vault(tmp_path)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    daily = vault / "knowledge" / "daily" / "2026-07-15.md"
    jsonl = vault / "knowledge" / "projects" / "demo" / ".blackboard" / "tasks.jsonl"

    def append(index: int) -> None:
        markdown_transaction.append_knowledge(
            f"daily:event-{index}", daily, f"D{index:02d}-start\nD{index:02d}-end\n".encode()
        )
        markdown_transaction.append_knowledge(
            f"project:event-{index}", jsonl, f'{{"id":{index}}}\n'.encode()
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(append, range(12)))

    daily_text = daily.read_text(encoding="utf-8")
    jsonl_text = jsonl.read_text(encoding="utf-8")
    for index in range(12):
        assert daily_text.count(f"D{index:02d}-start\nD{index:02d}-end\n") == 1
        assert jsonl_text.count(f'{{"id":{index}}}\n') == 1


def test_append_retries_a_cas_conflict_without_overwriting_winner(tmp_path, monkeypatch):
    import markdown_transaction

    vault, state = _vault(tmp_path)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    path = vault / "knowledge" / "daily" / "2026-07-15.md"
    original_prepare = markdown_transaction.MarkdownCoordinator.prepare
    injected = False

    def racing_prepare(self, changes, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            markdown_transaction.append_knowledge("daily:winner", path, b"winner\n")
        return original_prepare(self, changes, **kwargs)

    monkeypatch.setattr(markdown_transaction.MarkdownCoordinator, "prepare", racing_prepare)
    markdown_transaction.append_knowledge("daily:loser", path, b"loser\n")

    assert path.read_bytes() == b"winner\nloser\n"


def test_mutation_recovers_after_crash_at_prepared_state(tmp_path, monkeypatch):
    import markdown_transaction

    vault, state = _vault(tmp_path)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    target = vault / "knowledge" / "notes" / "recovered.md"
    original = markdown_transaction.MarkdownCoordinator._killpoint

    def crash(self, name, parent_transaction_id=None):
        if name == "after_prepared":
            raise KeyboardInterrupt("injected crash")
        return original(self, name, parent_transaction_id)

    monkeypatch.setattr(markdown_transaction.MarkdownCoordinator, "_killpoint", crash)
    with pytest.raises(KeyboardInterrupt, match="injected crash"):
        markdown_transaction.mutate_knowledge(
            "notes:crash", {target: b"---\ntype: pattern\n---\n# Recovered\n"}
        )

    monkeypatch.setattr(markdown_transaction.MarkdownCoordinator, "_killpoint", original)
    record = markdown_transaction.mutate_knowledge(
        "notes:crash", {target: b"---\ntype: pattern\n---\n# Recovered\n"}
    )
    assert record.state == "committed"
    assert target.exists()


def test_secure_parent_creation_rejects_symlink(tmp_path, monkeypatch):
    import markdown_transaction

    vault, state = _vault(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = vault / "knowledge" / "projects" / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))

    with pytest.raises((ValueError, RuntimeError)):
        markdown_transaction.append_knowledge(
            "project:escape", link / "events.jsonl", b"{}\n"
        )
    assert list(outside.iterdir()) == []


def test_operation_ids_are_stable_content_keys():
    from markdown_transaction import stable_operation_id

    value = stable_operation_id("feedback", "session-1", b"redacted bytes")
    assert value == stable_operation_id("feedback", "session-1", b"redacted bytes")
    assert hashlib.sha256(b"redacted bytes").hexdigest() in value
