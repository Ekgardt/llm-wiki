"""Shared descendant-process fixture, independent of LSP protocol behavior."""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time


def add_process_tree_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sleep-seconds", type=float, default=0.0)
    parser.add_argument("--spawn-descendant", action="store_true")
    parser.add_argument("--spawn-setsid-descendant", action="store_true")
    parser.add_argument("--descendant-exit-after", type=float)
    parser.add_argument("--exit-after-descendant-spawn", action="store_true")
    parser.add_argument("--descendant-pid-file")
    parser.add_argument("--descendant-pid-log")


def _report_group_signal(_signum: int, _frame: object) -> None:
    """One unbuffered write: `print` here re-entered stdout's buffer and killed the fixture.

    See `docs/research/2026-09-27-a-signal-handler-writes-without-a-buffer.md`.
    """
    os.write(sys.stdout.fileno(), json.dumps({"group_signal": "SIGTERM"}).encode() + b"\n")


def _terminate_descendant(descendant: subprocess.Popen) -> None:
    descendant.terminate()
    try:
        descendant.wait(timeout=2)
    except subprocess.TimeoutExpired:
        descendant.kill()
        descendant.wait(timeout=2)


def _run_setsid_descendant_fixture() -> None:
    """A descendant that leaves the process group, and is reaped by hand."""
    if os.name != "posix":
        raise RuntimeError("setsid descendant fixture requires POSIX")
    signal.signal(signal.SIGTERM, _report_group_signal)
    descendant = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os,time; os.setsid(); print('ready', flush=True); time.sleep(60)",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    assert descendant.stdout is not None
    assert descendant.stdout.readline() == b"ready\n"
    print(json.dumps({"descendant_pid": descendant.pid}), flush=True)
    try:
        sys.stdin.buffer.readline()
    finally:
        _terminate_descendant(descendant)
    print(json.dumps({"descendant_reaped": True}), flush=True)


def _descendant_code(args: argparse.Namespace) -> str:
    if args.descendant_exit_after is None:
        return "import time; time.sleep(60)"
    return (
        f"import os,time; time.sleep({args.descendant_exit_after!r}); os._exit(17)"
    )


def _write_pid_file(path: str, pid: int) -> None:
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="ascii") as stream:
        stream.write(str(pid))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _print_or_file_pid(args: argparse.Namespace, pid: int) -> None:
    """The pid file when one is asked for; stdout when no log is asked for either."""
    if args.descendant_pid_file:
        _write_pid_file(args.descendant_pid_file, pid)
        return
    if not args.descendant_pid_log:
        print(json.dumps({"descendant_pid": pid}), flush=True)


def _report_descendant_pid(args: argparse.Namespace, pid: int) -> None:
    """The pid goes to a file, to a log, or — with neither asked for — to stdout."""
    _print_or_file_pid(args, pid)
    if args.descendant_pid_log:
        with open(args.descendant_pid_log, "a", encoding="ascii") as stream:
            stream.write(f"{pid}\n")


def _spawn_descendant_and_exit(args: argparse.Namespace) -> bool:
    """Spawn the descendant, and say whether the fixture ends right there."""
    if not args.spawn_descendant:
        return False
    descendant = subprocess.Popen(
        [sys.executable, "-c", _descendant_code(args)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    _report_descendant_pid(args, descendant.pid)
    return bool(args.exit_after_descendant_spawn)


def main() -> None:
    parser = argparse.ArgumentParser()
    add_process_tree_arguments(parser)
    args = parser.parse_args()
    if args.spawn_setsid_descendant:
        _run_setsid_descendant_fixture()
        return
    if _spawn_descendant_and_exit(args):
        return
    _sleep(args.sleep_seconds)


def _sleep(seconds: float) -> None:
    if seconds:
        time.sleep(seconds)


if __name__ == "__main__":
    main()
