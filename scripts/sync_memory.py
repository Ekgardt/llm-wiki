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
from typing import NamedTuple

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


_DEPENDENCIES = "dependencies"


class _UvStep(NamedTuple):
    """One `uv` invocation of the dependency action and how its failures read."""

    command: tuple[str, ...]
    verb: str  # "synchronization" | "planning", for the time-limit messages
    lock_state: str  # what `details["lock"]` says while this step's outcome is unknown
    failure_message: str
    failure_details: dict


_LOCK_STEP = _UvStep(
    ("uv", "lock", "--check", "--no-python-downloads"),
    "synchronization",
    "unknown",
    "Locked dependency validation failed.",
    {"lock": "stale", "environment": "unknown"},
)
_PLAN_STEP = _UvStep(
    (
        "uv",
        "sync",
        "--locked",
        "--inexact",
        "--no-default-groups",
        "--dry-run",
        "--output-format",
        "json",
        "--no-python-downloads",
    ),
    "planning",
    "current",
    "Locked dependency planning failed.",
    {"lock": "current", "environment": "unknown"},
)
_SYNC_STEP = _UvStep(
    ("uv", "sync", "--locked", "--inexact", "--no-default-groups", "--no-python-downloads", "--quiet"),
    "synchronization",
    "current",
    "Locked baseline dependency synchronization failed.",
    {"lock": "current", "environment": "missing"},
)


def _dependency_error(message: str, details: dict) -> dict:
    return _result(_DEPENDENCIES, "error", message, details)


def _uv_timeout_result(step: _UvStep, cleanup_error: str | None) -> dict:
    details = {"lock": step.lock_state, "environment": "unknown", "timed_out": True}
    if cleanup_error:
        details["cleanup_error"] = cleanup_error
        return _dependency_error(
            f"Dependency {step.verb} timed out; process cleanup was not verified.", details
        )
    return _dependency_error(f"Dependency {step.verb} exceeded its time limit.", details)


def _run_uv_step(
    step: _UvStep, run_uv: Callable[..., subprocess.CompletedProcess[str]], *, root: Path, remaining
) -> tuple[subprocess.CompletedProcess[str] | None, dict | None]:
    """The completed process, or the error result of a step that could not finish."""
    command = list(step.command)
    try:
        if remaining() <= 0:
            raise subprocess.TimeoutExpired(command, 0)
        completed = run_uv(command, root=root, timeout=max(0.001, remaining()))
    except subprocess.TimeoutExpired as exc:  # ProcessTreeTimeout carries cleanup_error
        return None, _uv_timeout_result(step, getattr(exc, "cleanup_error", None))
    except OSError as exc:
        return None, _dependency_error(
            "The uv executable is unavailable.",
            {"lock": step.lock_state, "environment": "unknown", "error": type(exc).__name__},
        )
    if completed.returncode != 0:
        return None, _dependency_error(
            step.failure_message, {**step.failure_details, "returncode": completed.returncode}
        )
    return completed, None


def _missing_files(*paths: Path) -> list[str]:
    return [path.name for path in paths if not path.is_file()]


def _baseline_declared(project_text: str, lock_text: str) -> bool:
    return "mcp>=1.29,<2" in project_text and 'name = "mcp"' in lock_text


def _dependency_inputs_error(root: Path) -> dict | None:
    """The error result when pyproject/uv.lock are missing, unreadable or incomplete."""
    project, lock = root / "pyproject.toml", root / "uv.lock"
    missing = _missing_files(project, lock)
    if missing:
        return _dependency_error(
            "Dependency lock inputs are missing.",
            {"lock": "missing", "environment": "unknown", "missing": missing},
        )
    try:
        texts = (project.read_text(encoding="utf-8"), lock.read_text(encoding="utf-8"))
    except OSError as exc:
        return _dependency_error(
            "Dependency lock inputs are unreadable.",
            {"lock": "unreadable", "environment": "unknown", "error": type(exc).__name__},
        )
    if not _baseline_declared(*texts):
        return _dependency_error(
            "The locked MCP baseline is not declared.", {"lock": "incomplete", "environment": "unknown"}
        )
    return None


def _planned_changes(stdout: str) -> list | None:
    try:
        changes = json.loads(stdout)["sync"]["changes"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    if not isinstance(changes, list):
        return None
    return changes


def _locked_plan(run_uv, root: Path, remaining) -> tuple[object | None, dict | None]:
    """The completed plan step after a current lock, or the error of the step that failed."""
    _completed, error = _run_uv_step(_LOCK_STEP, run_uv, root=root, remaining=remaining)
    if error is not None:
        return None, error
    return _run_uv_step(_PLAN_STEP, run_uv, root=root, remaining=remaining)


def _dependency_plan(run_uv, root: Path, remaining) -> tuple[list | None, dict | None]:
    """The planned changes, or the error result of the step that failed."""
    planned, error = _locked_plan(run_uv, root, remaining)
    if error is not None:
        return None, error
    changes = _planned_changes(planned.stdout)
    if changes is None:
        return None, _dependency_error(
            "Locked dependency plan was invalid.",
            {"lock": "current", "environment": "unknown", "plan": "invalid"},
        )
    return changes, None


def _apply_dependency_sync(run_uv, root: Path, remaining) -> dict:
    _completed, error = _run_uv_step(_SYNC_STEP, run_uv, root=root, remaining=remaining)
    if error is not None:
        return error
    return _result(
        _DEPENDENCIES,
        "changed",
        "Locked baseline dependencies were synchronized.",
        {"lock": "current", "environment": "current"},
    )


def _dependency_deadline(deadline: float | None, timeout: float) -> float:
    if deadline is not None:
        return deadline
    return time.monotonic() + timeout


def _remaining_clock(deadline: float) -> Callable[[], float]:
    def remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    return remaining


def _dependency_action(
    *,
    root: Path,
    apply: bool,
    run_uv: Callable[..., subprocess.CompletedProcess[str]] = _run_uv,
    timeout: float = DEPENDENCY_TIMEOUT_SECONDS,
    deadline: float | None = None,
) -> dict:
    remaining = _remaining_clock(_dependency_deadline(deadline, timeout))
    changes, error = _checked_dependency_plan(run_uv, root, remaining)
    if error is not None:
        return error
    if changes and apply:
        return _apply_dependency_sync(run_uv, root, remaining)
    return _planned_but_not_applied(changes)


def _checked_dependency_plan(run_uv, root: Path, remaining) -> tuple[list | None, dict | None]:
    inputs_error = _dependency_inputs_error(root)
    if inputs_error is not None:
        return None, inputs_error
    return _dependency_plan(run_uv, root, remaining)


def _planned_but_not_applied(changes: list) -> dict:
    if not changes:
        return _result(
            _DEPENDENCIES,
            "ok",
            "Dependency lock and baseline environment are current.",
            {"lock": "current", "environment": "current"},
        )
    return _result(
        _DEPENDENCIES,
        "skipped",
        "Dependency lock is current; apply sync to repair the baseline environment.",
        {"lock": "current", "environment": "stale"},
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


def _popen_options(kwargs: dict) -> dict:
    """Popen options with captured streams and the child in its own group/session."""
    options = dict(kwargs)
    if options.pop("capture_output", False):
        options["stdout"] = subprocess.PIPE
        options["stderr"] = subprocess.PIPE
    if os.name == "nt":
        options["creationflags"] = int(options.get("creationflags", 0)) | WINDOWS_NEW_PROCESS_GROUP
        return options
    options["start_new_session"] = True
    return options


def _kill_process_tree(process: subprocess.Popen[str]) -> str | None:
    """End the child's whole group; the cleanup error, or None when it was ended."""
    if os.name == "nt":
        return _terminate_windows_tree(process)
    try:
        os.killpg(os.getpgid(process.pid), getattr(signal, "SIGKILL", 9))
    except OSError:
        return "process_group_kill_failed"
    return None


def _run_process_tree(
    command: list[str], *, timeout: float, **kwargs: object
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(command, **_popen_options(dict(kwargs)))
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        cleanup_error = _finish_timed_out_process(process, _kill_process_tree(process))
        raise ProcessTreeTimeout(command, exc.timeout, cleanup_error=cleanup_error) from exc
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


_INDEXES = "indexes"


def _sharing_violation(completed: subprocess.CompletedProcess[str]) -> bool:
    """A Windows sharing violation on the index file; retried, then deferred."""
    if completed.returncode == 0:
        return False
    return "PermissionError" in completed.stderr or "WinError 5" in completed.stderr


def _run_builder_with_retries(
    command: list[str], root: Path, environment: dict, deadline: float
) -> subprocess.CompletedProcess[str]:
    """Up to three attempts; a sharing violation is retried after a short pause."""
    completed = None
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
        if not _sharing_violation(completed) or attempt == 2:
            return completed
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return completed
        time.sleep(min(0.2, remaining))
    return completed


def _builder_timeout_result(exc: ProcessTreeTimeout) -> dict:
    if exc.cleanup_error:
        return _result(
            _INDEXES,
            "error",
            "Index rebuild timed out; process cleanup was not verified.",
            {"timed_out": True, "cleanup_error": exc.cleanup_error},
        )
    return _result(
        _INDEXES,
        "error",
        "Index rebuild exceeded its time limit and was terminated.",
        {"timed_out": True},
    )


def _builder_outcome(completed: subprocess.CompletedProcess[str]) -> dict:
    if completed.returncode == 0:
        return _result(_INDEXES, "changed", "Derived search index was rebuilt.", {})
    if _sharing_violation(completed):
        return _result(
            _INDEXES,
            "skipped",
            "Index rebuild was deferred after a transient sharing violation.",
            {"partial": True, "reason": "sharing_violation"},
        )
    return _result(_INDEXES, "error", "Index builder failed.", {"returncode": completed.returncode})


def _run_index_builder(*, root: Path, state_root: Path, timeout: float) -> dict:
    if timeout <= 0:
        return _result(
            _INDEXES,
            "skipped",
            "Index rebuild was not started because the sync time limit was reached.",
            {"bounded": True},
        )
    environment = os.environ.copy()
    environment.update(LLM_WIKI_ROOT=str(root), LLM_WIKI_STATE_ROOT=str(state_root))
    command = [sys.executable, str(INDEX_BUILDER_SCRIPT), "--rebuild"]
    try:
        completed = _run_builder_with_retries(command, root, environment, time.monotonic() + timeout)
    except ProcessTreeTimeout as exc:
        return _builder_timeout_result(exc)
    except OSError as exc:
        return _result(
            _INDEXES, "error", "Index builder could not be started.", {"error": type(exc).__name__}
        )
    return _builder_outcome(completed)


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
            "deferred": "Evidence generation refresh was deferred; retry will rebuild from source.",
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
    if status in {"ok", "error"}:
        return status
    return "skipped"


def _subset_status(statuses: set[str], repaired: bool) -> str:
    if "error" in statuses:
        return "error"
    return _unfailed_subset_status(statuses, repaired)


def _unfailed_subset_status(statuses: set[str], repaired: bool) -> str:
    if repaired:
        return "changed"
    if statuses - {"ok"}:
        return "skipped"
    return "ok"


def _subset_details(checks: list[dict], report: dict) -> dict:
    details = dict(checks[0].get("details", {}))
    if len(checks) > 1:
        details["checks"] = {check["id"]: check["status"] for check in checks}
    if report.get("repaired"):
        details["repairs"] = report["repaired"]
    return details


def _doctor_subset_action(action_id: str, report: dict, check_ids: tuple[str, ...]) -> dict:
    checks = [_check_by_id(report, check_id) for check_id in check_ids]
    statuses = {check["status"] for check in checks}
    status = _subset_status(statuses, bool(report.get("repaired")))
    message = str(checks[0].get("message") or action_id)
    return _result(action_id, status, message, _subset_details(checks, report))


def _refresh_status(statuses: set[str]) -> str:
    for status in ("error", "skipped", "changed"):
        if status in statuses:
            return status
    return "ok"


def _refresh_message(status: str, legacy_index_refresh: dict | None) -> str:
    if status == "skipped" and legacy_index_refresh is not None:
        return "Legacy search index was rebuilt, but evidence generation refresh was deferred."
    if status == "changed":
        return "Evidence generation and legacy search index were refreshed."
    return "Derived index synchronization requires attention."


def _refresh_generation_id(refreshes: list[dict]) -> object:
    for item in refreshes:
        if "generation" in item["details"]:
            return item["details"].get("generation")
    return None


def _refresh_partial(refreshes: list[dict]) -> bool:
    return any(
        item["details"].get("partial", False) or item["details"].get("timed_out", False)
        for item in refreshes
    )


def _refresh_state(refresh: dict | None) -> str:
    if refresh is None:
        return "not_needed"
    return refresh["status"]


def _combined_index_action(
    refreshes: list[dict], generation_refresh: dict | None, legacy_index_refresh: dict | None
) -> dict:
    status = _refresh_status({item["status"] for item in refreshes})
    return _result(
        _INDEXES,
        status,
        _refresh_message(status, legacy_index_refresh),
        {
            "generation": _refresh_generation_id(refreshes),
            "partial": _refresh_partial(refreshes),
            "generation_refresh": _refresh_state(generation_refresh),
            "legacy_index": _refresh_state(legacy_index_refresh),
            "actions": [item["status"] for item in refreshes],
            "results": [item["details"] for item in refreshes],
        },
    )


def _generation_reported(report: dict) -> bool:
    return any(check.get("id") == "generation" for check in report.get("checks", []))


def _index_check_ids(generation_reported: bool) -> tuple[str, ...]:
    if generation_reported:
        return ("index", "generation")
    return ("index",)


def _repair_actions(apply: bool) -> set[str] | None:
    if apply:
        return {"runtime"}
    return None


def _final_doctor_message(status: str) -> str:
    if status == "ok":
        return "Final doctor check completed."
    return "Final doctor check requires attention."


def _skipped_by_limit(action_id: str) -> dict:
    return _result(
        action_id, "skipped", "Action was not run because a sync limit was reached.", {"bounded": True}
    )


def _failed_action(action_id: str, exc: Exception) -> dict:
    return _result(
        action_id, "error", f"{action_id.title()} action failed.", {"error": type(exc).__name__}
    )


_SUBSET_CHECKS = {
    "integrations": ("integrations",),
    "transactions": ("transactions",),
    "queue": ("queue",),
}


class _SyncRun:
    """One `run_sync` call: its roots, mode, deadline and the cached doctor report.

    A plain class: the tests load this module through `importlib` without
    registering it in `sys.modules`, which a dataclass cannot survive under
    postponed annotations.
    """

    def __init__(self, root: Path, state_root: Path, home: Path, apply: bool, deadline: float) -> None:
        self.root = root
        self.state_root = state_root
        self.home = home
        self.apply = apply
        self.deadline = deadline
        self.doctor_cache: dict | None = None

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def doctor_report(
        self, *, repair: bool, repair_actions: set[str] | None = None, refresh: bool = False
    ) -> dict:
        if not repair and not refresh and self.doctor_cache is not None:
            return self.doctor_cache
        report = doctor.run_doctor(
            root=self.root,
            state_root=self.state_root,
            home=self.home,
            repair=repair,
            repair_actions=repair_actions,
            time_budget_seconds=self.remaining(),
        )
        if not repair:
            self.doctor_cache = report
        return report

    # --- the actions, in `ACTIONS` order ---------------------------------------

    def environment_action(self) -> dict:
        report = self.doctor_report(repair=self.apply, repair_actions=_repair_actions(self.apply))
        action = _doctor_subset_action(
            "environment", report, ("environment", "runtime", "filesystem")
        )
        if self.apply:
            self.doctor_cache = {**report, "repaired": []}
        return action

    def dependencies_action(self) -> dict:
        return _dependency_action(
            root=self.root,
            apply=self.apply,
            deadline=min(self.deadline, time.monotonic() + DEPENDENCY_TIMEOUT_SECONDS),
        )

    def subset_action(self, action_id: str, check_ids: tuple[str, ...]) -> dict:
        return _doctor_subset_action(action_id, self.doctor_report(repair=False), check_ids)

    def _needs_refresh(self, check: dict, reported: bool) -> bool:
        if not self.apply or not reported:
            return False
        return check["status"] != "ok" and bool(check["details"].get("repairable"))

    def _generation_refresh(self) -> dict:
        return _run_generation_builder(
            root=self.root,
            state_root=self.state_root,
            timeout=self.remaining(),
            max_sources=doctor.DEFAULT_GENERATION_SOURCE_LIMIT,
        )

    def _index_refresh(self, *, validate: bool) -> dict:
        """Rebuild the legacy index; alone, the rebuild is validated by a fresh doctor read."""
        index_action = _run_index_builder(
            root=self.root, state_root=self.state_root, timeout=self.remaining()
        )
        if index_action["status"] != "changed" or not validate:
            return index_action
        after = _doctor_subset_action(
            _INDEXES, self.doctor_report(repair=False, refresh=True), ("index",)
        )
        if after["status"] != "ok":
            return _result(
                _INDEXES, "error", "Rebuilt index did not pass freshness validation.", after["details"]
            )
        return index_action

    def _refreshes(self, report: dict, generation_reported: bool) -> tuple[dict | None, dict | None]:
        """(generation refresh, legacy index refresh), each None when not needed."""
        generation_refresh = None
        if self._needs_refresh(_check_by_id(report, "generation"), generation_reported):
            generation_refresh = self._generation_refresh()
        index_refresh = None
        if self._needs_refresh(_check_by_id(report, "index"), True):
            index_refresh = self._index_refresh(validate=generation_refresh is None)
        return generation_refresh, index_refresh

    def indexes_action(self) -> dict:
        report = self.doctor_report(repair=False)
        generation_reported = _generation_reported(report)
        generation_refresh, index_refresh = self._refreshes(report, generation_reported)
        refreshes = [item for item in (generation_refresh, index_refresh) if item is not None]
        if not refreshes:
            return _doctor_subset_action(_INDEXES, report, _index_check_ids(generation_reported))
        if len(refreshes) == 1:
            return refreshes[0]
        return _combined_index_action(refreshes, generation_refresh, index_refresh)

    def final_doctor_action(self) -> dict:
        report = self.doctor_report(repair=False, refresh=True)
        status = _mapped_status(str(report.get("overall_status", "error")))
        return _result(
            "doctor",
            status,
            _final_doctor_message(status),
            {"overall_status": report.get("overall_status", "error")},
        )

    def action(self, action_id: str) -> dict:
        if action_id in _SUBSET_CHECKS:
            return self.subset_action(action_id, _SUBSET_CHECKS[action_id])
        runners = {
            "environment": self.environment_action,
            "dependencies": self.dependencies_action,
            "indexes": self.indexes_action,
            "doctor": self.final_doctor_action,
        }
        return runners[action_id]()

    def guarded_action(self, action_id: str) -> dict:
        try:
            return self.action(action_id)
        except Exception as exc:  # noqa: BLE001 - every action reports independently
            return _failed_action(action_id, exc)

    def actions(self, limit: int) -> list[dict]:
        actions: list[dict] = []
        for position, action_id in enumerate(ACTIONS):
            if position >= limit or self.remaining() <= 0:
                actions.append(_skipped_by_limit(action_id))
                continue
            actions.append(self.guarded_action(action_id))
        return actions


def _sync_paths(root, state_root, home) -> tuple[Path, Path, Path]:
    root_path = Path(
        root or os.environ.get("LLM_WIKI_ROOT", Path(__file__).resolve().parent.parent)
    ).resolve()
    state_path = Path(
        os.path.abspath(state_root or os.environ.get("LLM_WIKI_STATE_ROOT", root_path))
    )
    home_path = Path(home).resolve() if home is not None else Path.home().resolve()
    return root_path, state_path, home_path


def _sync_limits(time_limit_seconds: float, action_limit: int) -> tuple[float, int]:
    seconds = float(time_limit_seconds)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("time_limit_seconds must be a positive finite number")
    if isinstance(action_limit, bool) or not 1 <= int(action_limit) <= len(ACTIONS):
        raise ValueError(f"action_limit must be between 1 and {len(ACTIONS)}")
    return seconds, int(action_limit)


def _action_counts(actions: list[dict]) -> dict[str, int]:
    return {
        status: sum(action["status"] == status for action in actions) for status in ACTION_STATUSES
    }


def _overall_status(counts: dict[str, int]) -> str:
    for status, overall in (("error", "error"), ("skipped", "degraded"), ("changed", "changed")):
        if counts[status]:
            return overall
    return "ok"


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
    root_path, state_path, home_path = _sync_paths(root, state_root, home)
    seconds, limit = _sync_limits(time_limit_seconds, action_limit)
    run = _SyncRun(root_path, state_path, home_path, apply, time.monotonic() + seconds)
    actions = run.actions(limit)
    counts = _action_counts(actions)
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "apply" if apply else "check",
        "overall_status": _overall_status(counts),
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
