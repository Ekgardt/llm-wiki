"""Check or apply bounded runtime synchronization without changing knowledge."""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import doctor

SCHEMA_VERSION = "1.0"
ACTIONS = (
    "environment",
    "dependencies",
    "integrations",
    "transactions",
    "queue",
    "indexes",
    "doctor",
)
ACTION_STATUSES = ("ok", "changed", "skipped", "error")
DEFAULT_TIME_LIMIT_SECONDS = 30.0
DEFAULT_ACTION_LIMIT = len(ACTIONS)
DEPENDENCY_TIMEOUT_SECONDS = 30.0
PROCESS_CLEANUP_TIMEOUT_SECONDS = 2.0
INDEX_BUILDER_SCRIPT = Path(__file__).resolve().with_name("search_memory.py")
WINDOWS_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)


class ProcessTreeTimeout(subprocess.TimeoutExpired):
    """A process timed out, with an explicit tree-cleanup result."""

    def __init__(self, cmd: list[str], timeout: float, *, cleanup_error: str | None):
        super().__init__(cmd, timeout)
        self.cleanup_error = cleanup_error


def _result(action_id: str, status: str, message: str, details: dict) -> dict:
    if status not in ACTION_STATUSES:
        raise ValueError(f"invalid sync action status: {status}")
    return {"id": action_id, "status": status, "message": message, "details": details}


def _run_uv(command: list[str], *, root: Path, timeout: float) -> subprocess.CompletedProcess[str]:
    return _run_process_tree(
        command,
        cwd=root,
        timeout=timeout,
        capture_output=True,
        text=True,
    )


def _dependency_action(
    *,
    root: Path,
    apply: bool,
    run_uv: Callable[..., subprocess.CompletedProcess[str]] = _run_uv,
    timeout: float = DEPENDENCY_TIMEOUT_SECONDS,
    deadline: float | None = None,
) -> dict:
    dependency_deadline = deadline if deadline is not None else time.monotonic() + timeout

    def remaining() -> float:
        return max(0.0, dependency_deadline - time.monotonic())

    project = root / "pyproject.toml"
    lock = root / "uv.lock"
    if not project.is_file() or not lock.is_file():
        missing = [path.name for path in (project, lock) if not path.is_file()]
        return _result(
            "dependencies",
            "error",
            "Dependency lock inputs are missing.",
            {"lock": "missing", "environment": "unknown", "missing": missing},
        )
    try:
        project_text = project.read_text(encoding="utf-8")
        lock_text = lock.read_text(encoding="utf-8")
    except OSError as exc:
        return _result(
            "dependencies",
            "error",
            "Dependency lock inputs are unreadable.",
            {"lock": "unreadable", "environment": "unknown", "error": type(exc).__name__},
        )
    if "mcp-server" not in project_text or "mcp-server" not in lock_text:
        return _result(
            "dependencies",
            "error",
            "The locked MCP baseline is not declared.",
            {"lock": "incomplete", "environment": "unknown"},
        )

    lock_command = ["uv", "lock", "--check", "--no-python-downloads"]
    try:
        if remaining() <= 0:
            raise subprocess.TimeoutExpired(lock_command, 0)
        completed = run_uv(lock_command, root=root, timeout=max(0.001, remaining()))
    except ProcessTreeTimeout as exc:
        return _result(
            "dependencies",
            "error",
            "Dependency synchronization timed out; process cleanup was not verified."
            if exc.cleanup_error
            else "Dependency synchronization exceeded its time limit.",
            {
                "lock": "unknown",
                "environment": "unknown",
                "timed_out": True,
                **({"cleanup_error": exc.cleanup_error} if exc.cleanup_error else {}),
            },
        )
    except subprocess.TimeoutExpired:
        return _result(
            "dependencies",
            "error",
            "Dependency synchronization exceeded its time limit.",
            {"lock": "unknown", "environment": "unknown", "timed_out": True},
        )
    except OSError as exc:
        return _result(
            "dependencies",
            "error",
            "The uv executable is unavailable.",
            {"lock": "unknown", "environment": "unknown", "error": type(exc).__name__},
        )
    if completed.returncode != 0:
        return _result(
            "dependencies",
            "error",
            "Locked dependency validation failed.",
            {"lock": "stale", "environment": "unknown", "returncode": completed.returncode},
        )

    plan_command = [
        "uv",
        "sync",
        "--locked",
        "--inexact",
        "--extra",
        "mcp-server",
        "--dry-run",
        "--output-format",
        "json",
        "--no-python-downloads",
    ]
    try:
        if remaining() <= 0:
            raise subprocess.TimeoutExpired(plan_command, 0)
        completed = run_uv(plan_command, root=root, timeout=max(0.001, remaining()))
    except ProcessTreeTimeout as exc:
        return _result(
            "dependencies",
            "error",
            "Dependency planning timed out; process cleanup was not verified."
            if exc.cleanup_error
            else "Dependency planning exceeded its time limit.",
            {
                "lock": "current",
                "environment": "unknown",
                "timed_out": True,
                **({"cleanup_error": exc.cleanup_error} if exc.cleanup_error else {}),
            },
        )
    except subprocess.TimeoutExpired:
        return _result(
            "dependencies",
            "error",
            "Dependency planning exceeded its time limit.",
            {"lock": "current", "environment": "unknown", "timed_out": True},
        )
    except OSError as exc:
        return _result(
            "dependencies",
            "error",
            "The uv executable is unavailable.",
            {"lock": "current", "environment": "unknown", "error": type(exc).__name__},
        )
    if completed.returncode != 0:
        return _result(
            "dependencies",
            "error",
            "Locked dependency planning failed.",
            {"lock": "current", "environment": "unknown", "returncode": completed.returncode},
        )
    try:
        planned = json.loads(completed.stdout)
        changes = planned["sync"]["changes"]
        if not isinstance(changes, list):
            raise TypeError("sync changes must be a list")
    except (json.JSONDecodeError, KeyError, TypeError):
        return _result(
            "dependencies",
            "error",
            "Locked dependency plan was invalid.",
            {"lock": "current", "environment": "unknown", "plan": "invalid"},
        )
    if not changes:
        return _result(
            "dependencies",
            "ok",
            "Dependency lock and baseline environment are current.",
            {"lock": "current", "environment": "current"},
        )
    if not apply:
        return _result(
            "dependencies",
            "skipped",
            "Dependency lock is current; apply sync to repair the baseline environment.",
            {"lock": "current", "environment": "stale"},
        )
    sync_command = [
        "uv",
        "sync",
        "--locked",
        "--inexact",
        "--extra",
        "mcp-server",
        "--no-python-downloads",
        "--quiet",
    ]
    try:
        if remaining() <= 0:
            raise subprocess.TimeoutExpired(sync_command, 0)
        completed = run_uv(sync_command, root=root, timeout=max(0.001, remaining()))
    except ProcessTreeTimeout as exc:
        return _result(
            "dependencies",
            "error",
            "Dependency synchronization timed out; process cleanup was not verified."
            if exc.cleanup_error
            else "Dependency synchronization exceeded its time limit.",
            {
                "lock": "current",
                "environment": "unknown",
                "timed_out": True,
                **({"cleanup_error": exc.cleanup_error} if exc.cleanup_error else {}),
            },
        )
    except subprocess.TimeoutExpired:
        return _result(
            "dependencies",
            "error",
            "Dependency synchronization exceeded its time limit.",
            {"lock": "current", "environment": "unknown", "timed_out": True},
        )
    except OSError as exc:
        return _result(
            "dependencies",
            "error",
            "The uv executable is unavailable.",
            {"lock": "current", "environment": "unknown", "error": type(exc).__name__},
        )
    if completed.returncode != 0:
        return _result(
            "dependencies",
            "error",
            "Locked baseline dependency synchronization failed.",
            {"lock": "current", "environment": "missing", "returncode": completed.returncode},
        )
    return _result(
        "dependencies",
        "changed",
        "Locked baseline dependencies were synchronized.",
        {"lock": "current", "environment": "current"},
    )


def _terminate_windows_tree(process: subprocess.Popen[str]) -> str | None:
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
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def _finish_timed_out_process(
    process: subprocess.Popen[str], cleanup_error: str | None
) -> str | None:
    if cleanup_error:
        try:
            process.kill()
        except OSError:
            cleanup_error = _append_cleanup_error(cleanup_error, "direct_kill_failed")
    try:
        process.communicate(timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS)
        return cleanup_error
    except subprocess.TimeoutExpired:
        cleanup_error = _append_cleanup_error(cleanup_error, "retained_pipes")
        _close_captured_pipes(process)
        try:
            process.kill()
        except OSError:
            cleanup_error = _append_cleanup_error(cleanup_error, "direct_kill_failed")
        try:
            process.wait(timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            cleanup_error = _append_cleanup_error(cleanup_error, "cleanup_wait_failed")
        return cleanup_error


def _run_process_tree(
    command: list[str], *, timeout: float, **kwargs: object
) -> subprocess.CompletedProcess[str]:
    popen_options = dict(kwargs)
    if popen_options.pop("capture_output", False):
        popen_options["stdout"] = subprocess.PIPE
        popen_options["stderr"] = subprocess.PIPE
    if os.name == "nt":
        popen_options["creationflags"] = (
            int(popen_options.get("creationflags", 0)) | WINDOWS_NEW_PROCESS_GROUP
        )
    else:
        popen_options["start_new_session"] = True
    process = subprocess.Popen(command, **popen_options)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        cleanup_error = None
        if os.name == "nt":
            cleanup_error = _terminate_windows_tree(process)
        else:
            try:
                os.killpg(os.getpgid(process.pid), getattr(signal, "SIGKILL", 9))
            except OSError:
                cleanup_error = "process_group_kill_failed"
        cleanup_error = _finish_timed_out_process(process, cleanup_error)
        raise ProcessTreeTimeout(command, exc.timeout, cleanup_error=cleanup_error) from exc
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _run_index_builder(*, root: Path, state_root: Path, timeout: float) -> dict:
    if timeout <= 0:
        return _result(
            "indexes",
            "skipped",
            "Index rebuild was not started because the sync time limit was reached.",
            {"bounded": True},
        )
    environment = os.environ.copy()
    environment.update(
        LLM_WIKI_ROOT=str(root),
        LLM_WIKI_STATE_ROOT=str(state_root),
    )
    command = [sys.executable, str(INDEX_BUILDER_SCRIPT), "--rebuild"]
    deadline = time.monotonic() + timeout
    try:
        for attempt in range(3):
            completed = _run_process_tree(
                command,
                cwd=root,
                env=environment,
                timeout=max(0.001, deadline - time.monotonic()),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
            )
            sharing_violation = completed.returncode != 0 and (
                "PermissionError" in completed.stderr or "WinError 5" in completed.stderr
            )
            if not sharing_violation or attempt == 2:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.2, remaining))
    except ProcessTreeTimeout as exc:
        return _result(
            "indexes",
            "error",
            "Index rebuild timed out; process cleanup was not verified."
            if exc.cleanup_error
            else "Index rebuild exceeded its time limit and was terminated.",
            {
                "timed_out": True,
                **({"cleanup_error": exc.cleanup_error} if exc.cleanup_error else {}),
            },
        )
    except OSError as exc:
        return _result(
            "indexes",
            "error",
            "Index builder could not be started.",
            {"error": type(exc).__name__},
        )
    if completed.returncode != 0:
        if sharing_violation:
            return _result(
                "indexes",
                "skipped",
                "Index rebuild was deferred after a transient sharing violation.",
                {"partial": True, "reason": "sharing_violation"},
            )
        return _result(
            "indexes",
            "error",
            "Index builder failed.",
            {"returncode": completed.returncode},
        )
    return _result(
        "indexes",
        "changed",
        "Derived search index was rebuilt.",
        {},
    )


def _run_generation_builder(
    *, root: Path, state_root: Path, timeout: float, max_sources: int
) -> dict:
    if timeout <= 0:
        return _result(
            "indexes",
            "skipped",
            "Generation refresh was not started because the sync time limit was reached.",
            {"bounded": True, "partial": True},
        )
    result = doctor.run_generation_maintenance(
        root=root,
        state_root=state_root,
        time_budget_seconds=timeout,
        max_sources=max_sources,
    )
    status = str(result.get("status", "error"))
    mapped = {
        "built": "changed",
        "current": "ok",
        "deferred": "skipped",
        "error": "error",
    }.get(status, "error")
    return _result(
        "indexes",
        mapped,
        {
            "built": "Evidence generation was refreshed.",
            "current": "Evidence generation is current.",
            "deferred": "Evidence generation refresh was deferred for continuation.",
        }.get(status, "Evidence generation refresh failed."),
        {
            "generation": result.get("generation_id"),
            "partial": bool(result.get("partial")),
            **({"reason": result["reason"]} if result.get("reason") else {}),
        },
    )


def _check_by_id(report: dict, check_id: str) -> dict:
    return next(
        (check for check in report.get("checks", []) if check.get("id") == check_id),
        {
            "id": check_id,
            "status": "error",
            "message": f"Doctor did not report {check_id}.",
            "details": {},
        },
    )


def _mapped_status(status: str) -> str:
    return "ok" if status == "ok" else "error" if status == "error" else "skipped"


def _doctor_subset_action(action_id: str, report: dict, check_ids: tuple[str, ...]) -> dict:
    checks = [_check_by_id(report, check_id) for check_id in check_ids]
    statuses = {check["status"] for check in checks}
    status = "error" if "error" in statuses else "skipped" if statuses - {"ok"} else "ok"
    if report.get("repaired"):
        status = "changed" if status != "error" else status
    primary = checks[0]
    details = dict(primary.get("details", {}))
    if len(checks) > 1:
        details["checks"] = {check["id"]: check["status"] for check in checks}
    if report.get("repaired"):
        details["repairs"] = report["repaired"]
    return _result(action_id, status, str(primary.get("message") or action_id), details)


def run_sync(
    root: Path | str | None = None,
    state_root: Path | str | None = None,
    home: Path | str | None = None,
    *,
    apply: bool = False,
    time_limit_seconds: float = DEFAULT_TIME_LIMIT_SECONDS,
    action_limit: int = DEFAULT_ACTION_LIMIT,
) -> dict:
    """Run ordered bounded sync actions; check mode is strictly read-only."""
    root_path = Path(
        root or os.environ.get("LLM_WIKI_ROOT", Path(__file__).resolve().parent.parent)
    ).resolve()
    state_path = Path(
        os.path.abspath(state_root or os.environ.get("LLM_WIKI_STATE_ROOT", root_path))
    )
    home_path = Path(home).resolve() if home is not None else Path.home().resolve()
    seconds = float(time_limit_seconds)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("time_limit_seconds must be a positive finite number")
    if isinstance(action_limit, bool) or not 1 <= int(action_limit) <= len(ACTIONS):
        raise ValueError(f"action_limit must be between 1 and {len(ACTIONS)}")
    limit = int(action_limit)
    deadline = time.monotonic() + seconds
    actions: list[dict] = []
    doctor_cache: dict | None = None

    def remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    def doctor_report(
        *,
        repair: bool,
        repair_actions: set[str] | None = None,
        refresh: bool = False,
    ) -> dict:
        nonlocal doctor_cache
        if not repair and not refresh and doctor_cache is not None:
            return doctor_cache
        report = doctor.run_doctor(
            root=root_path,
            state_root=state_path,
            home=home_path,
            repair=repair,
            repair_actions=repair_actions,
            time_budget_seconds=remaining(),
        )
        if not repair:
            doctor_cache = report
        return report

    for position, action_id in enumerate(ACTIONS):
        if position >= limit or remaining() <= 0:
            actions.append(
                _result(
                    action_id,
                    "skipped",
                    "Action was not run because a sync limit was reached.",
                    {"bounded": True},
                )
            )
            continue
        try:
            if action_id == "environment":
                report = doctor_report(
                    repair=apply,
                    repair_actions={"runtime"} if apply else None,
                )
                action = _doctor_subset_action(
                    action_id, report, ("environment", "runtime", "filesystem")
                )
                if apply:
                    doctor_cache = {**report, "repaired": []}
            elif action_id == "dependencies":
                action = _dependency_action(
                    root=root_path,
                    apply=apply,
                    deadline=min(deadline, time.monotonic() + DEPENDENCY_TIMEOUT_SECONDS),
                )
            elif action_id == "integrations":
                action = _doctor_subset_action(
                    action_id,
                    doctor_report(repair=False),
                    ("integrations",),
                )
            elif action_id in {"transactions", "queue"}:
                action = _doctor_subset_action(
                    action_id,
                    doctor_report(repair=False),
                    (action_id,),
                )
            elif action_id == "indexes":
                report = doctor_report(repair=False)
                generation_reported = any(
                    check.get("id") == "generation" for check in report.get("checks", [])
                )
                before = _doctor_subset_action(
                    action_id,
                    report,
                    ("index", "generation") if generation_reported else ("index",),
                )
                generation = _check_by_id(report, "generation")
                index = _check_by_id(report, "index")
                refreshes = []
                index_needs_refresh = (
                    apply and index["status"] != "ok" and index["details"].get("repairable")
                )
                generation_needs_refresh = (
                    apply
                    and generation_reported
                    and generation["status"] != "ok"
                    and generation["details"].get("repairable")
                )
                if generation_needs_refresh:
                    refreshes.append(
                        _run_generation_builder(
                            root=root_path,
                            state_root=state_path,
                            timeout=remaining(),
                            max_sources=doctor.DEFAULT_GENERATION_SOURCE_LIMIT,
                        )
                    )
                if index_needs_refresh:
                    index_action = _run_index_builder(
                        root=root_path,
                        state_root=state_path,
                        timeout=remaining(),
                    )
                    if index_action["status"] == "changed" and not generation_needs_refresh:
                        after = _doctor_subset_action(
                            action_id,
                            doctor_report(repair=False, refresh=True),
                            ("index",),
                        )
                        if after["status"] != "ok":
                            index_action = _result(
                                "indexes",
                                "error",
                                "Rebuilt index did not pass freshness validation.",
                                after["details"],
                            )
                    refreshes.append(index_action)
                if refreshes:
                    if len(refreshes) == 1:
                        action = refreshes[0]
                        actions.append(action)
                        continue
                    statuses = {item["status"] for item in refreshes}
                    fatal_error = any(
                        item["status"] == "error" and not item["details"].get("timed_out")
                        for item in refreshes
                    )
                    status = (
                        "error"
                        if fatal_error
                        else "changed"
                        if "changed" in statuses
                        else "skipped"
                        if "skipped" in statuses
                        else "ok"
                    )
                    action = _result(
                        "indexes",
                        status,
                        "Derived indexes were synchronized."
                        if status in {"ok", "changed"}
                        else "Derived index synchronization requires attention.",
                        {
                            "generation": next(
                                (
                                    item["details"].get("generation")
                                    for item in refreshes
                                    if "generation" in item["details"]
                                ),
                                None,
                            ),
                            "partial": any(
                                item["details"].get("partial", False)
                                or item["details"].get("timed_out", False)
                                for item in refreshes
                            ),
                            "actions": [item["status"] for item in refreshes],
                            "results": [item["details"] for item in refreshes],
                        },
                    )
                else:
                    action = before
            else:
                report = doctor_report(repair=False, refresh=True)
                status = _mapped_status(str(report.get("overall_status", "error")))
                action = _result(
                    "doctor",
                    status,
                    "Final doctor check completed."
                    if status == "ok"
                    else "Final doctor check requires attention.",
                    {"overall_status": report.get("overall_status", "error")},
                )
        except Exception as exc:  # noqa: BLE001 - every action reports independently
            action = _result(
                action_id,
                "error",
                f"{action_id.title()} action failed.",
                {"error": type(exc).__name__},
            )
        actions.append(action)

    counts = {
        status: sum(action["status"] == status for action in actions) for status in ACTION_STATUSES
    }
    overall = (
        "error"
        if counts["error"]
        else "degraded"
        if counts["skipped"]
        else "changed"
        if counts["changed"]
        else "ok"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "apply" if apply else "check",
        "overall_status": overall,
        "limits": {"actions": limit, "seconds": seconds},
        "actions": actions,
        "counts": counts,
    }


def main(argv: list[str] | None = None) -> int:
    def positive_finite(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed) or parsed <= 0:
            raise argparse.ArgumentTypeError("must be a positive finite number")
        return parsed

    def valid_action_limit(value: str) -> int:
        parsed = int(value)
        if not 1 <= parsed <= len(ACTIONS):
            raise argparse.ArgumentTypeError(f"must be between 1 and {len(ACTIONS)}")
        return parsed

    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Check synchronization state (default).")
    mode.add_argument("--apply", action="store_true", help="Apply bounded safe synchronization.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    parser.add_argument(
        "--time-limit-seconds",
        type=positive_finite,
        default=DEFAULT_TIME_LIMIT_SECONDS,
        help="Maximum elapsed sync time.",
    )
    parser.add_argument(
        "--action-limit",
        type=valid_action_limit,
        default=DEFAULT_ACTION_LIMIT,
        help="Maximum ordered actions to execute.",
    )
    args = parser.parse_args(argv)
    report = run_sync(
        apply=args.apply,
        time_limit_seconds=args.time_limit_seconds,
        action_limit=args.action_limit,
    )
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False))
    else:
        print(f"LLM-Wiki sync ({report['mode']}): {report['overall_status']}")
        for action in report["actions"]:
            print(f"{action['id']}: {action['status']} - {action['message']}")
    return {"ok": 0, "changed": 0, "degraded": 1, "error": 2}.get(report["overall_status"], 2)


if __name__ == "__main__":
    raise SystemExit(main())
