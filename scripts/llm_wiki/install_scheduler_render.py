"""Scheduler backend choice and the launchd and systemd definitions it renders."""

from __future__ import annotations

import plistlib
from pathlib import Path

_NATIVE_SCHEDULERS = {"win32": "task_scheduler", "darwin": "launchd"}


def _native_scheduler_backend(platform: str, systemd_available: bool) -> str:
    native = _NATIVE_SCHEDULERS.get(platform)
    if native is not None:
        return native
    return _linux_scheduler_backend(platform, systemd_available)


def _linux_scheduler_backend(platform: str, systemd_available: bool) -> str:
    if platform != "linux":
        raise ValueError("unsupported scheduler platform")
    if not systemd_available:
        raise ValueError("systemd user manager is unavailable; select cron explicitly")
    return "systemd_user"


def select_scheduler_backend(platform: str, requested: str, systemd_available: bool) -> str:
    if requested == "cron":
        return _cron_scheduler_backend(platform)
    if requested != "native":
        raise ValueError("scheduler selection must be native or cron")
    return _native_scheduler_backend(platform, systemd_available)


def _cron_scheduler_backend(platform: str) -> str:
    if platform == "win32":
        raise ValueError("cron fallback is unavailable on Windows")
    return "cron"


def installer_stable_uv_path(uv_path: Path) -> Path:
    from installer_config import stable_uv_path

    return stable_uv_path(uv_path)


def scheduled_arguments(root: Path, uv_path: Path, kind: str) -> list[str]:
    return [
        str(installer_stable_uv_path(uv_path)),
        "run",
        "--locked",
        "--no-sync",
        "--directory",
        str(Path(root).resolve()),
        "python",
        f"scripts/scheduled_{kind}.py",
    ]


def provider_items(root: Path) -> tuple[tuple[str, str], ...]:
    """The provider variables the installing process carries, as ordered pairs (#22)."""
    from integration_hook_config import provider_environment

    return tuple(sorted(provider_environment(root).items()))


def _launchd_calendar(kind: str) -> dict[str, int]:
    if kind == "nightly":
        return {"Hour": 3, "Minute": 0}
    return {"Hour": 4, "Minute": 0, "Weekday": 0}


def _launchd_path(uv_path: Path) -> str:
    from installer_config import scheduled_path

    return scheduled_path(uv_path, _LAUNCHD_DEFAULT_PATH)


def launchd_job(root: Path, state_root: Path, uv_path: Path, kind: str) -> bytes:
    label = f"io.github.ekgardt.llm-wiki.{kind}"
    log_path = Path(state_root).resolve() / "logs" / f"scheduled-{kind}.log"
    value = {
        "EnvironmentVariables": {
            "LLM_WIKI_ROOT": str(Path(root).resolve()),
            "LLM_WIKI_STATE_ROOT": str(Path(state_root).resolve()),
            **dict(provider_items(root)),
            "PATH": _launchd_path(uv_path),
        },
        "Label": label,
        "ProcessType": "Background",
        "ProgramArguments": scheduled_arguments(root, uv_path, kind),
        "StandardErrorPath": str(log_path),
        "StandardOutPath": str(log_path),
        "StartCalendarInterval": _launchd_calendar(kind),
        "WorkingDirectory": str(Path(root).resolve()),
    }
    return plistlib.dumps(value, fmt=plistlib.FMT_XML, sort_keys=True)


def render_launchd_definitions(root: Path, state_root: Path, uv_path: Path) -> dict[str, bytes]:
    return {
        f"io.github.ekgardt.llm-wiki.{kind}.plist": launchd_job(root, state_root, uv_path, kind)
        for kind in ("nightly", "weekly")
    }


def _systemd_quote(value: str) -> str:
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise ValueError("systemd argument contains a control character")
    escaped = value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _systemd_literal(value: str) -> str:
    """Render a single-value setting such as ``WorkingDirectory=``.

    systemd applies command-line quoting only to settings that take an argument
    vector (``ExecStart=``) or a quoted assignment list (``Environment=``).
    Single-path settings consume the rest of the line verbatim, so a quoted path
    is rejected with "path is not absolute". Only ``%`` needs escaping there.
    """
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise ValueError("systemd value contains a control character")
    return value.replace("%", "%%")


# The PATH a systemd user manager hands its services on Linux. It contains no
# per-user directory, which is where the LLM provider CLIs are installed.
_SYSTEMD_USER_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


# The PATH launchd gives a job whose EnvironmentVariables set none (launchd.plist(5)).
_LAUNCHD_DEFAULT_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def _scheduled_path(uv_path: Path) -> str:
    """Let a scheduled run see the tools installed beside uv.

    A user service inherits the manager's PATH, which reaches no per-user
    directory. The provider CLIs live in one, next to the uv this unit already
    calls by absolute path, so without this a scheduled compile probes every
    provider, finds none, and fails where the same command from a login shell
    succeeds.
    """
    from installer_config import scheduled_path

    return scheduled_path(uv_path, _SYSTEMD_USER_PATH)


# How long a scheduler lets each pass run: above each pass's own worst case in auto
# provider mode, which counts the checkout update and the whole provider order one
# model call may walk (`worst_case_seconds` of each; tests hold the order, and no
# comment carries a measured figure that drifts). One table for every scheduler; the
# systemd units once said 3 h and 5 h while the Windows tasks said 4 h and 6 h.
# Research: docs/research/2026-09-18-a-pass-that-knows-how-long-it-can-be.md,
# docs/research/2026-09-25-the-scheduler-says-what-the-night-could-not-do.md
# The default nightly now includes the compiler's own generation refresh:
# 15 092 seconds in auto mode exceeds four hours; configured budgets still win.
SCHEDULER_LIMIT_HOURS = {"nightly": 5, "weekly": 6}


# A oneshot service has no start timeout by default, so a hung pass would hold its
# lease forever. See `docs/research/2026-09-14-ci-and-scheduler-gaps.md`.
def scheduler_limit_hours(root: Path) -> dict[str, int]:
    """Retain the installed floors and outlast each configured pass plus startup."""
    import math

    import nightly_budget
    import weekly_budget

    bounds = {
        "nightly": nightly_budget.worst_case_seconds(root),
        "weekly": weekly_budget.worst_case_seconds(),
    }
    return {
        kind: max(SCHEDULER_LIMIT_HOURS[kind], math.ceil(
            (seconds + nightly_budget.STEP_START_MARGIN_SECONDS) / 3600
        ))
        for kind, seconds in bounds.items()
    }


def systemd_service(root: Path, state_root: Path, uv_path: Path, kind: str) -> bytes:
    arguments = " ".join(
        _systemd_quote(argument) for argument in scheduled_arguments(root, uv_path, kind)
    )
    lines = (
        "[Unit]",
        f"Description=LLM Wiki {kind} maintenance",
        "",
        "[Service]",
        "Type=oneshot",
        f"TimeoutStartSec={scheduler_limit_hours(root)[kind]}h",
        f"Environment={_systemd_quote(f'LLM_WIKI_ROOT={Path(root).resolve()}')}",
        f"Environment={_systemd_quote(f'LLM_WIKI_STATE_ROOT={Path(state_root).resolve()}')}",
        *(f"Environment={_systemd_quote(f'{key}={value}')}" for key, value in provider_items(root)),
        f"Environment={_systemd_quote(f'PATH={_scheduled_path(uv_path)}')}",
        f"WorkingDirectory={_systemd_literal(str(Path(root).resolve()))}",
        f"ExecStart={arguments}",
        "",
    )
    return "\n".join(lines).encode("utf-8")


def _systemd_timer(kind: str) -> bytes:
    calendar = _systemd_calendar(kind)
    lines = (
        "[Unit]",
        f"Description=Schedule LLM Wiki {kind} maintenance",
        "",
        "[Timer]",
        f"OnCalendar={calendar}",
        "Persistent=true",
        f"Unit=llm-wiki-{kind}.service",
        "",
        "[Install]",
        "WantedBy=timers.target",
        "",
    )
    return "\n".join(lines).encode("utf-8")


def _systemd_calendar(kind: str) -> str:
    if kind == "nightly":
        return "*-*-* 03:00:00"
    return "Sun *-*-* 04:00:00"


def render_systemd_definitions(root: Path, state_root: Path, uv_path: Path) -> dict[str, bytes]:
    definitions: dict[str, bytes] = {}
    for kind in ("nightly", "weekly"):
        definitions[f"llm-wiki-{kind}.service"] = systemd_service(root, state_root, uv_path, kind)
        definitions[f"llm-wiki-{kind}.timer"] = _systemd_timer(kind)
    return definitions
