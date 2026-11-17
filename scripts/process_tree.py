"""Run a child process and end its whole tree when it times out or is interrupted."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

# Time to reap a child after it was stopped; the stop itself is already bounded.
PROCESS_CLEANUP_TIMEOUT_SECONDS = 2.0


WINDOWS_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)


class ProcessTreeTimeout(subprocess.TimeoutExpired):
    """A process timed out, with an explicit tree-cleanup result."""

    def __init__(self, cmd: list[str], timeout: float, *, cleanup_error: str | None):
        super().__init__(cmd, timeout)
        self.cleanup_error = cleanup_error


def terminate_windows_tree(process: subprocess.Popen[str]) -> str | None:
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    taskkill = Path(system_root) / "System32" / "taskkill.exe"
    try:
        terminator = subprocess.Popen(
            [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return "taskkill_unavailable"
    try:
        terminator.communicate(timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            terminator.kill()
            terminator.wait(timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return "taskkill_timeout"
    return None if terminator.returncode == 0 else "taskkill_failed"


def _append_cleanup_error(current: str | None, error: str) -> str:
    return f"{current};{error}" if current else error


def _close_captured_pipes(process: subprocess.Popen[str]) -> None:
    for stream in (process.stdout, process.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except OSError:
            pass


def _kill_quietly(process: subprocess.Popen[str], cleanup_error: str | None) -> str | None:
    try:
        process.kill()
    except OSError:
        return _append_cleanup_error(cleanup_error, "direct_kill_failed")
    return cleanup_error


def _drain_retained_pipes(process: subprocess.Popen[str], cleanup_error: str | None) -> str | None:
    _close_captured_pipes(process)
    cleanup_error = _kill_quietly(process, cleanup_error)
    try:
        process.wait(timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        cleanup_error = _append_cleanup_error(cleanup_error, "cleanup_wait_failed")
    return cleanup_error


def _finish_timed_out_process(
    process: subprocess.Popen[str], cleanup_error: str | None
) -> str | None:
    if cleanup_error:
        cleanup_error = _kill_quietly(process, cleanup_error)
    try:
        process.communicate(timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS)
        return cleanup_error
    except subprocess.TimeoutExpired:
        return _drain_retained_pipes(process, _append_cleanup_error(cleanup_error, "retained_pipes"))


def _piped_streams(options: dict, stdin_text: object) -> None:
    """Which of the child's streams this call wants as pipes."""
    if options.pop("capture_output", False):
        options["stdout"] = subprocess.PIPE
        options["stderr"] = subprocess.PIPE
    if stdin_text is not None:
        options["stdin"] = subprocess.PIPE


def _popen_options(kwargs: dict, stdin_text: object = None) -> dict:
    """Popen options with captured streams and the child in its own group/session."""
    options = dict(kwargs)
    _piped_streams(options, stdin_text)
    if os.name == "nt":
        options["creationflags"] = int(options.get("creationflags", 0)) | WINDOWS_NEW_PROCESS_GROUP
        return options
    options["start_new_session"] = True
    return options


def kill_process_tree(process: subprocess.Popen[str]) -> str | None:
    """End the child's whole group; the cleanup error, or None when it was ended."""
    if os.name == "nt":
        return terminate_windows_tree(process)
    try:
        os.killpg(os.getpgid(process.pid), getattr(signal, "SIGKILL", 9))
    except OSError:
        return "process_group_kill_failed"
    return None


def run_process_tree(
    command: list[str], *, timeout: float, input: object = None, **kwargs: object
) -> subprocess.CompletedProcess[str]:
    """Run a command whose whole tree is ended when the deadline passes.

    `input` is written to the child's stdin and answers the same need as
    `subprocess.run(input=...)`: a caller with a long prompt keeps it in a pipe
    instead of a file on disk. It is the llm_client's CLI providers that need
    both at once — a prompt through stdin and a tree that dies with its wrapper.
    """
    process = subprocess.Popen(command, **_popen_options(dict(kwargs), input))
    try:
        stdout, stderr = process.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        cleanup_error = _finish_timed_out_process(process, kill_process_tree(process))
        raise ProcessTreeTimeout(command, exc.timeout, cleanup_error=cleanup_error) from exc
    except BaseException as error:
        _end_interrupted_process(process, error)
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _end_interrupted_process(
    process: subprocess.Popen[str], error: BaseException
) -> None:
    """Finish the owned child tree before propagating the original interruption."""
    cleanup_error = _finish_timed_out_process(process, kill_process_tree(process))
    setattr(error, "cleanup_error", cleanup_error)
    if cleanup_error:
        print(f"Process cleanup unverified after interruption: {cleanup_error}", file=sys.stderr)
