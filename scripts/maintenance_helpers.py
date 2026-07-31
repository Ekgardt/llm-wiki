"""Shared helpers for scheduled maintenance scripts (nightly / weekly).

Both ``scheduled_nightly.py`` and ``scheduled_weekly.py`` need the same
subprocess-step runner and compile-idle waiter. Extracted here to avoid
the prior copy-paste duplication.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import maybe_compile
from memory_state import ROOT

_WINDOWS = os.name == "nt"


def write_console(message: str, *, error: bool = False) -> None:
    """Write when a console stream exists; pythonw intentionally has none."""
    stream = sys.stderr if error else sys.stdout
    if stream is not None:
        print(message, file=stream)


def run_step(cmd: list[str], log_fn, label: str, timeout: int = 600) -> int:
    """Run a subprocess step with timeout protection.

    Returns 0 on success, 1 on non-zero exit, 2 on timeout/error. Any
    failure (timeout, missing script, OS error) is logged and the next
    step proceeds — never aborts the scheduled run.
    """
    try:
        run_kwargs = {}
        if _WINDOWS:
            run_kwargs["creationflags"] = getattr(
                subprocess,
                "CREATE_NO_WINDOW",
                0x08000000,
            )
        r = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            **run_kwargs,
        )
    except subprocess.TimeoutExpired:
        log_fn(f"  {label}: TIMEOUT after {timeout}s — skipping, continuing")
        return 2
    except OSError as e:
        log_fn(f"  {label}: OS error ({type(e).__name__}: {e}) — skipping, continuing")
        return 2
    stdout = r.stdout.strip()
    stderr = r.stderr.strip()
    if r.returncode != 0 and stderr:
        log_fn(f"  {label}: {stderr[:300]}")
    if stdout:
        for line in stdout.splitlines()[-6:]:
            log_fn(f"  {label}: {line}")
    elif not stderr:
        log_fn(f"  {label}: (no output)")
    return 0 if r.returncode == 0 else 1


def wait_for_compile_idle(log_fn) -> None:
    """If a compile is already running, wait (up to 3 retries x 10 s).

    Scheduled passes run unattended and must not skip compile just because
    a previous compile (triggered by a hook) is still running.
    """
    for attempt in range(3):
        st = maybe_compile.status()
        if not st["compile_running"]:
            return
        log_fn(f"  compile running ({st['reason']}), waiting 10s (attempt {attempt + 1}/3)...")
        time.sleep(10)
