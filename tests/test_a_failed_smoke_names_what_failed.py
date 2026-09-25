"""A failed install smoke names the doctor checks that failed.

A rerun stopped at "install smoke failed: RuntimeError". See
docs/research/2026-09-25-a-failed-reinstall-puts-the-old-one-back.md.
"""

from __future__ import annotations

import install_smoke


def test_the_message_names_the_failing_checks() -> None:
    report = {"checks": [{"id": "queue", "status": "error"}, {"id": "claims", "status": "ok"}]}

    text = install_smoke._bounded_error(install_smoke.SmokeFailure(install_smoke._doctor_error_message(report)))

    assert "Doctor reported error in: queue;" in text


def test_a_foreign_error_is_named_by_type_only() -> None:
    assert install_smoke._bounded_error(OSError("/private/path")) == "install smoke failed: OSError\n"


def test_a_doctor_error_exit_is_read_before_its_code() -> None:
    import json
    import subprocess

    report = {
        "schema_version": "1.0",
        "generated_at": "2026-09-25T00:00:00+00:00",
        "overall_status": "error",
        "repaired": [],
        "checks": [{"id": "claims", "status": "error"}],
        "counts": {},
        "run_deletion": {},
    }
    completed = subprocess.CompletedProcess(["doctor"], 2, stdout=json.dumps(report), stderr="")

    try:
        install_smoke._checked_doctor_report(completed)
    except install_smoke.SmokeFailure as failure:
        message = str(failure)
    assert message.startswith("Doctor reported error in: claims;")
