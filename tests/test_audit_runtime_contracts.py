"""Contract tests for audit-flagged runtime paths.

Covers:
- compile_memory no longer exposes heuristic lifecycle mutation
- feedback_capture stdin JSON (OpenCode plugin)
- loop_detector matches real breadcrumb format
- MEMORY_LLM_PROVIDER=fake smoke for compile plan apply
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import zipfile
from argparse import Namespace
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _write_test_archive(path: Path, archive_format: str, members: dict[str, bytes]) -> None:
    if archive_format == "zip":
        with zipfile.ZipFile(path, "w") as archive:
            for name, content in members.items():
                archive.writestr(name, content)
        return
    mode = "w:gz" if archive_format == "tar.gz" else "w"
    with tarfile.open(path, mode) as archive:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))


def _write_link_archive(path: Path, archive_format: str) -> None:
    if archive_format == "zip":
        info = zipfile.ZipInfo("notes/link.md")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(info, b"../outside.md")
        return
    info = tarfile.TarInfo("notes/link.md")
    info.type = tarfile.SYMTYPE
    info.linkname = "../outside.md"
    with tarfile.open(path, "w") as archive:
        archive.addfile(info)


def _write_dlp_policy(path: Path, *, literals: list[str], fingerprints: list[str]) -> None:
    from reliable_memory import canonical_json_bytes

    payload = {
        "version": 1,
        "literals": literals,
        "allow_fingerprints": fingerprints,
    }
    document = {
        **payload,
        "sha256": hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
    }
    path.write_text(json.dumps(document), encoding="utf-8")


def test_compile_memory_removes_heuristic_lifecycle_mutation():
    import compile_memory

    assert not hasattr(compile_memory, "_check_contradictions_pre_write")
    assert not hasattr(compile_memory, "_mark_superseded")
    assert not hasattr(compile_memory, "_mark_refined")


def test_feedback_capture_stdin_json(tmp_path, monkeypatch):
    import feedback_capture

    monkeypatch.setattr(feedback_capture, "ROOT", tmp_path)
    monkeypatch.setattr(feedback_capture, "FEEDBACK_DIR", tmp_path / "knowledge" / "feedback")
    (tmp_path / "knowledge" / "notes").mkdir(parents=True)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "runtime"))

    payload = json.dumps(
        {
            "text": "No, always use Postgres instead of SQLite for production",
            "session_id": "sess-test",
            "slug": "demo",
            "trigger": "opencode-idle",
        }
    )
    env = dict(os.environ)
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "feedback_capture.py")],
        input=payload,
        text=True,
        capture_output=True,
        env=env,
        cwd=str(ROOT),
    )
    assert result.returncode == 0
    # Candidate files under vault feedback dir (module uses its ROOT from env at import
    # in subprocess — script resolves ROOT from memory_state). Check via capture_from_text
    # unit path as well:
    cid = feedback_capture.capture_from_text(
        "No, always use Postgres instead of SQLite for production",
        session_id="sess-test",
        slug="demo",
        trigger="opencode-idle",
    )
    assert cid is not None
    assert (tmp_path / "knowledge" / "feedback" / f"{cid}.json").exists()


@pytest.mark.parametrize(
    ("archive_format", "suffix"),
    (("zip", ".zip"), ("tar", ".tar"), ("tar.gz", ".tar.gz")),
)
def test_export_verification_blocks_secret_content_without_echoing_it(
    tmp_path, capsys, archive_format, suffix
):
    import export_vault

    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    archive = tmp_path / f"export{suffix}"
    _write_test_archive(
        archive,
        archive_format,
        {"knowledge/notes/example.md": f"token={secret}".encode()},
    )

    result = export_vault._verify_archive(archive)
    output = capsys.readouterr()

    assert result == 1
    assert secret not in output.out + output.err


def test_export_verification_blocks_global_tar_metadata_without_echoing_it(tmp_path, capsys):
    import export_vault

    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    archive_path = tmp_path / "export.tar"
    with tarfile.open(
        archive_path,
        "w",
        format=tarfile.PAX_FORMAT,
        pax_headers={"comment": f"token={secret}"},
    ) as archive:
        info = tarfile.TarInfo("notes/example.md")
        info.size = 5
        archive.addfile(info, io.BytesIO(b"clean"))

    result = export_vault._verify_archive(archive_path)
    output = capsys.readouterr()

    assert result == 1
    assert secret not in output.out + output.err


def test_export_verification_uses_literals_and_exact_member_fingerprints(tmp_path, monkeypatch):
    import export_vault

    protected = b"customer marker ALPHA-PRIVATE"
    archive = tmp_path / "export.zip"
    policy = tmp_path / "policy.json"
    _write_test_archive(archive, "zip", {"notes/example.md": protected})
    _write_dlp_policy(policy, literals=["ALPHA-PRIVATE"], fingerprints=[])
    monkeypatch.setenv("LLM_WIKI_DLP_POLICY", str(policy))

    assert export_vault._verify_archive(archive) == 1

    allowed = hashlib.sha256(protected).hexdigest()
    _write_dlp_policy(
        policy,
        literals=["ALPHA-PRIVATE"],
        fingerprints=[allowed],
    )
    assert export_vault._verify_archive(archive) == 0


def test_export_verification_rejects_ambiguous_member_paths(tmp_path):
    import export_vault

    archive = tmp_path / "export.zip"
    _write_test_archive(archive, "zip", {"../outside.md": b"clean"})

    assert export_vault._verify_archive(archive) == 1


@pytest.mark.parametrize(("archive_format", "suffix"), (("zip", ".zip"), ("tar", ".tar")))
def test_export_verification_rejects_link_members(tmp_path, archive_format, suffix):
    import export_vault

    archive = tmp_path / f"export{suffix}"
    _write_link_archive(archive, archive_format)

    assert export_vault._verify_archive(archive) == 1


def test_export_verification_bounds_uncompressed_member_reads(tmp_path, monkeypatch):
    import export_vault

    archive = tmp_path / "export.zip"
    _write_test_archive(archive, "zip", {"notes/large.md": b"12345"})
    monkeypatch.setattr(export_vault, "MAX_MEMBER_BYTES", 4, raising=False)

    assert export_vault._verify_archive(archive) == 1


def test_export_verification_bounds_archive_member_count(tmp_path, monkeypatch):
    import export_vault

    archive = tmp_path / "export.zip"
    _write_test_archive(archive, "zip", {"one.md": b"1", "two.md": b"2"})
    monkeypatch.setattr(export_vault, "MAX_ARCHIVE_MEMBERS", 1)

    assert export_vault._verify_archive(archive) == 1


def test_export_verification_bounds_total_uncompressed_size(tmp_path, monkeypatch):
    import export_vault

    archive = tmp_path / "export.zip"
    _write_test_archive(archive, "zip", {"one.md": b"123", "two.md": b"456"})
    monkeypatch.setattr(export_vault, "MAX_TOTAL_UNCOMPRESSED_BYTES", 5)

    assert export_vault._verify_archive(archive) == 1


def test_export_no_verify_cannot_publish_failed_archive(tmp_path, monkeypatch):
    import export_vault

    final = tmp_path / "final.zip"
    secret = b"sk-abcdefghijklmnopqrstuvwxyz012345"
    args = Namespace(
        output=final,
        ref="HEAD",
        format="zip",
        verify=False,
        strict=False,
    )

    def build(_ref, _format, output):
        _write_test_archive(output, "zip", {"notes/private.md": secret})

    monkeypatch.setattr(export_vault, "parse_args", lambda: args)
    monkeypatch.setattr(export_vault, "_require_git", lambda: None)
    monkeypatch.setattr(export_vault, "_git_archive", build)

    assert export_vault.main() == 1
    assert not final.exists()


def test_adapter_uses_canonical_source_as_agent_identity():
    import integration_adapter

    agents = {
        source: integration_adapter.normalize_event(source, "session_start", {}).agent
        for source in ("opencode", "codex", "claude")
    }

    assert agents == {
        "opencode": "opencode",
        "codex": "codex",
        "claude": "claude",
    }


def test_agent_timeline_reports_five_canonical_agent_ids(tmp_path):
    import agent_timeline

    day = tmp_path / f"{date.today().isoformat()}.md"
    blocks = []
    for index, agent in enumerate(("opencode", "codex", "claude", "cursor", "antigravity")):
        blocks.append(
            f"## [10:0{index}:00] {agent}-session | session-{index}\n"
            "**Decisions made**\n"
            f"- Decision from {agent}.\n"
        )
    day.write_text("\n".join(blocks), encoding="utf-8")

    activity = agent_timeline._extract_activity(day, None, days=30)

    assert {item["agent"] for item in activity} == {
        "opencode",
        "codex",
        "claude",
        "cursor",
        "antigravity",
    }


def test_agent_timeline_reads_historical_tool_breadcrumbs():
    import agent_timeline

    breadcrumb = agent_timeline.parse_tool_breadcrumb(
        "- `[10:00:01] tool | abcd1234 | demo | edit` src/app.py",
        fallback_agent="codex",
    )

    assert breadcrumb == {
        "time": "10:00:01",
        "agent": "codex",
        "session": "abcd1234",
        "slug": "demo",
        "tool": "edit",
        "target": "src/app.py",
    }


def test_loop_detector_classifies_single_agent_churn(tmp_path, monkeypatch):
    import loop_detector

    daily = tmp_path / "knowledge" / "daily"
    daily.mkdir(parents=True)
    day = daily / f"{date.today().isoformat()}.md"
    day.write_text(
        "# Daily\n"
        "- `[10:00:01] tool | opencode | abcd1234 | demo | edit` src/app.py\n"
        "- `[10:05:02] tool | opencode | abcd1234 | demo | write` src/app.py\n"
        "- `[10:10:03] tool | opencode | efgh5678 | demo | Edit` src/app.py\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(loop_detector, "DAILY_DIR", daily)
    monkeypatch.setattr(loop_detector, "ROOT", tmp_path)
    loops = loop_detector.detect_file_edit_loops("demo", days=30, threshold=3)
    assert loops
    assert loops[0]["type"] == "single_agent_churn"
    assert loops[0]["agents"] == ["opencode"]
    assert loops[0]["target"] == "src/app.py"
    assert loops[0]["edit_count"] >= 3


def test_loop_detector_classifies_multi_agent_loop(tmp_path, monkeypatch):
    import loop_detector

    daily = tmp_path / "knowledge" / "daily"
    daily.mkdir(parents=True)
    day = daily / f"{date.today().isoformat()}.md"
    day.write_text(
        "# Daily\n"
        "- `[10:00:01] tool | opencode | session1 | demo | edit` src/app.py\n"
        "- `[10:05:02] tool | codex | session2 | demo | write` src/app.py\n"
        "- `[10:10:03] tool | opencode | session1 | demo | edit` src/app.py\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(loop_detector, "DAILY_DIR", daily)

    loops = loop_detector.detect_file_edit_loops("demo", days=30, threshold=3)

    assert loops[0]["type"] == "multi_agent_loop"
    assert loops[0]["agents"] == ["codex", "opencode"]


def test_loop_detector_groups_recurring_normalized_errors(tmp_path, monkeypatch):
    import loop_detector

    daily = tmp_path / "knowledge" / "daily"
    daily.mkdir(parents=True)
    agents = ("opencode", "codex", "claude")
    error_prefixes = ("Error: RuntimeError:", "Error: RuntimeError:", "RuntimeError:")
    for offset, agent in enumerate(agents):
        current = date.today() - timedelta(days=offset)
        (daily / f"{current.isoformat()}.md").write_text(
            f"## [10:00:0{offset}] {agent}-session | session-{offset}\n"
            "- Project slug: `demo`\n"
            f"- {error_prefixes[offset]} build {100 + offset} failed at "
            f"C:/tmp/job-{100 + offset}\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(loop_detector, "DAILY_DIR", daily)
    monkeypatch.setattr(loop_detector, "FEEDBACK_DIR", tmp_path / "feedback")

    loops = loop_detector.detect_all("demo", days=30, threshold=3)
    recurring = [item for item in loops if item["type"] == "recurring_error"]

    assert len(recurring) == 1
    assert recurring[0]["occurrence_count"] == 3
    assert recurring[0]["agents"] == ["claude", "codex", "opencode"]
    assert "<n>" in recurring[0]["signature"]


def test_fake_llm_provider_returns_canned_json(monkeypatch):
    import llm_client

    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "fake")
    monkeypatch.setenv(
        "MEMORY_LLM_FAKE_RESPONSE",
        '{"operations": [], "audit": {}}\nCOMPILE_AUDIT: verified 0 evidence citations; 0 dedup checks performed; 0 stubs skipped; 0 contradictions handled; 0 pages rejected as below-threshold',
    )
    out = llm_client.call_llm("hello", system_prompt="sys")
    assert "operations" in out
    assert "COMPILE_AUDIT" in out


def test_flush_memory_uses_maybe_compile(monkeypatch, tmp_path):
    """maybe_trigger_compile must call spawn_compile_if_idle, not spawn_detached."""
    import flush_memory

    calls = []

    def fake_spawn(force=False):
        calls.append(force)
        return True, "spawned compile pid=1"

    monkeypatch.setattr(flush_memory, "spawn_compile_if_idle", fake_spawn)
    monkeypatch.setattr(flush_memory, "file_hash", lambda p: "abc")
    monkeypatch.setenv("MEMORY_COMPILE_AFTER_HOUR", "0")
    monkeypatch.setenv("MEMORY_COMPILE_COOLDOWN_SECONDS", "0")

    daily = tmp_path / "2026-07-09.md"
    daily.write_text("# d\n", encoding="utf-8")
    state: dict = {"compiled_daily_hashes": {}}
    flush_memory.maybe_trigger_compile(state, daily, "major")
    assert calls == [False]
    assert state["last_compile_spawned_reason"] == "spawned compile pid=1"
