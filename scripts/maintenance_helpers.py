"""Shared helpers for scheduled maintenance scripts (nightly / weekly).

Both ``scheduled_nightly.py`` and ``scheduled_weekly.py`` need the same
subprocess-step runner and compile-idle waiter. Extracted here to avoid
the prior copy-paste duplication.

Two contracts live here that the reports depend on:

* **Step output is never held in memory.** A step writes straight to an
  owner-only artifact under ``logs/maintenance/``; the report keeps a short
  summary and always names the artifact holding the full output, so a
  truncated line is never the end of the trail.
* **Maintenance output has bounded retention.** ``prune_reports`` enforces
  age, count, and total size over the report and artifact files, so an
  unattended machine cannot fill its disk with its own diagnostics.
"""
from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

import maybe_compile
from memory_state import REPORTS_DIR, ROOT
from secret_redact import redact_secrets

ARTIFACT_DIR = REPORTS_DIR / "maintenance"
STEP_SUMMARY_LINES = 6
STEP_ERROR_CHARS = 300
MAX_STEP_TAIL_BYTES = 8 * 1024
MAX_STEP_HEAD_BYTES = 8 * 1024
REPORT_RETENTION_DAYS = 30
REPORT_RETENTION_FILES = 60
REPORT_RETENTION_BYTES = 32 * 1024 * 1024
MAINTENANCE_REPORT_PATTERNS = ("nightly-*.md", "weekly-*.md", "lint-*.md")
ARTIFACT_PATTERN = "*.log"


def _artifact_stem(label: str) -> str:
    safe_label = "".join(c if c.isalnum() else "-" for c in label)[:40]
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{safe_label}-{os.getpid()}"


def _open_owner_only(path: Path):
    """Create the artifact readable by its owner only."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    return os.fdopen(os.open(path, flags, 0o600), "wb")


def _run_to_files(cmd: list[str], out_path: Path, err_path: Path, timeout: int) -> int:
    """Run the step with both streams going to disk, never to memory."""
    with _open_owner_only(out_path) as out_handle, _open_owner_only(err_path) as err_handle:
        completed = subprocess.run(
            cmd,
            cwd=str(ROOT),
            stdout=out_handle,
            stderr=err_handle,
            timeout=timeout,
        )
    return completed.returncode


def _head_text(path: Path, max_bytes: int = MAX_STEP_HEAD_BYTES) -> str:
    try:
        with path.open("rb") as handle:
            return handle.read(max_bytes).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _tail_text(path: Path, max_bytes: int = MAX_STEP_TAIL_BYTES) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - max_bytes))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _discard_empty(path: Path) -> bool:
    """Drop an empty stream file; report whether the artifact survived."""
    if _file_size(path) > 0:
        return True
    try:
        path.unlink()
    except OSError:
        pass
    return False


def _artifact_note(out_path: Path, err_path: Path) -> str:
    kept = [path.name for path in (out_path, err_path) if _discard_empty(path)]
    if not kept:
        return "(no output captured)"
    return f"logs/maintenance/{', '.join(kept)}"


def _log_stdout_tail(log_fn, label: str, stdout_tail: str, stderr_tail: str) -> None:
    if not stdout_tail:
        _log_empty_output(log_fn, label, stderr_tail)
        return
    for line in stdout_tail.splitlines()[-STEP_SUMMARY_LINES:]:
        log_fn(f"  {label}: {line}")


def _log_empty_output(log_fn, label: str, stderr_tail: str) -> None:
    if not stderr_tail:
        log_fn(f"  {label}: (no output)")


def _log_step_output(
    log_fn, label: str, out_path: Path, err_path: Path, returncode: int
) -> None:
    """Summarise the step and always name the artifact with the full output."""
    stderr_head = redact_secrets(_head_text(err_path)).strip()
    stdout_tail = redact_secrets(_tail_text(out_path)).strip()
    if returncode != 0 and stderr_head:
        log_fn(f"  {label}: {stderr_head[:STEP_ERROR_CHARS]}")
    _log_stdout_tail(log_fn, label, stdout_tail, stderr_head)
    log_fn(f"  {label}: full output → {_artifact_note(out_path, err_path)}")


def _step_status(returncode: int) -> int:
    return 0 if returncode == 0 else 1


def _step_artifacts(label: str) -> tuple[Path, Path]:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    stem = _artifact_stem(label)
    return ARTIFACT_DIR / f"{stem}.out.log", ARTIFACT_DIR / f"{stem}.err.log"


def run_step(cmd: list[str], log_fn, label: str, timeout: int = 600) -> int:
    """Run a subprocess step with timeout protection.

    Returns 0 on success, 1 on non-zero exit, 2 on timeout/error. Any
    failure (timeout, missing script, OS error) is logged and the next
    step proceeds — never aborts the scheduled run. Full output stays on
    disk in an owner-only artifact named by the report line.
    """
    out_path, err_path = _step_artifacts(label)
    try:
        returncode = _run_to_files(cmd, out_path, err_path, timeout)
    except subprocess.TimeoutExpired:
        log_fn(f"  {label}: TIMEOUT after {timeout}s — skipping, continuing")
        log_fn(f"  {label}: partial output → {_artifact_note(out_path, err_path)}")
        return 2
    except OSError as e:
        error = redact_secrets(str(e))
        log_fn(f"  {label}: OS error ({type(e).__name__}: {error}) — skipping, continuing")
        return 2
    _log_step_output(log_fn, label, out_path, err_path, returncode)
    return _step_status(returncode)


def _safe_entry(path: Path) -> tuple[float, int, Path] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime, stat.st_size, path)


def _report_entries(directory: Path, pattern: str) -> list[tuple[float, int, Path]]:
    """(mtime, size, path) newest first; unreadable entries are ignored."""
    entries = [_safe_entry(path) for path in directory.glob(pattern)]
    present = [entry for entry in entries if entry is not None]
    present.sort(key=lambda entry: entry[0], reverse=True)
    return present


def _expired(entries: list[tuple[float, int, Path]], max_age_days: int) -> set[Path]:
    cutoff = time.time() - max_age_days * 86400
    return {path for mtime, _, path in entries if mtime < cutoff}


def _over_count(entries: list[tuple[float, int, Path]], max_files: int) -> set[Path]:
    return {path for _, _, path in entries[max_files:]}


def _over_size(entries: list[tuple[float, int, Path]], max_bytes: int) -> set[Path]:
    used = 0
    doomed: set[Path] = set()
    for _, size, path in entries:
        used += size
        if used > max_bytes:
            doomed.add(path)
    return doomed


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
    except OSError:
        return False
    return True


def prune_reports(
    directory: Path,
    pattern: str,
    *,
    max_age_days: int = REPORT_RETENTION_DAYS,
    max_files: int = REPORT_RETENTION_FILES,
    max_bytes: int = REPORT_RETENTION_BYTES,
) -> int:
    """Bounded retention over one report family: age, count, and total size."""
    if not directory.exists():
        return 0
    entries = _report_entries(directory, pattern)
    doomed = (
        _expired(entries, max_age_days)
        | _over_count(entries, max_files)
        | _over_size(entries, max_bytes)
    )
    return sum(1 for path in doomed if _unlink(path))


def prune_maintenance_output() -> int:
    """Apply retention to every maintenance report family and its artifacts."""
    removed = sum(
        prune_reports(REPORTS_DIR, pattern) for pattern in MAINTENANCE_REPORT_PATTERNS
    )
    return removed + prune_reports(ARTIFACT_DIR, ARTIFACT_PATTERN)


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
