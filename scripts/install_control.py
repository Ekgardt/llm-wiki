#!/usr/bin/env python3
"""Bounded install ownership and recovery control plane."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import plistlib
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
from pathlib import Path, PurePosixPath

from reliable_memory import canonical_json_bytes, fsync_directory, validate_schema

MAX_RECORD_BYTES = 1024 * 1024
MAX_PREIMAGE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_PREIMAGE_BYTES = 16 * 1024 * 1024
MAX_RESOURCES = 32
_BACKENDS = {"cron", "launchd", "systemd_user", "task_scheduler"}
PROFILE_START = b"# >>> LLM-Wiki installer >>>"
PROFILE_END = b"# <<< LLM-Wiki installer <<<"
CRON_START = b"# LLM-Wiki-cron-start"
CRON_END = b"# LLM-Wiki-cron-end"
_SCHEMA_ROOT = Path(__file__).resolve().parent / "schemas"
_MANIFEST_SCHEMA = _SCHEMA_ROOT / "install-manifest-v1.json"
_TRANSACTION_SCHEMA = _SCHEMA_ROOT / "install-transaction-v1.json"
_MANIFEST_V2_SCHEMA = _SCHEMA_ROOT / "install-manifest-v2.json"
_TRANSACTION_V2_SCHEMA = _SCHEMA_ROOT / "install-transaction-v2.json"
_INSTALL_SCHEMAS = {
    "install-manifest/v1": _MANIFEST_SCHEMA,
    "install-manifest/v2": _MANIFEST_V2_SCHEMA,
    "install-transaction/v1": _TRANSACTION_SCHEMA,
    "install-transaction/v2": _TRANSACTION_V2_SCHEMA,
}


class InstallControlError(RuntimeError):
    """Stable fail-closed install control-plane error."""


@dataclass(frozen=True, slots=True)
class ManagedResource:
    resource_id: str
    kind: str
    locator: str
    desired: bytes
    read_owned: Callable[[], bytes | None]
    write_owned: Callable[[bytes | None], None]
    recognizes: Callable[[bytes], bool]
    read_projections: Callable[[Sequence[bytes]], bytes | None] | None = None
    write_projection: Callable[[bytes | None, bytes | None, Mapping[str, object]], None] | None = (
        None
    )
    recover_legacy_projection: Callable[[Mapping[str, object]], tuple[bytes, bytes]] | None = None
    supports_v2_recovery: bool = True
    metadata: Mapping[str, object] = field(default_factory=dict)
    definitions: Mapping[str, bytes] = field(default_factory=dict, repr=False, compare=False)
    adopt_as_absent: bool = False


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_write(path: Path, value: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _restore_external_owner(path: Path, owner: tuple[int, int] | None) -> None:
    if owner is None or not hasattr(os, "chown"):
        return
    os.chown(path, *owner)


def _atomic_external_write(
    path: Path,
    value: bytes,
    mode: int,
    owner: tuple[int, int] | None = None,
) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        _restore_external_owner(temporary, owner)
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _write_record(path: Path, value: Mapping[str, object]) -> None:
    encoded = canonical_json_bytes(dict(value))
    if len(encoded) > MAX_RECORD_BYTES:
        raise InstallControlError("install_record_too_large")
    _atomic_write(path, encoded)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def _read_state_bytes(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise InstallControlError("install_state_unsafe")
    if path.stat().st_size > MAX_RECORD_BYTES:
        raise InstallControlError("install_state_oversized")
    return path.read_bytes()


def _strict_json_object(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise InstallControlError("install_state_invalid_json") from exc
    if not isinstance(value, dict) or canonical_json_bytes(value) != raw:
        raise InstallControlError("install_state_noncanonical")
    return value


def _read_record(path: Path, schema: Path) -> dict[str, object]:
    value = _strict_json_object(_read_state_bytes(path))
    try:
        validate_schema(value, schema)
    except ValueError as exc:
        raise InstallControlError("install_state_schema_invalid") from exc
    return value


def _optional_record(path: Path, schema: Path) -> dict[str, object] | None:
    if not path.exists() and not path.is_symlink():
        return None
    return _read_record(path, schema)


def _install_schema(value: Mapping[str, object], record_type: str) -> Path:
    schema_name = value.get("schema")
    if not isinstance(schema_name, str) or not schema_name.startswith(record_type):
        raise InstallControlError("install_state_schema_invalid")
    try:
        return _INSTALL_SCHEMAS[schema_name]
    except KeyError as exc:
        raise InstallControlError("install_state_schema_invalid") from exc


def _read_install_record(path: Path, record_type: str) -> dict[str, object]:
    value = _strict_json_object(_read_state_bytes(path))
    try:
        validate_schema(value, _install_schema(value, record_type))
    except ValueError as exc:
        raise InstallControlError("install_state_schema_invalid") from exc
    return value


def _optional_install_record(path: Path, record_type: str) -> dict[str, object] | None:
    if not path.exists() and not path.is_symlink():
        return None
    return _read_install_record(path, record_type)


def _try_lock(descriptor: int) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(descriptor: int) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


def _acquire_lock(descriptor: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            _try_lock(descriptor)
            return
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise InstallControlError("install_lock_timeout") from exc
            time.sleep(0.05)


@contextmanager
def _install_lock(path: Path, timeout: float) -> Iterator[None]:
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    acquired = False
    try:
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"0")
            os.fsync(descriptor)
        os.chmod(path, 0o600)
        _acquire_lock(descriptor, timeout)
        acquired = True
        yield
    finally:
        if acquired:
            _unlock(descriptor)
        os.close(descriptor)


def _prepare_install_root(state_root: Path) -> Path:
    install_root = Path(state_root) / "run" / "install"
    install_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(install_root, 0o700)
    for name in ("preimages", "scheduler"):
        directory = install_root / name
        directory.mkdir(exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
    fsync_directory(install_root.parent)
    fsync_directory(install_root)
    return install_root


def _profile_path_exists(profile: Path) -> bool:
    if profile.is_symlink():
        raise InstallControlError("install_profile_symlink")
    if not profile.exists():
        return False
    if not profile.is_file():
        raise InstallControlError("install_profile_invalid")
    return True


def _read_profile(profile: Path) -> bytes:
    if not _profile_path_exists(profile):
        return b""
    if profile.stat().st_size > MAX_PREIMAGE_BYTES:
        raise InstallControlError("install_profile_invalid")
    value = profile.read_bytes()
    if len(value) > MAX_PREIMAGE_BYTES:
        raise InstallControlError("install_profile_invalid")
    return value


def _marker_bounds(content: bytes) -> tuple[int, int] | None:
    start_count = content.count(PROFILE_START)
    end_count = content.count(PROFILE_END)
    if start_count == end_count == 0:
        return None
    if start_count != 1 or end_count != 1:
        raise InstallControlError("install_profile_ownership_invalid")
    start = content.index(PROFILE_START)
    end = content.index(PROFILE_END, start) + len(PROFILE_END)
    return start, end


def _extract_profile_block(content: bytes) -> bytes | None:
    bounds = _marker_bounds(content)
    if bounds is None:
        return None
    start, end = bounds
    starts_on_line = (b"\n" + content[:start]).endswith(b"\n")
    ends_on_line = content[end : end + 1] in {b"", b"\n", b"\r"}
    if not starts_on_line or not ends_on_line:
        raise InstallControlError("install_profile_ownership_invalid")
    return content[start:end]


def _absolute_export(line: str, name: str) -> bool:
    prefix = f"export {name}="
    if not line.startswith(prefix):
        return False
    try:
        values = shlex.split(line[len(prefix) :])
    except ValueError:
        return False
    return len(values) == 1 and Path(values[0]).is_absolute()


def _decode_profile_lines(block: bytes) -> list[str] | None:
    try:
        return block.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return None


def _profile_markers_match(lines: Sequence[str]) -> bool:
    if len(lines) != 4:
        return False
    return lines[0].encode() == PROFILE_START and lines[3].encode() == PROFILE_END


def _recognized_profile(block: bytes) -> bool:
    lines = _decode_profile_lines(block)
    if lines is None or not _profile_markers_match(lines):
        return False
    return _absolute_export(lines[1], "LLM_WIKI_ROOT") and _absolute_export(
        lines[2], "LLM_WIKI_STATE_ROOT"
    )


def _replace_profile_block(content: bytes, updated: bytes) -> bytes:
    start, end = _marker_bounds(content) or (0, 0)
    return content[:start] + updated + content[end:]


def _profile_separator(content: bytes) -> bytes:
    if not content or content.endswith(b"\n"):
        return b""
    return b"\n"


def _after_profile_block(content: bytes, end: int) -> int:
    if content[end : end + 2] == b"\r\n":
        return end + 2
    if content[end : end + 1] == b"\n":
        return end + 1
    return end


def _remove_profile_block(content: bytes, separator: str) -> bytes:
    start, end = _marker_bounds(content) or (0, 0)
    if separator == "newline" and start > 0:
        start -= 1
    return content[:start] + content[_after_profile_block(content, end) :]


def _profile_output(
    content: bytes,
    current: bytes | None,
    updated: bytes | None,
    separator: str,
) -> bytes:
    if current is not None:
        if updated is None:
            return _remove_profile_block(content, separator)
        return _replace_profile_block(content, updated)
    if updated is None:
        return content
    return content + _profile_separator(content) + updated + b"\n"


def _restore_absent_profile(profile: Path, output: bytes, existed: bool) -> bool:
    if existed or output:
        return False
    profile.unlink(missing_ok=True)
    fsync_directory(profile.parent)
    return True


def _profile_file_metadata(profile: Path) -> tuple[int, tuple[int, int] | None]:
    try:
        metadata = profile.stat()
    except FileNotFoundError:
        return 0o600, None
    owner = (metadata.st_uid, metadata.st_gid)
    return stat.S_IMODE(metadata.st_mode), owner


def _write_profile(profile: Path, updated: bytes | None, metadata: Mapping[str, object]) -> None:
    content = _read_profile(profile)
    current = _extract_profile_block(content)
    separator = str(metadata.get("profile_insert_separator", "none"))
    output = _profile_output(content, current, updated, separator)
    if output == content:
        return
    existed = bool(metadata.get("profile_existed", True))
    if updated is None and _restore_absent_profile(profile, output, existed):
        return
    mode, owner = _profile_file_metadata(profile)
    profile.parent.mkdir(parents=True, exist_ok=True)
    _atomic_external_write(profile, output, mode, owner)


def _profile_insert_separator(content: bytes) -> str:
    if content and not content.endswith(b"\n"):
        return "newline"
    return "none"


def profile_resource(
    profile: Path,
    vault_root: Path,
    state_root: Path,
    ownership_metadata: Mapping[str, object] | None = None,
) -> ManagedResource:
    profile = Path(profile).resolve(strict=False)
    content = _read_profile(profile)
    separator = _profile_insert_separator(content)
    metadata = dict(ownership_metadata or {})
    if ownership_metadata is None:
        metadata = {
            "profile_existed": profile.exists(),
            "profile_insert_separator": separator,
        }
    desired = b"\n".join(
        (
            PROFILE_START,
            f"export LLM_WIKI_ROOT={shlex.quote(str(Path(vault_root).resolve()))}".encode(),
            f"export LLM_WIKI_STATE_ROOT={shlex.quote(str(Path(state_root).resolve()))}".encode(),
            PROFILE_END,
        )
    )
    return ManagedResource(
        resource_id="unix-profile",
        kind="profile_fragment",
        locator=str(profile),
        desired=desired,
        read_owned=lambda: _extract_profile_block(_read_profile(profile)),
        write_owned=lambda value: _write_profile(profile, value, metadata),
        recognizes=_recognized_profile,
        metadata=metadata,
        adopt_as_absent=True,
    )


def _native_scheduler_backend(platform: str, systemd_available: bool) -> str:
    if platform == "win32":
        return "task_scheduler"
    if platform == "darwin":
        return "launchd"
    if platform != "linux":
        raise ValueError("unsupported scheduler platform")
    if not systemd_available:
        raise ValueError("systemd user manager is unavailable; select cron explicitly")
    return "systemd_user"


def select_scheduler_backend(platform: str, requested: str, systemd_available: bool) -> str:
    if requested == "cron":
        if platform == "win32":
            raise ValueError("cron fallback is unavailable on Windows")
        return "cron"
    if requested != "native":
        raise ValueError("scheduler selection must be native or cron")
    return _native_scheduler_backend(platform, systemd_available)


def _scheduled_arguments(root: Path, uv_path: Path, kind: str) -> list[str]:
    return [
        str(Path(uv_path).resolve()),
        "run",
        "--locked",
        "--no-sync",
        "--directory",
        str(Path(root).resolve()),
        "python",
        f"scripts/scheduled_{kind}.py",
    ]


def _launchd_calendar(kind: str) -> dict[str, int]:
    if kind == "nightly":
        return {"Hour": 3, "Minute": 0}
    return {"Hour": 4, "Minute": 0, "Weekday": 0}


def _launchd_job(root: Path, state_root: Path, uv_path: Path, kind: str) -> bytes:
    label = f"io.github.ekgardt.llm-wiki.{kind}"
    log_path = Path(state_root).resolve() / "logs" / f"scheduled-{kind}.log"
    value = {
        "EnvironmentVariables": {
            "LLM_WIKI_ROOT": str(Path(root).resolve()),
            "LLM_WIKI_STATE_ROOT": str(Path(state_root).resolve()),
        },
        "Label": label,
        "ProcessType": "Background",
        "ProgramArguments": _scheduled_arguments(root, uv_path, kind),
        "StandardErrorPath": str(log_path),
        "StandardOutPath": str(log_path),
        "StartCalendarInterval": _launchd_calendar(kind),
        "WorkingDirectory": str(Path(root).resolve()),
    }
    return plistlib.dumps(value, fmt=plistlib.FMT_XML, sort_keys=True)


def render_launchd_definitions(root: Path, state_root: Path, uv_path: Path) -> dict[str, bytes]:
    return {
        f"io.github.ekgardt.llm-wiki.{kind}.plist": _launchd_job(root, state_root, uv_path, kind)
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


def _scheduled_path(uv_path: Path) -> str:
    """Let a scheduled run see the tools installed beside uv.

    A user service inherits the manager's PATH, which reaches no per-user
    directory. The provider CLIs live in one, next to the uv this unit already
    calls by absolute path, so without this a scheduled compile probes every
    provider, finds none, and fails where the same command from a login shell
    succeeds.
    """
    return f"{Path(uv_path).resolve().parent}:{_SYSTEMD_USER_PATH}"


def _systemd_service(root: Path, state_root: Path, uv_path: Path, kind: str) -> bytes:
    arguments = " ".join(
        _systemd_quote(argument) for argument in _scheduled_arguments(root, uv_path, kind)
    )
    lines = (
        "[Unit]",
        f"Description=LLM Wiki {kind} maintenance",
        "",
        "[Service]",
        "Type=oneshot",
        f"Environment={_systemd_quote(f'LLM_WIKI_ROOT={Path(root).resolve()}')}",
        f"Environment={_systemd_quote(f'LLM_WIKI_STATE_ROOT={Path(state_root).resolve()}')}",
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
        definitions[f"llm-wiki-{kind}.service"] = _systemd_service(root, state_root, uv_path, kind)
        definitions[f"llm-wiki-{kind}.timer"] = _systemd_timer(kind)
    return definitions


def _read_managed_file(path: Path) -> bytes | None:
    if path.is_symlink():
        raise InstallControlError("install_resource_symlink")
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > MAX_PREIMAGE_BYTES:
        raise InstallControlError("install_resource_file_invalid")
    return path.read_bytes()


def _remove_managed_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise InstallControlError("install_resource_file_invalid")
    path.unlink()
    fsync_directory(path.parent)


def _write_managed_file(path: Path, value: bytes | None, mode: int) -> None:
    if value is None:
        _remove_managed_file(path)
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink():
        raise InstallControlError("install_resource_parent_symlink")
    _atomic_external_write(path, value, mode)


def file_resource(
    *,
    resource_id: str,
    kind: str,
    path: Path,
    desired: bytes,
    definition_path: str | None = None,
    mode: int = 0o600,
    adopt_as_absent: bool = True,
) -> ManagedResource:
    path = Path(path).resolve(strict=False)
    metadata: dict[str, object] = {"file_mode": mode}
    definitions: dict[str, bytes] = {}
    if definition_path is not None:
        metadata["definition_path"] = definition_path
        definitions[definition_path] = desired
    return ManagedResource(
        resource_id=resource_id,
        kind=kind,
        locator=str(path),
        desired=desired,
        read_owned=lambda: _read_managed_file(path),
        write_owned=lambda value: _write_managed_file(path, value, mode),
        recognizes=lambda current: current == desired,
        metadata=metadata,
        definitions=definitions,
        adopt_as_absent=adopt_as_absent,
    )


CommandRunner = Callable[[tuple[str, ...], bytes | None], tuple[int, bytes]]


def _default_command_runner(
    command: tuple[str, ...], input_value: bytes | None = None
) -> tuple[int, bytes]:
    if input_value is not None:
        try:
            completed = subprocess.run(
                command,
                input=input_value,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return 124, b""
        return completed.returncode, b""
    from installer_config import _bounded_process_output

    completed = _bounded_process_output(
        command,
        cwd=Path.cwd(),
        environment=os.environ.copy(),
        timeout_seconds=30.0,
        max_bytes=MAX_RECORD_BYTES,
    )
    if completed is None:
        return 124, b""
    return completed


def _try_command(runner: CommandRunner, command: tuple[str, ...]) -> None:
    """Run a teardown step whose own exit code settles nothing.

    Disabling or cleaning units the manager never registered exits nonzero,
    and that is precisely the state the teardown is trying to reach. Whether
    the teardown worked is decided by verifying the projection afterwards, so
    letting these steps stop it is what strands a half-registered scheduler.
    """
    _exit_code, output = runner(command, None)
    if len(output) > MAX_RECORD_BYTES:
        raise InstallControlError("install_scheduler_output_oversized")


def _require_command(runner: CommandRunner, command: tuple[str, ...]) -> bytes:
    exit_code, output = runner(command, None)
    if exit_code != 0:
        raise InstallControlError("install_scheduler_command_failed")
    if len(output) > MAX_RECORD_BYTES:
        raise InstallControlError("install_scheduler_output_oversized")
    return output


def _systemd_timer_state(runner: CommandRunner, systemctl: str, timer: str) -> str:
    enabled, _ = runner((systemctl, "--user", "is-enabled", timer), None)
    active, _ = runner((systemctl, "--user", "is-active", timer), None)
    if enabled == active == 0:
        return "active"
    if enabled != 0 and active != 0:
        return "absent"
    return "conflict"


def _systemd_file_state(unit_directory: Path, definitions: Mapping[str, bytes]) -> str:
    values = [_read_managed_file(unit_directory / name) for name in definitions]
    if set(values) == {None}:
        return "absent"
    expected = [definitions[name] for name in definitions]
    if values == expected:
        return "installed"
    return "conflict"


def _combined_scheduler_state(file_state: str, service_states: Sequence[str]) -> str:
    signature = (file_state, frozenset(service_states))
    if signature == ("absent", frozenset({"absent"})):
        return "absent"
    if signature == ("installed", frozenset({"active"})):
        return "installed"
    return "conflict"


def _systemd_read(
    *,
    unit_directory: Path,
    definitions: Mapping[str, bytes],
    desired: bytes,
    runner: CommandRunner,
    systemctl: str,
) -> bytes | None:
    combined = _systemd_projection_state(unit_directory, definitions, runner, systemctl)
    if combined == "absent":
        return None
    if combined == "installed":
        return desired
    return b"conflict"


def _systemd_projection_state(
    unit_directory: Path,
    definitions: Mapping[str, bytes],
    runner: CommandRunner,
    systemctl: str,
) -> str:
    timers = ("llm-wiki-nightly.timer", "llm-wiki-weekly.timer")
    file_state = _systemd_file_state(unit_directory, definitions)
    service_states = [_systemd_timer_state(runner, systemctl, timer) for timer in timers]
    return _combined_scheduler_state(file_state, service_states)


def _write_systemd_files(unit_directory: Path, definitions: Mapping[str, bytes]) -> None:
    for name, value in definitions.items():
        _write_managed_file(unit_directory / name, value, 0o600)


def _remove_systemd_files(unit_directory: Path, definitions: Mapping[str, bytes]) -> None:
    for name, expected in definitions.items():
        path = unit_directory / name
        current = _read_managed_file(path)
        if current is None:
            continue
        if current != expected:
            raise InstallControlError("install_scheduler_file_drift")
        _remove_managed_file(path)


def _install_systemd(
    unit_directory: Path,
    definitions: Mapping[str, bytes],
    runner: CommandRunner,
    systemctl: str,
) -> None:
    _write_systemd_files(unit_directory, definitions)
    _require_command(runner, (systemctl, "--user", "daemon-reload"))
    _require_command(
        runner,
        (
            systemctl,
            "--user",
            "enable",
            "--now",
            "llm-wiki-nightly.timer",
            "llm-wiki-weekly.timer",
        ),
    )


def _uninstall_systemd(
    unit_directory: Path,
    definitions: Mapping[str, bytes],
    runner: CommandRunner,
    systemctl: str,
) -> None:
    _try_command(
        runner,
        (
            systemctl,
            "--user",
            "disable",
            "--now",
            "llm-wiki-nightly.timer",
            "llm-wiki-weekly.timer",
        ),
    )
    _try_command(
        runner,
        (
            systemctl,
            "--user",
            "clean",
            "--what=state",
            "llm-wiki-nightly.timer",
            "llm-wiki-weekly.timer",
        ),
    )
    _remove_systemd_files(unit_directory, definitions)
    _require_command(runner, (systemctl, "--user", "daemon-reload"))


def _write_systemd(
    value: bytes | None,
    *,
    unit_directory: Path,
    definitions: Mapping[str, bytes],
    runner: CommandRunner,
    systemctl: str,
) -> None:
    if value is None:
        _uninstall_systemd(unit_directory, definitions, runner, systemctl)
        return
    _install_systemd(unit_directory, definitions, runner, systemctl)


def _definition_digest(definitions: Mapping[str, bytes]) -> bytes:
    value = {
        "definitions": {name: _sha256(content) for name, content in definitions.items()},
        "state": "enabled_active",
    }
    return canonical_json_bytes(value)


_DEFINITION_BUNDLE_FORMAT = "scheduler-definitions/v1"
_SYSTEMD_DEFINITION_NAMES = (
    "llm-wiki-nightly.service",
    "llm-wiki-nightly.timer",
    "llm-wiki-weekly.service",
    "llm-wiki-weekly.timer",
)
_LAUNCHD_DEFINITION_NAMES = (
    "io.github.ekgardt.llm-wiki.nightly.plist",
    "io.github.ekgardt.llm-wiki.weekly.plist",
)


def _definition_bundle(definitions: Mapping[str, bytes]) -> bytes:
    value = {
        "definitions": {
            name: base64.b64encode(content).decode("ascii") for name, content in definitions.items()
        },
        "format": _DEFINITION_BUNDLE_FORMAT,
        "state": "enabled_active",
    }
    encoded = canonical_json_bytes(value)
    if len(encoded) > MAX_PREIMAGE_BYTES:
        raise InstallControlError("install_definition_bundle_oversized")
    return encoded


def _decode_definition_value(value: object) -> bytes:
    if not isinstance(value, str):
        raise InstallControlError("install_definition_bundle_invalid")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InstallControlError("install_definition_bundle_invalid") from exc
    if len(decoded) > MAX_PREIMAGE_BYTES:
        raise InstallControlError("install_definition_bundle_oversized")
    return decoded


def _definition_values(value: object, expected_names: Sequence[str]) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise InstallControlError("install_definition_bundle_invalid")
    if set(value) != set(expected_names):
        raise InstallControlError("install_definition_bundle_invalid")
    return value


def _require_definition_total(definitions: Mapping[str, bytes]) -> None:
    if sum(len(content) for content in definitions.values()) > MAX_PREIMAGE_BYTES:
        raise InstallControlError("install_definition_bundle_oversized")


def _decode_definition_map(value: object, expected_names: Sequence[str]) -> dict[str, bytes]:
    encoded = _definition_values(value, expected_names)
    definitions = {name: _decode_definition_value(encoded[name]) for name in expected_names}
    _require_definition_total(definitions)
    return definitions


def _require_definition_bundle_header(value: Mapping[str, object]) -> None:
    if set(value) != {"definitions", "format", "state"}:
        raise InstallControlError("install_definition_bundle_invalid")
    if value.get("format") != _DEFINITION_BUNDLE_FORMAT:
        raise InstallControlError("install_definition_bundle_invalid")
    if value.get("state") != "enabled_active":
        raise InstallControlError("install_definition_bundle_invalid")


def _decode_definition_bundle(candidate: bytes, expected_names: Sequence[str]) -> dict[str, bytes]:
    if len(candidate) > MAX_PREIMAGE_BYTES:
        raise InstallControlError("install_definition_bundle_oversized")
    value = _strict_json_object(candidate)
    _require_definition_bundle_header(value)
    definitions = _decode_definition_map(value.get("definitions"), expected_names)
    if _definition_bundle(definitions) != candidate:
        raise InstallControlError("install_definition_bundle_invalid")
    return definitions


def _require_definition_directory(directory: Path) -> None:
    if directory.is_symlink():
        raise InstallControlError("install_legacy_definition_missing")
    if not directory.is_dir():
        raise InstallControlError("install_legacy_definition_missing")


def _require_definition_entries(directory: Path, expected_names: Sequence[str]) -> None:
    entries = {entry.name for entry in directory.iterdir()}
    if entries != set(expected_names):
        raise InstallControlError("install_legacy_definition_ambiguous")


def _read_definition_values(
    directory: Path, expected_names: Sequence[str]
) -> dict[str, bytes | None]:
    return {name: _read_managed_file(directory / name) for name in expected_names}


def _require_present_definitions(values: Mapping[str, bytes | None]) -> None:
    if any(value is None for value in values.values()):
        raise InstallControlError("install_legacy_definition_missing")


def _loaded_definition_values(directory: Path, expected_names: Sequence[str]) -> dict[str, bytes]:
    values = _read_definition_values(directory, expected_names)
    _require_present_definitions(values)
    return {name: value or b"" for name, value in values.items()}


def _load_persisted_definitions(directory: Path, expected_names: Sequence[str]) -> dict[str, bytes]:
    _require_definition_directory(directory)
    _require_definition_entries(directory, expected_names)
    return _loaded_definition_values(directory, expected_names)


def _definition_candidate(
    candidate: bytes,
    expected_names: Sequence[str],
    legacy_loader: Callable[[], Mapping[str, bytes]],
) -> dict[str, bytes]:
    value = _strict_json_object(candidate)
    if value.get("format") == _DEFINITION_BUNDLE_FORMAT:
        return _decode_definition_bundle(candidate, expected_names)
    definitions = dict(legacy_loader())
    if set(definitions) != set(expected_names):
        raise InstallControlError("install_legacy_definition_ambiguous")
    if _definition_digest(definitions) != candidate:
        raise InstallControlError("install_legacy_definition_mismatch")
    return definitions


def _recover_legacy_definitions(
    snapshot: Mapping[str, object],
    expected_names: Sequence[str],
    legacy_loader: Callable[[], Mapping[str, bytes]],
) -> tuple[bytes, bytes]:
    definitions = dict(legacy_loader())
    if set(definitions) != set(expected_names):
        raise InstallControlError("install_legacy_definition_ambiguous")
    bundle = _definition_bundle(definitions)
    candidates = (_definition_digest(definitions), bundle)
    matches = [candidate for candidate in candidates if _same_snapshot(candidate, snapshot)]
    if len(matches) != 1:
        raise InstallControlError("install_legacy_definition_mismatch")
    return matches[0], bundle


def _installed_projection(states: Sequence[tuple[bytes, str]]) -> bytes | None:
    installed = [candidate for candidate, state in states if state == "installed"]
    if not installed:
        return None
    if len(installed) != 1:
        raise InstallControlError("install_scheduler_projection_ambiguous")
    return installed[0]


def _all_scheduler_states_absent(states: Sequence[tuple[bytes, str]]) -> bool:
    return bool(states) and all(state == "absent" for _candidate, state in states)


def _select_scheduler_projection(states: Sequence[tuple[bytes, str]]) -> bytes | None:
    installed = _installed_projection(states)
    if installed is not None:
        return installed
    if _all_scheduler_states_absent(states):
        return None
    raise InstallControlError("install_scheduler_projection_conflict")


def _read_scheduler_projections(
    candidates: Sequence[bytes],
    resolver: Callable[[bytes], Mapping[str, bytes]],
    state_reader: Callable[[Mapping[str, bytes]], str],
    absence_reader: Callable[[], str],
) -> bytes | None:
    unique = tuple(dict.fromkeys(candidates))
    if not unique:
        if absence_reader() == "absent":
            return None
        raise InstallControlError("install_scheduler_projection_conflict")
    states = [(candidate, state_reader(resolver(candidate))) for candidate in unique]
    return _select_scheduler_projection(states)


def _projection_candidates(value: bytes | None) -> tuple[bytes, ...]:
    if value is None:
        return ()
    return (value,)


def _require_scheduler_projection(
    reader: Callable[[Sequence[bytes]], bytes | None], expected: bytes | None
) -> None:
    if reader(_projection_candidates(expected)) != expected:
        raise InstallControlError("install_scheduler_projection_drift")


def _require_projection_before_write(
    reader: Callable[[Sequence[bytes]], bytes | None],
    expected: bytes | None,
    replacement: bytes | None,
) -> None:
    """Where the projection has to start, unless this write is a removal.

    Removal is what a rollback does, and a rollback exists for a resource that
    was left half applied: demanding a readable, single-valued starting
    projection is exactly what makes such a failure impossible to undo. The
    result is still verified after the write, so nothing is accepted unproven.
    """
    if replacement is None:
        return
    _require_scheduler_projection(reader, expected)


def _systemd_projection_writer(
    expected: bytes | None,
    replacement: bytes | None,
    *,
    resolver: Callable[[bytes], Mapping[str, bytes]],
    reader: Callable[[Sequence[bytes]], bytes | None],
    unit_directory: Path,
    runner: CommandRunner,
    systemctl: str,
) -> None:
    _require_projection_before_write(reader, expected, replacement)
    old = None if expected is None else resolver(expected)
    new = None if replacement is None else resolver(replacement)
    if new is None:
        if old is None:
            return
        _uninstall_systemd(unit_directory, old, runner, systemctl)
    else:
        _install_systemd(unit_directory, new, runner, systemctl)
    _require_scheduler_projection(reader, replacement)


def systemd_scheduler_resource(
    *,
    root: Path,
    state_root: Path,
    uv_path: Path,
    unit_directory: Path,
    runner: CommandRunner = _default_command_runner,
    systemctl: str = "systemctl",
) -> ManagedResource:
    unit_directory = Path(unit_directory).resolve(strict=False)
    definitions = render_systemd_definitions(root, state_root, uv_path)
    desired = _definition_bundle(definitions)
    persisted = {f"scheduler/linux/{name}": value for name, value in definitions.items()}
    legacy_directory = Path(state_root) / "run" / "install" / "scheduler" / "linux"
    legacy_loader = partial(
        _load_persisted_definitions, legacy_directory, _SYSTEMD_DEFINITION_NAMES
    )
    resolver = partial(
        _definition_candidate,
        expected_names=_SYSTEMD_DEFINITION_NAMES,
        legacy_loader=legacy_loader,
    )
    state_reader = partial(
        _systemd_projection_state,
        unit_directory,
        runner=runner,
        systemctl=systemctl,
    )
    absence_reader = partial(state_reader, {name: b"" for name in _SYSTEMD_DEFINITION_NAMES})
    reader = partial(
        _read_scheduler_projections,
        resolver=resolver,
        state_reader=state_reader,
        absence_reader=absence_reader,
    )
    return ManagedResource(
        resource_id="systemd-user-maintenance",
        kind="systemd_scheduler",
        locator=f"systemd-user://{unit_directory}",
        desired=desired,
        read_owned=lambda: reader((desired,)),
        write_owned=lambda value: _write_systemd(
            value,
            unit_directory=unit_directory,
            definitions=definitions,
            runner=runner,
            systemctl=systemctl,
        ),
        recognizes=lambda current: current == desired,
        read_projections=reader,
        write_projection=lambda expected, replacement, _metadata: _systemd_projection_writer(
            expected,
            replacement,
            resolver=resolver,
            reader=reader,
            unit_directory=unit_directory,
            runner=runner,
            systemctl=systemctl,
        ),
        recover_legacy_projection=lambda snapshot: _recover_legacy_definitions(
            snapshot, _SYSTEMD_DEFINITION_NAMES, legacy_loader
        ),
        metadata={
            "definition_set": "linux",
            "unit_directory": str(unit_directory),
            "uv_path": str(Path(uv_path).resolve()),
        },
        definitions=persisted,
        adopt_as_absent=True,
    )


def _launchd_label(name: str) -> str:
    return name.removesuffix(".plist")


def _launchd_job_state(runner: CommandRunner, launchctl: str, domain: str, label: str) -> str:
    exit_code, _output = runner((launchctl, "print", f"{domain}/{label}"), None)
    if exit_code == 0:
        return "active"
    return "absent"


def _launchd_read(
    *,
    launch_agents_directory: Path,
    definitions: Mapping[str, bytes],
    desired: bytes,
    runner: CommandRunner,
    launchctl: str,
    domain: str,
) -> bytes | None:
    combined = _launchd_projection_state(
        launch_agents_directory,
        definitions,
        runner,
        launchctl,
        domain,
    )
    if combined == "absent":
        return None
    if combined == "installed":
        return desired
    return b"conflict"


def _launchd_projection_state(
    launch_agents_directory: Path,
    definitions: Mapping[str, bytes],
    runner: CommandRunner,
    launchctl: str,
    domain: str,
) -> str:
    file_state = _systemd_file_state(launch_agents_directory, definitions)
    states = [
        _launchd_job_state(runner, launchctl, domain, _launchd_label(name)) for name in definitions
    ]
    return _combined_scheduler_state(file_state, states)


def _install_launchd(
    launch_agents_directory: Path,
    definitions: Mapping[str, bytes],
    runner: CommandRunner,
    launchctl: str,
    domain: str,
) -> None:
    _write_systemd_files(launch_agents_directory, definitions)
    for name in definitions:
        _require_command(
            runner,
            (launchctl, "bootstrap", domain, str(launch_agents_directory / name)),
        )


def _uninstall_launchd(
    launch_agents_directory: Path,
    definitions: Mapping[str, bytes],
    runner: CommandRunner,
    launchctl: str,
    domain: str,
) -> None:
    for name in reversed(definitions):
        label = _launchd_label(name)
        _require_command(runner, (launchctl, "bootout", f"{domain}/{label}"))
    _remove_systemd_files(launch_agents_directory, definitions)


def _write_launchd(
    value: bytes | None,
    *,
    launch_agents_directory: Path,
    definitions: Mapping[str, bytes],
    runner: CommandRunner,
    launchctl: str,
    domain: str,
) -> None:
    if value is None:
        _uninstall_launchd(launch_agents_directory, definitions, runner, launchctl, domain)
        return
    _install_launchd(launch_agents_directory, definitions, runner, launchctl, domain)


def _launchd_projection_writer(
    expected: bytes | None,
    replacement: bytes | None,
    *,
    resolver: Callable[[bytes], Mapping[str, bytes]],
    reader: Callable[[Sequence[bytes]], bytes | None],
    launch_agents_directory: Path,
    runner: CommandRunner,
    launchctl: str,
    domain: str,
) -> None:
    _require_projection_before_write(reader, expected, replacement)
    if expected is not None:
        old = resolver(expected)
        _uninstall_launchd(launch_agents_directory, old, runner, launchctl, domain)
    if replacement is not None:
        new = resolver(replacement)
        _install_launchd(launch_agents_directory, new, runner, launchctl, domain)
    _require_scheduler_projection(reader, replacement)


def launchd_scheduler_resource(
    *,
    root: Path,
    state_root: Path,
    uv_path: Path,
    launch_agents_directory: Path,
    uid: int,
    runner: CommandRunner = _default_command_runner,
    launchctl: str = "launchctl",
) -> ManagedResource:
    launch_agents_directory = Path(launch_agents_directory).resolve(strict=False)
    definitions = render_launchd_definitions(root, state_root, uv_path)
    desired = _definition_bundle(definitions)
    domain = f"gui/{uid}"
    persisted = {f"scheduler/macos/{name}": value for name, value in definitions.items()}
    legacy_directory = Path(state_root) / "run" / "install" / "scheduler" / "macos"
    legacy_loader = partial(
        _load_persisted_definitions, legacy_directory, _LAUNCHD_DEFINITION_NAMES
    )
    resolver = partial(
        _definition_candidate,
        expected_names=_LAUNCHD_DEFINITION_NAMES,
        legacy_loader=legacy_loader,
    )
    state_reader = partial(
        _launchd_projection_state,
        launch_agents_directory,
        runner=runner,
        launchctl=launchctl,
        domain=domain,
    )
    absence_reader = partial(state_reader, {name: b"" for name in _LAUNCHD_DEFINITION_NAMES})
    reader = partial(
        _read_scheduler_projections,
        resolver=resolver,
        state_reader=state_reader,
        absence_reader=absence_reader,
    )
    return ManagedResource(
        resource_id="launchd-user-maintenance",
        kind="launchd_scheduler",
        locator=f"launchd-user://{domain}",
        desired=desired,
        read_owned=lambda: reader((desired,)),
        write_owned=lambda value: _write_launchd(
            value,
            launch_agents_directory=launch_agents_directory,
            definitions=definitions,
            runner=runner,
            launchctl=launchctl,
            domain=domain,
        ),
        recognizes=lambda current: current == desired,
        read_projections=reader,
        write_projection=lambda expected, replacement, _metadata: _launchd_projection_writer(
            expected,
            replacement,
            resolver=resolver,
            reader=reader,
            launch_agents_directory=launch_agents_directory,
            runner=runner,
            launchctl=launchctl,
            domain=domain,
        ),
        recover_legacy_projection=lambda snapshot: _recover_legacy_definitions(
            snapshot, _LAUNCHD_DEFINITION_NAMES, legacy_loader
        ),
        metadata={
            "definition_set": "macos",
            "launchd_domain": domain,
            "uv_path": str(Path(uv_path).resolve()),
        },
        definitions=persisted,
        adopt_as_absent=True,
    )


def render_cron_block(root: Path, state_root: Path, uv_path: Path) -> bytes:
    from installer_config import build_cron_command

    nightly = build_cron_command(
        root=Path(root).resolve(),
        state_root=Path(state_root).resolve(),
        uv_path=Path(uv_path).resolve(),
        kind="nightly",
        log_path=Path(state_root).resolve() / "logs" / "cron-nightly.log",
    )
    weekly = build_cron_command(
        root=Path(root).resolve(),
        state_root=Path(state_root).resolve(),
        uv_path=Path(uv_path).resolve(),
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
    start, end = bounds
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
    return table[:start] + table[_after_profile_block(table, end) :]


def _replace_cron_block(table: bytes, updated: bytes) -> bytes:
    start, end = _cron_bounds(table) or (0, 0)
    return table[:start] + updated + table[end:]


def _append_cron_block(table: bytes, updated: bytes) -> bytes:
    return table + _profile_separator(table) + updated + b"\n"


def _cron_output(
    table: bytes | None, current: bytes | None, updated: bytes | None, separator: str
) -> bytes:
    content = table or b""
    if current is not None:
        if updated is None:
            return _remove_cron_block(content, separator)
        return _replace_cron_block(content, updated)
    if updated is None:
        return content
    return _append_cron_block(content, updated)


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
    runner: CommandRunner = _default_command_runner,
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
            "uv_path": str(Path(uv_path).resolve()),
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


def _windows_read_user_env(name: str) -> str | None:
    if sys.platform != "win32":
        raise InstallControlError("install_windows_environment_unavailable")
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, value_type = winreg.QueryValueEx(key, name)
    except FileNotFoundError:
        return None
    if value_type not in {winreg.REG_SZ, winreg.REG_EXPAND_SZ} or not isinstance(value, str):
        raise InstallControlError("install_windows_environment_invalid")
    return value


def _windows_write_user_env(name: str, value: str | None) -> None:
    if sys.platform != "win32":
        raise InstallControlError("install_windows_environment_unavailable")
    import winreg

    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, "Environment", access=winreg.KEY_SET_VALUE
    ) as key:
        if value is None:
            try:
                winreg.DeleteValue(key, name)
            except FileNotFoundError:
                return
            return
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)


def _environment_resource(
    name: str,
    value: str,
    read_value: Callable[[str], str | None],
    write_value: Callable[[str, str | None], None],
) -> ManagedResource:
    desired = value.encode("utf-8")
    return ManagedResource(
        resource_id=name.lower(),
        kind="user_environment",
        locator=f"windows-user-env://{name}",
        desired=desired,
        read_owned=lambda: _encoded_environment_value(read_value(name)),
        write_owned=lambda updated: write_value(name, _environment_update(updated)),
        recognizes=lambda current: current == desired,
        metadata={"environment_name": name},
        adopt_as_absent=True,
    )


def _environment_update(updated: bytes | None) -> str | None:
    if updated is None:
        return None
    return updated.decode("utf-8")


def _encoded_environment_value(value: str | None) -> bytes | None:
    if value is None:
        return None
    return value.encode("utf-8")


def windows_environment_resources(
    root: Path,
    state_root: Path,
    *,
    read_value: Callable[[str], str | None] = _windows_read_user_env,
    write_value: Callable[[str, str | None], None] = _windows_write_user_env,
) -> list[ManagedResource]:
    values = (
        ("LLM_WIKI_ROOT", str(Path(root).resolve())),
        ("LLM_WIKI_STATE_ROOT", str(Path(state_root).resolve())),
    )
    return [_environment_resource(name, value, read_value, write_value) for name, value in values]


def render_windows_task_spec(root: Path, state_root: Path, uv_path: Path) -> bytes:
    value = {
        "root": str(Path(root).resolve()),
        "state_root": str(Path(state_root).resolve()),
        "tasks": [
            {"at": "03:00", "kind": "nightly", "name": "LLMWiki-Nightly"},
            {
                "at": "04:00",
                "day": "Sunday",
                "kind": "weekly",
                "name": "LLMWiki-Weekly",
            },
        ],
        "uv_path": str(Path(uv_path).resolve()),
    }
    return canonical_json_bytes(value)


def _expected_windows_tasks() -> list[dict[str, object]]:
    return [
        {"at": "03:00", "kind": "nightly", "name": "LLMWiki-Nightly"},
        {
            "at": "04:00",
            "day": "Sunday",
            "kind": "weekly",
            "name": "LLMWiki-Weekly",
        },
    ]


def _require_windows_spec_path(value: object, expected: Path | None = None) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise InstallControlError("install_windows_task_spec_invalid")
    resolved = Path(value).resolve()
    if expected is not None and resolved != Path(expected).resolve():
        raise InstallControlError("install_windows_task_spec_invalid")
    return resolved


def _decode_windows_task_spec(candidate: bytes, root: Path, state_root: Path) -> dict[str, object]:
    if len(candidate) > MAX_PREIMAGE_BYTES:
        raise InstallControlError("install_windows_task_spec_invalid")
    value = _strict_json_object(candidate)
    if set(value) != {"root", "state_root", "tasks", "uv_path"}:
        raise InstallControlError("install_windows_task_spec_invalid")
    if value.get("tasks") != _expected_windows_tasks():
        raise InstallControlError("install_windows_task_spec_invalid")
    _require_windows_spec_path(value.get("root"), root)
    _require_windows_spec_path(value.get("state_root"), state_root)
    _require_windows_spec_path(value.get("uv_path"))
    return value


def _windows_task_command(
    *,
    powershell: str,
    script_path: Path,
    root: Path,
    state_root: Path,
    uv_path: Path,
    mode: str | None,
) -> tuple[str, ...]:
    command = (
        powershell,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
        "-VaultRoot",
        str(Path(root).resolve()),
        "-StateRoot",
        str(Path(state_root).resolve()),
        "-UvPath",
        str(Path(uv_path).resolve()),
    )
    if mode is None:
        return command
    return (*command, mode)


def _windows_task_command_from_spec(
    spec: Mapping[str, object],
    *,
    powershell: str,
    script_path: Path,
    mode: str | None,
) -> tuple[str, ...]:
    return _windows_task_command(
        powershell=powershell,
        script_path=script_path,
        root=Path(str(spec["root"])),
        state_root=Path(str(spec["state_root"])),
        uv_path=Path(str(spec["uv_path"])),
        mode=mode,
    )


def _windows_task_state_value(output: bytes) -> str:
    if len(output) > MAX_RECORD_BYTES:
        raise InstallControlError("install_scheduler_output_oversized")
    try:
        value = json.loads(output)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallControlError("install_windows_task_state_invalid") from exc
    if not isinstance(value, dict) or value.get("state") not in {
        "absent",
        "conflict",
        "equivalent",
    }:
        raise InstallControlError("install_windows_task_state_invalid")
    return str(value["state"])


def _read_windows_tasks(
    *,
    desired: bytes,
    runner: CommandRunner,
    command: tuple[str, ...],
) -> bytes | None:
    exit_code, output = runner((*command, "-StateJson"), None)
    if exit_code != 0:
        raise InstallControlError("install_windows_task_state_failed")
    state = _windows_task_state_value(output)
    if state == "absent":
        return None
    if state == "equivalent":
        return desired
    return b"conflict"


def _write_windows_tasks(
    value: bytes | None, runner: CommandRunner, command: tuple[str, ...]
) -> None:
    if value is None:
        _require_command(runner, (*command, "-Uninstall"))
        return
    _require_command(runner, command)


def _windows_projection_state(
    spec: Mapping[str, object],
    *,
    runner: CommandRunner,
    powershell: str,
    script_path: Path,
) -> str:
    command = _windows_task_command_from_spec(
        spec, powershell=powershell, script_path=script_path, mode=None
    )
    exit_code, output = runner((*command, "-StateJson"), None)
    if exit_code != 0:
        raise InstallControlError("install_windows_task_state_failed")
    states = {"absent": "absent", "conflict": "conflict", "equivalent": "installed"}
    return states[_windows_task_state_value(output)]


def _windows_projection_writer(
    expected: bytes | None,
    replacement: bytes | None,
    *,
    resolver: Callable[[bytes], Mapping[str, object]],
    reader: Callable[[Sequence[bytes]], bytes | None],
    runner: CommandRunner,
    powershell: str,
    script_path: Path,
) -> None:
    _require_projection_before_write(reader, expected, replacement)
    if expected is not None:
        old = resolver(expected)
        command = _windows_task_command_from_spec(
            old, powershell=powershell, script_path=script_path, mode="-Uninstall"
        )
        _require_command(runner, command)
    if replacement is not None:
        new = resolver(replacement)
        command = _windows_task_command_from_spec(
            new, powershell=powershell, script_path=script_path, mode=None
        )
        _require_command(runner, command)
    _require_scheduler_projection(reader, replacement)


def _load_persisted_windows_spec(path: Path, root: Path, state_root: Path) -> bytes:
    value = _read_managed_file(path)
    if value is None:
        raise InstallControlError("install_legacy_definition_missing")
    _decode_windows_task_spec(value, root, state_root)
    return value


def _recover_windows_spec(
    snapshot: Mapping[str, object], loader: Callable[[], bytes]
) -> tuple[bytes, bytes]:
    candidate = loader()
    if not _same_snapshot(candidate, snapshot):
        raise InstallControlError("install_legacy_definition_mismatch")
    return candidate, candidate


def windows_task_scheduler_resource(
    *,
    root: Path,
    state_root: Path,
    uv_path: Path,
    script_path: Path,
    powershell: str,
    runner: CommandRunner = _default_command_runner,
) -> ManagedResource:
    script_path = Path(script_path).resolve(strict=False)
    desired = render_windows_task_spec(root, state_root, uv_path)
    command = _windows_task_command(
        powershell=powershell,
        script_path=script_path,
        root=root,
        state_root=state_root,
        uv_path=uv_path,
        mode=None,
    )
    persisted_path = Path(state_root) / "run" / "install" / "scheduler" / "windows" / "tasks.json"
    resolver = partial(_decode_windows_task_spec, root=root, state_root=state_root)
    state_reader = partial(
        _windows_projection_state,
        runner=runner,
        powershell=powershell,
        script_path=script_path,
    )
    absence_reader = partial(state_reader, resolver(desired))
    reader = partial(
        _read_scheduler_projections,
        resolver=resolver,
        state_reader=state_reader,
        absence_reader=absence_reader,
    )
    legacy_loader = partial(_load_persisted_windows_spec, persisted_path, root, state_root)
    return ManagedResource(
        resource_id="windows-task-scheduler-maintenance",
        kind="windows_task_scheduler",
        locator="windows-task-scheduler://LLMWiki",
        desired=desired,
        read_owned=lambda: reader((desired,)),
        write_owned=lambda value: _write_windows_tasks(value, runner, command),
        recognizes=lambda current: current == desired,
        read_projections=reader,
        write_projection=lambda expected, replacement, _metadata: _windows_projection_writer(
            expected,
            replacement,
            resolver=resolver,
            reader=reader,
            runner=runner,
            powershell=powershell,
            script_path=script_path,
        ),
        recover_legacy_projection=lambda snapshot: _recover_windows_spec(snapshot, legacy_loader),
        metadata={
            "definition_set": "windows",
            "uv_path": str(Path(uv_path).resolve()),
        },
        definitions={"scheduler/windows/tasks.json": desired},
        adopt_as_absent=True,
    )


def _validate_resources(resources: Sequence[ManagedResource]) -> None:
    _validate_resource_count(resources)
    _validate_resource_ids(resources)
    _validate_resource_sizes(resources)


def _validate_resource_count(resources: Sequence[ManagedResource]) -> None:
    if not resources or len(resources) > MAX_RESOURCES:
        raise InstallControlError("install_resource_count_invalid")


def _validate_resource_ids(resources: Sequence[ManagedResource]) -> None:
    identifiers = [resource.resource_id for resource in resources]
    if len(set(identifiers)) != len(identifiers):
        raise InstallControlError("install_resource_id_duplicate")


def _validate_resource_sizes(resources: Sequence[ManagedResource]) -> None:
    if any(len(resource.desired) > MAX_PREIMAGE_BYTES for resource in resources):
        raise InstallControlError("install_resource_too_large")


def _write_preimage(install_root: Path, value: bytes) -> str:
    digest = _sha256(value)
    relative = f"preimages/{digest}.bin"
    target = install_root / relative
    if target.exists():
        if target.read_bytes() != value:
            raise InstallControlError("install_preimage_conflict")
        return relative
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    fsync_directory(target.parent)
    return relative


def _snapshot(value: bytes | None, preimage: str | None = None) -> dict[str, object]:
    if value is None:
        return {"state": "absent"}
    result: dict[str, object] = {
        "sha256": _sha256(value),
        "size": len(value),
        "state": "present",
    }
    if preimage is not None:
        result["preimage"] = preimage
    return result


def _definition_target(install_root: Path, relative: str) -> Path:
    logical = PurePosixPath(relative)
    if logical.is_absolute() or ".." in logical.parts:
        raise InstallControlError("install_definition_path_invalid")
    if not logical.parts or logical.parts[0] != "scheduler":
        raise InstallControlError("install_definition_path_invalid")
    return install_root.joinpath(*logical.parts)


def _persist_resource_definition(install_root: Path, resource: ManagedResource) -> None:
    for relative, value in resource.definitions.items():
        if len(value) > MAX_PREIMAGE_BYTES:
            raise InstallControlError("install_definition_too_large")
        target = _definition_target(install_root, relative)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(target.parent, 0o700)
        if not target.exists() or target.read_bytes() != value:
            _atomic_write(target, value)


def _resource_origin(resource: ManagedResource, current: bytes | None) -> bytes | None:
    if current is not None and resource.adopt_as_absent:
        return None
    return current


def _resource_record(install_root: Path, resource: ManagedResource) -> dict[str, object]:
    _persist_resource_definition(install_root, resource)
    current = resource.read_owned()
    if current is not None and not resource.recognizes(current):
        raise InstallControlError("install_resource_ownership_ambiguous")
    origin = _resource_origin(resource, current)
    preimage = _write_preimage(install_root, origin) if origin is not None else None
    return {
        "id": resource.resource_id,
        "installed": _snapshot(resource.desired),
        "kind": resource.kind,
        "locator": resource.locator,
        "metadata": dict(resource.metadata),
        "origin": _snapshot(origin, preimage),
        "state": "pending",
    }


def _request_digest(
    vault_root: Path,
    release: Mapping[str, object],
    backend: str,
    resources: Sequence[ManagedResource],
) -> str:
    request = {
        "release": dict(release),
        "resources": [_resource_request(resource) for resource in resources],
        "scheduler_backend": backend,
        "vault_root": str(Path(vault_root).resolve()),
    }
    return _sha256(canonical_json_bytes(request))


def _resource_request(resource: ManagedResource) -> dict[str, object]:
    return {
        "id": resource.resource_id,
        "installed": _snapshot(resource.desired),
        "kind": resource.kind,
        "locator": resource.locator,
        "metadata": dict(resource.metadata),
    }


def _new_transaction(
    *,
    state_root: Path,
    vault_root: Path,
    release: Mapping[str, object],
    backend: str,
    resource_records: list[dict[str, object]],
    request_sha256: str,
) -> dict[str, object]:
    now = _utc_now()
    return {
        "error": None,
        "id": secrets.token_hex(16),
        "operation": "install",
        "release": dict(release),
        "request_sha256": request_sha256,
        "resources": resource_records,
        "scheduler_backend": backend,
        "schema": "install-transaction/v1",
        "started_at": now,
        "state": "prepared",
        "state_root": str(Path(state_root).resolve()),
        "target_manifest_sha256": None,
        "updated_at": now,
        "vault_root": str(Path(vault_root).resolve()),
    }


def _same_snapshot(value: bytes | None, snapshot: Mapping[str, object]) -> bool:
    if snapshot.get("state") == "absent":
        return value is None
    return value is not None and _snapshot(value) == {
        key: item for key, item in snapshot.items() if key != "preimage"
    }


def _set_transaction_state(
    transaction_path: Path, transaction: dict[str, object], state: str
) -> None:
    transaction["state"] = state
    transaction["updated_at"] = _utc_now()
    _write_record(transaction_path, transaction)


def _resource_snapshots(
    record: Mapping[str, object],
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    installed = record["installed"]
    origin = record["origin"]
    if not isinstance(installed, Mapping) or not isinstance(origin, Mapping):
        raise InstallControlError("install_transaction_invalid")
    return installed, origin


def _mark_resource_state(
    transaction_path: Path,
    transaction: dict[str, object],
    record: dict[str, object],
    state: str,
) -> None:
    record["state"] = state
    transaction["updated_at"] = _utc_now()
    _write_record(transaction_path, transaction)


def _mutate_resource(
    transaction_path: Path,
    transaction: dict[str, object],
    record: dict[str, object],
    resource: ManagedResource,
) -> None:
    _mark_resource_state(transaction_path, transaction, record, "mutating")
    resource.write_owned(resource.desired)
    if resource.read_owned() != resource.desired:
        raise InstallControlError("install_resource_verification_failed")
    _mark_resource_state(transaction_path, transaction, record, "verified")


def _apply_resource(
    transaction_path: Path,
    transaction: dict[str, object],
    record: dict[str, object],
    resource: ManagedResource,
) -> None:
    current = resource.read_owned()
    installed, origin = _resource_snapshots(record)
    if _same_snapshot(current, installed):
        _mark_resource_state(transaction_path, transaction, record, "verified")
        return
    if not _same_snapshot(current, origin):
        raise InstallControlError("install_resource_drift")
    _mutate_resource(transaction_path, transaction, record, resource)


def _manifest_from_transaction(transaction: Mapping[str, object]) -> dict[str, object]:
    resources = transaction["resources"]
    if not isinstance(resources, list):
        raise InstallControlError("install_transaction_invalid")
    return {
        "committed_at": transaction["started_at"],
        "generation": 1,
        "installation_id": transaction["id"],
        "release": transaction["release"],
        "request_sha256": transaction["request_sha256"],
        "resources": resources,
        "scheduler_backend": transaction["scheduler_backend"],
        "schema": "install-manifest/v1",
        "state_root": transaction["state_root"],
        "transaction_id": transaction["id"],
        "vault_root": transaction["vault_root"],
    }


def _publish_manifest(
    install_root: Path, transaction_path: Path, transaction: dict[str, object]
) -> dict[str, object]:
    manifest = _manifest_from_transaction(transaction)
    encoded = canonical_json_bytes(manifest)
    transaction["target_manifest_sha256"] = _sha256(encoded)
    _set_transaction_state(transaction_path, transaction, "publishing")
    _publish_manifest_bytes(install_root / "manifest.json", encoded)
    _set_transaction_state(transaction_path, transaction, "committed")
    return manifest


def _publish_manifest_bytes(path: Path, encoded: bytes) -> None:
    if path.exists() and path.read_bytes() != encoded:
        raise InstallControlError("install_manifest_conflict")
    if path.exists():
        return
    _atomic_write(path, encoded)


def _validate_backend(scheduler_backend: str) -> None:
    if scheduler_backend not in _BACKENDS:
        raise InstallControlError("install_scheduler_backend_invalid")


def _origin_size(record: Mapping[str, object]) -> int:
    origin = record.get("origin")
    if not isinstance(origin, Mapping):
        raise InstallControlError("install_transaction_invalid")
    return int(origin.get("size", 0))


def _prepare_resource_records(
    install_root: Path, resources: Sequence[ManagedResource]
) -> list[dict[str, object]]:
    records = [_resource_record(install_root, resource) for resource in resources]
    if sum(_origin_size(record) for record in records) > MAX_TOTAL_PREIMAGE_BYTES:
        raise InstallControlError("install_preimages_too_large")
    return records


def _preimage_path(install_root: Path, origin: Mapping[str, object]) -> Path:
    relative = origin.get("preimage")
    if not isinstance(relative, str):
        raise InstallControlError("install_preimage_reference_invalid")
    if not relative.startswith("preimages/"):
        raise InstallControlError("install_preimage_reference_invalid")
    return install_root / relative


def _read_preimage(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise InstallControlError("install_preimage_invalid")
    if path.stat().st_size > MAX_PREIMAGE_BYTES:
        raise InstallControlError("install_preimage_invalid")
    return path.read_bytes()


def _read_origin(install_root: Path, origin: Mapping[str, object]) -> bytes | None:
    if origin.get("state") == "absent":
        return None
    value = _read_preimage(_preimage_path(install_root, origin))
    if not _same_snapshot(value, origin):
        raise InstallControlError("install_preimage_invalid")
    return value


def _revert_resource(
    *,
    install_root: Path,
    transaction_path: Path,
    transaction: dict[str, object],
    record: dict[str, object],
    resource: ManagedResource,
) -> None:
    installed, origin = _resource_snapshots(record)
    current = resource.read_owned()
    if _same_snapshot(current, origin):
        _mark_resource_state(transaction_path, transaction, record, "reverted")
        return
    if not _same_snapshot(current, installed):
        raise InstallControlError("install_rollback_drift")
    _mark_resource_state(transaction_path, transaction, record, "reverting")
    resource.write_owned(_read_origin(install_root, origin))
    if not _same_snapshot(resource.read_owned(), origin):
        raise InstallControlError("install_rollback_verification_failed")
    _mark_resource_state(transaction_path, transaction, record, "reverted")


def _rollback_install(
    *,
    install_root: Path,
    transaction_path: Path,
    transaction: dict[str, object],
    records: Sequence[dict[str, object]],
    resources: Sequence[ManagedResource],
) -> None:
    _set_transaction_state(transaction_path, transaction, "reverting")
    pairs = zip(reversed(records), reversed(resources), strict=True)
    for record, resource in pairs:
        _revert_resource(
            install_root=install_root,
            transaction_path=transaction_path,
            transaction=transaction,
            record=record,
            resource=resource,
        )
    _set_transaction_state(transaction_path, transaction, "reverted")


def _failure_code(error: Exception) -> str:
    if isinstance(error, InstallControlError):
        return str(error)
    return "install_resource_mutation_failed"


def _record_failure(
    transaction_path: Path, transaction: dict[str, object], error: Exception
) -> None:
    transaction["error"] = {"code": _failure_code(error)}
    transaction["updated_at"] = _utc_now()
    _write_record(transaction_path, transaction)


def _mutate_resources(
    transaction_path: Path,
    transaction: dict[str, object],
    records: Sequence[dict[str, object]],
    resources: Sequence[ManagedResource],
) -> None:
    for record, resource in zip(records, resources, strict=True):
        _apply_resource(transaction_path, transaction, record, resource)


def _rollback_after_error(
    *,
    install_root: Path,
    transaction_path: Path,
    transaction: dict[str, object],
    records: Sequence[dict[str, object]],
    resources: Sequence[ManagedResource],
    error: Exception,
) -> None:
    _record_failure(transaction_path, transaction, error)
    try:
        _rollback_install(
            install_root=install_root,
            transaction_path=transaction_path,
            transaction=transaction,
            records=records,
            resources=resources,
        )
    except Exception as rollback_error:
        transaction["error"] = {"code": _failure_code(rollback_error)}
        _set_transaction_state(transaction_path, transaction, "quarantined")
        raise InstallControlError("install_rollback_quarantined") from error


def _transaction_resources(transaction: Mapping[str, object]) -> list[dict[str, object]]:
    resources = transaction.get("resources")
    if not isinstance(resources, list):
        raise InstallControlError("install_transaction_invalid")
    if any(not isinstance(resource, dict) for resource in resources):
        raise InstallControlError("install_transaction_invalid")
    return resources


def _record_request(record: Mapping[str, object]) -> dict[str, object]:
    return {
        "id": record.get("id"),
        "installed": record.get("installed"),
        "kind": record.get("kind"),
        "locator": record.get("locator"),
        "metadata": record.get("metadata"),
    }


def _require_resource_request(
    records: Sequence[Mapping[str, object]], resources: Sequence[ManagedResource]
) -> None:
    expected = [_resource_request(resource) for resource in resources]
    actual = [_record_request(record) for record in records]
    if actual != expected:
        raise InstallControlError("install_resource_request_mismatch")


def _require_request_digest(record: Mapping[str, object], request_sha256: str) -> None:
    if record.get("request_sha256") != request_sha256:
        raise InstallControlError("install_upgrade_not_supported")


def _require_installed_resources(
    records: Sequence[Mapping[str, object]], resources: Sequence[ManagedResource]
) -> None:
    for record, resource in zip(records, resources, strict=True):
        installed, _origin = _resource_snapshots(record)
        if not _same_snapshot(resource.read_owned(), installed):
            raise InstallControlError("install_resource_drift")


def _complete_published_transaction(
    transaction_path: Path,
    transaction: dict[str, object],
    manifest: Mapping[str, object],
) -> None:
    manifest_digest = _sha256(canonical_json_bytes(dict(manifest)))
    if transaction.get("target_manifest_sha256") != manifest_digest:
        raise InstallControlError("install_manifest_transaction_mismatch")
    if transaction.get("state") == "publishing":
        _set_transaction_state(transaction_path, transaction, "committed")


def _accept_active_manifest(
    *,
    transaction_path: Path,
    transaction: dict[str, object],
    manifest: dict[str, object],
    request_sha256: str,
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    _require_request_digest(manifest, request_sha256)
    records = _transaction_resources(manifest)
    _require_resource_request(records, resources)
    _require_installed_resources(records, resources)
    _complete_published_transaction(transaction_path, transaction, manifest)
    if transaction.get("state") != "committed":
        raise InstallControlError("install_transaction_manifest_conflict")
    return manifest


def _continue_install(
    *,
    install_root: Path,
    transaction_path: Path,
    transaction: dict[str, object],
    records: Sequence[dict[str, object]],
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    _set_transaction_state(transaction_path, transaction, "mutating")
    try:
        _mutate_resources(transaction_path, transaction, records, resources)
        return _publish_manifest(install_root, transaction_path, transaction)
    except Exception as error:
        _rollback_after_error(
            install_root=install_root,
            transaction_path=transaction_path,
            transaction=transaction,
            records=records,
            resources=resources,
            error=error,
        )
        raise


def _start_install(
    *,
    install_root: Path,
    state_root: Path,
    vault_root: Path,
    release: Mapping[str, object],
    scheduler_backend: str,
    resources: Sequence[ManagedResource],
    request_sha256: str,
) -> dict[str, object]:
    records = _prepare_resource_records(install_root, resources)
    transaction = _new_transaction(
        state_root=state_root,
        vault_root=vault_root,
        release=release,
        backend=scheduler_backend,
        resource_records=records,
        request_sha256=request_sha256,
    )
    transaction_path = install_root / "transaction.json"
    _write_record(transaction_path, transaction)
    return _continue_install(
        install_root=install_root,
        transaction_path=transaction_path,
        transaction=transaction,
        records=records,
        resources=resources,
    )


def _resume_install(
    *,
    install_root: Path,
    transaction_path: Path,
    transaction: dict[str, object],
    request_sha256: str,
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    _require_request_digest(transaction, request_sha256)
    records = _transaction_resources(transaction)
    _require_resource_request(records, resources)
    state = transaction.get("state")
    if state == "publishing":
        return _publish_manifest(install_root, transaction_path, transaction)
    if state not in {"prepared", "mutating"}:
        raise InstallControlError("install_transaction_blocks_new_work")
    return _continue_install(
        install_root=install_root,
        transaction_path=transaction_path,
        transaction=transaction,
        records=records,
        resources=resources,
    )


def _install_under_lock(
    *,
    install_root: Path,
    state_root: Path,
    vault_root: Path,
    release: Mapping[str, object],
    scheduler_backend: str,
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    manifest_path = install_root / "manifest.json"
    transaction_path = install_root / "transaction.json"
    manifest = _optional_record(manifest_path, _MANIFEST_SCHEMA)
    transaction = _optional_record(transaction_path, _TRANSACTION_SCHEMA)
    request_sha256 = _request_digest(vault_root, release, scheduler_backend, resources)
    if manifest is not None:
        if transaction is None:
            raise InstallControlError("install_manifest_without_transaction")
        return _accept_active_manifest(
            transaction_path=transaction_path,
            transaction=transaction,
            manifest=manifest,
            request_sha256=request_sha256,
            resources=resources,
        )
    if transaction is not None and transaction.get("state") not in {"reverted"}:
        return _resume_install(
            install_root=install_root,
            transaction_path=transaction_path,
            transaction=transaction,
            request_sha256=request_sha256,
            resources=resources,
        )
    return _start_install(
        install_root=install_root,
        state_root=state_root,
        vault_root=vault_root,
        release=release,
        scheduler_backend=scheduler_backend,
        resources=resources,
        request_sha256=request_sha256,
    )


def _v2_snapshot(install_root: Path, value: bytes | None) -> dict[str, object]:
    if value is None:
        return {"state": "absent"}
    return _snapshot(value, _write_preimage(install_root, value))


def _v2_resource_record(install_root: Path, resource: ManagedResource) -> dict[str, object]:
    _persist_resource_definition(install_root, resource)
    current = resource.read_owned()
    if current is not None and not resource.recognizes(current):
        raise InstallControlError("install_resource_ownership_ambiguous")
    origin = _resource_origin(resource, current)
    baseline = _v2_snapshot(install_root, origin)
    return {
        "desired": _v2_snapshot(install_root, resource.desired),
        "id": resource.resource_id,
        "kind": resource.kind,
        "locator": resource.locator,
        "metadata": dict(resource.metadata),
        "origin": baseline,
        "rollback": dict(baseline),
        "state": "pending",
    }


def _v2_snapshot_size(snapshot: Mapping[str, object]) -> int:
    return int(snapshot.get("size", 0))


def _v2_record_size(record: Mapping[str, object]) -> int:
    snapshots = (record.get("desired"), record.get("origin"), record.get("rollback"))
    return sum(
        _v2_snapshot_size(snapshot) for snapshot in snapshots if isinstance(snapshot, Mapping)
    )


def _v2_prepare_records(
    install_root: Path, resources: Sequence[ManagedResource]
) -> list[dict[str, object]]:
    records = [_v2_resource_record(install_root, resource) for resource in resources]
    if sum(_v2_record_size(record) for record in records) > MAX_TOTAL_PREIMAGE_BYTES:
        raise InstallControlError("install_preimages_too_large")
    return records


def _v2_resource_snapshots(
    record: Mapping[str, object],
) -> tuple[Mapping[str, object], Mapping[str, object], Mapping[str, object]]:
    values = (record.get("desired"), record.get("origin"), record.get("rollback"))
    if any(not isinstance(value, Mapping) for value in values):
        raise InstallControlError("install_transaction_invalid")
    desired, origin, rollback = values
    return desired, origin, rollback


def _read_v2_snapshot(install_root: Path, snapshot: Mapping[str, object]) -> bytes | None:
    return _read_origin(install_root, snapshot)


def _snapshot_values(install_root: Path, snapshots: Sequence[Mapping[str, object]]) -> list[bytes]:
    values = [_read_v2_snapshot(install_root, snapshot) for snapshot in snapshots]
    return [value for value in values if value is not None]


def _read_resource_projections(
    resource: ManagedResource, candidates: Sequence[bytes]
) -> bytes | None:
    if resource.read_projections is None:
        return resource.read_owned()
    return resource.read_projections(candidates)


def _write_resource_projection(
    resource: ManagedResource,
    expected: bytes | None,
    replacement: bytes | None,
    metadata: Mapping[str, object],
) -> None:
    if resource.write_projection is None:
        resource.write_owned(replacement)
        return
    resource.write_projection(expected, replacement, metadata)


def _read_v2_resource(
    install_root: Path,
    resource: ManagedResource,
    snapshots: Sequence[Mapping[str, object]],
) -> bytes | None:
    return _read_resource_projections(resource, _snapshot_values(install_root, snapshots))


def _v2_mutate_resource(
    *,
    install_root: Path,
    transaction_path: Path,
    transaction: dict[str, object],
    record: dict[str, object],
    resource: ManagedResource,
) -> None:
    desired, _origin, _rollback = _v2_resource_snapshots(record)
    desired_value = _read_v2_snapshot(install_root, desired)
    current_value = _read_v2_resource(install_root, resource, (desired, _rollback))
    _mark_resource_state(transaction_path, transaction, record, "mutating")
    metadata = record.get("metadata")
    if not isinstance(metadata, Mapping):
        raise InstallControlError("install_transaction_invalid")
    _write_resource_projection(resource, current_value, desired_value, metadata)
    if not _same_snapshot(_read_v2_resource(install_root, resource, (desired,)), desired):
        raise InstallControlError("install_resource_verification_failed")
    _mark_resource_state(transaction_path, transaction, record, "verified")


def _v2_apply_resource(
    *,
    install_root: Path,
    transaction_path: Path,
    transaction: dict[str, object],
    record: dict[str, object],
    resource: ManagedResource,
) -> None:
    desired, _origin, rollback = _v2_resource_snapshots(record)
    current = _read_v2_resource(install_root, resource, (desired, rollback))
    if _same_snapshot(current, desired):
        _mark_resource_state(transaction_path, transaction, record, "verified")
        return
    if not _same_snapshot(current, rollback):
        raise InstallControlError("install_resource_drift")
    _v2_mutate_resource(
        install_root=install_root,
        transaction_path=transaction_path,
        transaction=transaction,
        record=record,
        resource=resource,
    )


def _v2_mutate_resources(
    *,
    install_root: Path,
    transaction_path: Path,
    transaction: dict[str, object],
    records: Sequence[dict[str, object]],
    resources: Sequence[ManagedResource],
) -> None:
    for record, resource in zip(records, resources, strict=True):
        _v2_apply_resource(
            install_root=install_root,
            transaction_path=transaction_path,
            transaction=transaction,
            record=record,
            resource=resource,
        )


class _UnreadableProjection:
    """The current projection could not be selected, so drift cannot be judged."""


# What a revert starts from when the resource was left in a state no single
# projection describes. It is not a value the resource can ever hold.
_UNREADABLE_PROJECTION = _UnreadableProjection()


def _revert_start_projection(
    install_root: Path,
    resource: ManagedResource,
    snapshots: Sequence[Mapping[str, object]],
    rollback: Mapping[str, object],
) -> bytes | None | _UnreadableProjection:
    """Where the revert starts; a resource left half applied is reported, not refused.

    Only a revert back to absence tolerates this. Refusing to read a mixed
    projection is what leaves an interrupted install with no way forward and no
    way back, because the rollback dies on the same read that failed the install.
    """
    try:
        return _read_v2_resource(install_root, resource, snapshots)
    except InstallControlError as error:
        if rollback.get("state") != "absent":
            raise
        if str(error) != "install_scheduler_projection_conflict":
            raise
        return _UNREADABLE_PROJECTION


def _revert_already_done(
    current: bytes | None | _UnreadableProjection, rollback: Mapping[str, object]
) -> bool:
    """Whether the resource already sits where the revert wants it."""
    if isinstance(current, _UnreadableProjection):
        return False
    return _same_snapshot(current, rollback)


def _require_revertible(
    current: bytes | None | _UnreadableProjection, desired: Mapping[str, object]
) -> None:
    """A revert only touches a resource that is still where the install left it."""
    if isinstance(current, _UnreadableProjection):
        return
    if not _same_snapshot(current, desired):
        raise InstallControlError("install_rollback_drift")


def _v2_revert_resource(
    *,
    install_root: Path,
    transaction_path: Path,
    transaction: dict[str, object],
    record: dict[str, object],
    resource: ManagedResource,
) -> None:
    desired, _origin, rollback = _v2_resource_snapshots(record)
    current = _revert_start_projection(
        install_root, resource, (desired, rollback), rollback
    )
    if _revert_already_done(current, rollback):
        _mark_resource_state(transaction_path, transaction, record, "reverted")
        return
    _require_revertible(current, desired)
    _mark_resource_state(transaction_path, transaction, record, "reverting")
    rollback_value = _read_v2_snapshot(install_root, rollback)
    metadata = record.get("metadata")
    if not isinstance(metadata, Mapping):
        raise InstallControlError("install_transaction_invalid")
    _write_resource_projection(
        resource, _read_v2_snapshot(install_root, desired), rollback_value, metadata
    )
    if not _same_snapshot(_read_v2_resource(install_root, resource, (rollback,)), rollback):
        raise InstallControlError("install_rollback_verification_failed")
    _mark_resource_state(transaction_path, transaction, record, "reverted")


def _v2_rollback_mutations(
    *,
    install_root: Path,
    transaction_path: Path,
    transaction: dict[str, object],
    records: Sequence[dict[str, object]],
    resources: Sequence[ManagedResource],
) -> None:
    _set_transaction_state(transaction_path, transaction, "reverting")
    pairs = zip(reversed(records), reversed(resources), strict=True)
    for record, resource in pairs:
        _v2_revert_resource(
            install_root=install_root,
            transaction_path=transaction_path,
            transaction=transaction,
            record=record,
            resource=resource,
        )
    _set_transaction_state(transaction_path, transaction, "reverted")


def _v2_manifest_resources(
    records: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    resources = []
    for record in records:
        resource = dict(record)
        metadata = dict(resource.get("metadata", {}))
        if metadata.pop("_retire_after_commit", False):
            continue
        resource["metadata"] = metadata
        resource["state"] = "verified"
        resources.append(resource)
    return resources


def _v2_manifest_from_transaction(
    transaction: Mapping[str, object],
) -> dict[str, object]:
    return {
        "committed_at": transaction["started_at"],
        "generation": transaction["generation"],
        "installation_id": transaction["installation_id"],
        "release": transaction["release"],
        "request_sha256": transaction["request_sha256"],
        "resources": _v2_manifest_resources(_transaction_resources(transaction)),
        "rollback_point": transaction["rollback_point"],
        "scheduler_backend": transaction["scheduler_backend"],
        "schema": "install-manifest/v2",
        "state_root": transaction["state_root"],
        "transaction_id": transaction["id"],
        "vault_root": transaction["vault_root"],
    }


def _replace_manifest_cas(path: Path, replacement: bytes, expected_sha256: str | None) -> None:
    current = _manifest_bytes(path)
    _require_manifest_cas(current, expected_sha256)
    _atomic_write(path, replacement)


def _manifest_bytes(path: Path) -> bytes | None:
    if not path.exists():
        return None
    return path.read_bytes()


def _require_manifest_cas(current: bytes | None, expected_sha256: str | None) -> None:
    if expected_sha256 is None:
        if current is not None:
            raise InstallControlError("install_manifest_conflict")
        return
    if current is None:
        raise InstallControlError("install_manifest_changed")
    if _sha256(current) != expected_sha256:
        raise InstallControlError("install_manifest_changed")


def _publish_v2_manifest(
    install_root: Path,
    transaction_path: Path,
    transaction: dict[str, object],
) -> dict[str, object]:
    manifest = _v2_manifest_from_transaction(transaction)
    encoded = canonical_json_bytes(manifest)
    transaction["target_manifest_sha256"] = _sha256(encoded)
    _set_transaction_state(transaction_path, transaction, "publishing")
    _replace_manifest_cas(
        install_root / "manifest.json",
        encoded,
        transaction.get("base_manifest_sha256"),
    )
    _set_transaction_state(transaction_path, transaction, "committed")
    _prune_v2_preimages(install_root, (manifest, transaction))
    return manifest


def _direct_preimage(value: Mapping[str, object]) -> set[str]:
    direct = value.get("preimage")
    if isinstance(direct, str):
        return {direct}
    return set()


def _mapping_preimages(value: Mapping[str, object]) -> set[str]:
    references = _direct_preimage(value)
    for item in value.values():
        references.update(_preimage_references(item))
    return references


def _sequence_preimages(value: Sequence[object]) -> set[str]:
    references: set[str] = set()
    for item in value:
        references.update(_preimage_references(item))
    return references


def _preimage_references(value: object) -> set[str]:
    if isinstance(value, Mapping):
        return _mapping_preimages(value)
    if isinstance(value, list):
        return _sequence_preimages(value)
    return set()


def _owned_preimage_file(path: Path) -> bool:
    return (
        not path.is_symlink()
        and path.is_file()
        and re.fullmatch(r"[0-9a-f]{64}\.bin", path.name) is not None
    )


def _prune_v2_preimages(install_root: Path, records: Sequence[Mapping[str, object]]) -> None:
    reachable: set[str] = set()
    for record in records:
        reachable.update(_preimage_references(record))
    preimages = install_root / "preimages"
    removed = False
    for path in preimages.iterdir():
        removed = _prune_preimage(path, reachable) or removed
    if removed:
        fsync_directory(preimages)


def _prune_preimage(path: Path, reachable: set[str]) -> bool:
    relative = f"preimages/{path.name}"
    if not _owned_preimage_file(path) or relative in reachable:
        return False
    path.unlink()
    return True


def _v2_failed_operation(
    *,
    install_root: Path,
    transaction_path: Path,
    transaction: dict[str, object],
    records: Sequence[dict[str, object]],
    resources: Sequence[ManagedResource],
    error: Exception,
) -> None:
    _record_failure(transaction_path, transaction, error)
    try:
        _v2_rollback_mutations(
            install_root=install_root,
            transaction_path=transaction_path,
            transaction=transaction,
            records=records,
            resources=resources,
        )
    except Exception as rollback_error:
        transaction["error"] = {"code": _failure_code(rollback_error)}
        _set_transaction_state(transaction_path, transaction, "quarantined")
        raise InstallControlError("install_rollback_quarantined") from error


def _continue_v2_operation(
    *,
    install_root: Path,
    transaction_path: Path,
    transaction: dict[str, object],
    records: Sequence[dict[str, object]],
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    _set_transaction_state(transaction_path, transaction, "mutating")
    try:
        _v2_mutate_resources(
            install_root=install_root,
            transaction_path=transaction_path,
            transaction=transaction,
            records=records,
            resources=resources,
        )
        return _publish_v2_manifest(install_root, transaction_path, transaction)
    except Exception as error:
        _v2_failed_operation(
            install_root=install_root,
            transaction_path=transaction_path,
            transaction=transaction,
            records=records,
            resources=resources,
            error=error,
        )
        raise


def _new_v2_install_transaction(
    *,
    state_root: Path,
    vault_root: Path,
    release: Mapping[str, object],
    backend: str,
    records: list[dict[str, object]],
    request_sha256: str,
) -> dict[str, object]:
    now = _utc_now()
    transaction_id = secrets.token_hex(16)
    return {
        "base_manifest_sha256": None,
        "error": None,
        "generation": 1,
        "id": transaction_id,
        "installation_id": transaction_id,
        "operation": "install",
        "release": dict(release),
        "request_sha256": request_sha256,
        "resources": records,
        "rollback_point": None,
        "scheduler_backend": backend,
        "schema": "install-transaction/v2",
        "started_at": now,
        "state": "prepared",
        "state_root": str(Path(state_root).resolve()),
        "target_manifest_sha256": None,
        "updated_at": now,
        "vault_root": str(Path(vault_root).resolve()),
    }


def _start_v2_install(
    *,
    install_root: Path,
    state_root: Path,
    vault_root: Path,
    release: Mapping[str, object],
    scheduler_backend: str,
    resources: Sequence[ManagedResource],
    request_sha256: str,
) -> dict[str, object]:
    records = _v2_prepare_records(install_root, resources)
    transaction = _new_v2_install_transaction(
        state_root=state_root,
        vault_root=vault_root,
        release=release,
        backend=scheduler_backend,
        records=records,
        request_sha256=request_sha256,
    )
    _validate_v2_record_preimages(install_root, transaction)
    transaction_path = install_root / "transaction.json"
    _write_record(transaction_path, transaction)
    return _continue_v2_operation(
        install_root=install_root,
        transaction_path=transaction_path,
        transaction=transaction,
        records=records,
        resources=resources,
    )


def _record_desired(record: Mapping[str, object]) -> Mapping[str, object]:
    desired = record.get("desired", record.get("installed"))
    if not isinstance(desired, Mapping):
        raise InstallControlError("install_state_schema_invalid")
    return desired


def _normalized_snapshot(
    install_root: Path,
    snapshot: Mapping[str, object],
    actual: bytes | None,
) -> dict[str, object]:
    if not _same_snapshot(actual, snapshot):
        raise InstallControlError("install_resource_drift")
    return _v2_snapshot(install_root, actual)


def _checkpoint_desired_snapshot(
    install_root: Path,
    snapshot: Mapping[str, object],
    resource: ManagedResource,
) -> dict[str, object]:
    if snapshot.get("state") == "absent":
        return {"state": "absent"}
    if isinstance(snapshot.get("preimage"), str):
        value = _read_origin(install_root, snapshot)
        return _normalized_snapshot(install_root, snapshot, value)
    if resource.recover_legacy_projection is not None:
        actual, persisted = resource.recover_legacy_projection(snapshot)
        if not _same_snapshot(actual, snapshot):
            raise InstallControlError("install_resource_drift")
        return _v2_snapshot(install_root, persisted)
    return _normalized_snapshot(install_root, snapshot, resource.read_owned())


def _resource_identity_matches(record: Mapping[str, object], resource: ManagedResource) -> bool:
    return (
        record.get("id") == resource.resource_id
        and record.get("kind") == resource.kind
        and record.get("locator") == resource.locator
    )


def _resources_by_id(
    resources: Sequence[ManagedResource],
) -> dict[str, ManagedResource]:
    return {resource.resource_id: resource for resource in resources}


def _active_resource(
    resources: Mapping[str, ManagedResource], record: Mapping[str, object]
) -> ManagedResource:
    resource_id = record.get("id")
    if not isinstance(resource_id, str) or resource_id not in resources:
        raise InstallControlError("install_resource_request_mismatch")
    resource = resources[resource_id]
    if not _resource_identity_matches(record, resource):
        raise InstallControlError("install_resource_request_mismatch")
    return resource


def _checkpoint_resource(
    install_root: Path,
    record: Mapping[str, object],
    resource: ManagedResource,
) -> dict[str, object]:
    desired_record = _record_desired(record)
    desired = _checkpoint_desired_snapshot(install_root, desired_record, resource)
    origin_record = record.get("origin")
    if not isinstance(origin_record, Mapping):
        raise InstallControlError("install_state_schema_invalid")
    origin = _read_origin(install_root, origin_record)
    return {
        "desired": desired,
        "id": record["id"],
        "kind": record["kind"],
        "locator": record["locator"],
        "metadata": record["metadata"],
        "origin": _v2_snapshot(install_root, origin),
        "state": "verified",
    }


def _require_active_resources(
    install_root: Path,
    manifest: Mapping[str, object],
    resources: Sequence[ManagedResource],
) -> None:
    by_id = _resources_by_id(resources)
    for record in _transaction_resources(manifest):
        resource = _active_resource(by_id, record)
        desired = _record_desired(record)
        current = _read_v2_resource(install_root, resource, (desired,))
        if not _same_snapshot(current, desired):
            raise InstallControlError("install_resource_drift")


def _checkpoint_resources(
    install_root: Path,
    manifest: Mapping[str, object],
    resources: Sequence[ManagedResource],
) -> list[dict[str, object]]:
    by_id = _resources_by_id(resources)
    return [
        _checkpoint_resource(install_root, record, _active_resource(by_id, record))
        for record in _transaction_resources(manifest)
    ]


def _rollback_point(
    manifest: Mapping[str, object], records: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "generation": manifest["generation"],
        "release": manifest["release"],
        "request_sha256": manifest["request_sha256"],
        "resources": records,
        "scheduler_backend": manifest["scheduler_backend"],
    }


def _new_v2_resource_record(
    install_root: Path,
    resource: ManagedResource,
    previous: Mapping[str, object] | None,
) -> dict[str, object]:
    if previous is None:
        return _v2_resource_record(install_root, resource)
    return {
        "desired": _v2_snapshot(install_root, resource.desired),
        "id": resource.resource_id,
        "kind": resource.kind,
        "locator": resource.locator,
        "metadata": previous["metadata"],
        "origin": previous["origin"],
        "rollback": previous["desired"],
        "state": "pending",
    }


def _updated_resource_records(
    install_root: Path,
    resources: Sequence[ManagedResource],
    checkpoint: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    previous = {str(record["id"]): record for record in checkpoint}
    return [
        _new_v2_resource_record(install_root, resource, previous.get(resource.resource_id))
        for resource in resources
    ]


def _new_v2_update_transaction(
    *,
    manifest: Mapping[str, object],
    release: Mapping[str, object],
    backend: str,
    records: list[dict[str, object]],
    checkpoint: dict[str, object],
    request_sha256: str,
    base_manifest_sha256: str,
) -> dict[str, object]:
    now = _utc_now()
    return {
        "base_manifest_sha256": base_manifest_sha256,
        "error": None,
        "generation": int(manifest["generation"]) + 1,
        "id": secrets.token_hex(16),
        "installation_id": manifest["installation_id"],
        "operation": "update",
        "release": dict(release),
        "request_sha256": request_sha256,
        "resources": records,
        "rollback_point": checkpoint,
        "scheduler_backend": backend,
        "schema": "install-transaction/v2",
        "started_at": now,
        "state": "prepared",
        "state_root": manifest["state_root"],
        "target_manifest_sha256": None,
        "updated_at": now,
        "vault_root": manifest["vault_root"],
    }


def _validate_active_manifest(
    install_root: Path,
    manifest: Mapping[str, object],
    transaction: Mapping[str, object] | None,
) -> None:
    health = _active_install_health_for_schema(install_root, manifest, transaction)
    if health.get("status") != "active":
        raise InstallControlError("install_transaction_blocks_new_work")


def _start_v2_update(
    *,
    install_root: Path,
    manifest: dict[str, object],
    release: Mapping[str, object],
    scheduler_backend: str,
    resources: Sequence[ManagedResource],
    request_sha256: str,
) -> dict[str, object]:
    checkpoint_records = _checkpoint_resources(install_root, manifest, resources)
    checkpoint = _rollback_point(manifest, checkpoint_records)
    records = _updated_resource_records(install_root, resources, checkpoint_records)
    base_digest = _sha256(canonical_json_bytes(manifest))
    transaction = _new_v2_update_transaction(
        manifest=manifest,
        release=release,
        backend=scheduler_backend,
        records=records,
        checkpoint=checkpoint,
        request_sha256=request_sha256,
        base_manifest_sha256=base_digest,
    )
    _validate_v2_record_preimages(install_root, transaction)
    transaction_path = install_root / "transaction.json"
    _write_record(transaction_path, transaction)
    return _continue_v2_operation(
        install_root=install_root,
        transaction_path=transaction_path,
        transaction=transaction,
        records=records,
        resources=resources,
    )


def _active_v2_request(
    *,
    install_root: Path,
    manifest: dict[str, object],
    transaction: dict[str, object] | None,
    release: Mapping[str, object],
    scheduler_backend: str,
    resources: Sequence[ManagedResource],
    request_sha256: str,
) -> dict[str, object]:
    _validate_active_manifest(install_root, manifest, transaction)
    same_v2_request = (
        manifest.get("schema") == "install-manifest/v2"
        and manifest.get("request_sha256") == request_sha256
    )
    if same_v2_request:
        _require_active_resources(install_root, manifest, resources)
        _checkpoint_resources(install_root, manifest, resources)
        return manifest
    return _start_v2_update(
        install_root=install_root,
        manifest=manifest,
        release=release,
        scheduler_backend=scheduler_backend,
        resources=resources,
        request_sha256=request_sha256,
    )


def _v2_nonterminal(transaction: Mapping[str, object] | None) -> bool:
    if transaction is None or transaction.get("schema") != "install-transaction/v2":
        return False
    return transaction.get("state") in {"prepared", "mutating", "publishing"}


def _ordered_transaction_resources(
    records: Sequence[Mapping[str, object]],
    resources: Sequence[ManagedResource],
) -> list[ManagedResource]:
    by_id = _resources_by_id(resources)
    return [_active_resource(by_id, record) for record in records]


def _manifest_digest(manifest: Mapping[str, object] | None) -> str | None:
    if manifest is None:
        return None
    return _sha256(canonical_json_bytes(dict(manifest)))


def _published_v2_target(
    manifest: Mapping[str, object] | None, transaction: Mapping[str, object]
) -> bool:
    target = transaction.get("target_manifest_sha256")
    if not isinstance(target, str):
        return False
    return _manifest_digest(manifest) == target


def _require_v2_base_manifest(
    manifest: Mapping[str, object] | None, transaction: Mapping[str, object]
) -> None:
    if _manifest_digest(manifest) != transaction.get("base_manifest_sha256"):
        raise InstallControlError("install_manifest_transaction_mismatch")


def _complete_v2_publication(
    transaction_path: Path,
    transaction: dict[str, object],
    manifest: dict[str, object] | None,
) -> dict[str, object]:
    if manifest is None:
        raise InstallControlError("install_manifest_transaction_mismatch")
    _validate_v2_manifest_transaction(transaction_path.parent, manifest, transaction)
    _set_transaction_state(transaction_path, transaction, "committed")
    return manifest


def _resume_v2_operation(
    *,
    install_root: Path,
    manifest: dict[str, object] | None,
    transaction: dict[str, object] | None,
    request_sha256: str,
    resources: Sequence[ManagedResource],
) -> dict[str, object] | None:
    if not _v2_nonterminal(transaction):
        return None
    if transaction is None or transaction.get("request_sha256") != request_sha256:
        raise InstallControlError("install_transaction_blocks_new_work")
    records = _transaction_resources(transaction)
    ordered = _ordered_transaction_resources(records, resources)
    transaction_path = install_root / "transaction.json"
    if _published_v2_target(manifest, transaction):
        return _complete_v2_publication(transaction_path, transaction, manifest)
    _require_v2_base_manifest(manifest, transaction)
    return _continue_v2_operation(
        install_root=install_root,
        transaction_path=transaction_path,
        transaction=transaction,
        records=records,
        resources=ordered,
    )


def _install_v2_under_lock(
    *,
    install_root: Path,
    state_root: Path,
    vault_root: Path,
    release: Mapping[str, object],
    scheduler_backend: str,
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    manifest = _optional_install_record(install_root / "manifest.json", "install-manifest/")
    transaction = _optional_install_record(
        install_root / "transaction.json", "install-transaction/"
    )
    request_sha256 = _request_digest(vault_root, release, scheduler_backend, resources)
    resumed = _resume_v2_operation(
        install_root=install_root,
        manifest=manifest,
        transaction=transaction,
        request_sha256=request_sha256,
        resources=resources,
    )
    if resumed is not None:
        return resumed
    if manifest is not None:
        return _active_v2_request(
            install_root=install_root,
            manifest=manifest,
            transaction=transaction,
            release=release,
            scheduler_backend=scheduler_backend,
            resources=resources,
            request_sha256=request_sha256,
        )
    if transaction is not None and transaction.get("state") not in {
        "committed",
        "reverted",
    }:
        raise InstallControlError("install_transaction_blocks_new_work")
    return _start_v2_install(
        install_root=install_root,
        state_root=state_root,
        vault_root=vault_root,
        release=release,
        scheduler_backend=scheduler_backend,
        resources=resources,
        request_sha256=request_sha256,
    )


def _install_v2(
    *,
    state_root: Path,
    vault_root: Path,
    release: Mapping[str, object],
    scheduler_backend: str,
    resources: Sequence[ManagedResource],
    lock_timeout: float,
) -> dict[str, object]:
    install_root = _prepare_install_root(state_root)
    with _install_lock(install_root / "install.lock", lock_timeout):
        return _install_v2_under_lock(
            install_root=install_root,
            state_root=state_root,
            vault_root=vault_root,
            release=release,
            scheduler_backend=scheduler_backend,
            resources=resources,
        )


def _validate_v2_recovery_support(resources: Sequence[ManagedResource]) -> None:
    if any(not resource.supports_v2_recovery for resource in resources):
        raise InstallControlError("install_v2_resource_recovery_unsupported")


def install_resources(
    *,
    state_root: Path,
    vault_root: Path,
    release: Mapping[str, object],
    scheduler_backend: str,
    resources: Sequence[ManagedResource],
    lock_timeout: float = 10.0,
    control_version: int = 1,
) -> dict[str, object]:
    _validate_resources(resources)
    _validate_backend(scheduler_backend)
    if control_version == 2:
        _validate_v2_recovery_support(resources)
        return _install_v2(
            state_root=state_root,
            vault_root=vault_root,
            release=release,
            scheduler_backend=scheduler_backend,
            resources=resources,
            lock_timeout=lock_timeout,
        )
    if control_version != 1:
        raise InstallControlError("install_control_version_invalid")
    install_root = _prepare_install_root(state_root)
    with _install_lock(install_root / "install.lock", lock_timeout):
        return _install_under_lock(
            install_root=install_root,
            state_root=state_root,
            vault_root=vault_root,
            release=release,
            scheduler_backend=scheduler_backend,
            resources=resources,
        )


def _uninstall_resource_record(record: Mapping[str, object]) -> dict[str, object]:
    result = dict(record)
    result["state"] = "pending"
    return result


def _new_uninstall_transaction(manifest: Mapping[str, object]) -> dict[str, object]:
    now = _utc_now()
    resources = _transaction_resources(manifest)
    return {
        "error": None,
        "id": secrets.token_hex(16),
        "operation": "uninstall",
        "release": manifest["release"],
        "request_sha256": manifest["request_sha256"],
        "resources": [_uninstall_resource_record(record) for record in resources],
        "scheduler_backend": manifest["scheduler_backend"],
        "schema": "install-transaction/v1",
        "started_at": now,
        "state": "prepared",
        "state_root": manifest["state_root"],
        "target_manifest_sha256": None,
        "updated_at": now,
        "vault_root": manifest["vault_root"],
    }


def _retire_manifest(path: Path, expected: Mapping[str, object]) -> None:
    current = _read_record(path, _MANIFEST_SCHEMA)
    if current != expected:
        raise InstallControlError("install_manifest_changed")
    path.unlink()
    fsync_directory(path.parent)


def _continue_uninstall(
    *,
    install_root: Path,
    transaction_path: Path,
    transaction: dict[str, object],
    manifest: dict[str, object],
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    records = _transaction_resources(transaction)
    try:
        _rollback_install(
            install_root=install_root,
            transaction_path=transaction_path,
            transaction=transaction,
            records=records,
            resources=resources,
        )
        _set_transaction_state(transaction_path, transaction, "publishing")
        _retire_manifest(install_root / "manifest.json", manifest)
        _set_transaction_state(transaction_path, transaction, "committed")
        return transaction
    except Exception as error:
        _record_failure(transaction_path, transaction, error)
        _set_transaction_state(transaction_path, transaction, "quarantined")
        raise


def _resume_or_start_uninstall(
    *,
    install_root: Path,
    transaction_path: Path,
    transaction: dict[str, object] | None,
    manifest: dict[str, object],
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    manifest_records = _transaction_resources(manifest)
    _require_resource_request(manifest_records, resources)
    _require_installed_resources(manifest_records, resources)
    if transaction is None or transaction.get("operation") != "uninstall":
        transaction = _new_uninstall_transaction(manifest)
        _write_record(transaction_path, transaction)
    return _continue_uninstall(
        install_root=install_root,
        transaction_path=transaction_path,
        transaction=transaction,
        manifest=manifest,
        resources=resources,
    )


def _v2_uninstall_record(active: Mapping[str, object]) -> dict[str, object]:
    return {
        "desired": active["origin"],
        "id": active["id"],
        "kind": active["kind"],
        "locator": active["locator"],
        "metadata": active["metadata"],
        "origin": active["origin"],
        "rollback": active["desired"],
        "state": "pending",
    }


def _new_v2_uninstall_transaction(
    manifest: Mapping[str, object],
) -> dict[str, object]:
    now = _utc_now()
    records = [_v2_uninstall_record(record) for record in _transaction_resources(manifest)]
    return {
        "base_manifest_sha256": _sha256(canonical_json_bytes(dict(manifest))),
        "error": None,
        "generation": int(manifest["generation"]) + 1,
        "id": secrets.token_hex(16),
        "installation_id": manifest["installation_id"],
        "operation": "uninstall",
        "release": manifest["release"],
        "request_sha256": manifest["request_sha256"],
        "resources": records,
        "rollback_point": None,
        "scheduler_backend": manifest["scheduler_backend"],
        "schema": "install-transaction/v2",
        "started_at": now,
        "state": "prepared",
        "state_root": manifest["state_root"],
        "target_manifest_sha256": None,
        "updated_at": now,
        "vault_root": manifest["vault_root"],
    }


def _retire_v2_manifest(path: Path, expected_sha256: str) -> None:
    current = _read_state_bytes(path)
    if _sha256(current) != expected_sha256:
        raise InstallControlError("install_manifest_changed")
    path.unlink()
    fsync_directory(path.parent)


def _mutate_v2_uninstall(
    *,
    install_root: Path,
    transaction: dict[str, object],
    records: Sequence[dict[str, object]],
    resources: Sequence[ManagedResource],
) -> None:
    transaction_path = install_root / "transaction.json"
    _set_transaction_state(transaction_path, transaction, "mutating")
    try:
        _v2_mutate_resources(
            install_root=install_root,
            transaction_path=transaction_path,
            transaction=transaction,
            records=records,
            resources=resources,
        )
    except Exception as error:
        _v2_failed_operation(
            install_root=install_root,
            transaction_path=transaction_path,
            transaction=transaction,
            records=records,
            resources=resources,
            error=error,
        )
        raise


def _continue_v2_uninstall(
    install_root: Path,
    transaction: dict[str, object],
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    records = _transaction_resources(transaction)
    ordered = _ordered_transaction_resources(records, resources)
    _mutate_v2_uninstall(
        install_root=install_root,
        transaction=transaction,
        records=records,
        resources=ordered,
    )
    transaction_path = install_root / "transaction.json"
    _set_transaction_state(transaction_path, transaction, "publishing")
    _retire_v2_manifest(install_root / "manifest.json", str(transaction["base_manifest_sha256"]))
    _set_transaction_state(transaction_path, transaction, "committed")
    return transaction


def _start_v2_uninstall(
    install_root: Path,
    manifest: dict[str, object],
    transaction: dict[str, object] | None,
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    _validate_active_manifest(install_root, manifest, transaction)
    _require_active_resources(install_root, manifest, resources)
    _checkpoint_resources(install_root, manifest, resources)
    uninstall = _new_v2_uninstall_transaction(manifest)
    _validate_v2_record_preimages(install_root, uninstall)
    _write_record(install_root / "transaction.json", uninstall)
    return _continue_v2_uninstall(install_root, uninstall, resources)


def _resume_inactive_v2_uninstall(
    transaction_path: Path, transaction: dict[str, object]
) -> dict[str, object]:
    if transaction.get("state") == "committed":
        return transaction
    if transaction.get("state") != "publishing":
        raise InstallControlError("install_manifest_absent")
    _set_transaction_state(transaction_path, transaction, "committed")
    return transaction


def _active_v2_uninstall(
    install_root: Path,
    manifest: dict[str, object],
    transaction: dict[str, object] | None,
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    if transaction is not None and transaction.get("operation") == "uninstall":
        if transaction.get("state") == "reverting":
            return _resume_v2_uninstall_revert(install_root, transaction, resources)
        return _continue_v2_uninstall(install_root, transaction, resources)
    return _start_v2_uninstall(install_root, manifest, transaction, resources)


def _resume_v2_uninstall_revert(
    install_root: Path,
    transaction: dict[str, object],
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    records = _transaction_resources(transaction)
    ordered = _ordered_transaction_resources(records, resources)
    _v2_rollback_mutations(
        install_root=install_root,
        transaction_path=install_root / "transaction.json",
        transaction=transaction,
        records=records,
        resources=ordered,
    )
    return transaction


def _uninstall_v2_under_lock(
    install_root: Path,
    manifest: dict[str, object] | None,
    transaction: dict[str, object] | None,
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    if manifest is not None:
        return _active_v2_uninstall(install_root, manifest, transaction, resources)
    if transaction is None:
        raise InstallControlError("install_manifest_absent")
    if transaction.get("operation") != "uninstall":
        raise InstallControlError("install_manifest_absent")
    return _resume_inactive_v2_uninstall(install_root / "transaction.json", transaction)


def _uninstall_v1_under_lock(
    install_root: Path,
    manifest: dict[str, object] | None,
    transaction: dict[str, object] | None,
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    transaction_path = install_root / "transaction.json"
    if manifest is not None:
        return _resume_or_start_uninstall(
            install_root=install_root,
            transaction_path=transaction_path,
            transaction=transaction,
            manifest=manifest,
            resources=resources,
        )
    if transaction is not None and transaction.get("operation") == "uninstall":
        if transaction.get("state") == "committed":
            return transaction
    raise InstallControlError("install_manifest_absent")


def _uninstall_under_lock(
    install_root: Path, resources: Sequence[ManagedResource]
) -> dict[str, object]:
    manifest = _optional_install_record(install_root / "manifest.json", "install-manifest/")
    transaction = _optional_install_record(
        install_root / "transaction.json", "install-transaction/"
    )
    if _v2_manifest_record(manifest) or (
        transaction is not None and transaction.get("schema") == "install-transaction/v2"
    ):
        return _uninstall_v2_under_lock(install_root, manifest, transaction, resources)
    return _uninstall_v1_under_lock(install_root, manifest, transaction, resources)


def uninstall_resources(
    *,
    state_root: Path,
    resources: Sequence[ManagedResource],
    lock_timeout: float = 10.0,
) -> dict[str, object]:
    _validate_resources(resources)
    install_root = Path(state_root) / "run" / "install"
    if not install_root.is_dir() or install_root.is_symlink():
        raise InstallControlError("install_state_absent")
    with _install_lock(install_root / "install.lock", lock_timeout):
        return _uninstall_under_lock(install_root, resources)


def _retire_partial_manifest(install_root: Path, transaction: Mapping[str, object]) -> None:
    manifest_path = install_root / "manifest.json"
    manifest = _optional_record(manifest_path, _MANIFEST_SCHEMA)
    if manifest is None:
        return
    expected = transaction.get("target_manifest_sha256")
    actual = _sha256(canonical_json_bytes(manifest))
    if expected != actual:
        raise InstallControlError("install_manifest_transaction_mismatch")
    _retire_manifest(manifest_path, manifest)


def _rollback_checkpoint(manifest: Mapping[str, object]) -> Mapping[str, object]:
    checkpoint = manifest.get("rollback_point")
    if not isinstance(checkpoint, Mapping):
        raise InstallControlError("install_committed_rollback_unavailable")
    return checkpoint


def _checkpoint_by_id(
    checkpoint: Mapping[str, object],
) -> dict[str, Mapping[str, object]]:
    records = checkpoint.get("resources")
    if not isinstance(records, list):
        raise InstallControlError("install_state_schema_invalid")
    if any(not isinstance(record, Mapping) for record in records):
        raise InstallControlError("install_state_schema_invalid")
    return {str(record["id"]): record for record in records}


def _rollback_target(
    active: Mapping[str, object], previous: Mapping[str, object] | None
) -> tuple[Mapping[str, object], Mapping[str, object], dict[str, object]]:
    if previous is not None:
        return previous["desired"], previous["origin"], dict(previous["metadata"])
    metadata = dict(active["metadata"])
    metadata["_retire_after_commit"] = True
    return active["origin"], active["origin"], metadata


def _committed_rollback_record(
    active: Mapping[str, object], previous: Mapping[str, object] | None
) -> dict[str, object]:
    desired, origin, metadata = _rollback_target(active, previous)
    return {
        "desired": desired,
        "id": active["id"],
        "kind": active["kind"],
        "locator": active["locator"],
        "metadata": metadata,
        "origin": origin,
        "rollback": active["desired"],
        "state": "pending",
    }


def _committed_rollback_records(
    manifest: Mapping[str, object], checkpoint: Mapping[str, object]
) -> list[dict[str, object]]:
    previous = _checkpoint_by_id(checkpoint)
    return [
        _committed_rollback_record(active, previous.get(str(active["id"])))
        for active in _transaction_resources(manifest)
    ]


def _new_v2_rollback_transaction(
    manifest: Mapping[str, object],
    checkpoint: Mapping[str, object],
    records: list[dict[str, object]],
) -> dict[str, object]:
    now = _utc_now()
    return {
        "base_manifest_sha256": _sha256(canonical_json_bytes(dict(manifest))),
        "error": None,
        "generation": int(manifest["generation"]) + 1,
        "id": secrets.token_hex(16),
        "installation_id": manifest["installation_id"],
        "operation": "rollback",
        "release": checkpoint["release"],
        "request_sha256": checkpoint["request_sha256"],
        "resources": records,
        "rollback_point": None,
        "scheduler_backend": checkpoint["scheduler_backend"],
        "schema": "install-transaction/v2",
        "started_at": now,
        "state": "prepared",
        "state_root": manifest["state_root"],
        "target_manifest_sha256": None,
        "updated_at": now,
        "vault_root": manifest["vault_root"],
    }


def _continue_v2_rollback(
    install_root: Path,
    transaction: dict[str, object],
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    records = _transaction_resources(transaction)
    ordered = _ordered_transaction_resources(records, resources)
    _continue_v2_operation(
        install_root=install_root,
        transaction_path=install_root / "transaction.json",
        transaction=transaction,
        records=records,
        resources=ordered,
    )
    return transaction


def _resume_v2_rollback(
    install_root: Path,
    manifest: dict[str, object],
    transaction: dict[str, object],
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    if transaction.get("state") == "reverting":
        records = _transaction_resources(transaction)
        ordered = _ordered_transaction_resources(records, resources)
        _v2_rollback_mutations(
            install_root=install_root,
            transaction_path=install_root / "transaction.json",
            transaction=transaction,
            records=records,
            resources=ordered,
        )
        return transaction
    if _published_v2_target(manifest, transaction):
        _complete_v2_publication(install_root / "transaction.json", transaction, manifest)
        return transaction
    _require_v2_base_manifest(manifest, transaction)
    return _continue_v2_rollback(install_root, transaction, resources)


def _start_v2_committed_rollback(
    install_root: Path,
    manifest: dict[str, object],
    transaction: dict[str, object] | None,
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    _validate_active_manifest(install_root, manifest, transaction)
    checkpoint = _rollback_checkpoint(manifest)
    _require_active_resources(install_root, manifest, resources)
    _checkpoint_resources(install_root, manifest, resources)
    records = _committed_rollback_records(manifest, checkpoint)
    rollback = _new_v2_rollback_transaction(manifest, checkpoint, records)
    _validate_v2_record_preimages(install_root, rollback)
    _write_record(install_root / "transaction.json", rollback)
    return _continue_v2_rollback(install_root, rollback, resources)


def _interrupted_v2_operation(transaction: Mapping[str, object] | None) -> bool:
    if transaction is None or transaction.get("operation") not in {"install", "update"}:
        return False
    return transaction.get("state") in {
        "prepared",
        "mutating",
        "publishing",
        "reverting",
    }


def _required_active_manifest(
    manifest: dict[str, object] | None,
) -> dict[str, object]:
    if manifest is None:
        raise InstallControlError("install_manifest_absent")
    return manifest


def _revert_interrupted_v2_operation(
    install_root: Path,
    manifest: dict[str, object] | None,
    transaction: dict[str, object],
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    if _published_v2_target(manifest, transaction):
        active = _complete_v2_publication(install_root / "transaction.json", transaction, manifest)
        return _start_v2_committed_rollback(install_root, active, transaction, resources)
    _require_v2_base_manifest(manifest, transaction)
    records = _transaction_resources(transaction)
    ordered = _ordered_transaction_resources(records, resources)
    transaction_path = install_root / "transaction.json"
    try:
        _v2_rollback_mutations(
            install_root=install_root,
            transaction_path=transaction_path,
            transaction=transaction,
            records=records,
            resources=ordered,
        )
        return transaction
    except Exception as error:
        _record_failure(transaction_path, transaction, error)
        _set_transaction_state(transaction_path, transaction, "quarantined")
        raise


def _rollback_v2_under_lock(
    install_root: Path,
    manifest: dict[str, object] | None,
    transaction: dict[str, object] | None,
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    if _interrupted_v2_operation(transaction):
        return _revert_interrupted_v2_operation(install_root, manifest, transaction, resources)
    resumed = _resume_or_accept_v2_rollback(install_root, manifest, transaction, resources)
    if resumed is not None:
        return resumed
    return _start_v2_committed_rollback(
        install_root, _required_active_manifest(manifest), transaction, resources
    )


def _resume_or_accept_v2_rollback(
    install_root: Path,
    manifest: dict[str, object] | None,
    transaction: dict[str, object] | None,
    resources: Sequence[ManagedResource],
) -> dict[str, object] | None:
    if transaction is None or transaction.get("operation") != "rollback":
        return None
    active = _required_active_manifest(manifest)
    if transaction.get("state") == "committed":
        return _accepted_v2_rollback(active, transaction)
    if transaction.get("state") not in {
        "prepared",
        "mutating",
        "publishing",
        "reverting",
    }:
        return None
    return _resume_v2_rollback(install_root, active, transaction, resources)


def _accepted_v2_rollback(
    manifest: Mapping[str, object], transaction: dict[str, object]
) -> dict[str, object] | None:
    if manifest.get("transaction_id") != transaction.get("id"):
        return None
    return transaction


def _require_v1_rollback_transaction(transaction: Mapping[str, object]) -> None:
    if transaction.get("schema") != "install-transaction/v1":
        raise InstallControlError("install_rollback_operation_invalid")
    if transaction.get("operation") != "install":
        raise InstallControlError("install_rollback_operation_invalid")


def _continue_v1_rollback(
    install_root: Path,
    transaction: dict[str, object],
    records: Sequence[dict[str, object]],
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    transaction_path = install_root / "transaction.json"
    try:
        _rollback_install(
            install_root=install_root,
            transaction_path=transaction_path,
            transaction=transaction,
            records=records,
            resources=resources,
        )
        _retire_partial_manifest(install_root, transaction)
        return transaction
    except Exception as error:
        _record_failure(transaction_path, transaction, error)
        _set_transaction_state(transaction_path, transaction, "quarantined")
        raise


def _rollback_v1_under_lock(
    install_root: Path,
    transaction: dict[str, object],
    resources: Sequence[ManagedResource],
) -> dict[str, object]:
    _require_v1_rollback_transaction(transaction)
    if transaction.get("state") == "reverted":
        return transaction
    if transaction.get("state") not in {"prepared", "mutating", "publishing", "reverting"}:
        raise InstallControlError("install_transaction_blocks_rollback")
    records = _transaction_resources(transaction)
    _require_resource_request(records, resources)
    return _continue_v1_rollback(install_root, transaction, records, resources)


def _v2_manifest_record(manifest: Mapping[str, object] | None) -> bool:
    return manifest is not None and manifest.get("schema") == "install-manifest/v2"


def _rollback_under_lock(
    install_root: Path, resources: Sequence[ManagedResource]
) -> dict[str, object]:
    manifest = _optional_install_record(install_root / "manifest.json", "install-manifest/")
    transaction = _read_install_record(install_root / "transaction.json", "install-transaction/")
    if _v2_manifest_record(manifest) or transaction.get("schema") == "install-transaction/v2":
        return _rollback_v2_under_lock(install_root, manifest, transaction, resources)
    return _rollback_v1_under_lock(install_root, transaction, resources)


def rollback_resources(
    *,
    state_root: Path,
    resources: Sequence[ManagedResource],
    lock_timeout: float = 10.0,
) -> dict[str, object]:
    _validate_resources(resources)
    install_root = Path(state_root) / "run" / "install"
    if not install_root.is_dir() or install_root.is_symlink():
        raise InstallControlError("install_state_absent")
    with _install_lock(install_root / "install.lock", lock_timeout):
        return _rollback_under_lock(install_root, resources)


def _install_health(
    status: str,
    health: str,
    codes: Sequence[str],
    deletion_codes: Sequence[str],
) -> dict[str, object]:
    return {
        "codes": sorted(set(codes)),
        "deletion_codes": sorted(set(deletion_codes)),
        "health": health,
        "status": status,
    }


def _validate_preimage_references(
    install_root: Path, records: Sequence[Mapping[str, object]]
) -> None:
    total = 0
    for record in records:
        _installed, origin = _resource_snapshots(record)
        if origin.get("state") == "absent":
            continue
        value = _read_origin(install_root, origin)
        total += len(value or b"")
    if total > MAX_TOTAL_PREIMAGE_BYTES:
        raise InstallControlError("install_preimages_too_large")


def _v2_record_preimages(install_root: Path, record: Mapping[str, object]) -> int:
    desired, origin, rollback = _v2_resource_snapshots(record)
    values = (
        _read_v2_snapshot(install_root, desired),
        _read_v2_snapshot(install_root, origin),
        _read_v2_snapshot(install_root, rollback),
    )
    return sum(len(value or b"") for value in values)


def _v2_preimage_size(install_root: Path, records: Sequence[Mapping[str, object]]) -> int:
    return sum(_v2_record_preimages(install_root, record) for record in records)


def _v2_checkpoint_records(checkpoint: object) -> list[Mapping[str, object]]:
    if not isinstance(checkpoint, Mapping):
        raise InstallControlError("install_state_schema_invalid")
    records = checkpoint.get("resources")
    if not isinstance(records, list):
        raise InstallControlError("install_state_schema_invalid")
    if any(not isinstance(record, Mapping) for record in records):
        raise InstallControlError("install_state_schema_invalid")
    return records


def _v2_checkpoint_resource_size(install_root: Path, record: Mapping[str, object]) -> int:
    desired = _record_desired(record)
    origin = record.get("origin")
    if not isinstance(origin, Mapping):
        raise InstallControlError("install_state_schema_invalid")
    values = (
        _read_v2_snapshot(install_root, desired),
        _read_v2_snapshot(install_root, origin),
    )
    return sum(len(value or b"") for value in values)


def _v2_checkpoint_preimages(install_root: Path, checkpoint: object) -> int:
    if checkpoint is None:
        return 0
    records = _v2_checkpoint_records(checkpoint)
    return sum(_v2_checkpoint_resource_size(install_root, record) for record in records)


def _validate_v2_record_preimages(install_root: Path, record: Mapping[str, object]) -> None:
    records = _transaction_resources(record)
    total = _v2_preimage_size(install_root, records)
    total += _v2_checkpoint_preimages(install_root, record.get("rollback_point"))
    if total > MAX_TOTAL_PREIMAGE_BYTES:
        raise InstallControlError("install_preimages_too_large")


def _validate_v2_manifest_transaction(
    install_root: Path,
    manifest: Mapping[str, object],
    transaction: Mapping[str, object],
) -> None:
    if manifest.get("transaction_id") != transaction.get("id"):
        raise InstallControlError("install_manifest_transaction_mismatch")
    if manifest.get("request_sha256") != transaction.get("request_sha256"):
        raise InstallControlError("install_manifest_transaction_mismatch")
    expected = transaction.get("target_manifest_sha256")
    actual = _sha256(canonical_json_bytes(dict(manifest)))
    if expected != actual:
        raise InstallControlError("install_manifest_transaction_mismatch")
    if _v2_manifest_from_transaction(transaction) != manifest:
        raise InstallControlError("install_manifest_transaction_mismatch")
    _validate_v2_record_preimages(install_root, manifest)
    _validate_v2_record_preimages(install_root, transaction)


def _validate_manifest_transaction(
    install_root: Path,
    manifest: Mapping[str, object],
    transaction: Mapping[str, object],
) -> None:
    if manifest.get("transaction_id") != transaction.get("id"):
        raise InstallControlError("install_manifest_transaction_mismatch")
    if manifest.get("request_sha256") != transaction.get("request_sha256"):
        raise InstallControlError("install_manifest_transaction_mismatch")
    expected = transaction.get("target_manifest_sha256")
    actual = _sha256(canonical_json_bytes(dict(manifest)))
    if expected != actual:
        raise InstallControlError("install_manifest_transaction_mismatch")
    _validate_preimage_references(install_root, _transaction_resources(manifest))


def _required_install_transaction(
    transaction: Mapping[str, object] | None,
) -> Mapping[str, object]:
    if transaction is None:
        raise InstallControlError("install_manifest_without_transaction")
    if transaction.get("operation") != "install":
        raise InstallControlError("install_manifest_without_transaction")
    return transaction


def _active_health_for_state(state: object) -> dict[str, object]:
    values = {
        "committed": _install_health("active", "ok", [], ["install_manifest_retained"]),
        "quarantined": _install_health(
            "quarantined",
            "error",
            ["install_transaction_quarantined"],
            ["install_manifest_retained", "install_transaction_quarantined"],
        ),
        "publishing": _install_health(
            "nonterminal",
            "degraded",
            ["install_transaction_nonterminal"],
            ["install_manifest_retained", "install_transaction_nonterminal"],
        ),
    }
    try:
        return values[str(state)]
    except KeyError as exc:
        raise InstallControlError("install_manifest_transaction_mismatch") from exc


def _active_install_health(
    install_root: Path,
    manifest: Mapping[str, object],
    transaction: Mapping[str, object] | None,
) -> dict[str, object]:
    required = _required_install_transaction(transaction)
    _validate_manifest_transaction(install_root, manifest, required)
    return _active_health_for_state(required.get("state"))


def _active_v2_install_health(
    install_root: Path,
    manifest: Mapping[str, object],
    transaction: Mapping[str, object] | None,
) -> dict[str, object]:
    if transaction is None or transaction.get("schema") != "install-transaction/v2":
        raise InstallControlError("install_manifest_without_transaction")
    if transaction.get("operation") not in {
        "install",
        "update",
        "rollback",
        "uninstall",
    }:
        raise InstallControlError("install_manifest_without_transaction")
    if manifest.get("transaction_id") == transaction.get("id"):
        _validate_v2_manifest_transaction(install_root, manifest, transaction)
        return _active_health_for_state(transaction.get("state"))
    return _active_v2_base_health(install_root, manifest, transaction)


def _active_v2_base_health_state(state: object) -> dict[str, object]:
    if state == "quarantined":
        return _install_health(
            "quarantined",
            "error",
            ["install_transaction_quarantined"],
            ["install_manifest_retained", "install_transaction_quarantined"],
        )
    if state == "reverted":
        return _install_health("active", "ok", [], ["install_manifest_retained"])
    if state in {"prepared", "mutating", "publishing", "reverting"}:
        return _install_health(
            "nonterminal",
            "degraded",
            ["install_transaction_nonterminal"],
            ["install_manifest_retained", "install_transaction_nonterminal"],
        )
    raise InstallControlError("install_manifest_transaction_mismatch")


def _active_v2_base_health(
    install_root: Path,
    manifest: Mapping[str, object],
    transaction: Mapping[str, object],
) -> dict[str, object]:
    if transaction.get("base_manifest_sha256") != _manifest_digest(manifest):
        raise InstallControlError("install_manifest_transaction_mismatch")
    _validate_v2_base_linkage(manifest, transaction)
    _validate_v2_record_preimages(install_root, transaction)
    return _active_v2_base_health_state(transaction.get("state"))


def _validate_v2_base_linkage(
    manifest: Mapping[str, object], transaction: Mapping[str, object]
) -> None:
    if transaction.get("generation") != int(manifest["generation"]) + 1:
        raise InstallControlError("install_manifest_transaction_mismatch")
    linked = ("installation_id", "state_root", "vault_root")
    if any(transaction.get(key) != manifest.get(key) for key in linked):
        raise InstallControlError("install_manifest_transaction_mismatch")


def _active_install_health_for_schema(
    install_root: Path,
    manifest: Mapping[str, object],
    transaction: Mapping[str, object] | None,
) -> dict[str, object]:
    if (
        transaction is not None
        and transaction.get("schema") == "install-transaction/v2"
        and manifest.get("transaction_id") != transaction.get("id")
    ):
        return _active_v2_base_health(install_root, manifest, transaction)
    if manifest.get("schema") == "install-manifest/v2":
        return _active_v2_install_health(install_root, manifest, transaction)
    return _active_install_health(install_root, manifest, transaction)


def _inactive_committed_health(transaction: Mapping[str, object]) -> dict[str, object]:
    if transaction.get("operation") != "uninstall":
        raise InstallControlError("install_transaction_orphaned")
    return _install_health("uninstalled", "ok", [], [])


def _inactive_health_for_state(
    transaction: Mapping[str, object],
) -> dict[str, object]:
    nonterminal = _install_health(
        "nonterminal",
        "degraded",
        ["install_transaction_nonterminal"],
        ["install_transaction_nonterminal"],
    )
    handlers: dict[str, Callable[[], dict[str, object]]] = {
        "committed": lambda: _inactive_committed_health(transaction),
        "mutating": lambda: nonterminal,
        "prepared": lambda: nonterminal,
        "publishing": lambda: nonterminal,
        "quarantined": lambda: _install_health(
            "quarantined",
            "error",
            ["install_transaction_quarantined"],
            ["install_transaction_quarantined"],
        ),
        "reverted": lambda: _install_health("rolled_back", "ok", [], []),
        "reverting": lambda: nonterminal,
    }
    try:
        return handlers[str(transaction.get("state"))]()
    except KeyError as exc:
        raise InstallControlError("install_transaction_orphaned") from exc


def _inactive_install_health(
    install_root: Path, transaction: Mapping[str, object]
) -> dict[str, object]:
    _validate_preimage_references(install_root, _transaction_resources(transaction))
    return _inactive_health_for_state(transaction)


def _inactive_install_health_for_schema(
    install_root: Path, transaction: Mapping[str, object]
) -> dict[str, object]:
    if transaction.get("schema") == "install-transaction/v2":
        _validate_v2_record_preimages(install_root, transaction)
        return _inactive_health_for_state(transaction)
    return _inactive_install_health(install_root, transaction)


def _validate_install_records(install_root: Path) -> dict[str, object]:
    manifest = _optional_install_record(install_root / "manifest.json", "install-manifest/")
    transaction = _optional_install_record(
        install_root / "transaction.json", "install-transaction/"
    )
    if manifest is not None:
        return _active_install_health_for_schema(install_root, manifest, transaction)
    if transaction is not None:
        return _inactive_install_health_for_schema(install_root, transaction)
    entries = {entry.name for entry in install_root.iterdir()}
    if entries <= {"install.lock", "preimages", "scheduler"}:
        return _install_health("absent", "ok", [], [])
    raise InstallControlError("install_artifact_state_unknown")


def _install_root_kind(install_root: Path) -> str:
    try:
        metadata = install_root.lstat()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unsafe"
    if stat.S_ISLNK(metadata.st_mode):
        return "unsafe"
    if stat.S_ISDIR(metadata.st_mode):
        return "directory"
    return "unsafe"


def validate_install_state(state_root: Path) -> dict[str, object]:
    install_root = Path(state_root) / "run" / "install"
    kind = _install_root_kind(install_root)
    if kind == "missing":
        return _install_health("absent", "ok", [], [])
    if kind != "directory":
        return _install_health(
            "corrupt", "error", ["install_state_corrupt"], ["install_state_corrupt"]
        )
    try:
        return _validate_install_records(install_root)
    except (InstallControlError, OSError, UnicodeError, ValueError):
        return _install_health(
            "corrupt", "error", ["install_state_corrupt"], ["install_state_corrupt"]
        )


def inspect_install_state(state_root: Path) -> dict[str, str]:
    install_root = Path(state_root) / "run" / "install"
    manifest = "present" if (install_root / "manifest.json").is_file() else "absent"
    transaction = "present" if (install_root / "transaction.json").is_file() else "absent"
    status = "absent" if manifest == transaction == "absent" else "present"
    return {"manifest": manifest, "status": status, "transaction": transaction}


def _required_command_output(command: tuple[str, ...], code: str) -> bytes:
    exit_code, output = _default_command_runner(command, None)
    if exit_code != 0:
        raise InstallControlError(code)
    return output


def _project_version(root: Path) -> str:
    content = (Path(root) / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', content, re.MULTILINE)
    if match is None:
        raise InstallControlError("install_project_version_missing")
    return match.group(1)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _source_mode() -> str:
    if os.environ.get("LLM_WIKI_INSTALLER_CREATED_CLONE") == "1":
        return "pinned_remote"
    return "local_checkout"


def build_release_identity(root: Path) -> dict[str, object]:
    root = Path(root).resolve()
    commit = (
        _required_command_output(
            ("git", "-C", str(root), "rev-parse", "HEAD"),
            "install_release_identity_failed",
        )
        .decode("ascii", errors="strict")
        .strip()
        .lower()
    )
    if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit) is None:
        raise InstallControlError("install_release_identity_invalid")
    status = _required_command_output(
        ("git", "-C", str(root), "status", "--porcelain"),
        "install_release_status_failed",
    )
    return {
        "commit_oid": commit,
        "project_version": _project_version(root),
        "source_mode": _source_mode(),
        "uv_lock_sha256": _file_sha256(root / "uv.lock"),
        "worktree_clean": not bool(status.strip()),
    }


def _systemd_user_available(systemctl: str | None) -> bool:
    if systemctl is None:
        return False
    exit_code, _output = _default_command_runner((systemctl, "--user", "show-environment"), None)
    return exit_code == 0


def _xdg_systemd_directory() -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME")
    if configured and Path(configured).is_absolute():
        return Path(configured) / "systemd" / "user"
    return Path.home() / ".config" / "systemd" / "user"


def _require_profile(profile: Path | None) -> Path:
    if profile is None:
        raise InstallControlError("install_profile_required")
    return profile


def _posix_scheduler_resource(
    *,
    backend: str,
    root: Path,
    state_root: Path,
    uv_path: Path,
    ownership_metadata: Mapping[str, Mapping[str, object]],
) -> ManagedResource:
    builders: dict[str, Callable[[], ManagedResource]] = {
        "cron": lambda: cron_scheduler_resource(
            root=root,
            state_root=state_root,
            uv_path=uv_path,
            ownership_metadata=ownership_metadata.get("cron-user-maintenance"),
        ),
        "launchd": lambda: launchd_scheduler_resource(
            root=root,
            state_root=state_root,
            uv_path=uv_path,
            launch_agents_directory=Path.home() / "Library" / "LaunchAgents",
            uid=os.getuid(),
            launchctl=shutil.which("launchctl") or "launchctl",
        ),
        "systemd_user": lambda: systemd_scheduler_resource(
            root=root,
            state_root=state_root,
            uv_path=uv_path,
            unit_directory=_xdg_systemd_directory(),
            systemctl=shutil.which("systemctl") or "systemctl",
        ),
    }
    try:
        return builders[backend]()
    except KeyError as exc:
        raise InstallControlError("install_scheduler_backend_invalid") from exc


def _config_platform() -> str:
    """`opencode_global_dir` speaks posix/windows, not `sys.platform` names."""
    if sys.platform.startswith("win"):
        return "windows"
    return "posix"


def _opencode_plugin_destination(home: Path) -> Path:
    from installer_config import opencode_global_dir

    directory = opencode_global_dir(
        home, os.environ.get("XDG_CONFIG_HOME"), platform=_config_platform()
    )
    return directory / "plugins" / "llm-wiki-memory.js"


def _claude_settings_destination(home: Path) -> Path:
    return home / ".claude" / "settings.json"


def _codex_hooks_destination(home: Path) -> Path:
    return home / ".codex" / "hooks.json"


def _recorded_config_existed(
    metadata: Mapping[str, Mapping[str, object]], resource_id: str
) -> bool | None:
    recorded = metadata.get(resource_id)
    if not isinstance(recorded, Mapping) or "config_existed" not in recorded:
        return None
    return bool(recorded["config_existed"])


def _ide_hook_factories(
    root: Path,
    home: Path,
    state_root: Path | None,
    metadata: Mapping[str, Mapping[str, object]],
) -> dict[str, Callable[[], ManagedResource]]:
    """One factory per owned IDE resource, built only when it is selected."""
    from integration_hook_config import (
        claude_settings_resource,
        claude_settings_template,
        codex_hooks_resource,
        codex_hooks_template,
        managed_ide_hook_resources,
        opencode_plugin_resource,
    )

    def _managed(resource_id: str) -> ManagedResource:
        for resource in managed_ide_hook_resources(root, home):
            if resource.resource_id == resource_id:
                return resource
        raise InstallControlError("install_resource_unknown")

    return {
        "cursor-user-hooks": lambda: _managed("cursor-user-hooks"),
        "antigravity-user-hooks": lambda: _managed("antigravity-user-hooks"),
        "opencode-plugin": lambda: opencode_plugin_resource(
            root, _opencode_plugin_destination(home)
        ),
        "claude-user-settings": lambda: claude_settings_resource(
            _claude_settings_destination(home),
            claude_settings_template(root),
            root,
            state_root or root,
            config_existed=_recorded_config_existed(metadata, "claude-user-settings"),
        ),
        "codex-user-hooks": lambda: codex_hooks_resource(
            _codex_hooks_destination(home),
            codex_hooks_template(root),
            config_existed=_recorded_config_existed(metadata, "codex-user-hooks"),
        ),
    }


def _selected_ide_hook_resources(
    root: Path,
    home: Path,
    cursor_hooks: bool,
    antigravity_hooks: bool,
    opencode_plugin: bool = False,
    claude_settings: bool = False,
    codex_hooks: bool = False,
    state_root: Path | None = None,
    metadata: Mapping[str, Mapping[str, object]] | None = None,
) -> list[ManagedResource]:
    wanted = {
        "cursor-user-hooks": cursor_hooks,
        "antigravity-user-hooks": antigravity_hooks,
        "opencode-plugin": opencode_plugin,
        "claude-user-settings": claude_settings,
        "codex-user-hooks": codex_hooks,
    }
    factories = _ide_hook_factories(root, home, state_root, metadata or {})
    return [factories[key]() for key, selected in wanted.items() if selected]


def _posix_install_resources(
    *,
    backend: str,
    root: Path,
    state_root: Path,
    uv_path: Path,
    profile: Path | None,
    metadata: Mapping[str, Mapping[str, object]],
) -> list[ManagedResource]:
    return [
        profile_resource(
            _require_profile(profile),
            root,
            state_root,
            metadata.get("unix-profile"),
        ),
        _posix_scheduler_resource(
            backend=backend,
            root=root,
            state_root=state_root,
            uv_path=uv_path,
            ownership_metadata=metadata,
        ),
    ]


def _windows_install_resources(
    *,
    root: Path,
    state_root: Path,
    uv_path: Path,
    powershell_path: str | None,
) -> list[ManagedResource]:
    if powershell_path is None:
        raise InstallControlError("install_powershell_path_required")
    return [
        *windows_environment_resources(root, state_root),
        windows_task_scheduler_resource(
            root=root,
            state_root=state_root,
            uv_path=uv_path,
            script_path=root / "scripts" / "install-scheduled-tasks.ps1",
            powershell=powershell_path,
        ),
    ]


def _base_install_resources(
    *,
    backend: str,
    root: Path,
    state_root: Path,
    uv_path: Path,
    profile: Path | None,
    powershell_path: str | None,
    metadata: Mapping[str, Mapping[str, object]],
) -> list[ManagedResource]:
    if backend == "task_scheduler":
        return _windows_install_resources(
            root=root,
            state_root=state_root,
            uv_path=uv_path,
            powershell_path=powershell_path,
        )
    return _posix_install_resources(
        backend=backend,
        root=root,
        state_root=state_root,
        uv_path=uv_path,
        profile=profile,
        metadata=metadata,
    )


def build_install_resources(
    *,
    backend: str,
    root: Path,
    state_root: Path,
    uv_path: Path,
    home: Path,
    profile: Path | None,
    powershell_path: str | None,
    cursor_hooks: bool = False,
    antigravity_hooks: bool = False,
    opencode_plugin: bool = False,
    claude_settings: bool = False,
    codex_hooks: bool = False,
    ownership_metadata: Mapping[str, Mapping[str, object]] | None = None,
) -> list[ManagedResource]:
    metadata = ownership_metadata or {}
    return [
        *_base_install_resources(
            backend=backend,
            root=root,
            state_root=state_root,
            uv_path=uv_path,
            profile=profile,
            powershell_path=powershell_path,
            metadata=metadata,
        ),
        *_selected_ide_hook_resources(
            root,
            home,
            cursor_hooks,
            antigravity_hooks,
            opencode_plugin,
            claude_settings,
            codex_hooks,
            state_root,
            metadata,
        ),
    ]


def _selected_backend(requested: str) -> str:
    systemctl = shutil.which("systemctl")
    available = False
    if sys.platform == "linux":
        available = _systemd_user_available(systemctl)
    return select_scheduler_backend(sys.platform, requested, available)


def _install_from_args(args: argparse.Namespace) -> dict[str, object]:
    root = args.root.resolve()
    state_root = args.state_root.resolve()
    uv_path = args.uv_path.resolve()
    backend = _selected_backend(args.scheduler)
    resources = build_install_resources(
        backend=backend,
        root=root,
        state_root=state_root,
        uv_path=uv_path,
        home=args.home.resolve(),
        profile=args.profile,
        powershell_path=args.powershell_path,
        cursor_hooks=args.cursor_hooks,
        antigravity_hooks=args.antigravity_hooks,
        opencode_plugin=args.opencode_plugin,
        claude_settings=args.claude_settings,
        codex_hooks=args.codex_hooks,
        ownership_metadata=None,
    )
    manifest = install_resources(
        state_root=state_root,
        vault_root=root,
        release=build_release_identity(root),
        scheduler_backend=backend,
        resources=resources,
        control_version=2,
    )
    return {
        "scheduler_backend": backend,
        "status": "committed",
        "transaction_id": manifest["transaction_id"],
    }


def _record_metadata(record: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    result: dict[str, Mapping[str, object]] = {}
    for resource in _transaction_resources(record):
        resource_id = resource.get("id")
        metadata = resource.get("metadata")
        if not isinstance(resource_id, str) or not isinstance(metadata, Mapping):
            raise InstallControlError("install_state_schema_invalid")
        result[resource_id] = metadata
    return result


def _require_record_roots(record: Mapping[str, object], root: Path, state_root: Path) -> None:
    if record.get("vault_root") != str(root):
        raise InstallControlError("install_vault_root_mismatch")
    if record.get("state_root") != str(state_root):
        raise InstallControlError("install_state_root_mismatch")


def _record_backend(record: Mapping[str, object]) -> str:
    backend = record.get("scheduler_backend")
    if not isinstance(backend, str) or backend not in _BACKENDS:
        raise InstallControlError("install_scheduler_backend_invalid")
    return backend


def _existing_record(state_root: Path, command: str) -> dict[str, object]:
    install_root = state_root / "run" / "install"
    if command == "uninstall":
        return _read_install_record(install_root / "manifest.json", "install-manifest/")
    return _read_install_record(install_root / "transaction.json", "install-transaction/")


def _record_resource_ids(record: Mapping[str, object]) -> set[str]:
    return {str(resource["id"]) for resource in _transaction_resources(record)}


def _resources_from_existing_args(args: argparse.Namespace, command: str) -> list[ManagedResource]:
    root = args.root.resolve()
    state_root = args.state_root.resolve()
    record = _existing_record(state_root, command)
    _require_record_roots(record, root, state_root)
    identifiers = _record_resource_ids(record)
    return build_install_resources(
        backend=_record_backend(record),
        root=root,
        state_root=state_root,
        uv_path=args.uv_path.resolve(),
        home=args.home.resolve(),
        profile=args.profile,
        powershell_path=args.powershell_path,
        cursor_hooks="cursor-user-hooks" in identifiers,
        antigravity_hooks="antigravity-user-hooks" in identifiers,
        opencode_plugin="opencode-plugin" in identifiers,
        claude_settings="claude-user-settings" in identifiers,
        codex_hooks="codex-user-hooks" in identifiers,
        ownership_metadata=_record_metadata(record),
    )


def _rollback_from_args(args: argparse.Namespace) -> dict[str, object]:
    result = rollback_resources(
        state_root=args.state_root.resolve(),
        resources=_resources_from_existing_args(args, "rollback"),
    )
    return {"state": result["state"], "status": "rolled_back"}


def _uninstall_from_args(args: argparse.Namespace) -> dict[str, object]:
    result = uninstall_resources(
        state_root=args.state_root.resolve(),
        resources=_resources_from_existing_args(args, "uninstall"),
    )
    return {"state": result["state"], "status": "uninstalled"}


def _status_from_args(args: argparse.Namespace) -> dict[str, str]:
    return inspect_install_state(args.state_root)


def _add_existing_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--uv-path", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--powershell-path")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    status = subparsers.add_parser("status")
    status.add_argument("--state-root", type=Path, required=True)
    install = subparsers.add_parser("install")
    install.add_argument("--root", type=Path, required=True)
    install.add_argument("--state-root", type=Path, required=True)
    install.add_argument("--uv-path", type=Path, required=True)
    install.add_argument("--home", type=Path, required=True)
    install.add_argument("--scheduler", choices=("native", "cron"), default="native")
    install.add_argument("--profile", type=Path)
    install.add_argument("--powershell-path")
    install.add_argument("--cursor-hooks", action="store_true")
    install.add_argument("--antigravity-hooks", action="store_true")
    install.add_argument("--opencode-plugin", action="store_true")
    install.add_argument("--claude-settings", action="store_true")
    install.add_argument("--codex-hooks", action="store_true")
    _add_existing_arguments(subparsers.add_parser("rollback"))
    _add_existing_arguments(subparsers.add_parser("uninstall"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        handlers: dict[str, Callable[[argparse.Namespace], Mapping[str, object]]] = {
            "install": _install_from_args,
            "rollback": _rollback_from_args,
            "status": _status_from_args,
            "uninstall": _uninstall_from_args,
        }
        result = handlers[args.command](args)
        print(canonical_json_bytes(result).decode("utf-8"))
        return 0
    except (InstallControlError, OSError, UnicodeError, ValueError) as exc:
        print(f"install control failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
