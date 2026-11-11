"""Doctor's checks of the code update, the weekly and the nightly passes."""

from __future__ import annotations

import importlib.util
import os
import shlex
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import generation_maintenance  # noqa: E402
from llm_wiki import runtime_roots
from llm_wiki.doctor_base import MAX_CONFIG_BYTES, ok_or_error
from llm_wiki.install_scheduler_render import SCHEDULER_LIMIT_HOURS

# What a recorded code update outcome means for the operator, by field and value.
UPDATE_ATTENTION = {
    ("status", "error"): "The nightly code update failed; see the nightly log.",
    ("reason", "fetch_failed"): "The nightly code update could not fetch the remote.",
    ("reason", "not_on_default_branch"): (
        "The last nightly code update was skipped because the vault was not on its default branch."
    ),
    ("reason", "diverged_branch"): (
        "The last nightly code update was skipped because the vault's branch had diverged from the remote."
    ),
    ("reason", "local_changes_conflict"): (
        "The last nightly code update was stopped by a conflicting local change; see the nightly log."
    ),
    ("reason", "python_requirement_unmet"): (
        "The nightly code update is waiting for a Python this machine does not have: it needs "
        "{required}. Run `uv python install '{required}'`; the next nightly then updates."
    ),
    ("dependencies", "stale"): (
        "The code was updated but its dependencies were not synced; the nightly syncs "
        "them again, or run `uv sync --locked --inexact` with your `--extra` flags."
    ),
    ("resources", "rerun_installer"): (
        "The update changed what the installer renders; rerun the installer."
    ),
}


def update_verdict(record: object) -> tuple[str, str] | None:
    """(status, message) when the last code update needs the operator, else None.

    The nightly records the outcome since 2026-09-25; before, only its log did. See
    `docs/research/2026-09-25-the-scheduler-says-what-the-night-could-not-do.md`.
    """
    if not isinstance(record, dict):
        return None
    messages = [
        message.format(required=record.get("required"))
        for (field, value), message in UPDATE_ATTENTION.items()
        if record.get(field) == value
    ]
    if not messages:
        return None
    return "degraded", " ".join(messages)


def unit_limit_verdict(home: Path, root: Path | None = None) -> tuple[str, str] | None:
    """(status, message) when an installed systemd unit lacks this release's time limit."""
    stale = [kind for kind, limit in _installed_unit_limits(home).items() if limit != expected_unit_limit(kind, root)]
    if not stale:
        return None
    # The installer replaces units edited outside it, keeps the edited copy and moves a
    # line added by hand into a drop-in, so rerunning it is the whole advice
    # (docs/research/2026-09-28-an-update-replaces-what-it-owns.md).
    return (
        "degraded",
        "Installed maintenance units are older than this release ("
        + ", ".join(stale)
        + " time limit); rerun the installer. It replaces the units even if they were "
        "edited by hand, keeps the edited copy under run/install/displaced/, and moves an "
        "added setting into a drop-in (<unit>.d/50-local.conf).",
    )


def scheduled_program_verdict(home: Path) -> tuple[str, str] | None:
    """(status, message) when an installed schedule calls a uv that is no longer there.

    A package manager's upgrade removes the versioned target a resolved path named
    (audit B-30, docs/research/2026-09-25-a-scheduled-run-keeps-the-uv-link.md).
    """
    missing = sorted({program for program in _scheduled_programs(home) if not Path(program).exists()})
    if not missing:
        return None
    return (
        "degraded",
        "The scheduled runs call uv at " + ", ".join(missing) + ", which no longer exists; rerun the installer.",
    )


def _scheduled_programs(home: Path) -> list[str]:
    """The program each installed systemd unit or LaunchAgent starts; none installed, none read."""
    return [*_systemd_programs(home), *_launchd_programs(home)]


def _systemd_programs(home: Path) -> list[str]:
    directory = systemd_user_directory(home)
    programs = [_unit_program(directory / f"llm-wiki-{kind}.service") for kind in ("nightly", "weekly")]
    return [program for program in programs if program]


def _unit_program(path: Path) -> str:
    text = _read_small_text(path)
    if text is None:
        return ""
    return _first_word(_unit_setting(text, "ExecStart") or "")


def _first_word(command: str) -> str:
    try:
        words = shlex.split(command)
    except ValueError:
        return ""
    return words[0] if words else ""


def _launchd_programs(home: Path) -> list[str]:
    import plistlib

    programs = []
    for kind in ("nightly", "weekly"):
        path = home / "Library" / "LaunchAgents" / f"io.github.ekgardt.llm-wiki.{kind}.plist"
        text = _read_small_bytes(path)
        if text is not None:
            programs.append(_plist_program(plistlib, text))
    return [program for program in programs if program]


def _plist_program(plistlib, data: bytes) -> str:
    try:
        arguments = plistlib.loads(data).get("ProgramArguments") or [""]
    except (plistlib.InvalidFileException, ValueError, AttributeError):
        return ""
    return str(arguments[0])


def _read_small_bytes(path: Path) -> bytes | None:
    try:
        if not path.is_file() or path.stat().st_size > 64 * 1024:
            return None
        return path.read_bytes()
    except OSError:
        return None


def expected_unit_limit(kind: str, root: Path | None = None) -> str:
    from llm_wiki.install_scheduler_render import scheduler_limit_hours

    vault = runtime_roots.vault_root() if root is None else root
    return f"{scheduler_limit_hours(vault)[kind]}h"


def _installed_unit_limits(home: Path) -> dict[str, str | None]:
    """`TimeoutStartSec` of each installed llm-wiki systemd service; none installed, none read."""
    directory = systemd_user_directory(home)
    limits: dict[str, str | None] = {}
    for kind in ("nightly", "weekly"):
        text = _read_small_text(directory / f"llm-wiki-{kind}.service")
        if text is not None:
            limits[kind] = _unit_setting(text, "TimeoutStartSec")
    return limits


def systemd_user_directory(home: Path) -> Path:
    """Where the installer puts user units: `$XDG_CONFIG_HOME`, else under `home`."""
    configured = os.environ.get("XDG_CONFIG_HOME")
    if configured and Path(configured).is_absolute():
        return Path(configured) / "systemd" / "user"
    return home / ".config" / "systemd" / "user"


def _read_small_text(path: Path) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > 64 * 1024:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def _unit_setting(text: str, name: str) -> str | None:
    prefix = f"{name}="
    values = [line[len(prefix):].strip() for line in text.splitlines() if line.startswith(prefix)]
    return values[-1] if values else None


def with_findings(nightly: dict, verdicts: tuple[tuple[str, str] | None, ...]) -> dict:
    """The scheduler finding: the nightly's, joined by every other verdict that has one."""
    result = nightly
    for verdict in verdicts:
        result = _with_weekly(result, verdict)
    return result


# A seven-day period plus a day of grace. The weekly keeps its own record since
# 2026-09-24; a vault on which it has never recorded a run is not degraded for
# that. See docs/research/2026-09-24-the-weekly-pass-has-its-own-record.md.
WEEKLY_FRESH_SECONDS = 8 * 24 * 3600


# The scheduler kills a weekly after its limit; past that, a run with no result is over.
WEEKLY_LIMIT_SECONDS = SCHEDULER_LIMIT_HOURS["weekly"] * 3600
_SEVERITY = {"ok": 0, "skipped": 1, "degraded": 2, "error": 3}


def weekly_verdict(state: dict, now: datetime) -> tuple[str, str] | None:
    """(status, message) when the weekly pass needs attention, else None."""
    if state.get("last_weekly_status") == "failed":
        return "error", "Last weekly maintenance failed."
    lateness = weekly_lateness(state, now)
    if lateness is None:
        return None
    return "degraded", f"{lateness}{_weekly_skip_note(state)}"


def weekly_lateness(state: dict, now: datetime) -> str | None:
    checks = (_weekly_stale_note, _weekly_never_completed_note, _weekly_unfinished_note)
    return next((note for note in (check(state, now) for check in checks) if note), None)


def _weekly_stale_note(state: dict, now: datetime) -> str | None:
    if _weekly_is_stale(generation_maintenance.parse_utc(state.get("last_weekly_at")), now):
        return "Weekly maintenance is stale."
    return None


def _weekly_never_completed_note(state: dict, now: datetime) -> str | None:
    """No weekly run on record although one was due: skipped, or due for over a period.

    A weekly that never starts leaves no skip either; the nightly's first success
    (`weekly_due_since`) is the clock (audit 2026-09-27 B-15).
    """
    if state.get("last_weekly_at") is not None:
        return None
    due = generation_maintenance.parse_utc(state.get("weekly_due_since"))
    overdue = due is not None and (now - due).total_seconds() > WEEKLY_FRESH_SECONDS
    if overdue or _weekly_never_ran_but_skipped(state):
        return "Weekly maintenance has never completed."
    return None


def _weekly_finished_at(state: dict) -> datetime | None:
    failure = state.get("last_weekly_failure")
    failed_at = generation_maintenance.parse_utc(failure.get("failed_at")) if isinstance(failure, dict) else None
    return generation_maintenance.parse_utc(state.get("last_weekly_at")) or failed_at


def _weekly_unfinished_note(state: dict, now: datetime) -> str | None:
    """A weekly that started, outlived its scheduler limit, and left no result."""
    started = generation_maintenance.parse_utc(state.get("last_weekly_started_at"))
    if started is None or (now - started).total_seconds() <= WEEKLY_LIMIT_SECONDS:
        return None
    finished = _weekly_finished_at(state)
    if finished is not None and finished >= started:
        return None
    return f"The last weekly started at {state['last_weekly_started_at']} and did not finish."


def _weekly_never_ran_but_skipped(state: dict) -> bool:
    """A weekly with no run on record but a skip was due and did not happen (audit 2026-09-26 B-20)."""
    return state.get("last_weekly_at") is None and isinstance(state.get("last_weekly_skip"), dict)


def _weekly_skip_note(state: dict) -> str:
    """Why the last weekly did not run, when it recorded a skip."""
    skip = state.get("last_weekly_skip")
    if not isinstance(skip, dict):
        return ""
    return f" The last weekly was skipped at {skip.get('skipped_at')}: {skip.get('reason')}."


def _weekly_is_stale(ran_at: datetime | None, now: datetime) -> bool:
    """Stale only once it has run: the first Sunday of an install is not late."""
    if ran_at is None:
        return False
    return (now - ran_at).total_seconds() > WEEKLY_FRESH_SECONDS


def _with_weekly(nightly: dict, weekly: tuple[str, str] | None) -> dict:
    """The scheduler finding: the worse of the two passes, both messages named."""
    if weekly is None:
        return nightly
    status, message = weekly
    worse = max(nightly["status"], status, key=lambda name: _SEVERITY.get(name, 0))
    combined = message if nightly["status"] == "ok" else f"{nightly['message']} {message}"
    return generation_maintenance.result("scheduler", worse, combined, nightly["details"])


# A day plus slack for a run that starts late or takes long. Freshness of a
# scheduled job is an interval, not a calendar boundary: the nightly runs at
# 03:00, so comparing against "today" called a healthy timer stale every night
# from midnight until it ran. See
# docs/research/2026-08-22-scheduled-job-freshness.md.
NIGHTLY_FRESH_SECONDS = 26 * 3600


def _nightly_is_current(state: dict, status: str, last_date: str, now: datetime) -> bool:
    if status not in {"ok", "success"}:
        return False
    ran_at = generation_maintenance.parse_utc(state.get("last_nightly_at"))
    if ran_at is None:
        # State written before the timestamp existed keeps the day rule, so an
        # upgraded vault does not become noisier for lacking a new field.
        return last_date == now.date().isoformat()
    return (now - ran_at).total_seconds() <= NIGHTLY_FRESH_SECONDS


def _nightly_freshness_result(
    state: dict, status: object, last_date: str, now: datetime, details: dict
) -> dict:
    """Whether a recorded, non-failed nightly run is still current."""
    if state.get("nightly_deferred_compile"):
        return generation_maintenance.result(
            "scheduler", "degraded",
            "Compile-dependent nightly maintenance was deferred; completion is not verified.",
            details,
        )
    if _nightly_is_current(state, status, last_date, now):
        return generation_maintenance.result("scheduler", "ok", "Nightly maintenance is current.", details)
    return generation_maintenance.result("scheduler", "degraded", stale_nightly_message(state), details)


def stale_nightly_message(state: dict) -> str:
    """A pass that ran and skipped says so, with its reason (audit OPS-23)."""
    skip = _fresh_skip(state)
    if skip is None:
        return "Nightly maintenance is stale."
    reason = skip.get("reason") or "unknown"
    when = _skip_moment(skip)[:10]
    return f"Nightly maintenance is stale; the last pass skipped: {reason} ({when})."


def _fresh_skip(state: dict) -> dict | None:
    """The recorded skip when it is newer than the last recorded run."""
    skip = state.get("last_nightly_skip")
    if not isinstance(skip, dict):
        return None
    return skip if skip_is_newer_than_run(state, skip) else None


def _skip_moment(skip: dict) -> str:
    return str(skip.get("skipped_at") or skip.get("date") or "")


def skip_is_newer_than_run(state: dict, skip: dict) -> bool:
    """Compared as instants: an older state wrote the skip in local time, the run in UTC."""
    skipped = _state_instant(_skip_moment(skip))
    if skipped is None:
        return False
    ran = _state_instant(str(state.get("last_nightly_at") or state.get("last_nightly_date") or ""))
    return ran is None or skipped >= ran


def _state_instant(text: str) -> datetime | None:
    """An ISO instant or date from the state; one without an offset is local time."""
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.astimezone()


def nightly_result(
    state: dict, now: datetime, details: dict, installed: datetime | None = None
) -> dict:
    status = state.get("last_nightly_status")
    last_date = str(state.get("last_nightly_date", ""))[:10]
    problem = _nightly_recorded_problem(status, details)
    if problem is not None:
        return problem
    if not status or not last_date:
        return _never_ran_result(installed, now, details)
    return _nightly_freshness_result(state, status, last_date, now, details)


def _nightly_recorded_problem(status, details):
    problems = {
        "failed": ("error", "Last nightly maintenance failed."),
        "deferred": ("degraded", "Nightly maintenance was deferred; completion is not verified."),
    }
    problem = problems.get(status)
    if problem is None:
        return None
    return generation_maintenance.result("scheduler", problem[0], problem[1], details)


# The first nightly after an install is due within one schedule period (the pass
# runs daily) plus the longest a pass may run before its unit stops it; derived,
# so it follows `SCHEDULER_LIMIT_HOURS`. `NIGHTLY_FRESH_SECONDS` measures the gap
# between two completions and is too short here: an install at 03:30 waits 23.5 h
# for the next 03:00 and may then run 4 h. See
# docs/research/2026-09-27-what-is-not-yet-due-is-not-a-warning.md.
FIRST_NIGHTLY_DUE_SECONDS = 24 * 3600 + SCHEDULER_LIMIT_HOURS["nightly"] * 3600


def _never_ran_result(installed: datetime | None, now: datetime, details: dict) -> dict:
    """No nightly on record: pending until the first one is due, then a finding.

    Without an install record nothing says when the clock started, so the old
    answer stands.
    """
    if installed is None:
        return generation_maintenance.result("scheduler", "skipped", "Nightly maintenance status is unknown.", details)
    due_by = generation_maintenance.as_utc(installed) + timedelta(seconds=FIRST_NIGHTLY_DUE_SECONDS)
    details["first_nightly_due_by"] = due_by.isoformat()
    if generation_maintenance.as_utc(now) <= due_by:
        message = f"Nightly maintenance has not run yet; the first pass is due by {due_by:%Y-%m-%d %H:%M} UTC."
        return generation_maintenance.result("scheduler", "ok", message, details)
    message = f"Nightly maintenance has never run since the install at {generation_maintenance.as_utc(installed):%Y-%m-%d %H:%M} UTC."
    return generation_maintenance.result("scheduler", "degraded", message, details)


def _mcp_package_available() -> bool:
    try:
        return importlib.util.find_spec("mcp") is not None
    except (ImportError, ValueError):
        return False


def _mcp_message(source: bool) -> str:
    if source:
        return "MCP source is available; package capability was detected."
    return "MCP server source is missing."


def mcp_check(root: Path) -> dict:
    source = (root / "scripts" / "mcp_server.py").is_file()
    package = _mcp_package_available()
    details = {
        "source": ok_or_error(source),
        "package": "ok" if package else "skipped",
        "capability": "available" if package else "optional dependency not installed",
        "core_capture_required": False,
    }
    return generation_maintenance.result("mcp", ok_or_error(source), _mcp_message(source), details)


def _readable_config(path: Path) -> bool:
    kind, info = generation_maintenance.safe_kind(path, path.parent)
    if kind != "regular" or info is None:
        return False
    return info.st_size <= MAX_CONFIG_BYTES


def contains_markers(path: Path, markers: tuple[str, ...]) -> bool:
    if not _readable_config(path):
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    lowered = text.lower()
    return all(marker.lower() in lowered for marker in markers)


def _codex_shim_command(arguments: list[str]) -> list[str] | None:
    shim = shutil.which("codex.cmd")
    command_processor = os.environ.get("ComSpec")
    if not shim or not command_processor:
        return None
    command_line = subprocess.list2cmdline([shim, *arguments])
    return [command_processor, "/d", "/s", "/c", command_line]


def _windows_codex_command(arguments: list[str]) -> list[str] | None:
    executable = shutil.which("codex.exe")
    if executable:
        return [executable, *arguments]
    return _codex_shim_command(arguments)


def codex_app_server_command(*, platform: str = os.name) -> list[str] | None:
    arguments = ["app-server", "--listen", "stdio://"]
    if platform == "nt":
        return _windows_codex_command(arguments)
    executable = shutil.which("codex")
    return [executable, *arguments] if executable else None


PROBE_INCOMPLETE = object()
