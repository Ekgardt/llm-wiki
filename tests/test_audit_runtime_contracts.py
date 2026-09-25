"""Contract tests for audit-flagged runtime paths.

Covers:
- compile_memory no longer exposes heuristic lifecycle mutation
- feedback_capture stdin JSON (OpenCode plugin)
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
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _no_operator_dlp_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test of "protected content is refused" must own its allowlist.

    The owner enabled `LLM_WIKI_DLP_POLICY` on 2026-08-28 — a supported,
    documented setting that allowlists 33 known key-shaped fixtures so the
    vault can be exported. Five export tests here then failed on his machine
    while CI stayed green, because they assert a refusal that his policy
    legitimately lifts. The env is not wrong; the tests were reading it. Each
    test that wants a policy now sets one itself.
    """
    monkeypatch.delenv("LLM_WIKI_DLP_POLICY", raising=False)


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


