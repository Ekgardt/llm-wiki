"""How one generation of an LSP child is launched and guarded against reentrancy."""
from __future__ import annotations

import math
import os
import re
import shutil
import stat
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import TypeVar

from llm_wiki import lsp_process_limits
from llm_wiki.lsp_process_limits import (
    MAX_GENERATION_LAUNCH_ARGUMENTS,
    MAX_GENERATION_LAUNCH_BYTES,
)
from llm_wiki.lsp_process_owner import (
    validated_deadline,
)
from llm_wiki.lsp_process_state import (
    GenerationBootstrap,
    GenerationConfiguration,
    GenerationGuardFactory,
    GenerationLaunch,
    LifecycleCoordinator,
    LspProcessHandle,
    lsp_environment,
)

_LIFECYCLE_REENTRANCY_ERROR = (
    "LSP lifecycle operations are not reentrant from protocol callbacks"
)
_PROTOCOL_CALLBACK_CONTEXT = threading.local()


def _check_launch_command_bounds(arguments: tuple[str, ...]) -> None:
    """A launch command stays within its argument and byte bounds."""
    if len(arguments) > MAX_GENERATION_LAUNCH_ARGUMENTS:
        raise ValueError("generation launch command exceeds its argument bound")
    try:
        command_bytes = sum(
            len(argument.encode("utf-8", errors="strict")) + 1
            for argument in arguments
        )
    except UnicodeEncodeError as error:
        raise ValueError(
            "generation launch command must be valid UTF-8"
        ) from error
    if command_bytes > MAX_GENERATION_LAUNCH_BYTES:
        raise ValueError("generation launch command exceeds its byte bound")


def _launch_pass_fds(launch: GenerationLaunch) -> tuple[int, ...]:
    """The descriptors to inherit; only POSIX can inherit any."""
    if not isinstance(launch.pass_fds, tuple):
        raise TypeError("generation launch pass_fds must be a tuple")
    pass_fds = tuple(launch.pass_fds)
    if pass_fds and os.name != "posix":
        raise ValueError("generation launch pass_fds are supported only on POSIX")
    return pass_fds


def _launch_inside_owner(name: str, owner_root: Path | None) -> bool:
    """True when the program is the verified copy this owner root holds.

    A native server cannot be executed through an unlinked descriptor: gopls
    reads its own executable to hash it and to start its telemetry child, and
    both fail once the file is gone (measured 2026-09-12: `can't hash gopls
    executable: no such file or directory`, exit 1). The verified copy is
    therefore launched by path from the owner root, which is private to this
    process tree, and the launch is still not allowed to name anything else.
    Research: `docs/research/2026-09-12-installing-go-and-building-gopls.md`.
    """
    if owner_root is None:
        return False
    candidate = Path(name)
    return candidate.is_absolute() and candidate.parent == owner_root.resolve()


def generation_launch(
    command: Sequence[str],
    launch: GenerationLaunch | None,
    *,
    cwd: Path,
    owner_root: Path | None = None,
) -> tuple[tuple[str, ...], tuple[int, ...]]:
    if launch is None:
        return tuple(command), ()
    return (
        _launch_arguments(command, launch, cwd, owner_root),
        _launch_pass_fds(launch),
    )


def _launch_arguments(
    command: Sequence[str],
    launch: GenerationLaunch,
    cwd: Path,
    owner_root: Path | None = None,
) -> tuple[str, ...]:
    if not isinstance(launch.command, tuple):
        raise TypeError("generation launch command must be a tuple")
    arguments = tuple(validated_command(launch.command, cwd))
    _check_launch_command_bounds(arguments)
    require_same_program(arguments, command, launch, owner_root)
    return arguments


def require_same_program(
    arguments: tuple[str, ...],
    command: Sequence[str],
    launch: GenerationLaunch,
    owner_root: Path | None = None,
) -> None:
    """A generation may verify the program, never substitute a different one.

    An interpreted server keeps its interpreter as argv[0] and the verified
    copy arrives as an argument. A native server has no interpreter: the
    verified copy *is* the program, and it can only be named by the descriptor
    the guard opened and passes in. Anything else is a substitution and is
    refused. Research:
    `docs/research/2026-09-12-installing-go-and-building-gopls.md`.
    """
    if not arguments:
        raise ValueError("generation launch cannot replace the configured executable")
    if _same_program_name(arguments[0], command, launch, owner_root):
        return
    raise ValueError("generation launch cannot replace the configured executable")


def _same_program_name(
    name: str,
    command: Sequence[str],
    launch: GenerationLaunch,
    owner_root: Path | None,
) -> bool:
    return (
        name == command[0]
        or is_inherited_descriptor(name, launch.pass_fds)
        or _launch_inside_owner(name, owner_root)
    )


def is_inherited_descriptor(name: str, pass_fds: Sequence[int]) -> bool:
    number = _descriptor_number(name)
    return number is not None and number in tuple(pass_fds)


def _check_argument_strings(arguments: Sequence[object]) -> None:
    for argument in arguments:
        if not isinstance(argument, str):
            raise TypeError("command arguments must be strings")
        if "\0" in argument:
            raise ValueError("command arguments must not contain NUL")


def _validated_arguments(command: Sequence[str]) -> list[str]:
    """The command as a non-empty list of NUL-free strings."""
    if isinstance(command, (str, bytes)) or not isinstance(command, Sequence):
        raise TypeError("command must be a sequence of strings")
    if not command:
        raise ValueError("command must not be empty")
    return _checked_arguments(list(command))


def _checked_arguments(arguments: list[str]) -> list[str]:
    _check_argument_strings(arguments)
    if not arguments[0]:
        raise ValueError("command executable must not be empty")
    return arguments


# A native server is executed as an inherited descriptor whose file has already
# been unlinked, so resolving the symlink names a path that no longer exists.
# The descriptor itself is what has to be checked, and `os.stat` follows it to
# the open file. Research:
# `docs/research/2026-09-12-installing-go-and-building-gopls.md`.
DESCRIPTOR_ROOTS = ("/proc/self/fd/", "/dev/fd/")


_DESCRIPTOR_NUMBER = re.compile(r"[0-9]+")


def _descriptor_number(name: str) -> int | None:
    """The descriptor a path names, or None when it names anything else.

    Only `<root><decimal digits>` is a descriptor path. `/proc/self/fd/../../x`
    begins with the same characters and reaches a file of somebody else's
    choosing, and `os.stat` on it would follow it there.
    """
    for root in DESCRIPTOR_ROOTS:
        if name.startswith(root):
            return _decimal_descriptor(name[len(root) :])
    return None


def _decimal_descriptor(tail: str) -> int | None:
    """The whole remainder as a plain decimal descriptor number, or None."""
    if _DESCRIPTOR_NUMBER.fullmatch(tail) is None:
        return None
    return int(tail)


def _is_descriptor_path(name: str) -> bool:
    return _descriptor_number(name) is not None


def _resolved_executable(name: str, cwd: Path) -> Path:
    """The executable as an absolute path, however the caller wrote it."""
    if _is_descriptor_path(name):
        return Path(name)
    return _resolved_file_executable(Path(name), name, cwd)


def _resolved_file_executable(executable: Path, name: str, cwd: Path) -> Path:
    if executable.is_absolute():
        return executable.resolve()
    if executable.parent != Path("."):
        return (cwd / executable).resolve()
    return _executable_on_path(name)


def _executable_on_path(name: str) -> Path:
    found = shutil.which(name, path=lsp_environment().get("PATH"))
    if found is None:
        raise FileNotFoundError(name)
    return Path(found).resolve()


def _check_executable_kind(resolved: Path, name: str) -> None:
    """Refuse what the platform will not spawn as a program of its own."""
    if os.name == "nt" and resolved.suffix.casefold() in {".bat", ".cmd"}:
        raise ValueError("Windows shell scripts are not valid LSP executables")
    if not _is_regular_program(resolved, name):
        raise FileNotFoundError(name)


def _is_regular_program(resolved: Path, name: str) -> bool:
    """A regular file, reached through a descriptor path when that is what it is."""
    if not _is_descriptor_path(name):
        return resolved.is_file()
    return stat.S_ISREG(os.stat(resolved).st_mode)


def _check_executable_permission(resolved: Path) -> None:
    if os.name == "posix" and not os.access(resolved, os.X_OK):
        raise ValueError("LSP executable is not executable")


def validated_command(command: Sequence[str], cwd: Path) -> list[str]:
    arguments = _validated_arguments(command)
    resolved = _resolved_executable(arguments[0], cwd)
    _check_executable_kind(resolved, arguments[0])
    _check_executable_permission(resolved)
    arguments[0] = str(resolved)
    return arguments


def validated_future_deadline(deadline: float) -> float:
    value = validated_deadline(deadline)
    if value <= time.monotonic():
        raise ValueError("deadline must be a future monotonic timestamp")
    return value


def _copied_handler_mapping(
    handlers: Mapping[str, Callable[[object], object]],
    label: str,
) -> Mapping[str, Callable[[object], object]]:
    if not isinstance(handlers, Mapping):
        raise TypeError(f"{label} must be a mapping")
    for method, handler in handlers.items():
        _check_handler_entry(method, handler, label)
    return MappingProxyType(dict(handlers))


def _check_handler_entry(method: object, handler: object, label: str) -> None:
    """One handler entry: a non-empty method name bound to something callable."""
    if not isinstance(method, str) or not method:
        raise ValueError(f"{label} method names must be non-empty strings")
    if not callable(handler):
        raise TypeError(f"{label} handlers must be callable")


def validated_generation_configuration(
    server_request_handlers: Mapping[str, Callable[[object], object]],
    server_notification_handlers: Mapping[str, Callable[[object], None]],
    generation_bootstrap: GenerationBootstrap,
    bootstrap_timeout_seconds: float,
    generation_guard: GenerationGuardFactory | None,
) -> GenerationConfiguration:
    if not callable(generation_bootstrap):
        raise TypeError("generation_bootstrap must be callable")
    request_handlers = _copied_handler_mapping(
        server_request_handlers, "server_request_handlers"
    )
    notification_handlers = _copied_handler_mapping(
        server_notification_handlers, "server_notification_handlers"
    )
    if generation_guard is not None and not callable(generation_guard):
        raise TypeError("generation_guard must be callable or None")
    return GenerationConfiguration(
        request_handlers,
        notification_handlers,
        generation_bootstrap,
        validated_bootstrap_timeout(bootstrap_timeout_seconds),
        generation_guard,
    )


def validated_bootstrap_timeout(seconds: float) -> float:
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise TypeError("generation bootstrap timeout must be seconds")
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("generation bootstrap timeout must be positive and finite")
    return float(seconds)


def fresh_bootstrap_deadline(coordinator: LifecycleCoordinator) -> float:
    timeout = coordinator.generation_configuration.bootstrap_timeout_seconds
    if timeout is None:
        timeout = lsp_process_limits.GRACEFUL_CLEANUP_SECONDS
    return time.monotonic() + timeout


def fresh_cleanup_deadline() -> float:
    return time.monotonic() + lsp_process_limits.GRACEFUL_CLEANUP_SECONDS


_CallbackResult = TypeVar("_CallbackResult")


def wrap_protocol_callback(  # noqa: UP047 - preserve runtime TypeVar annotations
    coordinator: LifecycleCoordinator,
    handler: Callable[[object], _CallbackResult],
) -> Callable[[object], _CallbackResult]:
    def marked_handler(params: object) -> _CallbackResult:
        previous = getattr(_PROTOCOL_CALLBACK_CONTEXT, "coordinators", ())
        _PROTOCOL_CALLBACK_CONTEXT.coordinators = (*previous, coordinator)
        try:
            return handler(params)
        finally:
            if previous:
                _PROTOCOL_CALLBACK_CONTEXT.coordinators = previous
            else:
                del _PROTOCOL_CALLBACK_CONTEXT.coordinators

    return marked_handler


def reject_protocol_callback_lifecycle(instance: LspProcessHandle) -> None:
    coordinator = instance._coordinator
    callback_coordinators = getattr(_PROTOCOL_CALLBACK_CONTEXT, "coordinators", ())
    if any(current is coordinator for current in callback_coordinators):
        raise RuntimeError(_LIFECYCLE_REENTRANCY_ERROR)
