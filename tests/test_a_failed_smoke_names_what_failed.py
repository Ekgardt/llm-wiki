"""A failed install smoke names the doctor checks that failed.

A rerun stopped at "install smoke failed: RuntimeError". See
docs/research/2026-09-25-a-failed-reinstall-puts-the-old-one-back.md.
"""

from __future__ import annotations

import subprocess

import install_smoke


def test_the_message_names_the_failing_checks() -> None:
    report = {"checks": [{"id": "queue", "status": "error"}, {"id": "claims", "status": "ok"}]}

    text = install_smoke.bounded_error(install_smoke.SmokeFailure(install_smoke.doctor_error_message(report)))

    assert "Doctor reported error in: queue;" in text


def test_a_foreign_error_is_named_by_type_only() -> None:
    assert install_smoke.bounded_error(OSError("/private/path")) == "install smoke failed: OSError\n"


def test_a_doctor_error_exit_is_read_before_its_code() -> None:
    import json
    import subprocess

    report = {
        "schema_version": "1.0",
        "generated_at": "2026-09-25T00:00:00+00:00",
        "overall_status": "error",
        "repaired": [],
        "checks": [{"id": "environment", "status": "error"}],
        "counts": {},
        "run_deletion": {},
    }
    completed = subprocess.CompletedProcess(["doctor"], 2, stdout=json.dumps(report), stderr="")

    try:
        install_smoke.checked_doctor_report(completed)
    except install_smoke.SmokeFailure as failure:
        message = str(failure)
    assert message.startswith("Doctor reported error in: environment;")


_FAKE_SECRET = "abcdefghijklmnop" + "1234"


def test_a_credential_in_doctors_stderr_stays_out_of_the_smoke_failure() -> None:
    completed = subprocess.CompletedProcess(["doctor"], 1, "", "failed with token=" + _FAKE_SECRET)

    cause = install_smoke.doctor_reply_cause(completed)

    assert "abcdefghijklmnop1234" not in cause
