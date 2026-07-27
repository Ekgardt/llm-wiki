"""Contain LSP navigation evidence to a trusted repository and redact logs.

The language server still runs with the operator's permissions. This module is
not a sandbox: it validates requested and returned navigation evidence, while
the repository and configured Pyright process remain trusted.
"""

from __future__ import annotations

import ntpath
import os
import posixpath
import re
import stat
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import unquote_to_bytes, urlsplit

import windows_workspace
from lsp_positions import file_uri_to_path, path_to_file_uri
from repository_scope import RepositoryScope

_MAX_RELATIVE_PATH = 4096
_MAX_COMPONENTS = 256
_MAX_COMPONENT_CHARACTERS = 255
_MAX_COMPONENT_BYTES = 255
_MAX_PROVIDER_URI = 16 * 1024
_MAX_DIRECTORY_ENTRIES = 100_000
_MAX_REDACTION_RAW_INPUT = 256 * 1024
_MAX_REDACTION_PATH_TOKEN = 128 * 1024
_OVERSIZED_REDACTION_MARKER = "<redacted: oversized LSP log>"
_MALFORMED_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
_ENCODED_SEPARATOR = re.compile(r"%(?:2f|5c)", re.IGNORECASE)
_WINDOWS_DRIVE_PATH = re.compile(r"^[A-Za-z]:/")
_URL_AUTHORITY_TOKEN_BOUNDARIES = frozenset('"<>\\^`{|}')
_WINDOWS_TOKEN_STRUCTURAL_TERMINATORS = frozenset('<>|?*"')
_WINDOWS_LOG_TRAILING_PUNCTUATION = frozenset(".,;)]}")
_WINDOWS_RESERVED = frozenset(
    {
        "aux",
        "clock$",
        "con",
        "conin$",
        "conout$",
        "nul",
        "prn",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
        *(f"com{number}" for number in "¹²³"),
        *(f"lpt{number}" for number in "¹²³"),
    }
)
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?ai)(?<![a-z0-9_])(?P<quote>[\"']?)[a-z0-9_]*"
    r"(?:api_key|authorization|password|secret|token)"
    r"(?P=quote)\s*[:=]\s*"
)
class PathContainmentError(ValueError):
    """A source path cannot be proven to remain in its repository checkout."""


@dataclass(frozen=True, slots=True)
class RepositorySource:
    repository_id: str
    checkout_id: str
    relative_path: str
    absolute_path: Path
    uri: str


@dataclass(frozen=True, slots=True)
class _TraversalStep:
    name: str
    identity: tuple[object, ...]
    directory: bool


class _OwnedHandles:
    def __init__(self, close: Callable[[int], None]) -> None:
        self._close = close
        self._values: list[int] = []

    def __enter__(self) -> _OwnedHandles:
        return self

    def own(self, value: int) -> int:
        self._values.append(value)
        return value

    def __exit__(self, exception_type, _exception, _traceback) -> bool:
        close_error: BaseException | None = None
        for value in reversed(self._values):
            try:
                self._close(value)
            except BaseException as exc:  # close every owned descriptor or handle
                if close_error is None:
                    close_error = exc
        if close_error is not None and exception_type is None:
            raise close_error
        return False


def _resolution_barrier() -> None:
    return


def _is_control(character: str) -> bool:
    codepoint = ord(character)
    return (
        codepoint < 32
        or 127 <= codepoint <= 159
        or unicodedata.category(character) in {"Cf", "Zl", "Zp"}
    )


def _validate_relative_path(value: object) -> tuple[str, tuple[str, ...]]:
    if not isinstance(value, str):
        raise TypeError("relative_path must be a string")
    if (
        not value
        or len(value) > _MAX_RELATIVE_PATH
        or value.endswith("/")
        or "\\" in value
        or unicodedata.normalize("NFC", value) != value
        or any(_is_control(character) for character in value)
    ):
        raise PathContainmentError("repository source path is not canonical")
    try:
        encoded_value = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise PathContainmentError("repository source path is not canonical") from exc
    if len(encoded_value) > _MAX_RELATIVE_PATH:
        raise PathContainmentError("repository source path exceeds its byte ceiling")

    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    parts = tuple(value.split("/"))
    if (
        posix.is_absolute()
        or windows.drive
        or windows.root
        or len(parts) > _MAX_COMPONENTS
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise PathContainmentError("repository source path is not canonical")

    for component in parts:
        reserved_base = component.split(".", 1)[0].rstrip(" .").casefold()
        if (
            len(component) > _MAX_COMPONENT_CHARACTERS
            or len(component.encode("utf-8")) > _MAX_COMPONENT_BYTES
            or component[-1] in {".", " "}
            or any(character in '<>:"|?*' for character in component)
            or reserved_base in _WINDOWS_RESERVED
        ):
            raise PathContainmentError("repository source path contains an unsafe component")
    return value, parts


def _require_repository(value: object) -> RepositoryScope:
    if not isinstance(value, RepositoryScope):
        raise TypeError("repository must be RepositoryScope")
    return value


def _posix_identity(info: os.stat_result) -> tuple[object, ...]:
    if info.st_dev < 0 or info.st_ino < 0:
        raise PathContainmentError("stable repository source identity is unavailable")
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def _posix_directory_flags() -> int:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise PathContainmentError("no-follow repository traversal is unavailable")
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


def _posix_file_flags() -> int:
    if not hasattr(os, "O_NOFOLLOW"):
        raise PathContainmentError("no-follow repository traversal is unavailable")
    return (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


def _assert_ancestry(root: Path, target: Path) -> None:
    try:
        target.relative_to(root)
        normalized_root = os.path.normcase(str(root))
        normalized_target = os.path.normcase(str(target))
        common = os.path.commonpath((normalized_root, normalized_target))
    except (OSError, ValueError) as exc:
        raise PathContainmentError("repository source ancestry is invalid") from exc
    if common != normalized_root:
        raise PathContainmentError("repository source ancestry is invalid")


def _open_posix_checkout(
    root: Path,
    owned: _OwnedHandles,
) -> tuple[int, tuple[object, ...], tuple[_TraversalStep, ...]]:
    directory_flags = _posix_directory_flags()
    current = owned.own(os.open("/", directory_flags))
    filesystem_root_identity = _posix_identity(os.fstat(current))
    steps: list[_TraversalStep] = []
    for component in root.parts[1:]:
        opened = owned.own(os.open(component, directory_flags, dir_fd=current))
        info = os.fstat(opened)
        if not stat.S_ISDIR(info.st_mode):
            raise PathContainmentError("repository checkout root is not a directory")
        steps.append(_TraversalStep(component, _posix_identity(info), True))
        current = opened
    return current, filesystem_root_identity, tuple(steps)


def _revalidate_posix(
    filesystem_root_identity: tuple[object, ...],
    root_steps: tuple[_TraversalStep, ...],
    source_steps: tuple[_TraversalStep, ...],
) -> None:
    directory_flags = _posix_directory_flags()
    file_flags = _posix_file_flags()
    with _OwnedHandles(os.close) as owned:
        current = owned.own(os.open("/", directory_flags))
        root_info = os.fstat(current)
        if (
            not stat.S_ISDIR(root_info.st_mode)
            or _posix_identity(root_info) != filesystem_root_identity
        ):
            raise PathContainmentError("filesystem root changed during traversal")
        for step in (*root_steps, *source_steps):
            flags = directory_flags if step.directory else file_flags
            opened = owned.own(os.open(step.name, flags, dir_fd=current))
            info = os.fstat(opened)
            expected_kind = stat.S_ISDIR if step.directory else stat.S_ISREG
            if not expected_kind(info.st_mode) or _posix_identity(info) != step.identity:
                raise PathContainmentError("repository source changed during traversal")
            current = opened


def _resolve_posix(
    repository: RepositoryScope,
    relative_path: str,
    parts: tuple[str, ...],
    *,
    must_exist: bool,
) -> RepositorySource:
    root = Path(repository.checkout_root)
    if not root.is_absolute():
        raise PathContainmentError("repository checkout root is not local and absolute")
    directory_flags = _posix_directory_flags()
    file_flags = _posix_file_flags()

    with _OwnedHandles(os.close) as owned:
        root_descriptor, filesystem_root_identity, root_steps = _open_posix_checkout(
            root, owned
        )
        root_info = os.fstat(root_descriptor)
        if not stat.S_ISDIR(root_info.st_mode):
            raise PathContainmentError("repository checkout root is not a directory")

        current = root_descriptor
        steps: list[_TraversalStep] = []
        missing: tuple[str, ...] = ()
        for index, component in enumerate(parts):
            final = index == len(parts) - 1
            if final:
                try:
                    named = os.stat(component, dir_fd=current, follow_symlinks=False)
                except FileNotFoundError:
                    if must_exist:
                        raise PathContainmentError("repository source does not exist") from None
                    missing = parts[index:]
                    break
                if not stat.S_ISREG(named.st_mode):
                    raise PathContainmentError("repository source is not a regular file")
                opened = owned.own(os.open(component, file_flags, dir_fd=current))
                info = os.fstat(opened)
                identity = _posix_identity(info)
                if not stat.S_ISREG(info.st_mode) or identity != _posix_identity(named):
                    raise PathContainmentError("repository source changed before open")
                steps.append(_TraversalStep(component, identity, False))
                continue

            try:
                opened = owned.own(os.open(component, directory_flags, dir_fd=current))
            except FileNotFoundError:
                if must_exist:
                    raise PathContainmentError("repository source parent does not exist") from None
                missing = parts[index:]
                break
            info = os.fstat(opened)
            if not stat.S_ISDIR(info.st_mode):
                raise PathContainmentError("repository source parent is not a directory")
            steps.append(_TraversalStep(component, _posix_identity(info), True))
            current = opened

        _resolution_barrier()
        step_tuple = tuple(steps)
        _revalidate_posix(filesystem_root_identity, root_steps, step_tuple)

        if missing:
            try:
                os.stat(missing[0], dir_fd=current, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise PathContainmentError("repository source appeared during traversal")
        absolute_path = root.joinpath(*parts)

        return RepositorySource(
            repository.repository_id,
            repository.checkout_id,
            relative_path,
            absolute_path,
            path_to_file_uri(absolute_path),
        )


def _windows_entries(handle: int) -> dict[str, windows_workspace.WindowsEntry]:
    entries = windows_workspace.list_directory(
        handle, max_entries=_MAX_DIRECTORY_ENTRIES
    )
    by_folded_name: dict[str, windows_workspace.WindowsEntry] = {}
    for entry in entries:
        if (
            not isinstance(entry, windows_workspace.WindowsEntry)
            or unicodedata.normalize("NFC", entry.name) != entry.name
            or entry.kind not in {"directory", "file", "link"}
        ):
            raise PathContainmentError("Windows repository enumeration is invalid")
        folded = entry.name.casefold()
        previous = by_folded_name.get(folded)
        if previous is not None and previous.name != entry.name:
            raise PathContainmentError("Windows repository contains a case collision")
        by_folded_name[folded] = entry
    return by_folded_name


def _windows_entry(
    handle: int, component: str
) -> windows_workspace.WindowsEntry | None:
    entry = _windows_entries(handle).get(component.casefold())
    if entry is not None and entry.name != component:
        raise PathContainmentError("Windows repository source uses a case alias")
    return entry


def _prove_windows_component_missing(
    parent: int,
    component: str,
    owned: _OwnedHandles,
) -> None:
    opened = False
    failures: list[OSError] = []
    missing = 0
    for opener in (
        windows_workspace.open_directory,
        windows_workspace.open_shared_readonly_source_file,
    ):
        try:
            owned.own(opener(parent, component))
        except FileNotFoundError:
            missing += 1
        except OSError as exc:
            failures.append(exc)
        else:
            opened = True
    if opened:
        raise PathContainmentError("Windows repository source uses an unenumerated alias")
    if failures or missing != 2:
        raise PathContainmentError(
            "Windows repository source absence cannot be proven"
        ) from (failures[0] if failures else None)


def _windows_identity(handle: int, *, directory: bool) -> tuple[object, ...]:
    volume, file_id, actual_directory = windows_workspace.identity(
        handle, directory=directory
    )
    if actual_directory != directory or not isinstance(file_id, bytes) or not any(file_id):
        raise PathContainmentError("stable Windows repository identity is unavailable")
    return volume, file_id, actual_directory


def _open_windows_step(
    parent: int,
    entry: windows_workspace.WindowsEntry,
    component: str,
    *,
    directory: bool,
    owned: _OwnedHandles,
) -> tuple[int, tuple[object, ...]]:
    expected_kind = "directory" if directory else "file"
    if entry.kind != expected_kind:
        raise PathContainmentError("Windows repository source has the wrong kind")
    opener = (
        windows_workspace.open_directory
        if directory
        else windows_workspace.open_shared_readonly_source_file
    )
    opened = owned.own(opener(parent, component))
    identity = _windows_identity(opened, directory=directory)
    if identity[1] != entry.file_id:
        raise PathContainmentError("Windows repository source changed before open")
    return opened, identity


def _revalidate_windows(
    root: Path,
    root_identity: tuple[object, ...],
    steps: tuple[_TraversalStep, ...],
    owned: _OwnedHandles,
) -> None:
    current = owned.own(windows_workspace.open_directory_path(root))
    if _windows_identity(current, directory=True) != root_identity:
        raise PathContainmentError("repository checkout changed during traversal")
    for step in steps:
        entry = _windows_entry(current, step.name)
        if entry is None:
            raise PathContainmentError("Windows repository source changed during traversal")
        if not step.directory:
            if entry.kind != "file" or entry.file_id != step.identity[1]:
                raise PathContainmentError(
                    "Windows repository source changed during traversal"
                )
            continue
        opened, identity = _open_windows_step(
            current,
            entry,
            step.name,
            directory=step.directory,
            owned=owned,
        )
        if identity != step.identity:
            raise PathContainmentError("Windows repository source changed during traversal")
        current = opened


def _resolve_windows(
    repository: RepositoryScope,
    relative_path: str,
    parts: tuple[str, ...],
    *,
    must_exist: bool,
) -> RepositorySource:
    root = Path(repository.checkout_root)
    pure_root = PureWindowsPath(repository.checkout_root)
    if not pure_root.drive or not pure_root.root or pure_root.drive.startswith("\\"):
        raise PathContainmentError("repository checkout root is not a local drive path")

    with _OwnedHandles(windows_workspace.close_handle) as owned:
        root_handle = owned.own(windows_workspace.open_directory_path(root))
        root_identity = _windows_identity(root_handle, directory=True)
        canonical_root = root.resolve(strict=True)
        canonical_handle = owned.own(
            windows_workspace.open_directory_path(canonical_root)
        )
        if _windows_identity(canonical_handle, directory=True) != root_identity:
            raise PathContainmentError("repository checkout root changed")

        current = root_handle
        steps: list[_TraversalStep] = []
        missing: tuple[str, ...] = ()
        final_handle: int | None = None
        for index, component in enumerate(parts):
            final = index == len(parts) - 1
            entry = _windows_entry(current, component)
            if entry is None:
                if must_exist:
                    raise PathContainmentError("repository source does not exist")
                _prove_windows_component_missing(current, component, owned)
                missing = parts[index:]
                break
            opened, identity = _open_windows_step(
                current,
                entry,
                component,
                directory=not final,
                owned=owned,
            )
            steps.append(_TraversalStep(component, identity, not final))
            if final:
                final_handle = opened
            else:
                current = opened

        _resolution_barrier()
        step_tuple = tuple(steps)
        _revalidate_windows(canonical_root, root_identity, step_tuple, owned)

        existing_directory_steps = tuple(step for step in steps if step.directory)
        nearest_path = canonical_root.joinpath(
            *(step.name for step in existing_directory_steps)
        )
        canonical_parent = nearest_path.resolve(strict=True)
        _assert_ancestry(canonical_root, canonical_parent)
        parent_probe = owned.own(
            windows_workspace.open_directory_path(canonical_parent)
        )
        expected_parent = (
            existing_directory_steps[-1].identity
            if existing_directory_steps
            else root_identity
        )
        if _windows_identity(parent_probe, directory=True) != expected_parent:
            raise PathContainmentError("repository source parent changed")

        if missing:
            if _windows_entry(parent_probe, missing[0]) is not None:
                raise PathContainmentError("repository source appeared during traversal")
            _prove_windows_component_missing(parent_probe, missing[0], owned)
            absolute_path = canonical_parent.joinpath(*missing)
        else:
            candidate = canonical_root.joinpath(*parts)
            absolute_path = candidate.resolve(strict=True)
            _assert_ancestry(canonical_root, absolute_path)
            final_parent = owned.own(
                windows_workspace.open_directory_path(absolute_path.parent)
            )
            final_entry = _windows_entry(final_parent, absolute_path.name)
            if (
                final_handle is None
                or final_entry is None
                or final_entry.kind != "file"
                or final_entry.file_id != steps[-1].identity[1]
                or _windows_identity(final_handle, directory=False)
                != steps[-1].identity
            ):
                raise PathContainmentError("repository source changed")

        return RepositorySource(
            repository.repository_id,
            repository.checkout_id,
            relative_path,
            absolute_path,
            path_to_file_uri(absolute_path),
        )


def resolve_repository_source(
    repository: RepositoryScope,
    relative_path: str,
    *,
    must_exist: bool = True,
) -> RepositorySource:
    """Resolve one canonical repository-relative source through a no-follow walk."""
    repository = _require_repository(repository)
    if not isinstance(must_exist, bool):
        raise TypeError("must_exist must be a boolean")
    normalized, parts = _validate_relative_path(relative_path)
    try:
        if os.name == "posix":
            return _resolve_posix(
                repository, normalized, parts, must_exist=must_exist
            )
        if os.name == "nt":
            return _resolve_windows(
                repository, normalized, parts, must_exist=must_exist
            )
        raise PathContainmentError("no-follow repository traversal is unavailable")
    except PathContainmentError:
        raise
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        raise PathContainmentError("repository source containment failed") from exc


def _decoded_provider_uri(uri: object) -> tuple[str, str] | None:
    if (
        not isinstance(uri, str)
        or not uri
        or len(uri) > _MAX_PROVIDER_URI
        or any(ord(character) > 127 or _is_control(character) for character in uri)
        or any(character.isspace() for character in uri)
        or "\\" in uri
        or "?" in uri
        or "#" in uri
        or _MALFORMED_PERCENT.search(uri)
        or _ENCODED_SEPARATOR.search(uri)
    ):
        return None
    try:
        parsed = urlsplit(uri)
        authority = unquote_to_bytes(parsed.netloc).decode("utf-8", errors="strict")
        path = unquote_to_bytes(parsed.path).decode("utf-8", errors="strict")
    except (UnicodeError, ValueError):
        return None
    if (
        parsed.scheme.casefold() != "file"
        or parsed.query
        or parsed.fragment
        or authority.casefold() not in {"", "localhost"}
        or "@" in authority
        or any(_is_control(character) for character in authority + path)
        or "\\" in path
        or path.startswith("//")
    ):
        return None

    if os.name == "nt":
        local = path[1:] if path.startswith("/") else path
        if not _WINDOWS_DRIVE_PATH.match(local):
            return None
        components = local[3:].split("/") if len(local) > 3 else []
    elif os.name == "posix":
        if not path.startswith("/") or path.startswith("//"):
            return None
        components = path[1:].split("/") if len(path) > 1 else []
    else:
        return None
    if any(component in {"", ".", ".."} for component in components):
        return None
    return authority, path


def normalize_provider_uri(
    repository: RepositoryScope,
    uri: str,
) -> RepositorySource | None:
    """Return one canonical in-repository provider location or filter it."""
    repository = _require_repository(repository)
    if _decoded_provider_uri(uri) is None:
        return None
    try:
        provider_path = file_uri_to_path(uri, platform=os.name)
        if os.name == "nt":
            provider = PureWindowsPath(provider_path)
            root = PureWindowsPath(repository.checkout_root)
            if provider.drive.startswith("\\") or not provider.is_absolute():
                return None
            if (
                len(provider.parts) < len(root.parts)
                or provider.parts[0].casefold() != root.parts[0].casefold()
                or provider.parts[1 : len(root.parts)] != root.parts[1:]
            ):
                return None
        else:
            provider = PurePosixPath(provider_path)
            root = PurePosixPath(repository.checkout_root)
            if not provider.is_absolute() or str(provider).startswith("//"):
                return None
        relative = provider.relative_to(root).as_posix()
        normalized, _parts = _validate_relative_path(relative)
        return resolve_repository_source(repository, normalized)
    except Exception:
        return None


def _redact_assignments(value: str) -> str:
    pieces: list[str] = []
    cursor = 0
    for match in _CREDENTIAL_ASSIGNMENT.finditer(value):
        if match.start() < cursor:
            continue
        pieces.append(value[cursor : match.end()])
        value_start = match.end()
        value_end = value_start
        if value_start < len(value) and value[value_start] in {'"', "'"}:
            quote = value[value_start]
            value_end += 1
            escaped = False
            while value_end < len(value):
                character = value[value_end]
                value_end += 1
                if character == quote and not escaped:
                    break
                escaped = character == "\\" and not escaped
                if character != "\\":
                    escaped = False
        else:
            while value_end < len(value) and value[value_end] not in "\r\n":
                value_end += 1
        pieces.append("<redacted>")
        cursor = value_end
    pieces.append(value[cursor:])
    return "".join(pieces)


def _redact_url_userinfo(value: str) -> str:
    def scheme_character(character: str) -> bool:
        return character.isascii() and (
            character.isalpha() or character.isdigit() or character in "+.-"
        )

    pieces: list[str] = []
    cursor = 0
    search_start = 0
    while True:
        scheme_end = value.find("://", search_start)
        if scheme_end < 0:
            break
        scheme_start = scheme_end
        while scheme_start > 0 and scheme_character(value[scheme_start - 1]):
            scheme_start -= 1
        search_start = scheme_end + 3
        if not (
            scheme_start < scheme_end
            and value[scheme_start].isascii()
            and value[scheme_start].isalpha()
        ):
            continue
        authority_start = search_start
        authority_end = authority_start
        while authority_end < len(value):
            character = value[authority_end]
            if (
                character in "/?#"
                or character in _URL_AUTHORITY_TOKEN_BOUNDARIES
                or character.isspace()
                or _is_control(character)
            ):
                break
            authority_end += 1
        userinfo_end = value.rfind("@", authority_start, authority_end)
        if userinfo_end >= authority_start and authority_start >= cursor:
            pieces.append(value[cursor:authority_start])
            pieces.append("<redacted>@")
            cursor = userinfo_end + 1
    pieces.append(value[cursor:])
    return "".join(pieces)


def _canonical_windows_path_token(
    value: str,
    *,
    file_uri: bool,
) -> tuple[str, tuple[str, ...]] | None:
    candidate = value
    if file_uri:
        if candidate[:5].casefold() != "file:":
            return None
        uri_path = candidate[5:].replace("/", "\\")
        has_leading_separator = uri_path.startswith("\\")
        stripped = uri_path.lstrip("\\")
        if re.match(r"(?i)^[A-Z]:\\", stripped):
            candidate = stripped
        else:
            authority, separator, remainder = stripped.partition("\\")
            if (
                not has_leading_separator
                or not separator
                or authority.casefold() != "localhost"
            ):
                return None
            candidate = remainder.lstrip("\\")
    else:
        candidate = candidate.replace("/", "\\")

    if candidate.startswith("\\"):
        return None
    drive, tail = ntpath.splitdrive(candidate)
    if not re.fullmatch(r"(?i)[A-Z]:", drive) or not tail.startswith("\\"):
        return None
    raw_components = tuple(part for part in re.split(r"\\+", tail) if part)
    components: list[str] = []
    for component in raw_components:
        if component in {".", ".."}:
            components.append(component)
            continue
        component = component.rstrip(" .")
        if not component:
            continue
        if (
            len(component) > _MAX_COMPONENT_CHARACTERS
            or any(character in '<>:"|?*' or _is_control(character) for character in component)
        ):
            return None
        components.append(component)

    normalized = ntpath.normpath(drive + "\\" + "\\".join(components))
    normalized_drive, normalized_tail = ntpath.splitdrive(normalized)
    if not re.fullmatch(r"(?i)[A-Z]:", normalized_drive):
        return None
    normalized_components = tuple(
        component.casefold()
        for component in normalized_tail.split("\\")
        if component
    )
    if len(normalized_components) > _MAX_COMPONENTS or any(
        component in {".", ".."} for component in normalized_components
    ):
        return None
    return normalized_drive.casefold(), normalized_components


def _windows_root_component_aliases(
    path: Path,
) -> tuple[str, tuple[frozenset[str], ...]] | None:
    canonical = _canonical_windows_path_token(str(path), file_uri=False)
    if canonical is None:
        return None
    drive, components = canonical
    aliases = [{component} for component in components]
    try:
        short_path = windows_workspace.get_short_path(path)
    except (OSError, RuntimeError, ValueError):
        pass
    else:
        short = _canonical_windows_path_token(str(short_path), file_uri=False)
        if short is not None and short[0] == drive and len(short[1]) == len(aliases):
            for component_aliases, short_component in zip(aliases, short[1]):
                component_aliases.add(short_component)
    return drive, tuple(frozenset(component_aliases) for component_aliases in aliases)


def _windows_components_reach_root(
    drive: str,
    components: list[str],
    root: tuple[str, tuple[frozenset[str], ...]],
) -> bool:
    root_drive, root_component_aliases = root
    return (
        drive == root_drive
        and len(components) == len(root_component_aliases)
        and all(
            component in aliases
            for component, aliases in zip(components, root_component_aliases)
        )
    )


def _windows_candidate_inspection_characters(
    root: tuple[str, tuple[frozenset[str], ...]],
) -> int:
    _drive, aliases = root
    root_characters = 3 + sum(
        max((len(alias) for alias in component_aliases), default=0) + 1
        for component_aliases in aliases
    )
    return min(
        _MAX_REDACTION_PATH_TOKEN,
        _MAX_REDACTION_PATH_TOKEN // 2 + root_characters * 12,
    )


def _windows_candidate_starts_at(value: str, start: int) -> bool:
    return (
        value[start : start + 5].casefold() == "file:"
        or _windows_extended_local_start(value, start) is not None
        or (
            value[start : start + 1].isascii()
            and value[start : start + 1].isalpha()
            and value[start + 1 : start + 2] == ":"
            and value[start + 2 : start + 3] in {"/", "\\"}
        )
    )


def _windows_extended_local_start(value: str, start: int) -> int | None:
    if value[start : start + 4] not in {"\\\\?\\", "//?/"}:
        return None
    drive_start = start + 4
    if not (
        value[drive_start : drive_start + 1].isascii()
        and value[drive_start : drive_start + 1].isalpha()
        and value[drive_start + 1 : drive_start + 2] == ":"
        and value[drive_start + 2 : drive_start + 3] in {"/", "\\"}
    ):
        return None
    return drive_start


def _windows_root_boundary(value: str, index: int, *, quoted: bool) -> bool:
    if index >= len(value):
        return True
    character = value[index]
    if (
        character in {"/", "\\", ":"}
        or character.isspace()
        or _is_control(character)
        or character in _WINDOWS_TOKEN_STRUCTURAL_TERMINATORS
        or (quoted and character == '"')
    ):
        return True
    if character not in _WINDOWS_LOG_TRAILING_PUNCTUATION:
        return False

    punctuation_end = index
    while (
        punctuation_end < len(value)
        and value[punctuation_end] in _WINDOWS_LOG_TRAILING_PUNCTUATION
    ):
        punctuation_end += 1
    if punctuation_end >= len(value):
        return True
    following = value[punctuation_end]
    if following.isspace() or _is_control(following) or following == '"':
        return True
    return character in {",", ";"} and _windows_candidate_starts_at(
        value, punctuation_end
    )


def _windows_casefolded_prefix_end(
    value: str,
    start: int,
    folded_alias: str,
) -> int | None:
    folded = ""
    index = start
    while index < len(value) and len(folded) < len(folded_alias):
        folded += value[index].casefold()
        index += 1
        if not folded_alias.startswith(folded):
            return None
    return index if folded == folded_alias else None


def _windows_native_root_match_end(
    value: str,
    start: int,
    root: tuple[str, tuple[frozenset[str], ...]],
    *,
    quoted: bool,
) -> int | None:
    root_drive, root_component_aliases = root
    if value[start : start + 2].casefold() != root_drive:
        return None
    index = start + 2
    if value[index : index + 1] not in {"/", "\\"}:
        return None
    while value[index : index + 1] in {"/", "\\"}:
        index += 1
    if not root_component_aliases:
        return index

    for component_index, aliases in enumerate(root_component_aliases):
        component_end = None
        for alias in sorted(aliases, key=len, reverse=True):
            alias_end = _windows_casefolded_prefix_end(value, index, alias)
            if alias_end is not None:
                component_end = alias_end
                break
        if component_end is None:
            return None

        trailing_end = component_end
        while value[trailing_end : trailing_end + 1] in {".", " "}:
            trailing_end += 1
        if (
            trailing_end > component_end
            and value[trailing_end : trailing_end + 1] in {"/", "\\"}
        ):
            component_end = trailing_end

        if component_index + 1 < len(root_component_aliases):
            if value[component_end : component_end + 1] not in {"/", "\\"}:
                return None
            index = component_end
            while value[index : index + 1] in {"/", "\\"}:
                index += 1
            continue

        dots_end = component_end
        while value[dots_end : dots_end + 1] == ".":
            dots_end += 1
        if dots_end > component_end:
            following = value[dots_end : dots_end + 1]
            if (
                not following
                or following in {"/", "\\", '"'}
                or following.isspace()
                or _is_control(following)
            ):
                component_end = dots_end
        if _windows_root_boundary(value, component_end, quoted=quoted):
            return component_end
        return None
    return None


def _uri_character(
    value: str,
    index: int,
    limit: int,
) -> tuple[str, int, bool] | None:
    if index >= limit:
        return None
    if value[index] != "%":
        return value[index], index + 1, False
    if index + 3 > limit or re.fullmatch(
        r"[0-9A-Fa-f]{2}", value[index + 1 : index + 3]
    ) is None:
        return None

    first_byte = int(value[index + 1 : index + 3], 16)
    if first_byte < 0x80:
        byte_count = 1
    elif 0xC2 <= first_byte <= 0xDF:
        byte_count = 2
    elif 0xE0 <= first_byte <= 0xEF:
        byte_count = 3
    elif 0xF0 <= first_byte <= 0xF4:
        byte_count = 4
    else:
        return None

    raw = bytearray()
    source_end = index
    for _byte_index in range(byte_count):
        if (
            source_end + 3 > limit
            or value[source_end] != "%"
            or re.fullmatch(
                r"[0-9A-Fa-f]{2}", value[source_end + 1 : source_end + 3]
            )
            is None
        ):
            return None
        raw.append(int(value[source_end + 1 : source_end + 3], 16))
        source_end += 3
    try:
        character = bytes(raw).decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    if len(character) != 1:
        return None
    return character, source_end, True


def _windows_semantic_prefix(
    value: str,
    start: int,
    limit: int,
    *,
    file_uri: bool,
) -> tuple[str, int] | None:
    if not file_uri:
        drive = value[start : start + 2].casefold()
        index = start + 2
        if value[index : index + 1] not in {"/", "\\"}:
            return None
        while value[index : index + 1] in {"/", "\\"} and index < limit:
            index += 1
        return drive, index

    index = start + 5
    leading_separators = 0
    while index < limit:
        decoded = _uri_character(value, index, limit)
        if decoded is None or decoded[0] not in {"/", "\\"}:
            break
        leading_separators += 1
        index = decoded[1]

    first = _uri_character(value, index, limit)
    second = (
        _uri_character(value, first[1], limit) if first is not None else None
    )
    if not (
        first is not None
        and second is not None
        and first[0].isascii()
        and first[0].isalpha()
        and second[0] == ":"
    ):
        if not leading_separators:
            return None
        authority: list[str] = []
        while index < limit:
            decoded = _uri_character(value, index, limit)
            if decoded is None:
                return None
            character, source_end, _encoded = decoded
            if character in {"/", "\\"}:
                index = source_end
                break
            if _is_control(character) or character.isspace():
                return None
            authority.append(character)
            index = source_end
        if "".join(authority).casefold() != "localhost":
            return None
        while index < limit:
            decoded = _uri_character(value, index, limit)
            if decoded is None or decoded[0] not in {"/", "\\"}:
                break
            index = decoded[1]
        first = _uri_character(value, index, limit)
        second = (
            _uri_character(value, first[1], limit)
            if first is not None
            else None
        )

    if not (
        first is not None
        and second is not None
        and first[0].isascii()
        and first[0].isalpha()
        and second[0] == ":"
    ):
        return None
    drive = (first[0] + ":").casefold()
    index = second[1]
    separators = 0
    while index < limit:
        decoded = _uri_character(value, index, limit)
        if decoded is None or decoded[0] not in {"/", "\\"}:
            break
        separators += 1
        index = decoded[1]
    if not separators:
        return None
    return drive, index


def _windows_add_semantic_component(
    raw_component: str,
    components: list[str],
    root: tuple[str, tuple[frozenset[str], ...]],
    drive: str,
) -> tuple[bool, bool]:
    if raw_component == ".":
        return True, _windows_components_reach_root(drive, components, root)
    if raw_component == "..":
        if components:
            components.pop()
        return True, _windows_components_reach_root(drive, components, root)

    component = raw_component.rstrip(" .")
    if not component:
        return True, _windows_components_reach_root(drive, components, root)
    if (
        len(component) > _MAX_COMPONENT_CHARACTERS
        or any(
            character in '<>:"|?*' or _is_control(character)
            for character in component
        )
    ):
        return False, False
    components.append(component.casefold())
    return True, _windows_components_reach_root(drive, components, root)


def _windows_component_accepts_space(
    component: list[str],
    components: list[str],
    root: tuple[str, tuple[frozenset[str], ...]],
) -> bool:
    aliases = root[1]
    if len(components) >= len(aliases):
        return False
    candidate = ("".join(component) + " ").casefold()
    return any(alias.startswith(candidate) for alias in aliases[len(components)])


def _windows_native_component_is_canceled(
    value: str,
    index: int,
    limit: int,
) -> bool:
    separator = index
    while separator < limit and value[separator] not in {"/", "\\"}:
        character = value[separator]
        if _is_control(character) or character in _WINDOWS_TOKEN_STRUCTURAL_TERMINATORS:
            return False
        separator += 1
    if separator >= limit:
        return False
    while separator < limit and value[separator] in {"/", "\\"}:
        separator += 1
    return (
        value[separator : separator + 2] == ".."
        and value[separator + 2 : separator + 3] in {"", "/", "\\"}
    )


def _windows_semantic_root_match_end(
    value: str,
    start: int,
    root: tuple[str, tuple[frozenset[str], ...]],
    *,
    file_uri: bool,
    quoted: bool,
) -> int | None:
    limit = min(
        len(value), start + _windows_candidate_inspection_characters(root)
    )
    prefix = _windows_semantic_prefix(
        value, start, limit, file_uri=file_uri
    )
    if prefix is None:
        return None
    drive, index = prefix
    if drive != root[0]:
        return None
    components: list[str] = []
    if not root[1]:
        return index

    component: list[str] = []
    component_source_end = index
    disposable_component: bool | None = None
    while index < limit:
        if file_uri:
            decoded = _uri_character(value, index, limit)
            if decoded is None:
                return None
            character, source_end, encoded = decoded
        else:
            character = value[index]
            source_end = index + 1
            encoded = False

        separator = character in {"/", "\\"}
        unquoted_space = (
            not quoted
            and not encoded
            and character.isspace()
        )
        space_allowed = False
        if unquoted_space and not file_uri and character == " ":
            space_allowed = _windows_component_accepts_space(
                component, components, root
            )
            if not space_allowed:
                if disposable_component is None:
                    disposable_component = _windows_native_component_is_canceled(
                        value, index, limit
                    )
                space_allowed = disposable_component
        terminator = (
            _is_control(character)
            or (file_uri and character == "#" and not encoded)
            or (quoted and character == '"' and not encoded)
            or (unquoted_space and not space_allowed)
            or character in '<>:"|?*'
        )
        if separator or terminator:
            valid, matched = _windows_add_semantic_component(
                "".join(component), components, root, drive
            )
            if not valid:
                return None
            if matched:
                return component_source_end
            component.clear()
            disposable_component = None
            if terminator:
                return None
            index = source_end
            component_source_end = index
            continue

        component.append(character)
        component_source_end = source_end
        index = source_end

    valid, matched = _windows_add_semantic_component(
        "".join(component), components, root, drive
    )
    if valid and matched:
        return component_source_end
    return None


def _windows_redaction_end(
    value: str,
    start: int,
    root_end: int,
    *,
    file_uri: bool,
    quoted: bool,
) -> int:
    limit = min(len(value), start + _MAX_REDACTION_PATH_TOKEN)
    index = root_end
    while index < limit:
        character = value[index]
        if (
            _is_control(character)
            or (file_uri and character == "#")
            or (quoted and character == '"')
            or (
                not quoted
                and (
                    character.isspace()
                    or character in ":,;)]}"
                )
            )
            or character in _WINDOWS_TOKEN_STRUCTURAL_TERMINATORS
        ):
            break
        if file_uri and character == "%":
            if (
                index + 3 > limit
                or re.fullmatch(r"[0-9A-Fa-f]{2}", value[index + 1 : index + 3])
                is None
            ):
                break
            index += 3
            continue
        index += 1

    match_end = index
    punctuation_start = match_end
    while punctuation_start > root_end and (
        value[punctuation_start - 1] in _WINDOWS_LOG_TRAILING_PUNCTUATION
    ):
        punctuation_start -= 1
    if (
        punctuation_start == root_end
        or value[punctuation_start - 1 : punctuation_start] not in {"/", "\\"}
    ):
        match_end = punctuation_start
    return match_end


def _redact_windows_path_tokens(value: str, path: Path, marker: str) -> str:
    root = _windows_root_component_aliases(path)
    if root is None:
        return value
    pieces: list[str] = []
    cursor = 0
    index = 0
    while index < len(value):
        start = index
        previous = value[start - 1 : start]
        bounded = not (
            previous and (previous.isalnum() or previous in "_\\/%")
        )
        file_uri = bounded and value[start : start + 5].casefold() == "file:"
        native_start = (
            start
            if (
                bounded
                and value[start : start + 1].isascii()
                and value[start : start + 1].isalpha()
                and value[start + 1 : start + 2] == ":"
                and value[start + 2 : start + 3] in {"/", "\\"}
            )
            else (_windows_extended_local_start(value, start) if bounded else None)
        )
        native = native_start is not None
        if not (file_uri or native):
            index += 1
            continue

        quoted = previous == '"'
        root_end = (
            _windows_native_root_match_end(
                value, native_start, root, quoted=quoted
            )
            if native_start is not None
            else None
        )
        if root_end is None:
            root_end = _windows_semantic_root_match_end(
                value,
                native_start if native_start is not None else start,
                root,
                file_uri=file_uri,
                quoted=quoted,
            )

        if root_end is not None:
            match_end = _windows_redaction_end(
                value,
                start,
                root_end,
                file_uri=file_uri,
                quoted=quoted,
            )
            pieces.append(value[cursor:start])
            pieces.append(marker)
            cursor = match_end
            index = max(start + 1, match_end)
            continue
        index = start + 1
    pieces.append(value[cursor:])
    return "".join(pieces)


def _posix_root_components(
    path: Path,
) -> tuple[str, tuple[str, ...]] | None:
    try:
        raw = path.as_posix()
        encoded = raw.encode("utf-8", errors="strict")
    except (AttributeError, UnicodeError):
        return None
    if (
        not raw.startswith("/")
        or raw.startswith("//")
        or len(raw) > _MAX_REDACTION_PATH_TOKEN
        or len(encoded) > _MAX_REDACTION_PATH_TOKEN
        or any(_is_control(character) for character in raw)
    ):
        return None
    normalized = posixpath.normpath(raw)
    if not normalized.startswith("/") or normalized.startswith("//"):
        return None
    components = tuple(component for component in normalized.split("/") if component)
    if len(components) > _MAX_COMPONENTS:
        return None
    for component in components:
        try:
            component_bytes = component.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            return None
        if (
            len(component) > _MAX_COMPONENT_CHARACTERS
            or len(component_bytes) > _MAX_COMPONENT_BYTES
            or any(_is_control(character) or character == "/" for character in component)
        ):
            return None
    return normalized, components


def _posix_candidate_inspection_characters(
    root: tuple[str, tuple[str, ...]],
) -> int:
    root_characters = len(root[0]) + len(root[1])
    return min(
        _MAX_REDACTION_PATH_TOKEN,
        _MAX_REDACTION_PATH_TOKEN // 2 + root_characters * 12,
    )


def _posix_candidate_starts_at(value: str, start: int) -> bool:
    return value[start : start + 5].casefold() == "file:" or value[
        start : start + 1
    ] == "/"


def _posix_root_boundary(
    value: str,
    index: int,
    *,
    file_uri: bool,
    quoted: bool,
) -> bool:
    if index >= len(value):
        return True
    if file_uri:
        decoded = _uri_character(value, index, min(len(value), index + 12))
        if decoded is None:
            return False
        character, _source_end, encoded = decoded
    else:
        character = value[index]
        encoded = False
    if character == "/":
        return True
    if encoded:
        return False
    if (
        character in ":?#<>\""
        or character.isspace()
        or _is_control(character)
        or (quoted and character == '"')
    ):
        return True
    if character not in _WINDOWS_LOG_TRAILING_PUNCTUATION:
        return False

    punctuation_end = index
    while (
        punctuation_end < len(value)
        and value[punctuation_end] in _WINDOWS_LOG_TRAILING_PUNCTUATION
    ):
        punctuation_end += 1
    if punctuation_end >= len(value):
        return True
    following = value[punctuation_end]
    if following.isspace() or _is_control(following) or following == '"':
        return True
    return character in {",", ";"} and _posix_candidate_starts_at(
        value, punctuation_end
    )


def _posix_native_root_match_end(
    value: str,
    start: int,
    root: tuple[str, tuple[str, ...]],
    *,
    quoted: bool,
) -> int | None:
    root_text = root[0]
    if not value.startswith(root_text, start):
        return None
    end = start + len(root_text)
    if _posix_root_boundary(value, end, file_uri=False, quoted=quoted):
        return end
    return None


def _posix_semantic_prefix(
    value: str,
    start: int,
    limit: int,
    *,
    file_uri: bool,
) -> int | None:
    if not file_uri:
        if value[start : start + 1] != "/":
            return None
        index = start
        while index < limit and value[index : index + 1] == "/":
            index += 1
        return index

    index = start + 5
    first = _uri_character(value, index, limit)
    if first is None or first[0] != "/":
        return None
    index = first[1]
    second = _uri_character(value, index, limit)
    if second is None or second[0] != "/":
        return index

    index = second[1]
    third = _uri_character(value, index, limit)
    if third is not None and third[0] == "/":
        index = third[1]
        fourth = _uri_character(value, index, limit)
        if fourth is not None and fourth[0] == "/":
            return None
        return index

    authority: list[str] = []
    while index < limit:
        decoded = _uri_character(value, index, limit)
        if decoded is None:
            return None
        character, source_end, encoded = decoded
        if character == "/":
            index = source_end
            break
        if (
            _is_control(character)
            or character.isspace()
            or character == "\\"
            or (not encoded and character in "?#")
        ):
            return None
        authority.append(character)
        index = source_end
    else:
        return None
    if "".join(authority).casefold() != "localhost":
        return None
    while index < limit:
        decoded = _uri_character(value, index, limit)
        if decoded is None or decoded[0] != "/":
            break
        index = decoded[1]
    return index


def _posix_components_reach_root(
    components: list[str],
    root: tuple[str, tuple[str, ...]],
) -> bool:
    root_components = root[1]
    return len(components) == len(root_components) and all(
        component == expected
        for component, expected in zip(components, root_components)
    )


def _posix_components_end_at_root(
    components: list[str],
    root: tuple[str, tuple[str, ...]],
) -> bool:
    root_components = root[1]
    return (
        bool(root_components)
        and len(components) >= len(root_components)
        and all(
            component == expected
            for component, expected in zip(
                components[-len(root_components) :], root_components
            )
        )
    )


def _posix_add_semantic_component(
    raw_component: str,
    components: list[str],
    root: tuple[str, tuple[str, ...]],
) -> tuple[bool, bool]:
    if raw_component in {"", "."}:
        return True, _posix_components_reach_root(components, root)
    if raw_component == "..":
        if components:
            components.pop()
        return True, _posix_components_reach_root(components, root)
    try:
        component_bytes = raw_component.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False, False
    if (
        len(raw_component) > _MAX_COMPONENT_CHARACTERS
        or len(component_bytes) > _MAX_COMPONENT_BYTES
        or any(_is_control(character) or character == "/" for character in raw_component)
    ):
        return False, False
    components.append(raw_component)
    return True, _posix_components_reach_root(components, root)


def _posix_component_accepts_log_character(
    component: list[str],
    components: list[str],
    root: tuple[str, tuple[str, ...]],
    character: str,
) -> bool:
    if len(components) >= len(root[1]):
        return False
    return root[1][len(components)].startswith("".join(component) + character)


def _posix_native_component_is_canceled(
    value: str,
    index: int,
    limit: int,
) -> bool:
    separator = index
    while separator < limit and value[separator] != "/":
        character = value[separator]
        if _is_control(character) or character in '<>"':
            return False
        separator += 1
    if separator >= limit:
        return False
    while separator < limit and value[separator] == "/":
        separator += 1
    return (
        value[separator : separator + 2] == ".."
        and value[separator + 2 : separator + 3] in {"", "/"}
    )


def _posix_semantic_root_match_end(
    value: str,
    start: int,
    root: tuple[str, tuple[str, ...]],
    *,
    file_uri: bool,
    quoted: bool,
) -> tuple[int | None, int]:
    limit = min(len(value), start + _posix_candidate_inspection_characters(root))
    index = _posix_semantic_prefix(value, start, limit, file_uri=file_uri)
    if index is None:
        return None, start + 1
    components: list[str] = []
    if not root[1]:
        return index, index

    component: list[str] = []
    component_source_end = index
    disposable_component: bool | None = None
    while index < limit:
        if file_uri:
            decoded = _uri_character(value, index, limit)
            if decoded is None:
                return None, max(start + 1, index + 1)
            character, source_end, encoded = decoded
        else:
            character = value[index]
            source_end = index + 1
            encoded = False

        separator = character == "/"
        unquoted_boundary = not quoted and not encoded and (
            character.isspace() or character in ":,;)]}<>\""
        )
        boundary_allowed = False
        if unquoted_boundary and not file_uri:
            boundary_allowed = _posix_component_accepts_log_character(
                component, components, root, character
            )
            if not boundary_allowed:
                if disposable_component is None:
                    disposable_component = _posix_native_component_is_canceled(
                        value, index, limit
                    )
                boundary_allowed = disposable_component
        terminator = (
            _is_control(character)
            or (file_uri and not encoded and character in "?#")
            or (file_uri and not encoded and character == "\\")
            or (quoted and not encoded and character == '"')
            or (unquoted_boundary and not boundary_allowed)
        )
        if separator or terminator:
            valid, matched = _posix_add_semantic_component(
                "".join(component), components, root
            )
            if not valid:
                return None, index
            matched = matched or _posix_components_end_at_root(components, root)
            if matched:
                if _posix_root_boundary(
                    value,
                    component_source_end,
                    file_uri=file_uri,
                    quoted=quoted,
                ):
                    return component_source_end, component_source_end
                return None, source_end
            component.clear()
            disposable_component = None
            if terminator:
                return None, index
            index = source_end
            component_source_end = index
            continue

        component.append(character)
        component_source_end = source_end
        index = source_end

    valid, matched = _posix_add_semantic_component(
        "".join(component), components, root
    )
    if valid and (matched or _posix_components_end_at_root(components, root)):
        return component_source_end, component_source_end
    return None, limit


def _posix_redaction_end(
    value: str,
    start: int,
    root_end: int,
    *,
    file_uri: bool,
    quoted: bool,
) -> int:
    limit = min(len(value), start + _MAX_REDACTION_PATH_TOKEN)
    index = root_end
    while index < limit:
        character = value[index]
        if (
            _is_control(character)
            or (file_uri and character in "?#")
            or (quoted and character == '"')
            or (
                not quoted
                and (character.isspace() or character in ":,;)]}")
            )
            or character in '<>"'
            or (file_uri and character == "\\")
        ):
            break
        if file_uri and character == "%":
            if (
                index + 3 > limit
                or re.fullmatch(r"[0-9A-Fa-f]{2}", value[index + 1 : index + 3])
                is None
            ):
                break
            index += 3
            continue
        index += 1
    return index


def _redact_posix_path_tokens(value: str, path: Path, marker: str) -> str:
    root = _posix_root_components(path)
    if root is None:
        return value
    pieces: list[str] = []
    cursor = 0
    index = 0
    while index < len(value):
        start = index
        previous = value[start - 1 : start]
        bounded = not (
            previous and (previous.isalnum() or previous in "_/%")
        ) and not (
            previous == ":" and value[start + 1 : start + 2] == "/"
        )
        file_uri = bounded and value[start : start + 5].casefold() == "file:"
        native = bounded and value[start : start + 1] == "/"
        if not (file_uri or native):
            index += 1
            continue

        quoted = previous == '"'
        root_end = (
            _posix_native_root_match_end(
                value, start, root, quoted=quoted
            )
            if native
            else None
        )
        resume = start + 1
        if root_end is None:
            root_end, resume = _posix_semantic_root_match_end(
                value,
                start,
                root,
                file_uri=file_uri,
                quoted=quoted,
            )
        if root_end is not None:
            match_end = _posix_redaction_end(
                value,
                start,
                root_end,
                file_uri=file_uri,
                quoted=quoted,
            )
            pieces.append(value[cursor:start])
            pieces.append(marker)
            cursor = match_end
            index = max(start + 1, match_end)
            continue
        index = max(start + 1, resume)
    pieces.append(value[cursor:])
    return "".join(pieces)


def _redact_path(value: str, path: Path, marker: str) -> str:
    windows_path = bool(PureWindowsPath(str(path)).drive)
    if windows_path:
        return _redact_windows_path_tokens(value, path, marker)
    return _redact_posix_path_tokens(value, path, marker)


def _normalize_log_text(value: str) -> str:
    pieces: list[str] = []
    index = 0
    state = "ground"
    osc = False
    string_introducers = {
        "P": False,
        "X": False,
        "]": True,
        "^": False,
        "_": False,
    }
    c1_string_introducers = {
        "\x90": False,
        "\x98": False,
        "\x9d": True,
        "\x9e": False,
        "\x9f": False,
    }
    c1_introducers = frozenset(("\x9b", *c1_string_introducers))

    def append_safe_space() -> None:
        if not pieces or pieces[-1] != " ":
            pieces.append(" ")

    while index < len(value):
        character = value[index]
        if state == "string":
            if character == "\x9c":
                state = "ground"
                index += 1
                continue
            if character == "\x1b" and value[index + 1 : index + 2] == "\\":
                state = "ground"
                index += 2
                continue
            if osc and character == "\x07":
                state = "ground"
                index += 1
                continue
        if state != "ground" and (
            character == "\x1b" or character in c1_introducers
        ):
            state = "ground"
            continue
        if state == "escape":
            index += 1
            if "0" <= character <= "~":
                state = "ground"
            elif not " " <= character <= "/":
                state = "ground"
            continue
        if state == "csi":
            index += 1
            if "@" <= character <= "~":
                state = "ground"
            continue
        if state == "string":
            index += 1
            continue
        if character == "\x1b":
            following = value[index + 1 : index + 2]
            if following == "[":
                state = "csi"
                index += 2
                continue
            if following in string_introducers:
                state = "string"
                osc = string_introducers[following]
                index += 2
                continue
            if following == "\\":
                index += 2
                continue
            if following and " " <= following <= "/":
                state = "escape"
                index += 2
                continue
            if following and "0" <= following <= "~":
                index += 2
                continue
            index += 1
            continue
        elif character == "\x9b":
            state = "csi"
            index += 1
            continue
        elif character in c1_string_introducers:
            state = "string"
            osc = c1_string_introducers[character]
            index += 1
            continue
        if not _is_control(character):
            pieces.append(character)
        elif (
            ord(character) < 32
            or 127 <= ord(character) <= 159
            or unicodedata.category(character) in {"Zl", "Zp"}
        ):
            append_safe_space()
        index += 1
    return "".join(pieces)


def redact_lsp_text(
    value: str,
    *,
    repository: RepositoryScope | None = None,
) -> str:
    """Remove credentials, local roots, and log injection from bounded raw text."""
    if not isinstance(value, str):
        raise TypeError("value must be a string")
    if repository is not None:
        repository = _require_repository(repository)
    if len(value) > _MAX_REDACTION_RAW_INPUT:
        return _OVERSIZED_REDACTION_MARKER

    redacted = _normalize_log_text(value)
    redacted = _redact_assignments(redacted)
    redacted = _redact_url_userinfo(redacted)
    if repository is not None:
        redacted = _redact_path(
            redacted,
            Path(repository.checkout_root),
            "<repository>",
        )
    try:
        home = Path.home().absolute()
        home_paths = [home]
        try:
            resolved_home = home.resolve(strict=False)
        except (OSError, RuntimeError):
            pass
        else:
            if resolved_home not in home_paths:
                home_paths.append(resolved_home)
        for home_path in home_paths:
            redacted = _redact_path(redacted, home_path, "<home>")
    except (OSError, RuntimeError):
        pass
    return _normalize_log_text(redacted)[:1024]


__all__ = [
    "PathContainmentError",
    "RepositorySource",
    "normalize_provider_uri",
    "redact_lsp_text",
    "resolve_repository_source",
]
