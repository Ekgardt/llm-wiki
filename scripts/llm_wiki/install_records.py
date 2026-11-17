"""Install records, errors, the install lock and command running."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from install_state import (
    MAX_RECORD_BYTES,
    WHOLLY_OWNED_KINDS,
    InstallControlError,
    ManagedResource,
    atomic_write,
    read_state_bytes,
    replaced_whole,
    strict_json_object,
)
from llm_wiki.formats.canonical import canonical_json_bytes
from llm_wiki.formats.schema import validate_schema
from llm_wiki.runtime_publication import write_all
from reliable_memory import fsync_directory

# Basis unknown: value predates measurement; review when an install manages more resources.
MAX_RESOURCES = 32


BACKENDS = {"cron", "launchd", "systemd_user", "task_scheduler"}


PROFILE_START = b"# >>> LLM-Wiki installer >>>"


PROFILE_END = b"# <<< LLM-Wiki installer <<<"


CRON_START = b"# LLM-Wiki-cron-start"


CRON_END = b"# LLM-Wiki-cron-end"


class ResourceDriftError(InstallControlError):
    """A resource the installer owns no longer holds what it recorded, named with the way on.

    The message is for the operator; `code` is the stable record a transaction keeps.
    See `docs/research/2026-09-28-a-changed-file-is-named-and-can-be-taken-over.md`.
    """

    def __init__(self, code: str, resource: ManagedResource) -> None:
        super().__init__(
            f"{code}: {resource.resource_id} ({resource.locator}) was changed outside the "
            f"installer; {drift_next_step(resource)}"
        )
        self.code = code


class ExplainedInstallError(InstallControlError):
    """A refusal with the file and the command for the operator; `code` stays bare."""

    def __init__(self, code: str, explanation: str) -> None:
        super().__init__(f"{code}: {explanation}")
        self.code = code


def error_code(error: BaseException) -> str:
    """The stable code of an install error, without the operator's explanation."""
    return getattr(error, "code", None) or str(error)


def taken_as_is(resource: ManagedResource) -> bool:
    """Whether the resource is recorded as it is on disk, whatever it holds."""
    return resource.adopt_current or replaced_whole(resource)


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def write_record(path: Path, value: Mapping[str, object]) -> None:
    encoded = canonical_json_bytes(dict(value))
    if len(encoded) > MAX_RECORD_BYTES:
        raise InstallControlError("install_record_too_large")
    atomic_write(path, encoded)


def read_record(path: Path, schema: Path) -> dict[str, object]:
    value = strict_json_object(read_state_bytes(path))
    try:
        validate_schema(value, schema)
    except ValueError as exc:
        raise InstallControlError("install_state_schema_invalid") from exc
    return value


def optional_record(path: Path, schema: Path) -> dict[str, object] | None:
    if not path.exists() and not path.is_symlink():
        return None
    return read_record(path, schema)


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
            _wait_before_lock_retry(exc, deadline)


def _wait_before_lock_retry(failure: OSError, deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise InstallControlError("install_lock_timeout") from failure
    time.sleep(0.05)


@contextmanager
def install_lock(path: Path, timeout: float) -> Iterator[None]:
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    acquired = False
    try:
        if os.fstat(descriptor).st_size == 0:
            write_all(descriptor, b"0")
            os.fsync(descriptor)
        os.chmod(path, 0o600)
        _acquire_lock(descriptor, timeout)
        acquired = True
        yield
    finally:
        if acquired:
            _unlock(descriptor)
        os.close(descriptor)


def prepare_install_root(state_root: Path) -> Path:
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


CommandRunner = Callable[[tuple[str, ...], bytes | None], tuple[int, bytes]]


def default_command_runner(
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
    from installer_config import bounded_process_output

    completed = bounded_process_output(
        command,
        cwd=Path.cwd(),
        environment=os.environ.copy(),
        timeout_seconds=30.0,
        max_bytes=MAX_RECORD_BYTES,
    )
    if completed is None:
        return 124, b""
    return completed


def try_command(runner: CommandRunner, command: tuple[str, ...]) -> None:
    """Run a teardown step whose own exit code settles nothing.

    Disabling or cleaning units the manager never registered exits nonzero,
    and that is precisely the state the teardown is trying to reach. Whether
    the teardown worked is decided by verifying the projection afterwards, so
    letting these steps stop it is what strands a half-registered scheduler.
    """
    _exit_code, output = runner(command, None)
    if len(output) > MAX_RECORD_BYTES:
        raise InstallControlError("install_scheduler_output_oversized")


def require_command(runner: CommandRunner, command: tuple[str, ...]) -> bytes:
    exit_code, output = runner(command, None)
    if exit_code != 0:
        raise InstallControlError("install_scheduler_command_failed")
    if len(output) > MAX_RECORD_BYTES:
        raise InstallControlError("install_scheduler_output_oversized")
    return output


# What a resource is inside a transaction: its name, its kind and where it lives. A
# profile moved to another file is a second resource under the same name, so one
# update can write the new one before it takes the old one back
# (docs/research/2026-09-28-what-an-update-created-its-rollback-takes-back.md).
Identity = tuple[str, str, str]


def resource_identity(resource: ManagedResource) -> Identity:
    return (resource.resource_id, resource.kind, resource.locator)


def record_identity(record: Mapping[str, object]) -> Identity:
    return (str(record.get("id")), str(record.get("kind")), str(record.get("locator")))


def resources_by_identity(
    resources: Sequence[ManagedResource],
) -> dict[Identity, ManagedResource]:
    return {resource_identity(resource): resource for resource in resources}


def records_by_identity(
    records: Sequence[Mapping[str, object]],
) -> dict[Identity, Mapping[str, object]]:
    return {record_identity(record): record for record in records}


def adoptable(resource: ManagedResource) -> bool:
    """`_UNREADABLE_KINDS` cannot be recorded as a rollback point; the wholly owned kinds
    need no `--adopt`, an update replaces them
    (docs/research/2026-09-28-an-update-replaces-what-it-owns.md)."""
    return resource.kind not in UNREADABLE_KINDS


_NEXT_STEP_BY_KIND = {
    **{kind: "rerun the installer: it replaces the file and keeps the changed one" for kind in WHOLLY_OWNED_KINDS},
    "systemd_drop_in": (
        "it was changed after the update that wrote it; restore or remove it by hand, "
        "then run `install_control.py rollback` again"
    ),
}


def drift_next_step(resource: ManagedResource) -> str:
    """What the operator can do about a changed resource, as a command where there is one."""
    known = _NEXT_STEP_BY_KIND.get(resource.kind)
    if known is not None:
        return known
    if adoptable(resource):
        return (
            f"rerun the installer with --adopt {resource.resource_id} to take it over as it "
            "is now (it becomes the rollback point)"
        )
    return (
        "restore it or remove it by hand, or run `install_control.py uninstall`, "
        "then run the installer again"
    )


# A cron table entry or a Windows task cannot be read back as it is: it can be neither
# adopted as a rollback point nor installed beside the one it replaces.
UNREADABLE_KINDS = frozenset({"windows_task_scheduler", "cron_scheduler"})
