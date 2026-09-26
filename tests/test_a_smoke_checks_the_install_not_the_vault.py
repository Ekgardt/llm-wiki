"""The install smoke fails on a broken install, not on the vault's history (audit 2026-09-26 A-10).

docs/research/2026-09-26-a-smoke-checks-the-install-not-the-vault.md
"""
from __future__ import annotations

import json
import subprocess

import install_smoke
import pytest


def _completed(checks: list[dict], returncode: int = 2) -> subprocess.CompletedProcess:
    report = {
        "schema_version": "1.0",
        "generated_at": "2026-09-26T00:00:00+00:00",
        "overall_status": "error",
        "repaired": [],
        "checks": checks,
        "counts": {},
        "run_deletion": {},
    }
    return subprocess.CompletedProcess(["doctor"], returncode, stdout=json.dumps(report), stderr="")


def test_a_failed_night_does_not_block_the_reinstall() -> None:
    report = install_smoke._checked_doctor_report(_completed([{"id": "scheduler", "status": "error"}]))

    assert (install_smoke._smoke_status(report), install_smoke._vault_findings_note(report)[:47]) == (
        "degraded",
        "install smoke: the vault reports errors in: sch",
    )


@pytest.mark.parametrize("checks", [[{"id": "mcp", "status": "error"}], []])
def test_a_broken_install_or_an_unnamed_error_still_fails(checks) -> None:
    with pytest.raises(install_smoke.SmokeFailure, match="Doctor reported error in"):
        install_smoke._checked_doctor_report(_completed(checks))


def test_an_error_report_with_the_wrong_exit_code_fails() -> None:
    with pytest.raises(install_smoke.SmokeFailure, match="Doctor failed"):
        install_smoke._checked_doctor_report(_completed([{"id": "queue", "status": "error"}], returncode=1))
