"""The installers keep the smoke's JSON report in logs/ instead of printing it.

On the live update the whole doctor report, one line of about 15 kB, went into
the owner's terminal between the smoke's two short lines. The report is for a
machine; the operator reads the stderr lines. See clig.dev, "Output".
"""

from __future__ import annotations

from pathlib import Path

import install_smoke

ROOT = Path(__file__).resolve().parents[1]


def test_a_report_path_gets_the_report_and_stdout_stays_empty(tmp_path: Path, capsys) -> None:
    destination = tmp_path / "logs" / "install-smoke.json"

    install_smoke._emit_report('{"status": "ok"}', destination)

    assert (destination.read_text(encoding="utf-8"), capsys.readouterr().out) == ('{"status": "ok"}\n', "")


def test_without_a_path_the_report_is_printed_as_before(capsys) -> None:
    install_smoke._emit_report('{"status": "ok"}', None)

    assert capsys.readouterr().out == '{"status": "ok"}\n'


def test_both_installers_ask_for_the_report_file() -> None:
    shell = (ROOT / "install.sh").read_text(encoding="utf-8")
    powershell = (ROOT / "install.ps1").read_text(encoding="utf-8")

    assert ('--report "$STATE_ROOT/logs/install-smoke.json"' in shell, "logs\\install-smoke.json" in powershell) == (
        True,
        True,
    )
