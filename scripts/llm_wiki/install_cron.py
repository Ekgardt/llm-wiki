"""The crontab block the installer owns."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from install_state import (
    MAX_PREIMAGE_BYTES,
    InstallControlError,
    ManagedResource,
)
from llm_wiki.install_profile import after_profile_block, profile_separator
from llm_wiki.install_records import CRON_END, CRON_START, CommandRunner, default_command_runner
from llm_wiki.install_scheduler_render import installer_stable_uv_path


def render_cron_block(root: Path, state_root: Path, uv_path: Path) -> bytes:
    from installer_config import build_cron_command

    nightly = build_cron_command(
        root=Path(root).resolve(),
        state_root=Path(state_root).resolve(),
        uv_path=installer_stable_uv_path(uv_path),
        kind="nightly",
        log_path=Path(state_root).resolve() / "logs" / "cron-nightly.log",
    )
    weekly = build_cron_command(
        root=Path(root).resolve(),
        state_root=Path(state_root).resolve(),
        uv_path=installer_stable_uv_path(uv_path),
        kind="weekly",
        log_path=Path(state_root).resolve() / "logs" / "cron-weekly.log",
    )
    lines = (
        CRON_START.decode(),
        f"0 3 * * * {nightly}",
        f"0 4 * * 0 {weekly}",
        CRON_END.decode(),
    )
    return "\n".join(lines).encode("utf-8")


def _read_crontab(runner: CommandRunner, crontab: str) -> bytes | None:
    exit_code, output = runner((crontab, "-l"), None)
    if exit_code == 1:
        return None
    return _crontab_output(exit_code, output)


def _crontab_output(exit_code: int, output: bytes) -> bytes:
    if exit_code != 0:
        raise InstallControlError("install_crontab_read_failed")
    if len(output) > MAX_PREIMAGE_BYTES:
        raise InstallControlError("install_crontab_oversized")
    return output


def _cron_bounds(table: bytes) -> tuple[int, int] | None:
    start_count = table.count(CRON_START)
    end_count = table.count(CRON_END)
    if start_count == end_count == 0:
        return None
    if start_count != 1 or end_count != 1:
        raise InstallControlError("install_cron_ownership_invalid")
    start = table.index(CRON_START)
    end = table.index(CRON_END, start) + len(CRON_END)
    return start, end


def _extract_cron_block(table: bytes | None) -> bytes | None:
    if table is None:
        return None
    bounds = _cron_bounds(table)
    if bounds is None:
        return None
    return _owned_cron_block(table, *bounds)


def _owned_cron_block(table: bytes, start: int, end: int) -> bytes:
    starts_on_line = (b"\n" + table[:start]).endswith(b"\n")
    ends_on_line = table[end : end + 1] in {b"", b"\n", b"\r"}
    if not starts_on_line or not ends_on_line:
        raise InstallControlError("install_cron_ownership_invalid")
    return table[start:end]


def _cron_separator(table: bytes | None) -> str:
    if table and not table.endswith(b"\n"):
        return "newline"
    return "none"


def _remove_cron_block(table: bytes, separator: str) -> bytes:
    start, end = _cron_bounds(table) or (0, 0)
    if separator == "newline" and start > 0:
        start -= 1
    return table[:start] + table[after_profile_block(table, end) :]


def _replace_cron_block(table: bytes, updated: bytes) -> bytes:
    start, end = _cron_bounds(table) or (0, 0)
    return table[:start] + updated + table[end:]


def _append_cron_block(table: bytes, updated: bytes) -> bytes:
    return table + profile_separator(table) + updated + b"\n"


def _cron_output(
    table: bytes | None, current: bytes | None, updated: bytes | None, separator: str
) -> bytes:
    content = table or b""
    if current is not None:
        return _rewritten_cron_block(content, updated, separator)
    if updated is None:
        return content
    return _append_cron_block(content, updated)


def _rewritten_cron_block(content: bytes, updated: bytes | None, separator: str) -> bytes:
    """The table with its existing managed block replaced, or removed when `updated` is None."""
    if updated is None:
        return _remove_cron_block(content, separator)
    return _replace_cron_block(content, updated)


def _write_crontab(
    value: bytes | None,
    *,
    runner: CommandRunner,
    crontab: str,
    metadata: Mapping[str, object],
) -> None:
    table = _read_crontab(runner, crontab)
    current = _extract_cron_block(table)
    separator = str(metadata.get("cron_insert_separator", "none"))
    output = _cron_output(table, current, value, separator)
    if not output and not bool(metadata.get("cron_existed", True)):
        exit_code, _ignored = runner((crontab, "-r"), None)
    else:
        exit_code, _ignored = runner((crontab, "-"), output)
    if exit_code != 0:
        raise InstallControlError("install_crontab_write_failed")


def cron_scheduler_resource(
    *,
    root: Path,
    state_root: Path,
    uv_path: Path,
    runner: CommandRunner = default_command_runner,
    crontab: str = "crontab",
    ownership_metadata: Mapping[str, object] | None = None,
) -> ManagedResource:
    desired = render_cron_block(root, state_root, uv_path)
    table = _read_crontab(runner, crontab)
    metadata = dict(ownership_metadata or {})
    if ownership_metadata is None:
        metadata = {
            "cron_existed": table is not None,
            "cron_insert_separator": _cron_separator(table),
            "definition_set": "cron",
            "uv_path": str(installer_stable_uv_path(uv_path)),
        }
    return ManagedResource(
        resource_id="cron-user-maintenance",
        kind="cron_scheduler",
        locator="cron://current-user",
        desired=desired,
        read_owned=lambda: _extract_cron_block(_read_crontab(runner, crontab)),
        write_owned=lambda value: _write_crontab(
            value, runner=runner, crontab=crontab, metadata=metadata
        ),
        recognizes=lambda current: current == desired,
        metadata=metadata,
        definitions={"scheduler/cron/crontab.block": desired},
        adopt_as_absent=True,
    )
