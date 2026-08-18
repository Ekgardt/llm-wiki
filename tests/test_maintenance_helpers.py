"""Maintenance step contracts (OPEN-040).

Step output never lives in memory: both streams go straight to an owner-only
artifact, the report keeps a short redacted summary, and the artifact is always
named so a truncated line is not the end of the trail. Report families are kept
under bounded retention by age, count, and total size.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import maintenance_helpers
import pytest


@pytest.fixture
def artifacts(tmp_path, monkeypatch):
    directory = tmp_path / "maintenance"
    monkeypatch.setattr(maintenance_helpers, "ARTIFACT_DIR", directory)
    return directory


def _run_python(code: str, logs: list, label: str = "scan", timeout: int = 60) -> int:
    return maintenance_helpers.run_step(
        [sys.executable, "-c", code], logs.append, label, timeout=timeout
    )


def test_run_step_redacts_stdout_and_stderr_before_logging(artifacts):
    token = "sk-abcdefghijklmnopqrstuvwxyz012345"
    pem = "-----BEGIN PRIVATE KEY-----\\nc2VjcmV0LWtleQ==\\n-----END PRIVATE KEY-----"
    logs: list[str] = []

    status = _run_python(
        "import sys\n"
        f"print('captured {token}')\n"
        f"print('''{pem}''', file=sys.stderr)\n"
        "sys.exit(1)\n",
        logs,
    )

    rendered = "\n".join(logs)
    assert status == 1
    assert token not in rendered
    assert "c2VjcmV0LWtleQ" not in rendered
    assert "[REDACTED" in rendered


def test_run_step_names_the_full_output_artifact(artifacts):
    logs: list[str] = []

    _run_python("print('line one')\nprint('line two')\n", logs)

    assert any("full output → logs/maintenance/" in line for line in logs)
    written = sorted(artifacts.glob("*.out.log"))
    assert len(written) == 1
    assert "line two" in written[0].read_text(encoding="utf-8")


def test_step_artifact_is_owner_only(artifacts):
    logs: list[str] = []

    _run_python("print('x')\n", logs)

    mode = sorted(artifacts.glob("*.out.log"))[0].stat().st_mode & 0o777
    if os.name == "nt":  # Windows ignores POSIX permission bits
        pytest.skip("POSIX permission bits are not enforced on Windows")
    assert mode == 0o600


def test_long_output_is_summarised_but_kept_whole(artifacts):
    logs: list[str] = []

    _run_python("for i in range(500):\n    print(f'line-{i}')\n", logs)

    summary = [line for line in logs if "full output" not in line]
    assert len(summary) == maintenance_helpers.STEP_SUMMARY_LINES
    assert "line-499" in summary[-1]
    artifact = sorted(artifacts.glob("*.out.log"))[0]
    assert "line-0\n" in artifact.read_text(encoding="utf-8")


def test_empty_output_leaves_no_artifact(artifacts):
    logs: list[str] = []

    _run_python("pass\n", logs)

    assert not list(artifacts.glob("*.log"))
    assert any("(no output captured)" in line for line in logs)


def test_run_step_redacts_os_error_before_logging(artifacts, monkeypatch):
    token = "ghp_abcdefghijklmnopqrstuvwxyz012345"

    def fail(*args, **kwargs):
        raise OSError(f"failed with token={token}")

    monkeypatch.setattr(maintenance_helpers.subprocess, "run", fail)
    logs: list[str] = []

    assert maintenance_helpers.run_step(["tool"], logs.append, "scan") == 2

    rendered = "\n".join(logs)
    assert token not in rendered
    assert "[REDACTED" in rendered


def test_run_step_timeout_message_contains_no_command_data(artifacts, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], timeout=1)

    monkeypatch.setattr(maintenance_helpers.subprocess, "run", timeout)
    logs: list[str] = []

    assert maintenance_helpers.run_step(["tool", "secret-argument"], logs.append, "scan") == 2
    assert "secret-argument" not in "\n".join(logs)


def _report(directory, name: str, *, size: int = 10, age_days: float = 0.0):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"x" * size)
    stamp = time.time() - age_days * 86400
    os.utime(path, (stamp, stamp))
    return path


def test_prune_reports_drops_expired_files(tmp_path):
    fresh = _report(tmp_path, "nightly-fresh.md", age_days=1)
    stale = _report(tmp_path, "nightly-stale.md", age_days=45)

    removed = maintenance_helpers.prune_reports(tmp_path, "nightly-*.md")

    assert removed == 1
    assert fresh.exists()
    assert not stale.exists()


def test_prune_reports_enforces_count_and_size(tmp_path):
    for number in range(10):
        _report(tmp_path, f"weekly-{number:02}.md", size=1024, age_days=number * 0.1)

    removed = maintenance_helpers.prune_reports(
        tmp_path, "weekly-*.md", max_files=4, max_bytes=10 * 1024
    )

    survivors = sorted(path.name for path in tmp_path.glob("weekly-*.md"))
    assert removed == 6
    assert survivors == ["weekly-00.md", "weekly-01.md", "weekly-02.md", "weekly-03.md"]


def test_prune_reports_tolerates_a_missing_directory(tmp_path):
    assert maintenance_helpers.prune_reports(tmp_path / "absent", "*.md") == 0


def test_prune_maintenance_output_covers_reports_and_artifacts(tmp_path, monkeypatch):
    reports = tmp_path / "logs"
    artifacts = reports / "maintenance"
    monkeypatch.setattr(maintenance_helpers, "REPORTS_DIR", reports)
    monkeypatch.setattr(maintenance_helpers, "ARTIFACT_DIR", artifacts)
    _report(reports, "nightly-old.md", age_days=60)
    _report(reports, "weekly-old.md", age_days=60)
    _report(reports, "lint-old.md", age_days=60)
    _report(artifacts, "old-step.out.log", age_days=60)
    _report(reports, "nightly-new.md", age_days=1)

    removed = maintenance_helpers.prune_maintenance_output()

    assert removed == 4
    assert (reports / "nightly-new.md").exists()
