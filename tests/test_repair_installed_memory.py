from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "repair_installed_memory.py"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _snapshot(root: Path) -> dict[str, tuple[str, str]]:
    result = {}
    if not root.exists():
        return result
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[rel] = ("symlink", os.readlink(path))
        elif path.is_dir():
            result[rel] = ("dir", "")
        else:
            result[rel] = ("file", _sha(path.read_bytes()))
    return result


def _run(
    mode: str,
    vault: Path,
    state: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            mode,
            "--root",
            str(vault),
            "--state-root",
            str(state),
            *args,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    if check and result.returncode != 0:
        pytest.fail(f"repair command failed: {result.stderr}\n{result.stdout}")
    return result


def _vault(tmp_path: Path) -> tuple[Path, Path]:
    vault = tmp_path / "vault"
    state = tmp_path / "state"
    (vault / "knowledge" / "daily").mkdir(parents=True)
    (vault / "knowledge" / "notes").mkdir(parents=True)
    (vault / "knowledge" / "feedback").mkdir(parents=True)
    return vault, state


def _audit_file(vault: Path, state: Path, name: str = "audit.json") -> Path:
    output = vault.parent / name
    _run("audit", vault, state, "--output", str(output))
    return output


def _apply(
    vault: Path,
    state: Path,
    *args: str,
    audit_file: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    audit_file = audit_file or _audit_file(vault, state)
    if "--backup-only" not in args and "--manifest" not in args:
        backup = _run(
            "apply",
            vault,
            state,
            "--audit-report",
            str(audit_file),
            "--backup-only",
            check=check,
        )
        if backup.returncode != 0:
            return backup
        args = ("--manifest", json.loads(backup.stdout)["backup_manifest"], *args)
    return _run(
        "apply",
        vault,
        state,
        "--audit-report",
        str(audit_file),
        *args,
        check=check,
    )


def _crash_after_first_commit(vault: Path, state: Path, audit_file: Path) -> Path:
    code = f"""
import json, os, sys
from pathlib import Path
sys.path.insert(0, {str(ROOT / 'scripts')!r})
import repair_installed_memory as repair
vault = Path({str(vault)!r})
state = Path({str(state)!r})
audit_path = Path({str(audit_file)!r})
audit_bytes = audit_path.read_bytes()
report = json.loads(audit_bytes)
manifest = repair.create_backup(report, audit_bytes, vault, state)
real = repair._commit_staged_path
def crash(entry, manifest_path, root):
    real(entry, manifest_path, root)
    os._exit(91)
repair._commit_staged_path = crash
repair.apply_repair(report, audit_bytes, vault, state, manifest, backup_only=False)
"""
    crashed = subprocess.run([sys.executable, "-c", code], cwd=ROOT)
    assert crashed.returncode == 91
    daily_lock = state / "run" / "daily-append.lock"
    if daily_lock.exists():
        os.utime(daily_lock, (0, 0))
    manifests = list((state / "run" / "backups").glob("*/manifest.json"))
    assert len(manifests) == 1
    return manifests[0]


def test_audit_stdout_is_exact_json_and_absolutely_immutable(tmp_path):
    vault, state = _vault(tmp_path)
    _write(
        vault / "knowledge" / "daily" / "2026-07-20.md",
        "# Daily\n\n- `[10:00:00] tool | s1 | demo | Read` \n",
    )
    before_vault = _snapshot(vault)
    before_state = _snapshot(state)

    result = _run("audit", vault, state)

    assert _snapshot(vault) == before_vault
    assert _snapshot(state) == before_state
    assert result.stderr == ""
    assert result.stdout.endswith("\n")
    report = json.loads(result.stdout)
    assert set(report) == {
        "schema_version",
        "mode",
        "status",
        "root_fingerprint",
        "backup_manifest",
        "candidates",
        "summary",
    }
    assert report["mode"] == "audit"
    assert report["status"] == "ok"
    assert report["backup_manifest"] is None
    assert report["candidates"]
    assert set(report["candidates"][0]) == {
        "id",
        "kind",
        "path_id",
        "action",
        "before_sha256",
        "after_sha256",
        "reason",
        "status",
        "metadata",
    }


def test_audit_writes_only_an_explicit_safe_output(tmp_path):
    vault, state = _vault(tmp_path)
    output_dir = tmp_path / "reports"
    output_dir.mkdir()
    output = output_dir / "audit.json"

    result = _run("audit", vault, state, "--output", str(output))

    assert result.stdout == ""
    assert json.loads(output.read_text(encoding="utf-8"))["mode"] == "audit"
    assert not state.exists()


def test_audit_rejects_symlinked_sources_without_following_them(tmp_path, monkeypatch):
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks unavailable")
    vault, state = _vault(tmp_path)
    outside = _write(tmp_path / "outside.md", "do not inspect\n")
    link = vault / "knowledge" / "daily" / "linked.md"
    try:
        link.symlink_to(outside)
    except OSError:
        import repair_installed_memory

        original_is_symlink = Path.is_symlink
        monkeypatch.setattr(
            Path,
            "is_symlink",
            lambda self: self == link or original_is_symlink(self),
        )
        with pytest.raises(repair_installed_memory.RepairError, match="symlink"):
            repair_installed_memory._regular_file(link, link.parent)
        assert outside.read_text(encoding="utf-8") == "do not inspect\n"
        assert not state.exists()
        return

    result = _run("audit", vault, state, check=False)

    assert result.returncode != 0
    assert "symlink" in result.stderr.lower()
    assert outside.read_text(encoding="utf-8") == "do not inspect\n"
    assert not state.exists()


def test_apply_removes_only_structurally_empty_daily_noise(tmp_path):
    vault, state = _vault(tmp_path)
    original = (
        "# Daily\n\n\n\n"
        "- `[09:59:59] tool | s1 | demo | Read` README.md\n"
        "- `[10:00:00] tool | s1 | demo | Read` \n\n\n\n"
        "## [10:01:00] opencode-idle | s1\n"
        "- Tier: `major`\n"
        "- Trigger: `idle`\n\n"
        "(no body)\n\n"
        "## [10:02:00] session-end | s1\n"
        "- Tier: `major`\n\n"
        "A meaningful decision remains.\n\n\n\n\n"
    )
    daily = _write(
        vault / "knowledge" / "daily" / "2026-07-20.md",
        original,
    )
    before = daily.read_bytes()

    report = json.loads(_apply(vault, state).stdout)
    newline = b"\r\n" if b"\r\n" in before else b"\n"
    empty_tool = b"- `[10:00:00] tool | s1 | demo | Read` " + newline
    empty_idle = (
        b"## [10:01:00] opencode-idle | s1\n"
        b"- Tier: `major`\n"
        b"- Trigger: `idle`\n\n"
        b"(no body)\n\n"
    ).replace(b"\n", newline)
    expected = before.replace(empty_tool, b"").replace(
        empty_idle,
        b"",
    )
    content = daily.read_bytes()

    assert content == expected
    assert report["status"] == "applied"
    manifest_path = Path(report["backup_manifest"])
    assert manifest_path.is_file()
    transaction = json.loads(
        (manifest_path.parent / "transaction.json").read_text(encoding="utf-8")
    )
    assert transaction["status"] == "committed"
    assert transaction["rollback_errors"] == []
    assert all(c["status"] in {"applied", "review_only"} for c in report["candidates"])


def test_apply_quarantines_only_demonstrably_generated_false_feedback(tmp_path):
    vault, state = _vault(tmp_path)
    false = _write(
        vault / "knowledge" / "feedback" / "false.json",
        json.dumps(
            {
                "id": "false",
                "text": "No, the generated summary says this is wrong.",
                "trigger": "opencode-idle",
                "status": "candidate",
            }
        ),
    )
    true = _write(
        vault / "knowledge" / "feedback" / "true.json",
        json.dumps(
            {
                "id": "true",
                "text": "No, use PostgreSQL.",
                "trigger": "opencode-user-message",
                "source_role": "user",
                "status": "candidate",
            }
        ),
    )
    ambiguous = _write(
        vault / "knowledge" / "feedback" / "ambiguous.json",
        json.dumps(
            {
                "id": "ambiguous",
                "text": "No, this may be generated or user-authored.",
                "trigger": "session-end",
                "status": "candidate",
            }
        ),
    )

    report = json.loads(_apply(vault, state).stdout)

    assert not false.exists()
    assert true.exists()
    assert ambiguous.exists()
    false_item = next(c for c in report["candidates"] if c["kind"] == "false_feedback")
    assert false_item["kind"] == "false_feedback"
    assert false_item["action"] == "quarantine"
    preserved = [c for c in report["candidates"] if c["kind"] == "feedback_preserved"]
    assert len(preserved) == 2
    assert all(
        item["action"] == "preserve" and item["status"] == "preserved"
        for item in preserved
    )
    manifest_path = Path(report["backup_manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    false_entry = next(
        entry for entry in manifest["files"] if entry["path_id"] == false_item["path_id"]
    )
    quarantine = manifest_path.parent / "quarantine" / false_entry["path"]
    assert quarantine.read_bytes() == json.dumps(
        {
            "id": "false",
            "text": "No, the generated summary says this is wrong.",
            "trigger": "opencode-idle",
            "status": "candidate",
        }
    ).encode()


def test_duplicate_notes_are_classified_but_never_changed(tmp_path):
    vault, state = _vault(tmp_path)
    exact_a = _write(vault / "knowledge" / "notes" / "a.md", "# Same\n\nBody\n")
    exact_b = _write(vault / "knowledge" / "notes" / "b.md", "# Same\n\nBody\n")
    semantic = _write(
        vault / "knowledge" / "notes" / "c.md",
        "# Same\n\nOne-sentence summary: Similar meaning.\n\nDifferent body.\n",
    )
    summary_a = _write(
        vault / "knowledge" / "notes" / "d.md",
        "# First title\n\nOne-sentence summary: Shared semantic summary.\n\nFirst body.\n",
    )
    summary_b = _write(
        vault / "knowledge" / "notes" / "e.md",
        "# Second title\n\nOne-sentence summary: Shared semantic summary.\n\nSecond body.\n",
    )
    before = {
        p: p.read_bytes() for p in (exact_a, exact_b, semantic, summary_a, summary_b)
    }

    report = json.loads(_apply(vault, state).stdout)

    assert {p: p.read_bytes() for p in before} == before
    duplicate_kinds = {c["kind"] for c in report["candidates"] if "duplicate" in c["kind"]}
    assert duplicate_kinds == {"exact_duplicate_note", "semantic_duplicate_note"}
    duplicate_items = [c for c in report["candidates"] if "duplicate" in c["kind"]]
    assert all(c["action"] == "review" and c["status"] == "review_only" for c in duplicate_items)
    semantic_groups = [
        c for c in duplicate_items if c["kind"] == "semantic_duplicate_note"
    ]
    assert any(c["metadata"]["member_count"] == 2 for c in semantic_groups)


def test_sessions_require_strict_memory_title_and_are_report_only(tmp_path):
    vault, state = _vault(tmp_path)
    sessions = _write(
        tmp_path / "sessions.json",
        json.dumps(
            [
                {"id": "service", "title": "memory-compile-ephemeral", "orphaned": True},
                {"id": "user", "title": "ordinary user session", "orphaned": True},
                {"id": "active", "title": "memory-queue-ephemeral", "orphaned": False},
            ]
        ),
    )
    before = sessions.read_bytes()

    audit_file = vault.parent / "sessions-audit.json"
    _run(
        "audit",
        vault,
        state,
        "--sessions-file",
        str(sessions),
        "--output",
        str(audit_file),
    )
    report = json.loads(_apply(vault, state, audit_file=audit_file).stdout)

    assert sessions.read_bytes() == before
    items = [c for c in report["candidates"] if c["kind"] == "orphan_service_session"]
    assert len(items) == 1
    assert items[0]["id"].startswith("orphan_service_session:")
    assert items[0]["id"].rsplit(":", 1)[-1] != "service"
    assert items[0]["action"] == "propose_safe_api_delete"
    assert items[0]["status"] == "unsupported_safe_api"
    assert all("user" not in item["id"] for item in items)


def test_backup_only_creates_complete_verified_manifest_before_mutation(tmp_path):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    daily = _write(
        vault / "knowledge" / "daily" / "2026-07-20.md",
        "# Daily\n- `[10:00:00] tool | s1 | demo | Read` \n",
    )
    original = daily.read_bytes()

    audit_file = _audit_file(vault, state)
    audit_bytes = audit_file.read_bytes()
    audit_report = json.loads(audit_bytes)
    rejected = _run(
        "apply",
        vault,
        state,
        "--audit-report",
        str(audit_file),
        check=False,
    )

    assert rejected.returncode != 0
    assert "--manifest" in rejected.stderr
    assert daily.read_bytes() == original
    assert not state.exists()
    with pytest.raises(repair.RepairError, match="manifest"):
        repair.apply_repair(
            audit_report,
            audit_bytes,
            vault,
            state,
            None,
            backup_only=False,
        )
    assert not state.exists()
    missing_manifest = state / "run" / "backups" / "20260726T120000.000000Z" / "manifest.json"
    missing = _run(
        "apply",
        vault,
        state,
        "--audit-report",
        str(audit_file),
        "--manifest",
        str(missing_manifest),
        check=False,
    )
    assert missing.returncode != 0
    assert "manifest" in missing.stderr.lower()
    assert daily.read_bytes() == original
    assert not state.exists()

    report = json.loads(_apply(vault, state, "--backup-only", audit_file=audit_file).stdout)
    manifest_path = Path(report["backup_manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert daily.read_bytes() == original
    assert report["status"] == "backup_complete"
    assert manifest["status"] == "complete"
    assert manifest["approved"] is True
    assert manifest["audit_report_sha256"] == _sha(audit_file.read_bytes())
    assert manifest_path.parent.parent.name == "backups"
    assert manifest["files"]
    for item in manifest["files"]:
        backup = manifest_path.parent / item["backup_path"]
        assert backup.is_file()
        assert _sha(backup.read_bytes()) == item["sha256"]
        assert backup.stat().st_size == item["size"]
        staged = manifest_path.parent / item["staged_path"]
        assert staged.is_file()
        assert _sha(staged.read_bytes()) == item["staged_sha256"]
        assert staged.stat().st_size == item["staged_size"]


def test_apply_rejects_tampered_backup_without_mutating_source(tmp_path):
    vault, state = _vault(tmp_path)
    daily = _write(
        vault / "knowledge" / "daily" / "2026-07-20.md",
        "# Daily\n- `[10:00:00] tool | s1 | demo | Read` \n",
    )
    audit_file = _audit_file(vault, state)
    backup_report = json.loads(
        _apply(vault, state, "--backup-only", audit_file=audit_file).stdout
    )
    manifest_path = Path(backup_report["backup_manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    backup = manifest_path.parent / manifest["files"][0]["backup_path"]
    backup.write_text("tampered", encoding="utf-8")
    before = daily.read_bytes()

    result = _apply(
        vault,
        state,
        "--manifest",
        str(manifest_path),
        audit_file=audit_file,
        check=False,
    )

    assert result.returncode != 0
    assert "hash" in result.stderr.lower() or "tamper" in result.stderr.lower()
    assert daily.read_bytes() == before


def test_apply_rejects_tampered_manifest_action_without_mutation(tmp_path):
    vault, state = _vault(tmp_path)
    daily = _write(
        vault / "knowledge" / "daily" / "2026-07-20.md",
        "# Daily\n- `[10:00:00] tool | s1 | demo | Read` \n",
    )
    audit_file = _audit_file(vault, state)
    backup_report = json.loads(
        _apply(vault, state, "--backup-only", audit_file=audit_file).stdout
    )
    manifest_path = Path(backup_report["backup_manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["action"] = "quarantine"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    before = daily.read_bytes()

    result = _apply(
        vault,
        state,
        "--manifest",
        str(manifest_path),
        audit_file=audit_file,
        check=False,
    )

    assert result.returncode != 0
    assert "manifest action" in result.stderr.lower()
    assert daily.read_bytes() == before


def test_apply_rejects_source_changed_after_backup_without_any_mutation(tmp_path):
    vault, state = _vault(tmp_path)
    first = _write(
        vault / "knowledge" / "daily" / "2026-07-20.md",
        "# Daily\n- `[10:00:00] tool | s1 | demo | Read` \n",
    )
    second = _write(
        vault / "knowledge" / "daily" / "2026-07-21.md",
        "# Daily\n- `[11:00:00] tool | s2 | demo | Read` \n",
    )
    audit_file = _audit_file(vault, state)
    backup_report = json.loads(
        _apply(vault, state, "--backup-only", audit_file=audit_file).stdout
    )
    manifest = backup_report["backup_manifest"]
    first_before = first.read_bytes()
    second.write_text(second.read_text(encoding="utf-8") + "concurrent change\n", encoding="utf-8")
    second_before = second.read_bytes()

    result = _apply(
        vault,
        state,
        "--manifest",
        manifest,
        audit_file=audit_file,
        check=False,
    )

    assert result.returncode != 0
    assert "source" in result.stderr.lower() and "changed" in result.stderr.lower()
    assert first.read_bytes() == first_before
    assert second.read_bytes() == second_before


def test_verify_is_read_only_idempotent_and_checks_applied_manifest(tmp_path):
    vault, state = _vault(tmp_path)
    _write(
        vault / "knowledge" / "daily" / "2026-07-20.md",
        "# Daily\n- `[10:00:00] tool | s1 | demo | Read` \n",
    )
    apply_report = json.loads(_apply(vault, state).stdout)
    before_vault = _snapshot(vault)
    before_state = _snapshot(state)

    first = _run("verify", vault, state, "--manifest", apply_report["backup_manifest"])
    second = _run("verify", vault, state, "--manifest", apply_report["backup_manifest"])

    assert first.stdout == second.stdout
    assert json.loads(first.stdout)["status"] == "verified"
    assert _snapshot(vault) == before_vault
    assert _snapshot(state) == before_state


def test_apply_requires_exact_unchanged_audit_artifact(tmp_path):
    vault, state = _vault(tmp_path)
    daily = _write(
        vault / "knowledge" / "daily" / "2026-07-20.md",
        "# Daily\n- `[10:00:00] tool | s1 | demo | Read` \n",
    )
    audit_file = _audit_file(vault, state)
    daily.write_text(daily.read_text(encoding="utf-8") + "new append\n", encoding="utf-8")
    before = daily.read_bytes()

    result = _apply(vault, state, audit_file=audit_file, check=False)

    assert result.returncode != 0
    assert "fresh audit" in result.stderr.lower()
    assert daily.read_bytes() == before


def test_apply_rejects_changed_audit_after_backup(tmp_path):
    vault, state = _vault(tmp_path)
    _write(
        vault / "knowledge" / "daily" / "2026-07-20.md",
        "# Daily\n- `[10:00:00] tool | s1 | demo | Read` \n",
    )
    audit_file = _audit_file(vault, state)
    backup = json.loads(
        _apply(vault, state, "--backup-only", audit_file=audit_file).stdout
    )
    audit_file.write_bytes(audit_file.read_bytes() + b" ")

    result = _apply(
        vault,
        state,
        "--manifest",
        backup["backup_manifest"],
        audit_file=audit_file,
        check=False,
    )

    assert result.returncode != 0
    assert "audit report digest" in result.stderr.lower()


def test_audit_aggregates_noise_and_redacts_semantic_content(tmp_path):
    vault, state = _vault(tmp_path)
    daily = vault / "knowledge" / "daily" / "2026-07-20.md"
    daily.write_text(
        "# Daily\n"
        + (
            "- `[10:00:00] tool | session-secret | project-secret | Read` \n"
            "meaningful retained line\n"
        )
        * 35_000,
        encoding="utf-8",
    )
    _write(
        vault / "knowledge" / "notes" / "one.md",
        "# Private Architecture Name\n\nOne-sentence summary: confidential semantic phrase.\nA\n",
    )
    _write(
        vault / "knowledge" / "notes" / "two.md",
        "# Private Architecture Name\n\nOne-sentence summary: confidential semantic phrase.\nB\n",
    )

    result = _run("audit", vault, state)
    report = json.loads(result.stdout)
    daily_items = [c for c in report["candidates"] if c["action"] == "clean_daily"]

    assert len(daily_items) == 1
    assert daily_items[0]["metadata"]["empty_tool_breadcrumb_count"] == 35_000
    assert daily_items[0]["metadata"]["line_range_count"] == 35_000
    assert daily_items[0]["metadata"]["line_ranges"] == [[2, 2], [70000, 70000]]
    assert len(daily_items[0]["metadata"]["line_ranges_sha256"]) == 64
    assert len(result.stdout.encode("utf-8")) < 20_000
    assert "session-secret" not in result.stdout
    assert "project-secret" not in result.stdout
    assert "Private Architecture Name" not in result.stdout
    assert "confidential semantic phrase" not in result.stdout


def test_feedback_writer_waits_for_shared_feedback_lock(tmp_path, monkeypatch):
    import feedback_capture

    feedback_dir = tmp_path / "vault" / "knowledge" / "feedback"
    state = tmp_path / "state"
    monkeypatch.setattr(feedback_capture, "FEEDBACK_DIR", feedback_dir)
    monkeypatch.setattr(feedback_capture, "STATE_ROOT", state)
    started = threading.Event()
    finished = threading.Event()

    def writer():
        started.set()
        feedback_capture.capture_from_text(
            "No, always use the shared feedback lock.",
            trigger="opencode-user-message",
        )
        finished.set()

    with feedback_capture.feedback_writer_lock(state, timeout=2.0):
        thread = threading.Thread(target=writer)
        thread.start()
        assert started.wait(1.0)
        time.sleep(0.1)
        assert not finished.is_set()
    thread.join(timeout=2.0)

    assert finished.is_set()
    assert len(list(feedback_dir.glob("*.json"))) == 1


def test_transaction_rolls_back_earlier_file_when_later_commit_fails(tmp_path, monkeypatch):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    first = _write(
        vault / "knowledge" / "daily" / "2026-07-20.md",
        "# Daily\n- `[10:00:00] tool | s1 | demo | Read` \n",
    )
    second = _write(
        vault / "knowledge" / "daily" / "2026-07-21.md",
        "# Daily\n- `[11:00:00] tool | s2 | demo | Read` \n",
    )
    originals = {first: first.read_bytes(), second: second.read_bytes()}
    audit_file = _audit_file(vault, state)
    audit_bytes = audit_file.read_bytes()
    audit_report = json.loads(audit_bytes)
    manifest_path = repair.create_backup(audit_report, audit_bytes, vault, state)
    real_commit = repair._commit_staged_path
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected later-file failure")
        return real_commit(*args, **kwargs)

    monkeypatch.setattr(repair, "_commit_staged_path", fail_second)

    with pytest.raises(repair.TransactionError):
        repair.apply_repair(
            audit_report,
            audit_bytes,
            vault,
            state,
            manifest_path,
            backup_only=False,
        )

    assert {path: path.read_bytes() for path in originals} == originals
    transaction_files = list((state / "run" / "backups").glob("*/transaction.json"))
    assert len(transaction_files) == 1
    transaction = json.loads(transaction_files[0].read_text(encoding="utf-8"))
    assert transaction["status"] == "rolled_back"
    assert transaction["commit_error"] == "OSError: injected later-file failure"
    assert transaction["rollback_errors"] == []
    assert transaction["attempted_paths"] == [
        "knowledge/daily/2026-07-20.md",
        "knowledge/daily/2026-07-21.md",
    ]
    assert transaction["restored_paths"] == [
        "knowledge/daily/2026-07-21.md",
        "knowledge/daily/2026-07-20.md",
    ]


def test_concurrent_daily_append_waits_and_is_not_lost(tmp_path, monkeypatch):
    import daily_log_append
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    daily = _write(
        vault / "knowledge" / "daily" / "2026-07-20.md",
        "# Daily\n- `[10:00:00] tool | s1 | demo | Read` \n",
    )
    audit_file = _audit_file(vault, state)
    audit_bytes = audit_file.read_bytes()
    audit_report = json.loads(audit_bytes)
    manifest_path = repair.create_backup(audit_report, audit_bytes, vault, state)
    commit_entered = threading.Event()
    release_commit = threading.Event()
    writer_done = threading.Event()
    real_commit = repair._commit_staged_path

    def paused_commit(*args, **kwargs):
        commit_entered.set()
        assert release_commit.wait(2.0)
        return real_commit(*args, **kwargs)

    def append_writer():
        assert commit_entered.wait(2.0)
        daily_log_append.locked_append(daily, "meaningful concurrent append\n", state_root=state)
        writer_done.set()

    monkeypatch.setattr(repair, "_commit_staged_path", paused_commit)
    writer = threading.Thread(target=append_writer)
    writer.start()
    apply_error = []

    def apply_worker():
        try:
            repair.apply_repair(
                audit_report,
                audit_bytes,
                vault,
                state,
                manifest_path,
                backup_only=False,
            )
        except Exception as exc:  # pragma: no cover - surfaced by assertion below
            apply_error.append(exc)

    apply_thread = threading.Thread(target=apply_worker)
    apply_thread.start()
    assert commit_entered.wait(2.0)
    time.sleep(0.1)
    assert not writer_done.is_set()
    release_commit.set()
    apply_thread.join(timeout=3.0)
    writer.join(timeout=3.0)

    assert apply_error == []
    assert writer_done.is_set()
    content = daily.read_text(encoding="utf-8")
    assert "tool |" not in content
    assert "meaningful concurrent append" in content


def test_concurrent_feedback_writer_waits_and_is_not_lost(tmp_path, monkeypatch):
    import feedback_capture
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    false = _write(
        vault / "knowledge" / "feedback" / "false.json",
        json.dumps(
            {
                "id": "false",
                "text": "generated",
                "trigger": "opencode-idle",
                "status": "candidate",
            }
        ),
    )
    monkeypatch.setattr(feedback_capture, "FEEDBACK_DIR", false.parent)
    monkeypatch.setattr(feedback_capture, "STATE_ROOT", state)
    audit_file = _audit_file(vault, state)
    audit_bytes = audit_file.read_bytes()
    audit_report = json.loads(audit_bytes)
    manifest_path = repair.create_backup(audit_report, audit_bytes, vault, state)
    commit_entered = threading.Event()
    release_commit = threading.Event()
    writer_done = threading.Event()
    real_commit = repair._commit_staged_path

    def paused_commit(*args, **kwargs):
        commit_entered.set()
        assert release_commit.wait(2.0)
        return real_commit(*args, **kwargs)

    def feedback_writer():
        assert commit_entered.wait(2.0)
        feedback_capture.capture_from_text(
            "No, always preserve this concurrent user correction.",
            trigger="opencode-user-message",
        )
        writer_done.set()

    monkeypatch.setattr(repair, "_commit_staged_path", paused_commit)
    writer = threading.Thread(target=feedback_writer)
    writer.start()
    apply_errors = []

    def apply_worker():
        try:
            repair.apply_repair(
                audit_report,
                audit_bytes,
                vault,
                state,
                manifest_path,
                backup_only=False,
            )
        except Exception as exc:  # pragma: no cover - surfaced by assertion below
            apply_errors.append(exc)

    apply_thread = threading.Thread(target=apply_worker)
    apply_thread.start()
    assert commit_entered.wait(2.0)
    time.sleep(0.1)
    assert not writer_done.is_set()
    release_commit.set()
    apply_thread.join(timeout=3.0)
    writer.join(timeout=3.0)

    assert apply_errors == []
    assert writer_done.is_set()
    assert not false.exists()
    remaining = list(false.parent.glob("*.json"))
    assert len(remaining) == 1
    assert json.loads(remaining[0].read_text(encoding="utf-8"))["trigger"] == (
        "opencode-user-message"
    )


def test_rollback_failure_persists_critical_recovery_outcome(tmp_path, monkeypatch):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    _write(
        vault / "knowledge" / "daily" / "2026-07-20.md",
        "# Daily\n- `[10:00:00] tool | s1 | demo | Read` \n",
    )
    _write(
        vault / "knowledge" / "daily" / "2026-07-21.md",
        "# Daily\n- `[11:00:00] tool | s2 | demo | Read` \n",
    )
    audit_file = _audit_file(vault, state)
    audit_bytes = audit_file.read_bytes()
    audit_report = json.loads(audit_bytes)
    manifest_path = repair.create_backup(audit_report, audit_bytes, vault, state)
    real_commit = repair._commit_staged_path
    commits = 0

    def fail_second(*args, **kwargs):
        nonlocal commits
        commits += 1
        if commits == 2:
            raise OSError("injected commit failure")
        return real_commit(*args, **kwargs)

    monkeypatch.setattr(repair, "_commit_staged_path", fail_second)
    monkeypatch.setattr(
        repair,
        "_restore_source",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("injected rollback failure")),
    )

    with pytest.raises(repair.TransactionError, match="critical rollback failure"):
        repair.apply_repair(
            audit_report,
            audit_bytes,
            vault,
            state,
            manifest_path,
            backup_only=False,
        )

    backup_dirs = list((state / "run" / "backups").iterdir())
    assert len(backup_dirs) == 1
    outcome = json.loads((backup_dirs[0] / "transaction.json").read_text(encoding="utf-8"))
    assert outcome["status"] == "critical_rollback_failed"
    assert len(outcome["rollback_errors"]) == 1
    assert (backup_dirs[0] / "manifest.json").is_file()
    assert (backup_dirs[0] / "files").is_dir()
    assert (backup_dirs[0] / "staged").is_dir()


def test_quarantine_rejects_reparse_destination_component(tmp_path, monkeypatch):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    feedback = _write(
        vault / "knowledge" / "feedback" / "false.json",
        json.dumps(
            {
                "id": "false",
                "text": "generated",
                "trigger": "opencode-idle",
                "status": "candidate",
            }
        ),
    )
    audit_file = _audit_file(vault, state)
    audit_bytes = audit_file.read_bytes()
    audit_report = json.loads(audit_bytes)
    manifest_path = repair.create_backup(audit_report, audit_bytes, vault, state)
    quarantine = manifest_path.parent / "quarantine"
    real_check = repair._path_is_link_or_reparse
    monkeypatch.setattr(
        repair,
        "_path_is_link_or_reparse",
        lambda path: path == quarantine or real_check(path),
    )

    with pytest.raises(repair.RepairError, match="reparse"):
        repair.apply_repair(
            audit_report,
            audit_bytes,
            vault,
            state,
            manifest_path,
            backup_only=False,
        )

    assert feedback.exists()


def test_verify_reads_each_source_once_even_with_multiple_candidates(tmp_path, monkeypatch):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    first = _write(
        vault / "knowledge" / "notes" / "one.md",
        "# Shared\n\nOne-sentence summary: Also shared.\nA\n",
    )
    second = _write(
        vault / "knowledge" / "notes" / "two.md",
        "# Shared\n\nOne-sentence summary: Also shared.\nB\n",
    )
    audit_file = _audit_file(vault, state)
    audit_bytes = audit_file.read_bytes()
    audit_report = json.loads(audit_bytes)
    manifest = repair.create_backup(audit_report, audit_bytes, vault, state)
    reads = {first: 0, second: 0}
    real_read = Path.read_bytes

    def counted_read(path):
        if path in reads:
            reads[path] += 1
        return real_read(path)

    monkeypatch.setattr(Path, "read_bytes", counted_read)

    report = repair.verify_repair(vault, state, manifest)

    assert report["status"] == "verified"
    assert reads == {first: 1, second: 1}


def test_staged_artifact_tampered_after_validation_is_rejected_under_lock(
    tmp_path, monkeypatch
):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    daily = _write(
        vault / "knowledge" / "daily" / "secret-day.md",
        "# Daily\n- `[10:00:00] tool | s1 | demo | Read` \n",
    )
    original = daily.read_bytes()
    audit_file = _audit_file(vault, state)
    audit_bytes = audit_file.read_bytes()
    audit_report = json.loads(audit_bytes)
    manifest_path = repair.create_backup(audit_report, audit_bytes, vault, state)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    staged = manifest_path.parent / manifest["files"][0]["staged_path"]
    real_commit = repair._commit_staged_path

    def tamper_immediately_before_use(*args, **kwargs):
        staged.write_bytes(b"tampered after validation\n")
        return real_commit(*args, **kwargs)

    monkeypatch.setattr(repair, "_commit_staged_path", tamper_immediately_before_use)

    with pytest.raises(repair.TransactionError, match="staged artifact"):
        repair.apply_repair(
            audit_report,
            audit_bytes,
            vault,
            state,
            manifest_path,
            backup_only=False,
        )

    assert daily.read_bytes() == original


def test_edited_preserved_feedback_cannot_become_quarantine_instruction(tmp_path):
    vault, state = _vault(tmp_path)
    feedback = _write(
        vault / "knowledge" / "feedback" / "private-feedback.json",
        json.dumps(
            {
                "id": "private",
                "text": "No, preserve this user instruction.",
                "trigger": "opencode-user-message",
                "source_role": "user",
                "status": "candidate",
            }
        ),
    )
    audit_file = _audit_file(vault, state)
    report = json.loads(audit_file.read_bytes())
    item = next(c for c in report["candidates"] if c["kind"] == "feedback_preserved")
    item["kind"] = "false_feedback"
    item["action"] = "quarantine"
    item["status"] = "candidate"
    item["metadata"] = {"classification": "generated_idle"}
    edited = json.dumps(report, sort_keys=True).encode()
    audit_file.write_bytes(edited)

    result = _apply(vault, state, audit_file=audit_file, check=False)

    assert result.returncode != 0
    assert "authoritative classification" in result.stderr.lower()
    assert feedback.exists()


def test_per_file_cas_detects_change_after_prior_file_commit_and_rolls_back(
    tmp_path, monkeypatch
):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    first = _write(
        vault / "knowledge" / "daily" / "2026-07-20.md",
        "# Daily\n- `[10:00:00] tool | s1 | demo | Read` \n",
    )
    second = _write(
        vault / "knowledge" / "daily" / "2026-07-21.md",
        "# Daily\n- `[11:00:00] tool | s2 | demo | Read` \n",
    )
    originals = {first: first.read_bytes(), second: second.read_bytes()}
    audit_file = _audit_file(vault, state)
    audit_bytes = audit_file.read_bytes()
    audit_report = json.loads(audit_bytes)
    manifest_path = repair.create_backup(audit_report, audit_bytes, vault, state)
    real_commit = repair._commit_staged_path
    calls = 0

    def change_second_before_cas(entry, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            second.write_bytes(second.read_bytes() + b"concurrent late append\n")
        return real_commit(entry, *args, **kwargs)

    monkeypatch.setattr(repair, "_commit_staged_path", change_second_before_cas)

    with pytest.raises(repair.TransactionError, match="stale source"):
        repair.apply_repair(
            audit_report,
            audit_bytes,
            vault,
            state,
            manifest_path,
            backup_only=False,
        )

    assert first.read_bytes() == originals[first]
    assert second.read_bytes() == originals[second] + b"concurrent late append\n"


def test_external_audit_redacts_roots_paths_and_secret_filenames(tmp_path):
    secret_root = tmp_path / "root-SECRET-CUSTOMER"
    vault, state = _vault(secret_root)
    _write(
        vault / "knowledge" / "daily" / "client-secret-project.md",
        "# Daily\n- `[10:00:00] tool | secret-session | secret-project | Read` \n",
    )
    _write(
        vault / "knowledge" / "notes" / "secret-product-roadmap.md",
        "# Confidential Product Name\n\nOne-sentence summary: hidden strategy.\nA\n",
    )
    _write(
        vault / "knowledge" / "notes" / "another-secret-roadmap.md",
        "# Confidential Product Name\n\nOne-sentence summary: hidden strategy.\nB\n",
    )

    result = _run("audit", vault, state)

    for secret in (
        str(vault),
        str(state),
        "SECRET-CUSTOMER",
        "client-secret-project",
        "secret-product-roadmap",
        "another-secret-roadmap",
        "Confidential Product Name",
        "hidden strategy",
        "secret-session",
        "secret-project",
        "knowledge/notes",
        "knowledge/daily",
    ):
        assert secret not in result.stdout
    report = json.loads(result.stdout)
    assert set(report) == {
        "schema_version",
        "mode",
        "status",
        "root_fingerprint",
        "backup_manifest",
        "candidates",
        "summary",
    }
    assert all("path_id" in candidate and "path" not in candidate for candidate in report["candidates"])


def test_duplicate_group_report_is_single_record_and_bounded_for_300_notes(tmp_path):
    vault, state = _vault(tmp_path)
    for index in range(300):
        _write(
            vault / "knowledge" / "notes" / f"customer-secret-{index:03d}.md",
            "# Same Private Title\n\nOne-sentence summary: same private summary.\n"
            f"Unique body {index}.\n",
        )

    result = _run("audit", vault, state)
    report = json.loads(result.stdout)
    groups = [c for c in report["candidates"] if c["kind"] == "semantic_duplicate_note"]

    assert len(groups) == 1
    assert len(groups[0]["metadata"]["member_ids"]) == 300
    assert len(set(groups[0]["metadata"]["member_ids"])) == 300
    assert "peer_paths" not in result.stdout
    assert "customer-secret" not in result.stdout
    assert len(result.stdout.encode()) < 35_000


def test_crash_after_commit_is_recovered_by_next_backup_invocation(tmp_path):
    vault, state = _vault(tmp_path)
    daily = _write(
        vault / "knowledge" / "daily" / "2026-07-20.md",
        "# Daily\n- `[10:00:00] tool | s1 | demo | Read` \n",
    )
    original = daily.read_bytes()
    audit_file = _audit_file(vault, state)
    _crash_after_first_commit(vault, state, audit_file)
    assert daily.read_bytes() != original

    recovered = _apply(vault, state, "--backup-only", audit_file=audit_file)

    assert recovered.returncode == 0
    assert daily.read_bytes() == original
    journals = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (state / "run" / "backups").glob("*/transaction.json")
    ]
    assert any(journal["status"] == "rolled_back" for journal in journals)


def test_recovery_never_rolls_back_committed_transaction(tmp_path):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    daily = _write(
        vault / "knowledge" / "daily" / "2026-07-20.md",
        "# Daily\n- `[10:00:00] tool | s1 | demo | Read` \n",
    )
    report = json.loads(_apply(vault, state).stdout)
    committed = daily.read_bytes()

    repair.recover_incomplete_transactions(vault, state)

    assert daily.read_bytes() == committed
    journal = json.loads(
        (Path(report["backup_manifest"]).parent / "transaction.json").read_text(
            encoding="utf-8"
        )
    )
    assert journal["status"] == "committed"


def test_crash_then_daily_append_recovery_preserves_original_and_append(tmp_path):
    import daily_log_append
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    daily = _write(
        vault / "knowledge" / "daily" / "2026-07-20.md",
        "# Daily\n- `[10:00:00] tool | s1 | demo | Read` \n",
    )
    original = daily.read_bytes()
    audit_file = _audit_file(vault, state)
    manifest_path = _crash_after_first_commit(vault, state, audit_file)
    staged = daily.read_bytes()
    appended = b"meaningful post-crash append\n"
    daily_log_append.locked_append(daily, appended.decode(), state_root=state)
    suffix = daily.read_bytes()[len(staged) :]

    repair.recover_incomplete_transactions(vault, state)

    assert daily.read_bytes() == original + suffix
    journal = json.loads(
        (manifest_path.parent / "transaction.json").read_text(encoding="utf-8")
    )
    assert journal["status"] == "rolled_back"
    assert journal["recovery_merges"][0]["strategy"] == "append_suffix"
    assert journal["recovery_merges"][0]["status"] == "complete"


@pytest.mark.parametrize("_stress_attempt", range(3))
def test_two_concurrent_recoverers_serialize_and_keep_post_crash_append(
    tmp_path, _stress_attempt
):
    import daily_log_append

    vault, state = _vault(tmp_path)
    daily = _write(
        vault / "knowledge" / "daily" / "2026-07-20.md",
        "# Daily\n- `[10:00:00] tool | s1 | demo | Read` \n",
    )
    original = daily.read_bytes()
    audit_file = _audit_file(vault, state)
    manifest_path = _crash_after_first_commit(vault, state, audit_file)
    staged = daily.read_bytes()
    appended = b"one serialized append\n"
    daily_log_append.locked_append(daily, appended.decode(), state_root=state)
    suffix = daily.read_bytes()[len(staged) :]
    code = f"""
import sys
from pathlib import Path
sys.path.insert(0, {str(ROOT / 'scripts')!r})
import repair_installed_memory as repair
repair.recover_incomplete_transactions(Path({str(vault)!r}), Path({str(state)!r}))
"""
    workers = [
        subprocess.Popen(
            [sys.executable, "-c", code],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(2)
    ]
    results = [worker.communicate(timeout=10) + (worker.returncode,) for worker in workers]

    assert all(returncode == 0 for _stdout, _stderr, returncode in results), results
    assert daily.read_bytes() == original + suffix
    journal = json.loads(
        (manifest_path.parent / "transaction.json").read_text(encoding="utf-8")
    )
    assert journal["status"] == "rolled_back"
    assert len(journal["recovery_merges"]) == 1


def test_two_concurrent_appliers_preserve_committed_terminal_journal(tmp_path):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    daily = _write(
        vault / "knowledge" / "daily" / "2026-07-20.md",
        "# Daily\n- `[10:00:00] tool | s1 | demo | Read` \n",
    )
    audit_file = _audit_file(vault, state)
    audit_bytes = audit_file.read_bytes()
    audit_report = json.loads(audit_bytes)
    manifest_path = repair.create_backup(audit_report, audit_bytes, vault, state)
    barrier = threading.Barrier(2)
    errors = []

    def apply_worker():
        barrier.wait()
        try:
            repair.apply_repair(
                audit_report,
                audit_bytes,
                vault,
                state,
                manifest_path,
                backup_only=False,
            )
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    workers = [threading.Thread(target=apply_worker) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert errors == []
    assert b"tool |" not in daily.read_bytes()
    journal = json.loads(
        (manifest_path.parent / "transaction.json").read_text(encoding="utf-8")
    )
    assert journal["status"] == "committed"


def test_lock_timeout_does_not_overwrite_active_transaction_journal(
    tmp_path, monkeypatch
):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    _write(
        vault / "knowledge" / "daily" / "2026-07-20.md",
        "# Daily\n- `[10:00:00] tool | s1 | demo | Read` \n",
    )
    audit_file = _audit_file(vault, state)
    audit_bytes = audit_file.read_bytes()
    audit_report = json.loads(audit_bytes)
    manifest_path = repair.create_backup(audit_report, audit_bytes, vault, state)
    manifest = repair.validate_manifest(manifest_path, vault, state)
    transaction_path = manifest_path.parent / "transaction.json"
    active_journal = {
        "schema_version": repair.SCHEMA_VERSION,
        "audit_report_sha256": manifest["audit_report_sha256"],
        "status": "committing",
        "attempted_path_ids": [manifest["files"][0]["path_id"]],
        "mutated_path_ids": [],
        "restored_path_ids": [],
    }
    transaction_path.write_text(json.dumps(active_journal), encoding="utf-8")
    before = transaction_path.read_bytes()

    @contextmanager
    def unavailable_repair_lease(_state_root):
        raise TimeoutError("repair lease is held")
        yield

    monkeypatch.setattr(repair, "_repair_writer_locks", unavailable_repair_lease)

    with pytest.raises(repair.TransactionError, match="transaction lock failure"):
        repair._execute_transaction(manifest, manifest_path, vault, state)

    assert transaction_path.read_bytes() == before


def test_feedback_recreated_after_quarantine_crash_is_preserved_as_valid_json(tmp_path):
    import repair_installed_memory as repair
    from feedback_capture import feedback_writer_lock

    vault, state = _vault(tmp_path)
    feedback = _write(
        vault / "knowledge" / "feedback" / "false.json",
        json.dumps(
            {
                "id": "false",
                "text": "generated",
                "trigger": "opencode-idle",
                "status": "candidate",
            }
        ),
    )
    original = feedback.read_bytes()
    audit_file = _audit_file(vault, state)
    manifest_path = _crash_after_first_commit(vault, state, audit_file)
    assert not feedback.exists()
    recreated = json.dumps(
        {
            "id": "new-user-feedback",
            "text": "No, preserve this post-crash correction.",
            "trigger": "opencode-user-message",
            "source_role": "user",
            "status": "candidate",
        }
    ).encode()
    with feedback_writer_lock(state, timeout=2.0):
        feedback.write_bytes(recreated)

    repair.recover_incomplete_transactions(vault, state)

    assert feedback.read_bytes() == original
    feedback_files = list(feedback.parent.glob("*.json"))
    assert len(feedback_files) == 2
    preserved = next(path for path in feedback_files if path != feedback)
    assert json.loads(preserved.read_bytes()) == json.loads(recreated)
    journal = json.loads(
        (manifest_path.parent / "transaction.json").read_text(encoding="utf-8")
    )
    assert journal["recovery_merges"][0]["strategy"] == "feedback_recreation"


def test_feedback_rolling_back_reentry_reuses_verified_preserved_copy(tmp_path):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    feedback = _write(
        vault / "knowledge" / "feedback" / "false.json",
        json.dumps(
            {
                "id": "false",
                "text": "generated",
                "trigger": "opencode-idle",
                "status": "candidate",
            }
        ),
    )
    audit_file = _audit_file(vault, state)
    manifest_path = _crash_after_first_commit(vault, state, audit_file)
    recreated = json.dumps(
        {
            "id": "new-user-feedback",
            "text": "Keep this correction.",
            "trigger": "opencode-user-message",
            "source_role": "user",
            "status": "candidate",
        }
    ).encode()
    feedback.write_bytes(recreated)
    repair.recover_incomplete_transactions(vault, state)
    journal_path = manifest_path.parent / "transaction.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    preserved = vault / journal["recovery_merges"][0]["preserved_path"]
    preserved_before = preserved.read_bytes()
    journal["status"] = "rolling_back"
    journal["recovery_merges"][0]["status"] = "planned"
    repair._atomic_write(journal_path, repair._json_bytes(journal), manifest_path.parent)

    repair.recover_incomplete_transactions(vault, state)

    assert preserved.read_bytes() == preserved_before == recreated
    assert len(list(feedback.parent.glob("*.json"))) == 2
    final = json.loads(journal_path.read_text(encoding="utf-8"))
    assert final["status"] == "rolled_back"
    assert final["recovery_merges"][0]["status"] == "complete"


def test_recovery_preserves_non_append_divergence_for_manual_recovery(tmp_path):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    daily = _write(
        vault / "knowledge" / "daily" / "2026-07-20.md",
        "# Daily\n- `[10:00:00] tool | s1 | demo | Read` \n",
    )
    audit_file = _audit_file(vault, state)
    manifest_path = _crash_after_first_commit(vault, state, audit_file)
    divergent = b"replacement that is not an append of staged bytes\n"
    daily.write_bytes(divergent)

    with pytest.raises(repair.TransactionError, match="manual recovery"):
        repair.recover_incomplete_transactions(vault, state)

    assert daily.read_bytes() == divergent
    journal = json.loads(
        (manifest_path.parent / "transaction.json").read_text(encoding="utf-8")
    )
    assert journal["status"] == "critical_manual_recovery"
    issue = journal["manual_recovery"][0]
    assert issue["current_sha256"] == _sha(divergent)
    assert {"backup_sha256", "staged_sha256", "path_id"} <= set(issue)


def test_recovery_does_not_remove_diverged_quarantine_output(tmp_path):
    import repair_installed_memory as repair

    vault, state = _vault(tmp_path)
    feedback = _write(
        vault / "knowledge" / "feedback" / "false.json",
        json.dumps(
            {
                "id": "false",
                "text": "generated",
                "trigger": "opencode-idle",
                "status": "candidate",
            }
        ),
    )
    audit_file = _audit_file(vault, state)
    manifest_path = _crash_after_first_commit(vault, state, audit_file)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    destination = manifest_path.parent / "quarantine" / manifest["files"][0]["path"]
    divergent = b"changed quarantine evidence"
    destination.write_bytes(divergent)

    with pytest.raises(repair.TransactionError, match="manual recovery"):
        repair.recover_incomplete_transactions(vault, state)

    assert not feedback.exists()
    assert destination.read_bytes() == divergent
