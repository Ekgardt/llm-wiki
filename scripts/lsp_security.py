"""Contain LSP navigation evidence to a trusted repository and redact logs.

The language server still runs with the operator's permissions. This module is
not a sandbox: it validates requested and returned navigation evidence, while
the repository and configured Pyright process remain trusted.
"""

from __future__ import annotations

import ntpath
import os
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
_MAX_REDACTION_PATH_TOKEN = 128 * 1024
_MALFORMED_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
_ENCODED_SEPARATOR = re.compile(r"%(?:2f|5c)", re.IGNORECASE)
_WINDOWS_DRIVE_PATH = re.compile(r"^[A-Za-z]:/")
_URL_AUTHORITY_TOKEN_BOUNDARIES = frozenset('"<>\\^`{|}')
_WINDOWS_TOKEN_STRUCTURAL_TERMINATORS = frozenset('<>|?*"')
_WINDOWS_QUOTED_TOKEN_STRUCTURAL_TERMINATORS = frozenset("<>|?*")
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


def _path_variants(path: Path) -> tuple[str, ...]:
    variants = {str(path), path.as_posix()}
    raw = str(path)
    variants.add(raw.replace("\\", "/"))
    variants.add(raw.replace("/", "\\"))
    try:
        uri = path_to_file_uri(path)
    except (TypeError, ValueError):
        uri = ""
    if uri:
        variants.add(uri)
        match = re.match(r"file:///([A-Za-z]):/", uri)
        if match is not None:
            drive = match.group(1)
            variants.add(uri.replace(f"/{drive}:/", f"/{drive.lower()}:/", 1))
            variants.add(uri.replace(f"/{drive}:/", f"/{drive}%3A/", 1))
            variants.add(uri.replace("file:///", "file:/", 1))
            variants.add(uri.replace("file:///", "file:", 1))
    return tuple(sorted((item for item in variants if len(item) > 1), key=len, reverse=True))


def _uri_character_pattern(character: str) -> str:
    encoded = "".join(f"%{byte:02X}" for byte in character.encode("utf-8"))
    return f"(?:{re.escape(character)}|(?i:{encoded}))"


def _case_insensitive_character_pattern(
    character: str,
    *equivalent_characters: str,
) -> str:
    seeds = tuple(dict.fromkeys((character, *equivalent_characters)))
    aliases = tuple(
        dict.fromkeys(
            alias
            for seed in seeds
            for alias in (seed, seed.lower(), seed.upper(), seed.casefold())
        )
    )
    raw_aliases: dict[tuple[int, str], str] = {}
    for alias in aliases:
        raw_aliases.setdefault((len(alias), alias.casefold()), alias)
    encoded_aliases = sorted(
        {
            "".join(f"%{byte:02X}" for byte in alias.encode("utf-8"))
            for alias in aliases
        }
    )
    patterns = [
        *(re.escape(alias) for alias in raw_aliases.values()),
        *(f"(?i:{alias})" for alias in encoded_aliases),
    ]
    patterns.sort(key=lambda pattern: (-len(pattern), pattern))
    return "(?:" + "|".join(patterns) + ")"


def _encoded_file_uri_pattern(path: Path) -> re.Pattern[str]:
    logical_path = PurePosixPath(str(path)).as_posix()
    path_pattern = "".join(_uri_character_pattern(character) for character in logical_path)
    localhost_pattern = "".join(
        _case_insensitive_character_pattern(character) for character in "localhost"
    )
    prefix = rf"(?i:file:)(?://(?i:{localhost_pattern})?)?"
    return re.compile(prefix + path_pattern)


def _decode_uri_percent_token_once(
    value: str,
    start: int,
    end: int,
) -> tuple[str, tuple[int, ...], tuple[bool, ...]] | None:
    characters: list[str] = []
    source_ends: list[int] = []
    encoded: list[bool] = []
    byte_values = bytearray()
    byte_ends: list[int] = []

    def flush_bytes() -> bool:
        if not byte_values:
            return True
        try:
            decoded = bytes(byte_values).decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return False
        byte_index = 0
        for character in decoded:
            byte_index += len(character.encode("utf-8"))
            characters.append(character)
            source_ends.append(byte_ends[byte_index - 1])
            encoded.append(True)
        byte_values.clear()
        byte_ends.clear()
        return True

    index = start
    while index < end:
        if value[index] == "%":
            digits = value[index + 1 : index + 3]
            if len(digits) == 2 and re.fullmatch(r"[0-9A-Fa-f]{2}", digits):
                byte_values.append(int(digits, 16))
                byte_ends.append(index + 3)
                index += 3
                continue
            return None
        if not flush_bytes():
            return None
        characters.append(value[index])
        source_ends.append(index + 1)
        encoded.append(False)
        index += 1
    if not flush_bytes():
        return None
    return "".join(characters), tuple(source_ends), tuple(encoded)


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


def _windows_path_is_within_root(
    normalized: tuple[str, tuple[str, ...]] | None,
    root: tuple[str, tuple[frozenset[str], ...]],
) -> bool:
    if normalized is None:
        return False
    drive, components = normalized
    root_drive, root_component_aliases = root
    return (
        drive == root_drive
        and len(components) >= len(root_component_aliases)
        and all(
            component in aliases
            for component, aliases in zip(components, root_component_aliases)
        )
    )


def _windows_root_punctuation_suffix_start(
    text: str,
    encoded: tuple[bool, ...],
    normalized: tuple[str, tuple[str, ...]] | None,
    root: tuple[str, tuple[frozenset[str], ...]],
) -> int | None:
    if normalized is None:
        return None
    drive, components = normalized
    root_drive, root_component_aliases = root
    if (
        drive != root_drive
        or not root_component_aliases
        or len(components) != len(root_component_aliases)
        or any(
            component not in aliases
            for component, aliases in zip(
                components[:-1], root_component_aliases[:-1]
            )
        )
    ):
        return None

    final_component = components[-1]
    for alias in sorted(root_component_aliases[-1], key=len, reverse=True):
        if not final_component.startswith(alias):
            continue
        suffix = final_component[len(alias) :]
        suffix_start = len(text) - len(suffix)
        if (
            suffix
            and all(character in _WINDOWS_LOG_TRAILING_PUNCTUATION for character in suffix)
            and text[suffix_start:].casefold() == suffix
            and all(not encoded[index] for index in range(suffix_start, len(text)))
        ):
            return suffix_start
    return None


def _windows_terminal_path_end(
    text: str,
    encoded: tuple[bool, ...],
) -> int | None:
    cursor = len(text)
    while (
        cursor > 0
        and not encoded[cursor - 1]
        and text[cursor - 1] in _WINDOWS_LOG_TRAILING_PUNCTUATION
    ):
        cursor -= 1
    punctuation_start = cursor
    locations = 0
    for _part in range(2):
        digits_end = cursor
        while (
            cursor > 0
            and not encoded[cursor - 1]
            and text[cursor - 1].isascii()
            and text[cursor - 1].isdigit()
        ):
            cursor -= 1
        colon = cursor - 1
        if (
            cursor == digits_end
            or colon < 0
            or encoded[colon]
            or text[colon] != ":"
        ):
            break
        locations += 1
        cursor = colon
    if locations:
        return cursor
    if punctuation_start < len(text):
        return punctuation_start
    return None


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
        native = (
            bounded
            and value[start : start + 1].isascii()
            and value[start : start + 1].isalpha()
            and value[start + 1 : start + 2] == ":"
            and value[start + 2 : start + 3] in {"/", "\\"}
        )
        if not (file_uri or native):
            index += 1
            continue

        quoted = previous == '"'
        limit = min(len(value), start + _MAX_REDACTION_PATH_TOKEN)
        token_end = start
        while token_end < limit:
            character = value[token_end]
            if (
                _is_control(character)
                or (file_uri and character == "#")
                or (
                    quoted
                    and character == '"'
                )
                or (
                    not quoted
                    and (
                        character.isspace()
                        or character in _WINDOWS_TOKEN_STRUCTURAL_TERMINATORS
                    )
                )
                or (
                    quoted
                    and character in _WINDOWS_QUOTED_TOKEN_STRUCTURAL_TERMINATORS
                )
            ):
                break
            if file_uri and character == "%":
                digits = value[token_end + 1 : token_end + 3]
                if (
                    token_end + 3 > limit
                    or len(digits) != 2
                    or re.fullmatch(r"[0-9A-Fa-f]{2}", digits) is None
                ):
                    break
                token_end += 3
                continue
            token_end += 1

        next_index = token_end
        if token_end < len(value) and token_end < limit:
            next_index += 1
        if token_end == start:
            index = max(start + 1, next_index)
            continue

        source_ends: tuple[int, ...] | None = None
        if file_uri:
            decoded = _decode_uri_percent_token_once(value, start, token_end)
            if decoded is None:
                index = next_index
                continue
            text, source_ends, encoded = decoded
        else:
            text = value[start:token_end]
            encoded = (False,) * len(text)

        normalized = _canonical_windows_path_token(text, file_uri=file_uri)
        logical_match_end: int | None = None
        if _windows_path_is_within_root(normalized, root):
            logical_match_end = len(text)
        else:
            logical_match_end = _windows_root_punctuation_suffix_start(
                text, encoded, normalized, root
            )
            if logical_match_end is None:
                terminal_path_end = _windows_terminal_path_end(text, encoded)
                if terminal_path_end is not None and _windows_path_is_within_root(
                    _canonical_windows_path_token(
                        text[:terminal_path_end], file_uri=file_uri
                    ),
                    root,
                ):
                    logical_match_end = terminal_path_end

        if logical_match_end is not None:
            if logical_match_end == len(text):
                match_end = token_end
            elif source_ends is None:
                match_end = start + logical_match_end
            else:
                match_end = source_ends[logical_match_end - 1]
            pieces.append(value[cursor:start])
            pieces.append(marker)
            cursor = match_end
        index = next_index
    pieces.append(value[cursor:])
    return "".join(pieces)


def _redact_path(value: str, path: Path, marker: str) -> str:
    windows_path = bool(PureWindowsPath(str(path)).drive)
    if windows_path:
        return _redact_windows_path_tokens(value, path, marker)
    for variant in _path_variants(path):
        value = re.sub(
            re.escape(variant),
            lambda _match: marker,
            value,
            flags=re.IGNORECASE if variant.startswith("file:") else 0,
        )
    return _encoded_file_uri_pattern(path).sub(lambda _match: marker, value)


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
    while index < len(value):
        character = value[index]
        if state == "escape":
            if character == "\x1b":
                state = "ground"
                continue
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
            if character == "\x9c":
                state = "ground"
                index += 1
            elif character == "\x1b" and value[index + 1 : index + 2] == "\\":
                state = "ground"
                index += 2
            elif osc and character == "\x07":
                state = "ground"
                index += 1
            else:
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
        index += 1
    return "".join(pieces)


def redact_lsp_text(
    value: str,
    *,
    repository: RepositoryScope | None = None,
) -> str:
    """Remove credentials, local roots, and log injection before bounding text."""
    if not isinstance(value, str):
        raise TypeError("value must be a string")
    if repository is not None:
        repository = _require_repository(repository)

    redacted = _normalize_log_text(value)
    redacted = _redact_assignments(redacted)
    redacted = _redact_url_userinfo(redacted)
    if repository is not None:
        redacted = _redact_path(
            redacted, Path(repository.checkout_root), "<repository>"
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
