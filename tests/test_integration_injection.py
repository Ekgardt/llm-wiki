"""The installers' guarded steps on every platform: the smoke run's exit status, timeouts, signals and
test-tree cleanup, the MCP function and Codex hook probes in a temporary home, the official Codex hooks
and the one external state root.

The CI installer job selects these with `-k installer` on Linux, Windows and macOS; the other
integration scenarios live in `tests/test_integration_injection_*.py`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from tests.breadcrumb_worker_helpers import ingress as ingress
from tests.integration_injection_case_helpers import (
    _codex_inline_hooks_toml,
    _shell_function,
    _shell_functions,
    _unmet_substrings,
    require_tool,
)
from tests.integration_injection_helpers import (
    _ALIVE_SHELL,
    ROOT,
    _codex_mcp_toml,
)
from tests.powershell_literal import ps_literal


def test_codex_installers_use_official_hooks_and_request_trust_review():
    shell = (ROOT / "install.sh").read_text(encoding="utf-8")
    powershell = (ROOT / "install.ps1").read_text(encoding="utf-8")

    assert (
        _unmet_codex_installer_checks(shell),
        _unmet_codex_installer_checks(powershell),
        "not installed automatically" in powershell.casefold(),
    ) == ([], [], True)


def _unmet_codex_installer_checks(installer: str) -> list[tuple[str, bool]]:
    return _unmet_substrings(
        (
            ("integrations/codex/hooks.json", installer.replace("\\", "/"), True),
            ("codex_memory.py", installer, True),
            ("hooks-state", installer, True),
            ("--config", installer, True),
            ("/hooks", installer, True),
            ("trust", installer.casefold(), True),
        )
    )


def _installer_test_section(source: str) -> str:
    section = source.split("4. Run production smoke", 1)[1].split(
        "5. Set environment variables", 1
    )[0]
    return section.split("\n", 1)[1]


def _write_fake_uv(directory: Path, *, exit_code: int, last_line: str) -> None:
    fake_uv = directory / "uv"
    fake_uv.write_text(
        "#!/bin/sh\n"
        "i=0\n"
        "while [ \"$i\" -lt 100 ]; do echo 'filler output'; i=$((i + 1)); done\n"
        f"printf '%s\\n' {json.dumps(last_line)}\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o700)


@pytest.fixture(scope="module")
def windows_fake_uv(tmp_path_factory):
    if os.name != "nt":
        return None
    compiler = shutil.which("powershell")
    if compiler is None:
        pytest.skip("Windows PowerShell compiler unavailable")
    build = tmp_path_factory.mktemp("native-fake-uv")
    source = build / "fake-uv.cs"
    executable = build / "uv.exe"
    source.write_text(
        "using System;\n"
        "using System.Diagnostics;\n"
        "using System.IO;\n"
        "using System.Threading;\n"
        "public static class FakeUv {\n"
        "  public static int Main() {\n"
        '    var pidFile = Environment.GetEnvironmentVariable("FAKE_UV_PID_FILE");\n'
        '    var startedFile = Environment.GetEnvironmentVariable("FAKE_UV_STARTED_FILE");\n'
        "    if (!String.IsNullOrEmpty(pidFile)) File.WriteAllText(pidFile, Process.GetCurrentProcess().Id.ToString());\n"
        '    if (!String.IsNullOrEmpty(startedFile)) File.WriteAllText(startedFile, "started");\n'
        '    var output = Environment.GetEnvironmentVariable("FAKE_UV_OUTPUT");\n'
        "    if (!String.IsNullOrEmpty(output)) Console.WriteLine(output);\n"
        '    if (Environment.GetEnvironmentVariable("FAKE_UV_MODE") == "block") {\n'
        '      var python = Environment.GetEnvironmentVariable("FAKE_UV_PYTHON");\n'
        '      var childScript = Environment.GetEnvironmentVariable("FAKE_UV_CHILD_SCRIPT");\n'
        '      var childInfo = new ProcessStartInfo(python, "\\"" + childScript + "\\"");\n'
        "      childInfo.UseShellExecute = false;\n"
        "      Process.Start(childInfo);\n"
        "      Thread.Sleep(Timeout.Infinite);\n"
        "    }\n"
        "    int code;\n"
        '    return Int32.TryParse(Environment.GetEnvironmentVariable("FAKE_UV_EXIT"), out code) ? code : 0;\n'
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    return _compiled_fake_uv(compiler, source, executable)


def _compiled_fake_uv(compiler: str, source: Path, executable: Path) -> Path:
    """Compile the C# fake `uv`, or skip where the compiler cannot."""
    try:
        compiled = subprocess.run(
            [
                compiler,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"Add-Type -Path {ps_literal(str(source))} "
                f"-OutputAssembly {ps_literal(str(executable))} "
                "-OutputType ConsoleApplication",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.skip("Windows PowerShell compiler unavailable within 120 seconds")
    if compiled.returncode != 0:
        pytest.skip(f"Windows PowerShell compiler unavailable: {compiled.stderr[-500:]}")
    return executable


# The deadline the installer is given, and how long the fake `uv` would run if
# nothing stopped it. The gap between the two is what makes a surviving child
# visible at all: a run that lasts the sleep instead of the deadline was never
# killed, so both numbers belong in the failure rather than in two literals.
_SMOKE_TIMEOUT_SECONDS = 15


_FAKE_UV_SLEEP_SECONDS = 60


def _child_state(directory: Path) -> str:
    """What the stopped child left behind, for a failure that must explain itself."""
    markers = ("child.started", "child.pid", "child.stopped", "child.completed")
    seen = {
        name: (directory / name).read_text(encoding="utf-8") or "yes"
        for name in markers
        if (directory / name).exists()
    }
    return f"child markers: {seen or 'none'}"


def _write_blocking_fake_uv(directory: Path) -> None:
    fake_uv = directory / "uv"
    fake_uv.write_text(
        "#!/bin/sh\n"
        # The trap comes first, before anything that can block or be
        # descheduled. Everything between exec and this line is a window in
        # which the installer's timer kills the child before it can record
        # having been stopped, and the test then fails for the machine being
        # busy rather than for the installer being wrong.
        "stop_child() { : > child.stopped; exit 0; }\n"
        "trap stop_child HUP INT TERM\n"
        "printf '%s' \"$$\" > child.pid\n"
        ": > child.started\n"
        # `wait` is interruptible by a trap; a foreground command is not. With
        # the sleep in the foreground the shell reached its handler only after
        # the sleep returned, which spent part of the installer's half-second
        # escalation window on nothing. Backgrounding it costs the test nothing
        # and hands the whole window to the handler.
        # Long enough that the installer's own timer, not the sleep, ends it,
        # and short enough that a missed signal still ends the run with the
        # assertion that names the cause instead of the caller's wall clock.
        f"sleep {_FAKE_UV_SLEEP_SECONDS} &\n"
        "wait\n"
        ": > child.completed\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o700)


def _write_stubborn_fake_uv_tree(directory: Path) -> None:
    grandchild = directory / "fake-pytest-grandchild"
    grandchild.write_text(
        "#!/bin/bash\n"
        "printf '%s' \"$$\" > grandchild.pid\n"
        ": > grandchild.started\n"
        "trap '' HUP INT TERM\n"
        "while :; do sleep 1; done\n",
        encoding="utf-8",
    )
    grandchild.chmod(0o700)
    child = directory / "fake-pytest-child"
    child.write_text(
        "#!/bin/bash\n"
        "printf '%s' \"$$\" > child.pid\n"
        "trap '' HUP INT TERM\n"
        '"$(dirname "$0")/fake-pytest-grandchild" &\n'
        "wait\n",
        encoding="utf-8",
    )
    child.chmod(0o700)
    fake_uv = directory / "uv"
    fake_uv.write_text(
        "#!/bin/bash\n"
        "printf '%s' \"$$\" > uv.pid\n"
        "stop_uv() { exit 0; }\n"
        "trap stop_uv HUP INT TERM\n"
        '"$(dirname "$0")/fake-pytest-child" &\n'
        "wait\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o700)


def _bash_search_paths() -> list[str | None]:
    git = shutil.which("git")
    if git is None:
        return [None]
    return [None, str(Path(git).resolve().parent.parent / "bin")]


def _bash_runs(bash: str) -> bool:
    try:
        result = subprocess.run([bash, "--version"], capture_output=True, timeout=5, check=False)
    except OSError:
        return False
    return result.returncode == 0


def _working_bash_at(search_path: str | None) -> str | None:
    bash = shutil.which("bash", path=search_path)
    if bash is None or not _bash_runs(bash):
        return None
    return bash


def _require_bash() -> str:
    """A working bash, or skip the test that needs one."""
    bash = _find_working_bash()
    if bash is None:
        pytest.skip("bash unavailable")
    return bash


def _find_working_bash() -> str | None:
    if os.name == "nt":
        return None
    candidates = (_working_bash_at(path) for path in _bash_search_paths())
    return next((bash for bash in candidates if bash is not None), None)


@pytest.mark.parametrize(
    ("fake_exit", "fake_output", "expected_exit", "expected_marker"),
    [
        (1, "smoke ok", 1, "[FAIL] Production smoke failed; installation aborted"),
        (0, "smoke failed", 0, "[OK] Production smoke passed"),
    ],
    ids=["smoke_exit_1_success_output", "smoke_exit_0_failure_output"],
)
def test_unix_installer_trusts_smoke_exit_status(
    tmp_path, fake_exit, fake_output, expected_exit, expected_marker
):
    bash = _require_bash()
    section = _installer_test_section((ROOT / "install.sh").read_text(encoding="utf-8"))
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_uv(fake_bin, exit_code=fake_exit, last_line=fake_output)
    runner = tmp_path / "installer-pytest-contract.sh"
    runner.write_text(
        textwrap.dedent(
            f"""
            set -euo pipefail
            RED='' GREEN='' YELLOW='' BLUE='' NC=''
            PATH="$(dirname "$0")/bin:$PATH"
            # Defined before the smoke step in the real installer; the report goes under it.
            STATE_ROOT="$(dirname "$0")/state"
            export PATH
            info() {{ echo "[INFO] $1"; }}
            ok() {{ echo "[OK] $1"; }}
            warn() {{ echo "[WARN] $1"; }}
            fail() {{ echo "[FAIL] $1"; exit 1; }}
            {section}
            case "$-" in *m*) exit 91 ;; esac
            """
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["LLM_WIKI_INSTALL_SMOKE_TIMEOUT_SECONDS"] = "5"

    result = subprocess.run(
        [bash, str(runner)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )

    assert result.returncode == expected_exit, result.stderr
    assert expected_marker in result.stdout
    assert fake_output in result.stdout


def test_unix_installer_timeout_stops_tests_and_aborts(tmp_path):
    bash = _require_bash()
    section = _installer_test_section((ROOT / "install.sh").read_text(encoding="utf-8"))
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_blocking_fake_uv(fake_bin)
    runner = tmp_path / "installer-timeout-contract.sh"
    runner.write_text(
        textwrap.dedent(
            f"""
            set -euo pipefail
            # The timer must outlast the child reaching its signal trap. The
            # fake `uv` now installs that trap as its first statement, so the
            # window is one fork and exec; one second raced it and five raced it
            # again while the trap sat behind two writes.
            export LLM_WIKI_INSTALL_SMOKE_TIMEOUT_SECONDS={_SMOKE_TIMEOUT_SECONDS}
            PATH="$(dirname "$0")/bin:$PATH"
            # Defined before the smoke step in the real installer; the report goes under it.
            STATE_ROOT="$(dirname "$0")/state"
            export PATH
            info() {{ :; }}
            ok() {{ : > passed.marker; }}
            warn() {{ :; }}
            fail() {{ printf '%s' "$1" > failed.message; exit 1; }}
            {section}
            : > continued.marker
            """
        ),
        encoding="utf-8",
    )
    runner.chmod(0o700)

    started = time.monotonic()
    result = subprocess.run(
        [bash, str(runner)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 1, result.stderr
    # The child must be gone because the installer killed it, not because it
    # ran out of work of its own. Those two look identical in the marker alone,
    # and the clock tells them apart: a run that lasts as long as the fake `uv`
    # sleeps was never signalled, while a run that ends at the deadline was.
    # The measured failure of this assertion was the second kind — the handler
    # reached its escalation 45s late — and `assert not True` said none of it.
    assert not (tmp_path / "child.completed").exists(), (
        f"the smoke child ran to completion instead of being killed: "
        f"installer took {elapsed:.1f}s for a {_SMOKE_TIMEOUT_SECONDS}s deadline "
        f"against a {_FAKE_UV_SLEEP_SECONDS}s child; {_child_state(tmp_path)}; "
        f"stderr={result.stderr[-400:]!r}"
    )
    assert (
        (tmp_path / "failed.message").read_text(),
        (tmp_path / "passed.marker").exists(),
        (tmp_path / "continued.marker").exists(),
    ) == (
        f"Production smoke timed out after {_SMOKE_TIMEOUT_SECONDS}s; installation aborted",
        False,
        False,
    )
    # Named last and with its evidence: this is the assertion that fails when a
    # loaded machine keeps the child off the CPU for the whole half-second the
    # installer waits before escalating to KILL. Without the evidence the
    # failure reads `assert False` and says nothing about which happened.
    assert (tmp_path / "child.stopped").exists(), _child_state(tmp_path)


@pytest.mark.parametrize(
    ("signal_name", "expected_exit"),
    [("HUP", 129), ("INT", 130), ("TERM", 143)],
)
def test_unix_installer_signal_traps_cleanup_and_exit(tmp_path, signal_name, expected_exit):
    bash = _require_bash()
    section = _installer_test_section((ROOT / "install.sh").read_text(encoding="utf-8"))
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_blocking_fake_uv(fake_bin)
    continued = tmp_path / "continued.marker"
    installer = tmp_path / "installer-under-test.sh"
    installer.write_text(
        textwrap.dedent(
            f"""
            set -euo pipefail
            export LLM_WIKI_INSTALL_SMOKE_TIMEOUT_SECONDS=30
            PATH="$(dirname "$0")/bin:$PATH"
            # Defined before the smoke step in the real installer; the report goes under it.
            STATE_ROOT="$(dirname "$0")/state"
            export PATH
            info() {{ :; }}
            ok() {{ :; }}
            warn() {{ :; }}
            {section}
            : > continued.marker
            """
        ),
        encoding="utf-8",
    )
    installer.chmod(0o700)
    orchestrator = tmp_path / "orchestrate-signal.sh"
    orchestrator.write_text(
        _ALIVE_SHELL
        + textwrap.dedent(
            f"""
            set -euo pipefail
            # A shell without job control starts an asynchronous job with
            # SIGINT ignored, and an ignored signal cannot be trapped. bash 5
            # installs the trap here anyway, bash 3.2 on macOS does not, so
            # the installer never saw the INT it traps. Job control gives the
            # job its own process group with the default disposition, which is
            # how a terminal delivers Ctrl-C to a real installation.
            set -m
            ./installer-under-test.sh &
            installerPid=$!
            started=0
            for ((attempt = 0; attempt < 500; attempt++)); do
              if [ -f child.started ]; then started=1; break; fi
              if ! alive "$installerPid"; then break; fi
              sleep 0.01
            done
            if [ "$started" -ne 1 ]; then exit 90; fi
            kill -s {signal_name} "$installerPid"
            if wait "$installerPid"; then
              installerExit=0
            else
              installerExit=$?
            fi
            printf '%s' "$installerExit" > installer.status
            childPid="$(cat child.pid)"
            if alive "$childPid"; then
              : > child.alive
              kill -TERM "$childPid"
            fi
            """
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [bash, str(orchestrator)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (
        (tmp_path / "installer.status").read_text(),
        (tmp_path / "child.stopped").exists(),
        not (tmp_path / "child.completed").exists(),
        not (tmp_path / "child.alive").exists(),
        not continued.exists(),
    ) == (
        str(expected_exit),
        True,
        True,
        True,
        True,
    )


def test_unix_installer_signal_kills_complete_stubborn_test_tree(tmp_path):
    bash = _require_bash()
    section = _installer_test_section((ROOT / "install.sh").read_text(encoding="utf-8"))
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_stubborn_fake_uv_tree(fake_bin)
    installer = tmp_path / "installer-under-test.sh"
    installer.write_text(
        textwrap.dedent(
            f"""
            set -euo pipefail
            export LLM_WIKI_INSTALL_SMOKE_TIMEOUT_SECONDS=30
            PATH="$(dirname "$0")/bin:$PATH"
            # Defined before the smoke step in the real installer; the report goes under it.
            STATE_ROOT="$(dirname "$0")/state"
            export PATH
            info() {{ :; }}
            ok() {{ :; }}
            warn() {{ :; }}
            {section}
            : > continued.marker
            """
        ),
        encoding="utf-8",
    )
    installer.chmod(0o700)
    orchestrator = tmp_path / "orchestrate-tree-signal.sh"
    orchestrator.write_text(
        _ALIVE_SHELL
        + textwrap.dedent(
            """
            set -euo pipefail
            ./installer-under-test.sh &
            installerPid=$!
            started=0
            for ((attempt = 0; attempt < 500; attempt++)); do
              if [ -f grandchild.started ]; then started=1; break; fi
              if ! alive "$installerPid"; then break; fi
              sleep 0.01
            done
            if [ "$started" -ne 1 ]; then exit 90; fi
            kill -s TERM "$installerPid"
            if wait "$installerPid"; then
              installerExit=0
            else
              installerExit=$?
            fi
            printf '%s' "$installerExit" > installer.status
            : > survivors
            for pidFile in uv.pid child.pid grandchild.pid; do
              pid="$(cat "$pidFile")"
              if alive "$pid"; then
                printf '%s:%s\n' "$pidFile" "$pid" >> survivors
                kill -s KILL "$pid" 2>/dev/null || :
              fi
            done
            """
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [bash, str(orchestrator)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "installer.status").read_text() == "143"
    assert (tmp_path / "survivors").read_text() == ""
    assert not (tmp_path / "continued.marker").exists()


def test_unix_installer_initial_monitor_mode_cleans_stopped_test_tree(tmp_path):
    bash = _require_bash()
    section = _installer_test_section((ROOT / "install.sh").read_text(encoding="utf-8"))
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_stubborn_fake_uv_tree(fake_bin)
    installer = tmp_path / "installer-under-test.sh"
    installer.write_text(
        textwrap.dedent(
            f"""
            set -euo pipefail
            set -m
            export LLM_WIKI_INSTALL_SMOKE_TIMEOUT_SECONDS=3
            PATH="$(dirname "$0")/bin:$PATH"
            # Defined before the smoke step in the real installer; the report goes under it.
            STATE_ROOT="$(dirname "$0")/state"
            export PATH
            info() {{ :; }}
            ok() {{ : > passed.marker; }}
            warn() {{ : > warned.marker; }}
            fail() {{
              case "$-" in *m*) : > monitor-restored.marker ;; esac
              : > failed.marker
              exit 1
            }}
            {section}
            case "$-" in *m*) : > monitor-restored.marker ;; esac
            : > continued.marker
            """
        ),
        encoding="utf-8",
    )
    installer.chmod(0o700)
    orchestrator = tmp_path / "orchestrate-stopped-tree.sh"
    orchestrator.write_text(
        _ALIVE_SHELL
        + textwrap.dedent(
            """
            set -euo pipefail
            ./installer-under-test.sh &
            installerPid=$!
            started=0
            for ((attempt = 0; attempt < 500; attempt++)); do
              if [ -f grandchild.started ]; then started=1; break; fi
              if ! alive "$installerPid"; then break; fi
              sleep 0.01
            done
            if [ "$started" -ne 1 ]; then exit 90; fi
            kill -s STOP "$(cat uv.pid)"
            if wait "$installerPid"; then
              installerExit=0
            else
              installerExit=$?
            fi
            printf '%s' "$installerExit" > installer.status
            : > survivors
            for pidFile in uv.pid child.pid grandchild.pid; do
              pid="$(cat "$pidFile")"
              if alive "$pid"; then
                printf '%s:%s\n' "$pidFile" "$pid" >> survivors
                kill -s KILL "$pid" 2>/dev/null || :
              fi
            done
            """
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [bash, str(orchestrator)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (
        (tmp_path / "installer.status").read_text(),
        (tmp_path / "survivors").read_text(),
        (tmp_path / "failed.marker").exists(),
        not (tmp_path / "passed.marker").exists(),
        (tmp_path / "monitor-restored.marker").exists(),
        not (tmp_path / "continued.marker").exists(),
    ) == (
        "1",
        "",
        True,
        True,
        True,
        True,
    )


@pytest.mark.parametrize("stop_signal", ["STOP", "TTIN"])
def test_unix_installer_initial_monitor_off_cleans_stopped_test_tree(tmp_path, stop_signal):
    bash = _require_bash()
    section = _installer_test_section((ROOT / "install.sh").read_text(encoding="utf-8"))
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_stubborn_fake_uv_tree(fake_bin)
    installer = tmp_path / "installer-under-test.sh"
    installer.write_text(
        textwrap.dedent(
            f"""
            set -euo pipefail
            set +m
            export LLM_WIKI_INSTALL_SMOKE_TIMEOUT_SECONDS=3
            PATH="$(dirname "$0")/bin:$PATH"
            # Defined before the smoke step in the real installer; the report goes under it.
            STATE_ROOT="$(dirname "$0")/state"
            export PATH
            info() {{ :; }}
            ok() {{ : > passed.marker; }}
            warn() {{ : > warned.marker; }}
            fail() {{
              case "$-" in
                *m*) : > monitor-wrong.marker ;;
                *) : > monitor-restored.marker ;;
              esac
              : > failed.marker
              exit 1
            }}
            {section}
            case "$-" in
              *m*) : > monitor-wrong.marker ;;
              *) : > monitor-restored.marker ;;
            esac
            : > continued.marker
            """
        ),
        encoding="utf-8",
    )
    installer.chmod(0o700)
    orchestrator = tmp_path / "orchestrate-stopped-tree.sh"
    orchestrator.write_text(
        _ALIVE_SHELL
        + textwrap.dedent(
            f"""
            set -euo pipefail
            ./installer-under-test.sh &
            installerPid=$!
            started=0
            for ((attempt = 0; attempt < 500; attempt++)); do
              if [ -f grandchild.started ]; then started=1; break; fi
              if ! alive "$installerPid"; then break; fi
              sleep 0.01
            done
            if [ "$started" -ne 1 ]; then exit 90; fi
            kill -s {stop_signal} "$(cat uv.pid)"
            finished=0
            # Bash reports a stopped job to `wait` only where job control does
            # it; elsewhere the installer's own smoke timer ends the wait, so
            # the poll has to outlast that timer.
            for ((attempt = 0; attempt < 1000; attempt++)); do
              if ! alive "$installerPid"; then finished=1; break; fi
              sleep 0.01
            done
            if [ "$finished" -ne 1 ]; then
              : > hung.marker
              kill -s TERM "$installerPid" 2>/dev/null || :
            fi
            if wait "$installerPid"; then
              installerExit=0
            else
              installerExit=$?
            fi
            printf '%s' "$installerExit" > installer.status
            : > survivors
            for pidFile in uv.pid child.pid grandchild.pid; do
              pid="$(cat "$pidFile")"
              if alive "$pid"; then
                printf '%s:%s\n' "$pidFile" "$pid" >> survivors
                kill -s KILL "$pid" 2>/dev/null || :
              fi
            done
            """
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [bash, str(orchestrator)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (
        not (tmp_path / "hung.marker").exists(),
        (tmp_path / "installer.status").read_text(),
        (tmp_path / "survivors").read_text(),
        (tmp_path / "failed.marker").exists(),
        not (tmp_path / "passed.marker").exists(),
        (tmp_path / "monitor-restored.marker").exists(),
        not (tmp_path / "monitor-wrong.marker").exists(),
        not (tmp_path / "continued.marker").exists(),
        result.stdout,
        result.stderr,
    ) == (
        True,
        "1",
        "",
        True,
        True,
        True,
        True,
        True,
        "",
        "",
    )


def test_unix_installer_sigttin_wait_status_enters_bounded_group_cleanup(tmp_path):
    bash = _require_bash()
    source = (ROOT / "install.sh").read_text(encoding="utf-8")
    functions = _shell_functions(
        source,
        "restore_test_monitor_mode",
        "send_signal",
        "test_group_is_own",
        "stop_test_group",
        "stop_test_process",
        "test_tree_alive",
        "stop_live_test_tree",
        "stop_test_child",
        "stop_test_timer",
        "wait_test_child",
    )
    runner = tmp_path / "exercise-stopped-wait.sh"
    runner.write_text(
        textwrap.dedent(
            f"""
            set -euo pipefail
            set -m
            calls="$(pwd)/calls"
            : > "$calls"
            waitCount=0
            killed=0
            kill() {{
              printf 'kill %s\n' "$*" >> "$calls"
              case "$*" in
                "-0 -- -4242") [ "$killed" -eq 0 ] ;;
                "-s KILL -- -4242") killed=1; return 0 ;;
                *) return 0 ;;
              esac
            }}
            sleep() {{ printf 'sleep %s\n' "$*" >> "$calls"; }}
            wait() {{
              waitCount=$((waitCount + 1))
              printf 'wait %s\n' "$*" >> "$calls"
              [ "$waitCount" -gt 1 ] && return 143
              return 149
            }}
            testPid=4242
            testPgid=4242
            testTimerPid=""
            testMonitorMode=on
            {functions}
            if wait_test_child; then status=0; else status=$?; fi
            printf '%s' "$status" > status
            case "$-" in *m*) : > monitor-restored ;; esac
            printf '%s:%s' "$testPid" "$testPgid" > state
            """
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [bash, str(runner)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (
        result.stderr,
        (tmp_path / "status").read_text(),
        (tmp_path / "state").read_text(),
        (tmp_path / "monitor-restored").exists(),
    ) == (
        "",
        "149",
        ":",
        True,
    )
    calls = (tmp_path / "calls").read_text().splitlines()
    assert calls.count("wait 4242") == 2
    assert (
        _unmet_substrings(
            (
                ("kill -s TERM -- -4242", calls, True),
                ("kill -s CONT -- -4242", calls, True),
                ("kill -s KILL -- -4242", calls, True),
            )
        )
        == []
    )


def test_unix_installer_signal_trap_restores_initial_monitor_mode(tmp_path):
    bash = _require_bash()
    source = (ROOT / "install.sh").read_text(encoding="utf-8")
    functions = _shell_functions(
        source,
        "restore_test_monitor_mode",
        "send_signal",
        "test_group_is_own",
        "stop_test_group",
        "stop_test_process",
        "test_tree_alive",
        "stop_live_test_tree",
        "stop_test_child",
        "stop_test_timer",
        "handle_test_signal",
    )
    runner = tmp_path / "exercise-signal-mode-restore.sh"
    runner.write_text(
        textwrap.dedent(
            f"""
            set -euo pipefail
            set -m
            testPid=""
            testPgid=""
            testTimerPid=""
            testMonitorMode=off
            exitStatus=""
            exit() {{ exitStatus="$1"; }}
            {functions}
            handle_test_signal 143
            printf '%s' "$exitStatus" > status
            case "$-" in
              *m*) : > monitor-wrong ;;
              *) : > monitor-restored ;;
            esac
            """
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [bash, str(runner)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (
        result.stderr,
        (tmp_path / "status").read_text(),
        (tmp_path / "monitor-restored").exists(),
        not (tmp_path / "monitor-wrong").exists(),
    ) == (
        "",
        "143",
        True,
        True,
    )


def test_unix_installer_cleanup_targets_group_with_term_then_kill(tmp_path):
    bash = _require_bash()
    source = (ROOT / "install.sh").read_text(encoding="utf-8")
    functions = _shell_functions(
        source, "send_signal", "test_group_is_own", "stop_test_group", "stop_test_process", "test_tree_alive", "stop_live_test_tree", "stop_test_child"
    )
    runner = tmp_path / "exercise-cleanup.sh"
    runner.write_text(
        textwrap.dedent(
            f"""
            set -euo pipefail
            calls="$(pwd)/calls"
            : > "$calls"
            kill() {{
              printf 'kill %s\n' "$*" >> "$calls"
              return 0
            }}
            sleep() {{ printf 'sleep %s\n' "$*" >> "$calls"; }}
            wait() {{ printf 'wait %s\n' "$*" >> "$calls"; return 0; }}
            testPid=4242
            testPgid=4242
            {functions}
            stop_test_child
            """
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [bash, str(runner)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls").read_text().splitlines()
    assert (
        _unmet_substrings(
            (
                ("kill -s TERM -- -4242", calls, True),
                ("kill -s KILL -- -4242", calls, True),
            )
        )
        == []
    )
    assert (
        calls.index("kill -s TERM -- -4242") < calls.index("kill -s KILL -- -4242"),
        calls.count("sleep 0.1"),
        calls[-1],
    ) == (
        True,
        5,
        "wait 4242",
    )


@pytest.mark.parametrize(
    ("fake_exit", "fake_output", "expected_exit", "expected_marker"),
    [
        (1, "smoke ok", 1, "[FAIL] Production smoke failed; installation aborted"),
        (0, "smoke failed", 0, "[OK] Production smoke passed"),
    ],
    ids=["smoke_exit_1_success_output", "smoke_exit_0_failure_output"],
)
@pytest.mark.parametrize("powershell_name", ["powershell", "pwsh"])
def test_windows_installer_trusts_smoke_exit_status(
    tmp_path,
    windows_fake_uv,
    powershell_name,
    fake_exit,
    fake_output,
    expected_exit,
    expected_marker,
):
    powershell = shutil.which(powershell_name)
    if powershell is None:
        pytest.skip(f"{powershell_name} unavailable")
    section = _installer_test_section((ROOT / "install.ps1").read_text(encoding="utf-8"))
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_uv(fake_bin, exit_code=fake_exit, last_line=fake_output)
    if windows_fake_uv is not None:
        shutil.copy2(windows_fake_uv, fake_bin / "uv.exe")
    command = textwrap.dedent(
        f"""
        $ErrorActionPreference = "Stop"
        if (Get-Variable PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {{
            $PSNativeCommandUseErrorActionPreference = $true
        }}
        function Info($msg) {{ Write-Output "[INFO] $msg" }}
        function Ok($msg) {{ Write-Output "[OK] $msg" }}
        function Warn($msg) {{ Write-Output "[WARN] $msg" }}
        function Fail($msg) {{ Write-Output "[FAIL] $msg"; exit 1 }}
        # Defined before the smoke step in the real installer; the report goes under it.
        $STATE_ROOT = {ps_literal(str(tmp_path / "state"))}
        {section}
        """
    )
    env = os.environ.copy()
    env.update(
        PATH=f"{fake_bin}{os.pathsep}{env['PATH']}",
        FAKE_UV_EXIT=str(fake_exit),
        FAKE_UV_OUTPUT=fake_output,
    )

    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )

    assert result.returncode == expected_exit, result.stderr
    assert (
        _unmet_substrings(
            (
                (expected_marker, result.stdout, True),
                (fake_output, result.stdout, True),
            )
        )
        == []
    )


def _windows_process_survived(pid_file: Path) -> bool:
    import ctypes

    pid = int(pid_file.read_text())
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    exit_code = ctypes.c_ulong()
    assert ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
    alive = exit_code.value == 259
    if alive:
        ctypes.windll.kernel32.TerminateProcess(handle, 1)
    ctypes.windll.kernel32.CloseHandle(handle)
    return alive


def _windows_survivors(targets) -> list[str]:
    return [
        f"{label}:{pid_file.read_text()}"
        for label, pid_file in targets
        if _windows_process_survived(pid_file)
    ]


@pytest.mark.parametrize("powershell_name", ["powershell", "pwsh"])
def test_windows_installer_error_stops_native_child_and_later_steps(
    tmp_path, windows_fake_uv, powershell_name
):
    if os.name != "nt":
        pytest.skip("Windows-only native process cleanup contract")
    powershell = shutil.which(powershell_name)
    if powershell is None:
        pytest.skip(f"{powershell_name} unavailable")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    shutil.copy2(windows_fake_uv, fake_bin / "uv.exe")
    started = tmp_path / "child.started"
    uv_pid_file = tmp_path / "uv.pid"
    python_pid_file = tmp_path / "python.pid"
    python_started = tmp_path / "python.started"
    child_script = tmp_path / "blocking-child.py"
    child_script.write_text(
        "import os\n"
        "import time\n"
        "from pathlib import Path\n"
        "Path(os.environ['FAKE_PYTHON_PID_FILE']).write_text(str(os.getpid()))\n"
        "Path(os.environ['FAKE_PYTHON_STARTED_FILE']).write_text('started')\n"
        "while True:\n"
        "    time.sleep(1)\n",
        encoding="utf-8",
    )
    later = tmp_path / "later.marker"
    section = _installer_test_section((ROOT / "install.ps1").read_text(encoding="utf-8"))
    injected_wait = textwrap.dedent(
        """
        $testDeadline = [DateTime]::UtcNow.AddSeconds(10)
        while (-not (Test-Path -LiteralPath $env:FAKE_PYTHON_STARTED_FILE)) {
            if ([DateTime]::UtcNow -ge $testDeadline) { throw "fake uv did not start" }
            Start-Sleep -Milliseconds 10
        }
        throw "injected installer interruption"
        """
    ).strip()
    section = section.replace(
        "if (-not $testProcess.WaitForExit($testTimeoutMilliseconds)) {",
        injected_wait + "\nif ($false) {",
        1,
    )
    command = textwrap.dedent(
        f"""
        $ErrorActionPreference = "Stop"
        function Info($msg) {{ Write-Output "[INFO] $msg" }}
        function Ok($msg) {{ Write-Output "[OK] $msg" }}
        function Warn($msg) {{ Write-Output "[WARN] $msg" }}
        # Defined before the smoke step in the real installer; the report goes under it.
        $STATE_ROOT = {ps_literal(str(tmp_path / "state"))}
        {section}
        New-Item -ItemType File -Path {ps_literal(str(later))} | Out-Null
        """
    )
    env = os.environ.copy()
    env.update(
        PATH=f"{fake_bin}{os.pathsep}{env['PATH']}",
        FAKE_UV_MODE="block",
        FAKE_UV_PID_FILE=str(uv_pid_file),
        FAKE_UV_STARTED_FILE=str(started),
        FAKE_UV_PYTHON=sys.executable,
        FAKE_UV_CHILD_SCRIPT=str(child_script),
        FAKE_PYTHON_PID_FILE=str(python_pid_file),
        FAKE_PYTHON_STARTED_FILE=str(python_started),
    )
    error_log = tmp_path / "powershell-error.log"
    with error_log.open("wb") as stderr:
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
            timeout=180,
            check=False,
        )

    assert (
        result.returncode != 0,
        b"injected installer interruption" in error_log.read_bytes(),
        started.exists(),
        python_started.exists(),
        later.exists(),
    ) == (True, True, True, True, False)

    survivors = _windows_survivors((("uv", uv_pid_file), ("python", python_pid_file)))
    assert not survivors, f"native process tree survived installer cleanup: {survivors}"


def _mcp_config_kept(config: Path, scenario: str, original: str, before: bytes) -> bool:
    """An absent entry is appended after the original text; any other config is untouched."""
    if scenario == "absent":
        return config.read_text(encoding="utf-8").startswith(original) and (
            config.read_bytes() != before
        )
    return config.read_bytes() == before


@pytest.mark.parametrize(
    ("scenario", "expected_exit"),
    [("equivalent", 0), ("conflicting", 2), ("absent", 0)],
    ids=["quoted-equivalent", "conflicting", "absent"],
)
def test_unix_installer_mcp_function_uses_parser_in_temp_home(tmp_path, scenario, expected_exit):
    bash = Path(r"C:\Program Files\Git\bin\bash.exe")
    if not bash.exists():
        pytest.skip("Git Bash unavailable")
    source = (ROOT / "install.sh").read_text(encoding="utf-8")
    function = _shell_functions(
        source,
        "write_codex_mcp_block",
        "add_codex_mcp_block",
        "codex_mcp_state_status",
        "replace_codex_mcp",
        "configure_codex_mcp",
    )
    home = tmp_path / "home"
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    original = (
        'model = "gpt-5.6"\n'
        if scenario == "absent"
        else _codex_mcp_toml(ROOT, quoted=True, conflicting=scenario == "conflicting")
    )
    config.write_text(original, encoding="utf-8")
    before = config.read_bytes()
    runner = tmp_path / "installer-mcp-contract.sh"
    runner.write_text(
        function
        + "\nuv() {\n"
        + "  while [[ $# -gt 0 && $1 != config-state && $1 != config-replace && $1 != config-block ]]; do shift; done\n"
        + '  command "$TEST_PYTHON" "$TEST_VAULT/scripts/codex_memory.py" "$@"\n'
        + "}\nset +e\n"
        + 'configure_codex_mcp "$TEST_VAULT" "$HOME/.codex/config.toml"\n'
        + "exit $?\n",
        encoding="utf-8",
    )
    from tests.shell_functions_stay_simple_helpers import _shape_violations, _violations

    assert _violations(runner.read_bytes()) == {}
    assert _shape_violations(runner.read_bytes()) == []
    env = os.environ.copy()
    env.update(HOME=str(home), TEST_VAULT=str(ROOT), TEST_PYTHON=sys.executable)

    result = subprocess.run(
        [str(bash), str(runner)],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == expected_exit, result.stderr
    assert (
        _mcp_config_kept(config, scenario, original, before),
        config.read_text(encoding="utf-8").count("[mcp_servers."),
    ) == (True, 1)


@pytest.mark.parametrize(
    ("scenario", "expected_exit"),
    [("equivalent", 0), ("conflicting", 2), ("absent", 0)],
    ids=["quoted-equivalent", "conflicting", "absent"],
)
def test_windows_installer_mcp_function_uses_parser_in_temp_home(tmp_path, scenario, expected_exit):
    source = ROOT / "install.ps1"
    home = tmp_path / "home"
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    original = (
        'model = "gpt-5.6"\n'
        if scenario == "absent"
        else _codex_mcp_toml(ROOT, quoted=True, conflicting=scenario == "conflicting")
    )
    config.write_text(original, encoding="utf-8")
    before = config.read_bytes()
    command = textwrap.dedent(
        f"""
        $tokens = $null
        $errors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile(
            {ps_literal(str(source))}, [ref]$tokens, [ref]$errors)
        if ($errors.Count) {{ throw ($errors | Out-String) }}
        foreach ($name in @('Install-CodexMcp', 'Complete-CodexMcpInstall',
            'Get-CodexMcpInstallState', 'Update-CodexMcpEntry',
            'Get-CodexMcpSeparator', 'Add-CodexMcpEntry')) {{
            $fn = $ast.Find({{ param($node)
                $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
                $node.Name -eq $name
            }}, $true)
            if ($null -eq $fn) {{ throw "$name missing" }}
            Invoke-Expression $fn.Extent.Text
        }}
        function uv {{
            $all = @($args)
            $index = -1
            foreach ($command in @('config-state', 'config-replace', 'config-block')) {{
                $index = [Array]::IndexOf($all, $command)
                if ($index -ge 0) {{ break }}
            }}
            if ($index -lt 0) {{ throw 'config command missing' }}
            & {ps_literal(sys.executable)} {ps_literal(str(ROOT / "scripts/codex_memory.py"))} $all[$index..($all.Count - 1)]
        }}
        $code = Install-CodexMcp -VaultRoot {ps_literal(str(ROOT))} -Config {ps_literal(str(config))}
        exit $code
        """
    )

    from tests.powershell_functions_stay_simple_helpers import _measure, _problems

    emitted = tmp_path / "installer-mcp-contract.ps1"
    emitted.write_text(command, encoding="utf-8")
    assert [problem for record in _measure([emitted], tmp_path)
            for problem in _problems(record)] == []

    result = subprocess.run(
        [require_tool("pwsh"), "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == expected_exit, result.stderr
    assert (
        _mcp_config_kept(config, scenario, original, before),
        config.read_text(encoding="utf-8").count("[mcp_servers."),
    ) == (True, 1)


def test_unix_installer_probe_reports_absent_without_touching_the_file(tmp_path):
    """The probe replaced a merge: it must answer and change nothing.

    The installer asks this before the ownership transaction and owns
    `hooks.json` only when the answer is `absent`.
    """
    bash = Path(r"C:\Program Files\Git\bin\bash.exe")
    if not bash.exists():
        pytest.skip("Git Bash unavailable")
    source = (ROOT / "install.sh").read_text(encoding="utf-8")
    function = _shell_function(source, "codex_inline_hooks_state")
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text('model = "gpt-5.6"\n', encoding="utf-8")
    destination = codex_dir / "hooks.json"
    destination.write_text(
        '{"custom":true,"hooks":{"Stop":[{"hooks":[{"type":"command","command":"echo user"}]}]}}\n',
        encoding="utf-8",
    )
    before = destination.read_bytes()
    runner = tmp_path / "installer-contract.sh"
    runner.write_text(
        function
        + "\nuv() {\n"
        + "  while [[ $# -gt 0 && $1 != hooks-state ]]; do shift; done\n"
        + '  command "$TEST_PYTHON" "$TEST_VAULT/scripts/codex_memory.py" "$@"\n'
        + "}\n"
        + 'codex_inline_hooks_state "$TEST_VAULT" "$HOME/.codex"\n',
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(HOME=str(home), TEST_VAULT=str(ROOT), TEST_PYTHON=sys.executable)

    result = subprocess.run(
        [str(bash), str(runner)],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.stdout.strip() == "absent"
    assert destination.read_bytes() == before


def test_unix_installer_probe_reports_conflict_for_unrelated_inline_hooks(tmp_path):
    bash = Path(r"C:\Program Files\Git\bin\bash.exe")
    if not bash.exists():
        pytest.skip("Git Bash unavailable")
    function = _shell_function(
        (ROOT / "install.sh").read_text(encoding="utf-8"), "codex_inline_hooks_state"
    )
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text(
        '[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype = "command"\ncommand = "echo user"\n',
        encoding="utf-8",
    )
    destination = codex_dir / "hooks.json"
    destination.write_text('{"custom":true}\n', encoding="utf-8")
    before = destination.read_bytes()
    runner = tmp_path / "installer-inline-contract.sh"
    runner.write_text(
        function
        + "\nuv() {\n"
        + "  while [[ $# -gt 0 && $1 != hooks-state ]]; do shift; done\n"
        + '  command "$TEST_PYTHON" "$TEST_VAULT/scripts/codex_memory.py" "$@"\n'
        + "}\nset +e\n"
        + 'codex_inline_hooks_state "$TEST_VAULT" "$HOME/.codex"\n',
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(HOME=str(home), TEST_VAULT=str(ROOT), TEST_PYTHON=sys.executable)

    result = subprocess.run(
        [str(bash), str(runner)],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.stdout.strip() == "conflict"
    assert destination.read_bytes() == before


def test_windows_installer_probe_reports_absent_without_touching_the_file(tmp_path):
    """The probe replaced a merge: it must answer and change nothing."""
    source = ROOT / "install.ps1"
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text('model = "gpt-5.6"\n', encoding="utf-8")
    destination = codex_dir / "hooks.json"
    destination.write_text(
        '{"custom":true,"hooks":{"Stop":[{"hooks":[{"type":"command","command":"echo user"}]}]}}\n',
        encoding="utf-8",
    )
    before = destination.read_bytes()
    command = textwrap.dedent(
        f"""
        $tokens = $null
        $errors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile(
            {ps_literal(str(source))}, [ref]$tokens, [ref]$errors)
        if ($errors.Count) {{ throw ($errors | Out-String) }}
        $fn = $ast.Find({{ param($node)
            $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq 'Get-CodexInlineHooksState'
        }}, $true)
        if ($null -eq $fn) {{ throw 'Get-CodexInlineHooksState missing' }}
        Invoke-Expression $fn.Extent.Text
        function uv {{
            $all = @($args)
            $index = [Array]::IndexOf($all, 'hooks-state')
            if ($index -lt 0) {{ throw 'hooks-state missing' }}
            & {ps_literal(sys.executable)} {ps_literal(str(ROOT / "scripts/codex_memory.py"))} $all[$index..($all.Count - 1)]
        }}
        Get-CodexInlineHooksState -VaultRoot {ps_literal(str(ROOT))} -CodexDir {ps_literal(str(codex_dir))}
        """
    )

    result = subprocess.run(
        [require_tool("pwsh"), "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "absent"
    assert destination.read_bytes() == before


def test_windows_installer_probe_reports_conflict_for_partial_inline_hooks(tmp_path):
    source = ROOT / "install.ps1"
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text(
        _codex_inline_hooks_toml(include_stop=False), encoding="utf-8"
    )
    destination = codex_dir / "hooks.json"
    destination.write_text('{"custom":true}\n', encoding="utf-8")
    before = destination.read_bytes()
    command = textwrap.dedent(
        f"""
        $tokens = $null
        $errors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile(
            {ps_literal(str(source))}, [ref]$tokens, [ref]$errors)
        if ($errors.Count) {{ throw ($errors | Out-String) }}
        $fn = $ast.Find({{ param($node)
            $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq 'Get-CodexInlineHooksState'
        }}, $true)
        if ($null -eq $fn) {{ throw 'Get-CodexInlineHooksState missing' }}
        Invoke-Expression $fn.Extent.Text
        function uv {{
            $all = @($args)
            $index = [Array]::IndexOf($all, 'hooks-state')
            if ($index -lt 0) {{ throw 'hooks-state missing' }}
            & {ps_literal(sys.executable)} {ps_literal(str(ROOT / "scripts/codex_memory.py"))} $all[$index..($all.Count - 1)]
        }}
        Get-CodexInlineHooksState -VaultRoot {ps_literal(str(ROOT))} -CodexDir {ps_literal(str(codex_dir))}
        """
    )

    result = subprocess.run(
        [require_tool("pwsh"), "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.stdout.strip() == "conflict"
    assert destination.read_bytes() == before


def test_unix_installer_probe_reports_disabled_when_the_feature_is_off(tmp_path):
    bash = Path(r"C:\Program Files\Git\bin\bash.exe")
    if not bash.exists():
        pytest.skip("Git Bash unavailable")
    function = _shell_function(
        (ROOT / "install.sh").read_text(encoding="utf-8"), "codex_inline_hooks_state"
    )
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text("[features]\ncodex_hooks = false\n", encoding="utf-8")
    destination = codex_dir / "hooks.json"
    destination.write_text('{"custom":true}\n', encoding="utf-8")
    before = destination.read_bytes()
    runner = tmp_path / "installer-disabled-contract.sh"
    runner.write_text(
        function
        + "\nuv() {\n"
        + "  while [[ $# -gt 0 && $1 != hooks-state ]]; do shift; done\n"
        + '  command "$TEST_PYTHON" "$TEST_VAULT/scripts/codex_memory.py" "$@"\n'
        + "}\nset +e\n"
        + 'codex_inline_hooks_state "$TEST_VAULT" "$HOME/.codex"\n',
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(HOME=str(home), TEST_VAULT=str(ROOT), TEST_PYTHON=sys.executable)

    result = subprocess.run(
        [str(bash), str(runner)],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.stdout.strip() == "disabled"
    assert destination.read_bytes() == before


def test_windows_installer_probe_reports_disabled_when_the_feature_is_off(tmp_path):
    source = ROOT / "install.ps1"
    codex_dir = tmp_path / "home" / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text(
        "[features]\nhooks = false\ncodex_hooks = true\n", encoding="utf-8"
    )
    destination = codex_dir / "hooks.json"
    destination.write_text('{"custom":true}\n', encoding="utf-8")
    before = destination.read_bytes()
    command = textwrap.dedent(
        f"""
        $tokens = $null
        $errors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile(
            {ps_literal(str(source))}, [ref]$tokens, [ref]$errors)
        if ($errors.Count) {{ throw ($errors | Out-String) }}
        $fn = $ast.Find({{ param($node)
            $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq 'Get-CodexInlineHooksState'
        }}, $true)
        if ($null -eq $fn) {{ throw 'Get-CodexInlineHooksState missing' }}
        Invoke-Expression $fn.Extent.Text
        function uv {{
            $all = @($args)
            $index = [Array]::IndexOf($all, 'hooks-state')
            if ($index -lt 0) {{ throw 'hooks-state missing' }}
            & {ps_literal(sys.executable)} {ps_literal(str(ROOT / "scripts/codex_memory.py"))} $all[$index..($all.Count - 1)]
        }}
        Get-CodexInlineHooksState -VaultRoot {ps_literal(str(ROOT))} -CodexDir {ps_literal(str(codex_dir))}
        """
    )

    result = subprocess.run(
        [require_tool("pwsh"), "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.stdout.strip() == "disabled"
    assert destination.read_bytes() == before


def test_windows_installer_resolves_one_external_state_root(tmp_path):
    """Resolve-StateRoot keeps an external state root, from the process or the user."""
    install_path = ROOT / "install.ps1"
    external = str(tmp_path / "external runtime")
    vault = str(tmp_path / "vault")
    command = textwrap.dedent(
        f"""
        $tokens = $null
        $errors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile(
            {ps_literal(str(install_path))}, [ref]$tokens, [ref]$errors)
        if ($errors.Count) {{ throw ($errors | Out-String) }}
        $fn = $ast.Find({{ param($node)
            $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq 'Resolve-StateRoot'
        }}, $true)
        if ($null -eq $fn) {{ throw 'Resolve-StateRoot missing' }}
        Invoke-Expression $fn.Extent.Text
        $first = Resolve-StateRoot -ProcessState '{external.replace("'", "''")}' -UserState '' -VaultRoot '{vault.replace("'", "''")}'
        $second = Resolve-StateRoot -ProcessState $first -UserState '' -VaultRoot '{vault.replace("'", "''")}'
        $fromUser = Resolve-StateRoot -ProcessState '' -UserState '{external.replace("'", "''")}' -VaultRoot '{vault.replace("'", "''")}'
        @($first, $second, $fromUser) | ConvertTo-Json -Compress
        """
    )

    result = subprocess.run(
        [require_tool("pwsh"), "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        # pwsh startup alone can take ten seconds on a loaded hosted runner.
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [external, external, external]
