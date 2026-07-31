"""Bounded, capability-honest Pyright language-server session."""

import atexit
import hashlib
import math
import os
import re
import secrets
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import BinaryIO

import windows_workspace as _windows_workspace
from bounded_io import read_stable_bytes
from code_intelligence import PositionEncoding
from compile_cache import _restrict_owner_only, _verify_owner_only
from lsp_paths import lsp_owner_root
from lsp_positions import (
    LspPosition,
    LspRange,
    SourceAnchor,
    SourceDocument,
    path_to_file_uri,
)
from lsp_process import GenerationLaunch, LspProcess, ProcessState, StartupCleanupError
from lsp_protocol import (
    MAX_FRAME_BYTES,
    MAX_HOVER_BYTES,
    MAX_LOCATIONS,
    JsonRpcResponseError,
    LspProtocol,
    ProtocolViolation,
    encode_frame,
)
from lsp_security import (
    RepositorySource,
    normalize_provider_uri,
    resolve_repository_source,
)
from pyright_profile import (
    MAX_SERVER_BYTES,
    PYRIGHT_CONFIGURATION,
    PYRIGHT_INITIALIZATION_OPTIONS,
    PYRIGHT_INITIALIZATION_OPTIONS_SHA256,
    PyrightIdentity,
    thaw_pyright_profile_value,
)
from reliable_memory import _known_network_path
from repository_scope import RepositoryScope

STARTUP_SECONDS = 60.0
MAX_LSP_PROCESSES = 4

_OWNER_CLEANUP_SECONDS = 2.0
_MAX_DOCUMENT_BYTES = MAX_FRAME_BYTES - 1
_MAX_OPEN_DOCUMENTS = 256
_MAX_OPEN_DOCUMENT_BYTES = 64 * 1024 * 1024
_MAX_CONFIGURATION_ITEMS = 64
_MAX_CONFIGURATION_SECTION_BYTES = 256
_MAX_PREPARED_CALL_ITEMS = MAX_LOCATIONS
_MAX_CALL_ITEM_TEXT_BYTES = 4096
_MAX_DIAGNOSTIC_TEXT_BYTES = 64 * 1024
_MAX_DIAGNOSTIC_URIS = 256
_MAX_DIAGNOSTIC_BYTES = 16 * 1024 * 1024
_DIAGNOSTIC_BASE_BYTES = 128
_DIAGNOSTIC_RELATED_BASE_BYTES = 96
_MAX_PROGRESS_TEXT_BYTES = 4096
_MAX_PROGRESS_EVENTS = 256
_MAX_PROGRESS_BYTES = 1024 * 1024
_PROGRESS_EVENT_BASE_BYTES = 32
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_LSP_UINTEGER_MAX = 2**31 - 1
_SHA256 = re.compile(r"[0-9a-f]{64}")
_CAPABILITY_FIELDS = {
    "calls": "callHierarchyProvider",
    "definition": "definitionProvider",
    "document_symbols": "documentSymbolProvider",
    "hover": "hoverProvider",
    "implementations": "implementationProvider",
    "references": "referencesProvider",
    "type_definition": "typeDefinitionProvider",
    "workspace_symbols": "workspaceSymbolProvider",
}
_NODE_MAIN_LOADER = "\n".join(
    (
        '"use strict";',
        'const fs = require("node:fs");',
        'const path = require("node:path");',
        'const Module = require("node:module");',
        "const descriptorText = process.argv[1];",
        "const filename = process.argv[2];",
        'if (!/^(0|[1-9][0-9]*)$/.test(descriptorText)) throw new Error("invalid held descriptor");',
        "const descriptor = Number(descriptorText);",
        'if (!Number.isSafeInteger(descriptor)) throw new Error("invalid held descriptor");',
        'const source = fs.readFileSync(descriptor, "utf8");',
        "fs.closeSync(descriptor);",
        "const argv = process.argv.slice(3);",
        "process.argv = [process.execPath, filename, ...argv];",
        "const main = new Module(filename, null);",
        'main.id = ".";',
        "main.path = path.dirname(filename);",
        "main.filename = filename;",
        "main.paths = Module._nodeModulePaths(main.path);",
        "process.mainModule = main;",
        "Module._cache[filename] = main;",
        "main._compile(source, filename);",
        "main.loaded = true;",
    )
)
_CLIENT_CAPABILITIES = {
    "general": {"positionEncodings": ("utf-8", "utf-16", "utf-32")},
    "textDocument": {
        "callHierarchy": {"dynamicRegistration": False},
        "definition": {"dynamicRegistration": False, "linkSupport": True},
        "documentSymbol": {
            "dynamicRegistration": False,
            "hierarchicalDocumentSymbolSupport": True,
        },
        "hover": {
            "dynamicRegistration": False,
            "contentFormat": ("plaintext",),
        },
        "implementation": {"dynamicRegistration": False, "linkSupport": True},
        "publishDiagnostics": {
            "relatedInformation": True,
            "versionSupport": True,
        },
        "references": {"dynamicRegistration": False},
        "typeDefinition": {"dynamicRegistration": False, "linkSupport": True},
    },
    "window": {"workDoneProgress": True},
    "workspace": {
        "configuration": True,
        "symbol": {"dynamicRegistration": False},
        "workspaceFolders": True,
    },
}


@dataclass(frozen=True, slots=True)
class OpenDocument:
    source: RepositorySource
    content: bytes
    source_sha256: str
    version: int


@dataclass(frozen=True, slots=True)
class LspLocation:
    uri: str
    range: LspRange


@dataclass(frozen=True, slots=True)
class ProviderLocations:
    locations: tuple[LspLocation, ...]
    coverage: str
    partial: bool


@dataclass(frozen=True, slots=True)
class ProviderHover:
    contents: str | None
    range: LspRange | None
    partial: bool


@dataclass(frozen=True, slots=True)
class ProviderCalls:
    direction: str
    locations: tuple[LspLocation, ...]
    coverage: str
    partial: bool


@dataclass(frozen=True, slots=True)
class LspDiagnostic:
    uri: str
    range: LspRange
    severity: int | None
    code: str | None
    message: str
    related: tuple[tuple[LspLocation, str | None], ...]


@dataclass(frozen=True, slots=True)
class ProviderDiagnostics:
    diagnostics: tuple[LspDiagnostic, ...]
    document_version: int | None
    partial: bool


@dataclass(frozen=True, slots=True)
class _DiagnosticSnapshot:
    diagnostics: tuple[LspDiagnostic, ...]
    document_version: int | None
    partial: bool
    retained_bytes: int


def _validated_deadline(deadline: float) -> float:
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        raise TypeError("deadline must be a monotonic timestamp")
    if not math.isfinite(deadline):
        raise ValueError("deadline must be finite")
    return float(deadline)


def _require_startup_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError("Pyright startup deadline expired")


class _BootstrapDegradation(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _path_identity(path: Path) -> os.stat_result:
    info = path.lstat()
    if (
        stat.S_ISLNK(info.st_mode)
        or int(getattr(info, "st_file_attributes", 0))
        & _FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise PermissionError(f"path must not traverse a link or reparse point: {path}")
    return info


def _validated_local_file(value: object, label: str, *, deadline: float) -> Path:
    _require_startup_deadline(deadline)
    if not isinstance(value, Path):
        raise TypeError(f"{label} must be a Path")
    raw = os.fspath(value)
    if (
        not value.is_absolute()
        or raw.startswith(("\\\\", "//"))
        or "\0" in raw
        or ".." in value.parts
    ):
        raise ValueError(f"{label} must be an absolute local path")
    for parent in value.parents:
        if parent == Path(parent.anchor):
            break
        _require_startup_deadline(deadline)
        info = _path_identity(parent)
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"{label} parent must be a directory")
    _require_startup_deadline(deadline)
    info = _path_identity(value)
    if not stat.S_ISREG(info.st_mode) or _known_network_path(value):
        raise ValueError(f"{label} must be a local regular file")
    _require_startup_deadline(deadline)
    return value


def _ensure_lsp_parent(state_root: Path, *, deadline: float) -> Path:
    _require_startup_deadline(deadline)
    if not state_root.is_absolute() or _known_network_path(state_root):
        raise ValueError("state_root must be an absolute local path")
    for ancestor in state_root.parents:
        if ancestor == Path(ancestor.anchor):
            break
        _require_startup_deadline(deadline)
        ancestor_info = _path_identity(ancestor)
        if not stat.S_ISDIR(ancestor_info.st_mode):
            raise ValueError("state_root parent must be a directory")
    _require_startup_deadline(deadline)
    root_info = _path_identity(state_root)
    if not stat.S_ISDIR(root_info.st_mode):
        raise NotADirectoryError(state_root)
    run_root = state_root / "run"
    _require_startup_deadline(deadline)
    try:
        run_root.mkdir(mode=0o700)
    except FileExistsError:
        pass
    _require_startup_deadline(deadline)
    run_info = _path_identity(run_root)
    if not stat.S_ISDIR(run_info.st_mode):
        raise PermissionError("LSP run parent must be a directory")
    parent = run_root / "lsp"
    _require_startup_deadline(deadline)
    try:
        parent.mkdir(mode=0o700)
    except FileExistsError:
        pass
    _require_startup_deadline(deadline)
    parent_info = _path_identity(parent)
    if not stat.S_ISDIR(parent_info.st_mode):
        raise PermissionError("LSP owner parent must be a directory")
    _require_startup_deadline(deadline)
    _restrict_owner_only(parent, 0o700)
    _require_startup_deadline(deadline)
    _verify_owner_only(parent, 0o700)
    _require_startup_deadline(deadline)
    return parent


def _provider_supported(value: object, label: str) -> bool:
    if value is None or value is False:
        return False
    if value is True or isinstance(value, dict):
        return True
    raise _BootstrapDegradation(f"pyright_{label}_capability_invalid")


def _parse_server_capabilities(
    result: object,
) -> tuple[dict[str, bool], PositionEncoding]:
    if not isinstance(result, dict) or not isinstance(result.get("capabilities"), dict):
        raise _BootstrapDegradation("pyright_initialize_result_invalid")
    server = result["capabilities"]
    encoding_value = server.get("positionEncoding", "utf-16")
    encodings = {
        "utf-8": PositionEncoding.UTF8,
        "utf-16": PositionEncoding.UTF16,
        "utf-32": PositionEncoding.UTF32,
    }
    if not isinstance(encoding_value, str) or encoding_value not in encodings:
        raise _BootstrapDegradation("pyright_position_encoding_unsupported")
    capabilities = {
        name: _provider_supported(server.get(field), name)
        for name, field in _CAPABILITY_FIELDS.items()
    }
    capabilities["diagnostics"] = True
    return dict(sorted(capabilities.items())), encodings[encoding_value]


def _startup_code(error: BaseException) -> str:
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, _BootstrapDegradation):
            return current.code
        if isinstance(current, TimeoutError):
            return "pyright_startup_timeout"
        current = current.__cause__
    return "pyright_startup_failed"


def _startup_interruption(
    error: BaseException,
) -> KeyboardInterrupt | SystemExit | None:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (KeyboardInterrupt, SystemExit)):
            return current
        current = current.__cause__
    return None


def _lsp_coordinate(value: object) -> int | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= _LSP_UINTEGER_MAX
    ):
        return None
    return value


def _lsp_range(value: object) -> LspRange | None:
    if not isinstance(value, dict):
        return None
    start = value.get("start")
    end = value.get("end")
    if not isinstance(start, dict) or not isinstance(end, dict):
        return None
    start_line = _lsp_coordinate(start.get("line"))
    start_character = _lsp_coordinate(start.get("character"))
    end_line = _lsp_coordinate(end.get("line"))
    end_character = _lsp_coordinate(end.get("character"))
    if None in {start_line, start_character, end_line, end_character}:
        return None
    assert start_line is not None and start_character is not None
    assert end_line is not None and end_character is not None
    if (end_line, end_character) < (start_line, start_character):
        return None
    return LspRange(
        LspPosition(start_line, start_character),
        LspPosition(end_line, end_character),
    )


def _location_key(location: LspLocation) -> tuple[object, ...]:
    return (
        location.uri,
        location.range.start.line,
        location.range.start.character,
        location.range.end.line,
        location.range.end.character,
    )


def _range_json(value: LspRange) -> dict[str, dict[str, int]]:
    return {
        "start": {
            "line": value.start.line,
            "character": value.start.character,
        },
        "end": {
            "line": value.end.line,
            "character": value.end.character,
        },
    }


def _bounded_hover_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError:
        return None
    if size > MAX_HOVER_BYTES:
        return None
    return value


def _hover_fragment(value: object) -> str | None:
    if isinstance(value, str):
        return _bounded_hover_string(value)
    if not isinstance(value, dict):
        return None
    text = _bounded_hover_string(value.get("value"))
    if text is None:
        return None
    if "kind" in value:
        return text if value.get("kind") in {"plaintext", "markdown"} else None
    language = value.get("language")
    if not isinstance(language, str) or not language:
        return None
    return text


def _hover_contents(value: object) -> tuple[str | None, bool]:
    if isinstance(value, list):
        if len(value) > 1024:
            return None, True
        fragments: list[str] = []
        partial = False
        for item in value:
            fragment = _hover_fragment(item)
            if fragment is None:
                partial = True
            else:
                fragments.append(fragment)
        if not fragments:
            return None, True
        joined = "\n\n".join(fragments)
        if _bounded_hover_string(joined) is None:
            return None, True
        return joined, partial
    fragment = _hover_fragment(value)
    return fragment, fragment is None


def _bounded_text(value: object, max_bytes: int) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError:
        return None
    return value if size <= max_bytes else None


def _launch_file_state(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        stat.S_IFMT(info.st_mode),
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


class _LaunchServerGuard:
    def __init__(
        self,
        path: Path,
        expected_sha256: str,
        *,
        command: tuple[str, ...],
        deadline: float,
    ) -> None:
        self._path = path
        self._expected_sha256 = expected_sha256
        self._command = command
        self._deadline = deadline
        self._descriptor: int | None = None
        self._snapshot: BinaryIO | None = None
        self._snapshot_path: Path | None = None
        self._launch_descriptor: int | None = None
        self._state: tuple[int, int, int, int, int, int] | None = None

    def __enter__(self) -> "_LaunchServerGuard | GenerationLaunch":
        _require_startup_deadline(self._deadline)
        before = _path_identity(self._path)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_SERVER_BYTES:
            raise _BootstrapDegradation("pyright_executable_digest_mismatch")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        _require_startup_deadline(self._deadline)
        if os.name == "nt":
            import msvcrt

            handle = _windows_workspace.open_exclusive_readonly_source_file(
                self._path
            )
            try:
                descriptor = msvcrt.open_osfhandle(handle, flags)
            except BaseException:
                _windows_workspace.close_handle(handle)
                raise
        else:
            descriptor = os.open(
                self._path,
                flags | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
        self._descriptor = descriptor
        try:
            _require_startup_deadline(self._deadline)
            opened = os.fstat(descriptor)
            state = _launch_file_state(opened)
            if _launch_file_state(before) != state or not stat.S_ISREG(opened.st_mode):
                raise _BootstrapDegradation("pyright_executable_digest_mismatch")
            self._state = state
            if os.name == "posix":
                snapshot_descriptor, snapshot_name = tempfile.mkstemp()
                self._snapshot_path = Path(snapshot_name)
                try:
                    snapshot = os.fdopen(snapshot_descriptor, "w+b")
                except BaseException:
                    os.close(snapshot_descriptor)
                    raise
                self._snapshot = snapshot
                actual = self._copy_snapshot(snapshot)
                self._verify_digest(actual)
                launch_descriptor = os.open(
                    self._snapshot_path,
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                )
                self._launch_descriptor = launch_descriptor
                launch_info = os.fstat(launch_descriptor)
                snapshot_info = os.fstat(snapshot.fileno())
                if (
                    not stat.S_ISREG(launch_info.st_mode)
                    or launch_info.st_size != before.st_size
                    or _launch_file_state(launch_info)
                    != _launch_file_state(snapshot_info)
                ):
                    raise _BootstrapDegradation(
                        "pyright_executable_digest_mismatch"
                    )
                os.unlink(self._snapshot_path)
                self._snapshot_path = None
                snapshot.close()
                self._snapshot = None
                return GenerationLaunch(
                    self._posix_launch_command(launch_descriptor),
                    (launch_descriptor,),
                )
            self.verify()
        except BaseException:
            self.close()
            raise
        return self

    @staticmethod
    def _descriptor_path(descriptor: int) -> str:
        for root in (Path("/proc/self/fd"), Path("/dev/fd")):
            if root.is_dir():
                return str(root / str(descriptor))
        raise RuntimeError("POSIX inherited descriptor paths are unavailable")

    def _posix_launch_command(self, descriptor: int) -> tuple[str, ...]:
        if self._path.suffix.casefold() == ".js":
            return (
                self._command[0],
                "--eval",
                _NODE_MAIN_LOADER,
                "--",
                str(descriptor),
                str(self._path),
                *self._command[2:],
            )
        return (
            self._command[0],
            self._descriptor_path(descriptor),
            *self._command[2:],
        )

    def _copy_snapshot(self, snapshot: BinaryIO) -> str:
        descriptor = self._descriptor
        if descriptor is None:
            raise RuntimeError("Pyright launch server guard is closed")
        os.lseek(descriptor, 0, os.SEEK_SET)
        snapshot.seek(0)
        digest = hashlib.sha256()
        total = 0
        while True:
            _require_startup_deadline(self._deadline)
            chunk = os.read(descriptor, 64 * 1024)
            _require_startup_deadline(self._deadline)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_SERVER_BYTES:
                raise _BootstrapDegradation("pyright_executable_digest_mismatch")
            snapshot.write(chunk)
            digest.update(chunk)
        snapshot.flush()
        snapshot.seek(0)
        return digest.hexdigest()

    def _digest(self) -> str:
        descriptor = self._descriptor
        if descriptor is None:
            raise RuntimeError("Pyright launch server guard is closed")
        os.lseek(descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        total = 0
        while True:
            _require_startup_deadline(self._deadline)
            chunk = os.read(descriptor, 64 * 1024)
            _require_startup_deadline(self._deadline)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_SERVER_BYTES:
                raise _BootstrapDegradation("pyright_executable_digest_mismatch")
            digest.update(chunk)
        return digest.hexdigest()

    def verify(self) -> None:
        _require_startup_deadline(self._deadline)
        descriptor = self._descriptor
        state = self._state
        if descriptor is None or state is None:
            raise RuntimeError("Pyright launch server guard is not open")
        if _launch_file_state(os.fstat(descriptor)) != state:
            raise _BootstrapDegradation("pyright_executable_digest_mismatch")
        actual = self._digest()
        self._verify_digest(actual)

    def _verify_digest(self, actual: str) -> None:
        descriptor = self._descriptor
        state = self._state
        if descriptor is None or state is None:
            raise RuntimeError("Pyright launch server guard is not open")
        _require_startup_deadline(self._deadline)
        after = os.fstat(descriptor)
        current = _path_identity(self._path)
        if (
            actual != self._expected_sha256
            or _launch_file_state(after) != state
            or _launch_file_state(current) != state
        ):
            raise _BootstrapDegradation("pyright_executable_digest_mismatch")
        _require_startup_deadline(self._deadline)

    def close(self) -> None:
        snapshot = self._snapshot
        self._snapshot = None
        snapshot_path = self._snapshot_path
        self._snapshot_path = None
        launch_descriptor = self._launch_descriptor
        self._launch_descriptor = None
        descriptor = self._descriptor
        self._descriptor = None
        first_error: BaseException | None = None
        if snapshot is not None:
            try:
                snapshot.close()
            except BaseException as error:
                first_error = error
        if snapshot_path is not None:
            try:
                os.unlink(snapshot_path)
            except FileNotFoundError:
                pass
            except BaseException as error:
                if first_error is None:
                    first_error = error
        if launch_descriptor is not None:
            try:
                os.close(launch_descriptor)
            except BaseException as error:
                if first_error is None:
                    first_error = error
        if descriptor is not None:
            try:
                os.close(descriptor)
            except BaseException as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error

    def __exit__(self, error_type: object, *_error: object) -> None:
        try:
            if error_type is None:
                self.verify()
        finally:
            self.close()


class PyrightSession:
    """Own one repository-scoped Pyright protocol lifecycle."""

    def __init__(
        self,
        repository: RepositoryScope,
        identity: PyrightIdentity,
        *,
        state_root: Path,
    ) -> None:
        if not isinstance(repository, RepositoryScope):
            raise TypeError("repository must be a RepositoryScope")
        if not isinstance(identity, PyrightIdentity):
            raise TypeError("identity must be a PyrightIdentity")
        if not isinstance(state_root, Path):
            raise TypeError("state_root must be a Path")
        self._repository = repository
        self._identity = identity
        self._state_root = state_root
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._document_lock = threading.Lock()
        self._wire_condition = threading.Condition(threading.Lock())
        self._readiness = "not_ready"
        self._readiness_evidence: tuple[str, ...] = ()
        self._position_encoding = None
        self._degradation_codes = identity.degradation_codes
        self._capabilities: dict[str, bool] = {}
        self._active_operations = 0
        self._last_used_monotonic = time.monotonic()
        self._process: LspProcess | None = None
        self._documents: dict[str, OpenDocument] = {}
        self._document_bytes = 0
        self._readiness_target_uri: str | None = None
        self._generation_nonce: str | None = None
        self._ready_uri_generations: dict[str, str] = {}
        self._diagnostics: dict[str, _DiagnosticSnapshot] = {}
        self._diagnostic_bytes = 0
        self._progress_events: list[tuple[object, ...]] = []
        self._progress_bytes = 0
        self._wire_generation: str | None = None
        self._wire_opened: set[tuple[str, str, int]] = set()
        self._wire_failed: set[tuple[str, str, int]] = set()
        self._wire_sending: set[tuple[str, str, int]] = set()
        self._starting = False
        self._startup_attempted = False
        self._startup_cleanup_error: StartupCleanupError | None = None
        self._startup_process: LspProcess | None = None
        self._startup_atexit_registered = False
        self._closing = False
        self._closed = False

    @property
    def readiness(self) -> str:
        with self._lock:
            self._reconcile_process_state_locked()
            return self._readiness

    @property
    def readiness_evidence(self) -> tuple[str, ...]:
        with self._lock:
            self._reconcile_process_state_locked()
            return self._readiness_evidence

    @property
    def position_encoding(self) -> PositionEncoding | None:
        with self._lock:
            self._reconcile_process_state_locked()
            return self._position_encoding

    @property
    def degradation_codes(self) -> tuple[str, ...]:
        with self._lock:
            return self._degradation_codes

    @property
    def capabilities(self) -> Mapping[str, bool]:
        with self._lock:
            self._reconcile_process_state_locked()
            return MappingProxyType(dict(self._capabilities))

    @property
    def active_operations(self) -> int:
        with self._lock:
            return self._active_operations

    @property
    def last_used_monotonic(self) -> float:
        with self._lock:
            return self._last_used_monotonic

    @property
    def progress_events(self) -> tuple[tuple[object, ...], ...]:
        with self._lock:
            return tuple(self._progress_events)

    def _sync_startup_atexit_locked(self) -> None:
        retained = (
            self._startup_cleanup_error is not None
            or self._startup_process is not None
        )
        if retained and not self._startup_atexit_registered:
            atexit.register(self._atexit_cleanup)
            self._startup_atexit_registered = True
        elif not retained and self._startup_atexit_registered:
            atexit.unregister(self._atexit_cleanup)
            self._startup_atexit_registered = False

    def _retain_startup_cleanup_locked(self, error: StartupCleanupError) -> None:
        self._startup_cleanup_error = error
        self._sync_startup_atexit_locked()
        error.transfer_cleanup_ownership()

    def _atexit_cleanup(self) -> None:
        try:
            self.close(deadline=time.monotonic() + _OWNER_CLEANUP_SECONDS)
        except BaseException:
            pass

    @contextmanager
    def _operation(self) -> Iterator[None]:
        now = time.monotonic()
        with self._lock:
            self._active_operations += 1
            self._last_used_monotonic = now
        try:
            yield
        finally:
            with self._lock:
                self._active_operations -= 1
                self._last_used_monotonic = time.monotonic()
                self._condition.notify_all()

    def _configuration(self, params: object) -> object:
        settings = thaw_pyright_profile_value(PYRIGHT_CONFIGURATION)
        assert isinstance(settings, dict)
        if not isinstance(params, dict) or set(params) - {"items"}:
            return []
        items = params.get("items")
        if not isinstance(items, list) or len(items) > _MAX_CONFIGURATION_ITEMS:
            return []
        results: list[object] = []
        for item in items:
            if not isinstance(item, dict) or set(item) - {"scopeUri", "section"}:
                results.append(None)
                continue
            scope_uri = item.get("scopeUri")
            if scope_uri is not None and (
                not isinstance(scope_uri, str)
                or len(scope_uri.encode("utf-8", errors="strict")) > 16 * 1024
            ):
                results.append(None)
                continue
            section = item.get("section")
            if section is None:
                results.append(thaw_pyright_profile_value(PYRIGHT_CONFIGURATION))
                continue
            if not isinstance(section, str):
                results.append(None)
                continue
            try:
                section_size = len(section.encode("utf-8", errors="strict"))
            except UnicodeEncodeError:
                results.append(None)
                continue
            if not section or section_size > _MAX_CONFIGURATION_SECTION_BYTES:
                results.append(None)
                continue
            current: object = settings
            for component in section.split("."):
                if not component or not isinstance(current, dict):
                    current = None
                    break
                current = current.get(component)
            results.append(current)
        return results

    @staticmethod
    def _benign_server_request(params: object) -> None:
        if not isinstance(params, dict):
            raise ValueError("server request params must be an object")
        if len(params) > 16:
            raise ValueError("server request params exceed their bound")
        return None

    @staticmethod
    def _progress_event_bytes(event: tuple[object, ...]) -> int:
        return _PROGRESS_EVENT_BASE_BYTES + sum(
            len(value.encode("utf-8", errors="strict"))
            for value in event
            if isinstance(value, str)
        )

    def _retain_progress(self, event: tuple[object, ...]) -> None:
        event_bytes = self._progress_event_bytes(event)
        if event_bytes > _MAX_PROGRESS_BYTES:
            return
        with self._lock:
            self._progress_events.append(event)
            self._progress_bytes += event_bytes
            while (
                len(self._progress_events) > _MAX_PROGRESS_EVENTS
                or self._progress_bytes > _MAX_PROGRESS_BYTES
            ):
                removed = self._progress_events.pop(0)
                self._progress_bytes -= self._progress_event_bytes(removed)

    def _begin_wire_generation(self, generation_nonce: str) -> None:
        with self._wire_condition:
            self._wire_generation = generation_nonce
            self._wire_opened.clear()
            self._wire_failed.clear()
            self._wire_condition.notify_all()

    def _discard_wire_generation(self, generation_nonce: str) -> None:
        with self._wire_condition:
            if self._wire_generation == generation_nonce:
                self._wire_generation = None
                self._wire_opened.clear()
                self._wire_failed.clear()
            self._wire_condition.notify_all()

    def _clear_wire_state(self) -> None:
        with self._wire_condition:
            self._wire_generation = None
            self._wire_opened.clear()
            self._wire_failed.clear()
            self._wire_condition.notify_all()

    def _wire_document_opened(
        self,
        document: OpenDocument,
        generation_nonce: str | None,
    ) -> bool:
        if generation_nonce is None:
            return False
        key = (generation_nonce, document.source.uri, document.version)
        with self._wire_condition:
            return self._wire_generation == generation_nonce and key in self._wire_opened

    def _send_did_open_once(
        self,
        document: OpenDocument,
        generation_nonce: str,
        *,
        deadline: float,
        notify: Callable[[], bool | None],
    ) -> bool:
        key = (generation_nonce, document.source.uri, document.version)
        with self._wire_condition:
            while key in self._wire_sending:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self._wire_condition.wait(remaining):
                    raise TimeoutError("Pyright didOpen send gate deadline expired")
            if self._wire_generation != generation_nonce:
                return False
            if key in self._wire_opened:
                return True
            if key in self._wire_failed:
                return False
            self._wire_sending.add(key)

        sent = False
        try:
            sent = notify() is not False
        except BaseException:
            with self._wire_condition:
                if self._wire_generation == generation_nonce:
                    self._wire_failed.add(key)
            raise
        finally:
            with self._wire_condition:
                self._wire_sending.remove(key)
                retained = sent and self._wire_generation == generation_nonce
                if retained:
                    self._wire_opened.add(key)
                self._wire_condition.notify_all()
        return retained

    @staticmethod
    def _did_open_params(document: OpenDocument) -> dict[str, object]:
        return {
            "textDocument": {
                "uri": document.source.uri,
                "languageId": "python",
                "version": document.version,
                "text": document.content.decode("utf-8", errors="strict"),
            }
        }

    def _send_protocol_did_open(
        self,
        document: OpenDocument,
        protocol: LspProtocol,
        generation_nonce: str,
        *,
        deadline: float,
    ) -> bool:
        params = self._did_open_params(document)
        return self._send_did_open_once(
            document,
            generation_nonce,
            deadline=deadline,
            notify=lambda: protocol.notify(
                "textDocument/didOpen",
                params,
                deadline=deadline,
            ),
        )

    def _send_process_did_open(
        self,
        document: OpenDocument,
        process: LspProcess,
        *,
        deadline: float,
    ) -> bool:
        attempted: set[str] = set()
        while True:
            with self._lock:
                generation_nonce = self._generation_nonce
            if generation_nonce is None or generation_nonce in attempted:
                return False
            attempted.add(generation_nonce)
            params = self._did_open_params(document)
            if self._send_did_open_once(
                document,
                generation_nonce,
                deadline=deadline,
                notify=lambda: process.notify_generation(
                    "textDocument/didOpen",
                    params,
                    generation_nonce=generation_nonce,
                    deadline=deadline,
                ),
            ):
                return True
            with self._lock:
                if self._generation_nonce == generation_nonce:
                    return False

    def _progress(self, method: str, params: object) -> None:
        if method == "$/progress":
            if not isinstance(params, dict) or set(params) != {"token", "value"}:
                return
            token = params["token"]
            if isinstance(token, str):
                token = _bounded_text(token, 256)
            elif isinstance(token, bool) or not isinstance(token, int):
                return
            if token is None:
                return
            value = params["value"]
            if not isinstance(value, dict):
                return
            kind = value.get("kind")
            if kind not in {"begin", "report", "end"}:
                return
            text_value = value.get("title") if kind == "begin" else value.get("message")
            if text_value is None:
                text = ""
            else:
                text = _bounded_text(text_value, _MAX_PROGRESS_TEXT_BYTES)
                if text is None:
                    return
            self._retain_progress((method, token, kind, text))
            return
        if method in {"pyright/beginProgress", "pyright/endProgress"}:
            if params is None or params == {}:
                self._retain_progress((method,))
            return
        if method == "pyright/reportProgress":
            value = params.get("message") if isinstance(params, dict) else params
            text = _bounded_text(value, _MAX_PROGRESS_TEXT_BYTES)
            if text is not None:
                self._retain_progress((method, text))

    def _parse_diagnostic(
        self,
        value: object,
        uri: str,
    ) -> tuple[LspDiagnostic | None, bool]:
        if not isinstance(value, dict):
            return None, True
        range_ = _lsp_range(value.get("range"))
        message = _bounded_text(value.get("message"), _MAX_DIAGNOSTIC_TEXT_BYTES)
        if range_ is None or message is None:
            return None, True
        severity_value = value.get("severity")
        if severity_value is None:
            severity = None
        elif (
            isinstance(severity_value, bool)
            or not isinstance(severity_value, int)
            or severity_value not in {1, 2, 3, 4}
        ):
            return None, True
        else:
            severity = severity_value
        code_value = value.get("code")
        if code_value is None:
            code = None
        elif isinstance(code_value, str):
            code = _bounded_text(code_value, 4096)
            if code is None:
                return None, True
        elif (
            isinstance(code_value, bool)
            or not isinstance(code_value, int)
            or not -(2**31) <= code_value <= 2**31 - 1
        ):
            return None, True
        else:
            code = str(code_value)

        related: list[tuple[LspLocation, str | None]] = []
        partial = False
        related_value = value.get("relatedInformation")
        if related_value is not None:
            if not isinstance(related_value, list):
                partial = True
            else:
                if len(related_value) > MAX_LOCATIONS:
                    partial = True
                for relation in related_value[:MAX_LOCATIONS]:
                    if not isinstance(relation, dict):
                        partial = True
                        continue
                    location = self._normalize_location(relation.get("location"))
                    related_message_value = relation.get("message")
                    related_message = (
                        None
                        if related_message_value is None
                        else _bounded_text(
                            related_message_value,
                            _MAX_DIAGNOSTIC_TEXT_BYTES,
                        )
                    )
                    if location is None or (
                        related_message_value is not None and related_message is None
                    ):
                        partial = True
                        continue
                    related.append((location, related_message))
        return (
            LspDiagnostic(
                uri,
                range_,
                severity,
                code,
                message,
                tuple(related),
            ),
            partial,
        )

    @staticmethod
    def _diagnostic_retained_bytes(diagnostic: LspDiagnostic) -> int:
        size = (
            _DIAGNOSTIC_BASE_BYTES
            + len(diagnostic.uri.encode("utf-8", errors="strict"))
            + len(diagnostic.message.encode("utf-8", errors="strict"))
        )
        if diagnostic.code is not None:
            size += len(diagnostic.code.encode("utf-8", errors="strict"))
        for location, message in diagnostic.related:
            size += _DIAGNOSTIC_RELATED_BASE_BYTES
            size += len(location.uri.encode("utf-8", errors="strict"))
            if message is not None:
                size += len(message.encode("utf-8", errors="strict"))
        return size

    @staticmethod
    def _diagnostic_update_is_stale(
        document: OpenDocument,
        existing: _DiagnosticSnapshot | None,
        version: int | None,
    ) -> bool:
        if version is not None and version < document.version:
            return True
        if existing is None:
            return False
        return (
            version is not None
            and existing.document_version is not None
            and version < existing.document_version
        ) or (version is None and existing.document_version is not None)

    def _publish_diagnostics(self, params: object) -> None:
        if not isinstance(params, dict):
            return
        uri_value = params.get("uri")
        diagnostics_value = params.get("diagnostics")
        if not isinstance(uri_value, str) or not isinstance(diagnostics_value, list):
            return
        source = normalize_provider_uri(self._repository, uri_value)
        if source is None:
            return
        version_value = params.get("version")
        if version_value is None:
            version = None
        else:
            version = _lsp_coordinate(version_value)
            if version is None:
                return
        with self._lock:
            document = self._documents.get(source.uri)
            if document is None:
                return
            existing = self._diagnostics.get(source.uri)
            if self._diagnostic_update_is_stale(document, existing, version):
                return
            if existing is None and len(self._diagnostics) >= _MAX_DIAGNOSTIC_URIS:
                return

        partial = len(diagnostics_value) > MAX_LOCATIONS
        diagnostics: list[LspDiagnostic] = []
        for value in diagnostics_value[:MAX_LOCATIONS]:
            diagnostic, filtered = self._parse_diagnostic(value, source.uri)
            partial = partial or filtered
            if diagnostic is not None:
                diagnostics.append(diagnostic)
        retained_bytes = _DIAGNOSTIC_BASE_BYTES + sum(
            self._diagnostic_retained_bytes(diagnostic) for diagnostic in diagnostics
        )
        if retained_bytes > _MAX_DIAGNOSTIC_BYTES:
            return
        snapshot = _DiagnosticSnapshot(
            tuple(diagnostics),
            version,
            partial,
            retained_bytes,
        )
        with self._lock:
            current_document = self._documents.get(source.uri)
            if current_document is None:
                return
            existing = self._diagnostics.get(source.uri)
            if self._diagnostic_update_is_stale(
                current_document,
                existing,
                version,
            ):
                return
            if existing is None and len(self._diagnostics) >= _MAX_DIAGNOSTIC_URIS:
                return
            previous_bytes = existing.retained_bytes if existing is not None else 0
            aggregate_bytes = self._diagnostic_bytes - previous_bytes + retained_bytes
            if aggregate_bytes > _MAX_DIAGNOSTIC_BYTES:
                return
            self._diagnostics[source.uri] = snapshot
            self._diagnostic_bytes = aggregate_bytes
            self._condition.notify_all()

    def _bootstrap_generation(
        self,
        protocol: LspProtocol,
        _process_id: int,
        _generation_nonce: str,
        deadline: float,
    ) -> ProcessState:
        root = Path(self._repository.checkout_root)
        root_uri = path_to_file_uri(root)
        result = protocol.request(
            "initialize",
            {
                "processId": os.getpid(),
                "clientInfo": {"name": "llm-wiki"},
                "rootUri": root_uri,
                "workspaceFolders": [{"uri": root_uri, "name": root.name}],
                "initializationOptions": thaw_pyright_profile_value(
                    PYRIGHT_INITIALIZATION_OPTIONS
                ),
                "capabilities": thaw_pyright_profile_value(_CLIENT_CAPABILITIES),
            },
            deadline=deadline,
        )
        capabilities, encoding = _parse_server_capabilities(result)
        protocol.notify("initialized", {}, deadline=deadline)
        protocol.notify(
            "workspace/didChangeConfiguration",
            {"settings": thaw_pyright_profile_value(PYRIGHT_CONFIGURATION)},
            deadline=deadline,
        )
        documents: tuple[OpenDocument, ...] = ()
        try:
            self._begin_wire_generation(_generation_nonce)
            with self._lock:
                documents = tuple(self._documents.values())
                self._generation_nonce = _generation_nonce
                self._ready_uri_generations.clear()
                self._diagnostics.clear()
                self._diagnostic_bytes = 0
                self._capabilities = capabilities
                self._position_encoding = encoding
                self._readiness_evidence = (
                    "initialize",
                    "initialized",
                    "configuration",
                )
                self._readiness = "protocol_initialized"
            for document in documents:
                if not self._send_protocol_did_open(
                    document,
                    protocol,
                    _generation_nonce,
                    deadline=deadline,
                ):
                    raise RuntimeError("Pyright generation changed during didOpen")
            if capabilities["document_symbols"]:
                for document in documents:
                    try:
                        symbols = protocol.request(
                            "textDocument/documentSymbol",
                            {"textDocument": {"uri": document.source.uri}},
                            deadline=deadline,
                        )
                    except JsonRpcResponseError:
                        continue
                    if not self._normalize_document_symbols(
                        symbols,
                        document.source.uri,
                    )[1]:
                        with self._lock:
                            if self._generation_nonce == _generation_nonce:
                                self._ready_uri_generations[
                                    document.source.uri
                                ] = _generation_nonce
                with self._lock:
                    self._refresh_readiness_locked(deadline=deadline)
                    ready = self._readiness == "query_ready"
                return (
                    ProcessState.WORKSPACE_READY
                    if ready
                    else ProcessState.PROTOCOL_INITIALIZED
                )
        except BaseException:
            if documents:
                with self._lock:
                    self._readiness = "not_ready"
                    self._readiness_evidence = ()
                    self._ready_uri_generations.clear()
                    self._degradation_codes = tuple(
                        sorted(
                            {
                                *self._degradation_codes,
                                "pyright_restart_bootstrap_failed",
                            }
                        )
                    )
            self._discard_wire_generation(_generation_nonce)
            raise
        return ProcessState.PROTOCOL_INITIALIZED

    def _validated_qualified_paths(self, *, deadline: float) -> tuple[Path, Path]:
        identity = self._identity
        if identity.status != "qualified" or identity.degradation_codes:
            raise ValueError("qualified Pyright identity is internally inconsistent")
        if identity.initialization_options_sha256 != PYRIGHT_INITIALIZATION_OPTIONS_SHA256:
            raise ValueError("Pyright initialization options identity is inconsistent")
        if (
            not isinstance(identity.configuration_sha256, str)
            or _SHA256.fullmatch(identity.configuration_sha256) is None
        ):
            raise ValueError("Pyright configuration identity is invalid")
        if (
            not isinstance(identity.executable_sha256, str)
            or _SHA256.fullmatch(identity.executable_sha256) is None
        ):
            raise ValueError("Pyright executable identity is invalid")
        node = _validated_local_file(
            identity.node_executable,
            "node_executable",
            deadline=deadline,
        )
        server = _validated_local_file(
            identity.server_executable,
            "server_executable",
            deadline=deadline,
        )
        return node, server

    def start(self, *, deadline: float) -> None:
        caller_deadline = _validated_deadline(deadline)
        startup_started = time.monotonic()
        startup_deadline = min(
            caller_deadline,
            startup_started + STARTUP_SECONDS,
        )
        bootstrap_timeout_seconds = startup_deadline - startup_started
        with self._operation():
            retained_cleanup: StartupCleanupError | None = None
            retained_process: LspProcess | None = None
            with self._lock:
                if self._closed or self._closing:
                    raise RuntimeError("Pyright session is closed")
                if bootstrap_timeout_seconds <= 0:
                    self._readiness = "not_ready"
                    self._readiness_evidence = ()
                    self._position_encoding = None
                    self._capabilities = {}
                    self._degradation_codes = tuple(
                        sorted(
                            {
                                *self._degradation_codes,
                                "pyright_startup_timeout",
                            }
                        )
                    )
                    return
                while self._starting:
                    remaining = startup_deadline - time.monotonic()
                    if remaining <= 0 or not self._condition.wait(remaining):
                        raise TimeoutError("Pyright startup did not finish before deadline")
                self._reconcile_process_state_locked()
                if self._process is not None or self._readiness != "not_ready":
                    return
                if not self._identity.qualified:
                    return
                retained_cleanup = self._startup_cleanup_error
                retained_process = self._startup_process
                if (
                    self._startup_attempted
                    and retained_cleanup is None
                    and retained_process is None
                ):
                    return
                self._starting = True
                if retained_cleanup is None and retained_process is None:
                    self._startup_attempted = True

            process: LspProcess | None = None
            try:
                if retained_cleanup is not None:
                    try:
                        retained_cleanup.retry_cleanup(startup_deadline)
                    except (KeyboardInterrupt, SystemExit):
                        raise
                    except (OSError, RuntimeError, TimeoutError):
                        return
                    with self._lock:
                        if self._startup_cleanup_error is retained_cleanup:
                            self._startup_cleanup_error = None
                            self._sync_startup_atexit_locked()

                if retained_process is not None:
                    try:
                        retained_process.close(startup_deadline)
                    except (KeyboardInterrupt, SystemExit):
                        raise
                    except (OSError, RuntimeError, TimeoutError):
                        return
                    with self._lock:
                        if self._startup_process is retained_process:
                            self._startup_process = None
                            self._sync_startup_atexit_locked()

                with self._lock:
                    self._startup_attempted = True

                try:
                    node, server = self._validated_qualified_paths(
                        deadline=startup_deadline
                    )
                    _ensure_lsp_parent(self._state_root, deadline=startup_deadline)
                    owner = lsp_owner_root(self._state_root, secrets.token_hex(16))
                    process = LspProcess.start_configured(
                        (
                            str(node),
                            str(server),
                            "--stdio",
                            f"--cancellationReceive=file:{owner / 'cancellation'}",
                        ),
                        cwd=Path(self._repository.checkout_root),
                        owner_root=owner,
                        deadline=startup_deadline,
                        server_request_handlers={
                            "client/registerCapability": self._benign_server_request,
                            "client/unregisterCapability": self._benign_server_request,
                            "window/workDoneProgress/create": self._benign_server_request,
                            "workspace/configuration": self._configuration,
                        },
                        server_notification_handlers={
                            "$/progress": lambda params: self._progress(
                                "$/progress", params
                            ),
                            "pyright/beginProgress": lambda params: self._progress(
                                "pyright/beginProgress", params
                            ),
                            "pyright/endProgress": lambda params: self._progress(
                                "pyright/endProgress", params
                            ),
                            "pyright/reportProgress": lambda params: self._progress(
                                "pyright/reportProgress", params
                            ),
                            "textDocument/publishDiagnostics": self._publish_diagnostics,
                        },
                        generation_bootstrap=self._bootstrap_generation,
                        bootstrap_timeout_seconds=bootstrap_timeout_seconds,
                        generation_guard=(
                            lambda _generation_nonce, generation_deadline: (
                                _LaunchServerGuard(
                                    server,
                                    self._identity.executable_sha256,
                                    command=(
                                        str(node),
                                        str(server),
                                        "--stdio",
                                        f"--cancellationReceive=file:{owner / 'cancellation'}",
                                    ),
                                    deadline=generation_deadline,
                                )
                            )
                        ),
                    )
                except BaseException as error:
                    interruption = _startup_interruption(error)
                    retained_error: StartupCleanupError | None = None
                    if isinstance(error, StartupCleanupError):
                        try:
                            error.retry_cleanup(
                                min(
                                    startup_deadline,
                                    time.monotonic() + _OWNER_CLEANUP_SECONDS,
                                )
                            )
                        except (KeyboardInterrupt, SystemExit):
                            with self._lock:
                                self._retain_startup_cleanup_locked(error)
                            raise
                        except BaseException:
                            retained_error = error
                    if interruption is not None:
                        with self._lock:
                            if retained_error is not None:
                                self._retain_startup_cleanup_locked(retained_error)
                            else:
                                self._startup_cleanup_error = None
                                self._sync_startup_atexit_locked()
                        if interruption is error:
                            raise
                        raise interruption.with_traceback(
                            interruption.__traceback__
                        ) from error
                    if isinstance(error, (TypeError, ValueError)):
                        with self._lock:
                            self._startup_attempted = False
                        raise
                    if not isinstance(
                        error,
                        (
                            JsonRpcResponseError,
                            OSError,
                            ProtocolViolation,
                            RuntimeError,
                            TimeoutError,
                        ),
                    ):
                        raise
                    code = _startup_code(error)
                    with self._lock:
                        self._process = None
                        if retained_error is not None:
                            self._retain_startup_cleanup_locked(retained_error)
                        else:
                            self._startup_cleanup_error = None
                            self._sync_startup_atexit_locked()
                        self._readiness = "not_ready"
                        self._readiness_evidence = ()
                        self._position_encoding = None
                        self._capabilities = {}
                        self._degradation_codes = tuple(
                            sorted({*self._degradation_codes, code})
                        )
                    return

                with self._lock:
                    self._process = process
                    self._startup_process = None
                    self._sync_startup_atexit_locked()
            except BaseException as error:
                if process is not None:
                    cleanup_deadline = min(
                        startup_deadline,
                        time.monotonic() + _OWNER_CLEANUP_SECONDS,
                    )
                    retained_process_owner: LspProcess | None = None
                    try:
                        process.close(cleanup_deadline)
                    except BaseException:
                        retained_process_owner = process
                    with self._lock:
                        if self._process is process:
                            self._process = None
                        self._startup_process = retained_process_owner
                        self._sync_startup_atexit_locked()
                        self._readiness = "not_ready"
                        self._readiness_evidence = ()
                        self._position_encoding = None
                        self._capabilities = {}
                        self._generation_nonce = None
                        self._ready_uri_generations.clear()
                        self._diagnostics.clear()
                        self._diagnostic_bytes = 0
                        self._clear_wire_state()
                if _startup_interruption(error) is not None:
                    with self._lock:
                        self._startup_attempted = False
                raise
            finally:
                with self._lock:
                    self._starting = False
                    self._condition.notify_all()

    def _document_ready_locked(self, uri: str) -> bool:
        generation = self._generation_nonce
        document = self._documents.get(uri)
        return (
            generation is not None
            and document is not None
            and self._ready_uri_generations.get(uri) == generation
            and self._wire_document_opened(document, generation)
        )

    def _reconcile_process_state_locked(self) -> None:
        process = self._process
        if process is None or process.state not in {
            ProcessState.DEGRADED,
            ProcessState.FAILED,
        }:
            return
        if (
            process.state is ProcessState.DEGRADED
            and self._generation_nonce is not None
            and self._generation_nonce != process.generation_nonce
        ):
            return
        self._readiness = "not_ready"
        self._readiness_evidence = ()
        self._position_encoding = None
        self._capabilities = {}
        self._generation_nonce = None
        self._ready_uri_generations.clear()
        self._diagnostics.clear()
        self._diagnostic_bytes = 0
        self._clear_wire_state()

    def _refresh_readiness_locked(self, *, deadline: float) -> None:
        if self._position_encoding is None:
            self._readiness = "not_ready"
            self._readiness_evidence = ()
            return
        target = self._readiness_target_uri
        process = self._process
        generation = self._generation_nonce
        if target is not None and self._document_ready_locked(target):
            if (
                process is not None
                and generation is not None
                and process.generation_nonce == generation
            ):
                try:
                    promoted = process.promote_workspace_ready(
                        generation_nonce=generation,
                        deadline=deadline,
                    )
                except BaseException:
                    self._ready_uri_generations.pop(target, None)
                    self._readiness = "protocol_initialized"
                    self._readiness_evidence = (
                        "initialize",
                        "initialized",
                        "configuration",
                        "didOpen",
                    )
                    raise
                if not promoted:
                    self._ready_uri_generations.pop(target, None)
                    self._readiness = "protocol_initialized"
                    self._readiness_evidence = (
                        "initialize",
                        "initialized",
                        "configuration",
                        "didOpen",
                    )
                    return
            self._readiness = "query_ready"
            self._readiness_evidence = (
                "initialize",
                "initialized",
                "configuration",
                "didOpen",
                "documentSymbol",
            )
            return
        self._readiness = "protocol_initialized"
        did_open = any(
            self._wire_document_opened(document, generation)
            for document in self._documents.values()
        )
        self._readiness_evidence = (
            "initialize",
            "initialized",
            "configuration",
            *(("didOpen",) if did_open else ()),
        )

    def _mark_protocol_initialized(self, *, did_open: bool, deadline: float) -> None:
        with self._lock:
            if not did_open and self._readiness_target_uri is not None:
                self._ready_uri_generations.pop(self._readiness_target_uri, None)
            self._refresh_readiness_locked(deadline=deadline)

    def _probe_document(
        self,
        document: OpenDocument,
        process: LspProcess,
        *,
        deadline: float,
    ) -> None:
        with self._lock:
            supported = self._capabilities.get("document_symbols", False)
        if not supported:
            self._mark_protocol_initialized(did_open=True, deadline=deadline)
            return
        try:
            result = process.request(
                "textDocument/documentSymbol",
                {"textDocument": {"uri": document.source.uri}},
                deadline=deadline,
            )
        except (JsonRpcResponseError, ProtocolViolation, RuntimeError, TimeoutError):
            with self._lock:
                self._ready_uri_generations.pop(document.source.uri, None)
                self._refresh_readiness_locked(deadline=deadline)
            return
        if self._normalize_document_symbols(result, document.source.uri)[1]:
            with self._lock:
                self._ready_uri_generations.pop(document.source.uri, None)
                self._refresh_readiness_locked(deadline=deadline)
            return
        with self._lock:
            generation = process.generation_nonce
            self._generation_nonce = generation
            self._ready_uri_generations[document.source.uri] = generation
            self._refresh_readiness_locked(deadline=deadline)

    def open_document(self, path: str, *, deadline: float) -> OpenDocument:
        deadline = _validated_deadline(deadline)
        if time.monotonic() >= deadline:
            raise TimeoutError("Pyright document deadline expired")
        with self._operation(), self._document_lock:
            self.start(deadline=deadline)
            source = resolve_repository_source(self._repository, path)
            content = read_stable_bytes(
                source.absolute_path,
                _MAX_DOCUMENT_BYTES,
                label="Pyright source document",
                deadline=deadline,
            )
            content.decode("utf-8", errors="strict")
            digest = hashlib.sha256(content).hexdigest()
            with self._lock:
                current = self._documents.get(source.uri)
                process = self._process
                ready = self._document_ready_locked(source.uri)
            if process is None:
                raise RuntimeError("Pyright session is not protocol initialized")
            if current is not None:
                if (
                    current.source != source
                    or current.content != content
                    or current.source_sha256 != digest
                ):
                    raise RuntimeError("open Pyright document changed without synchronization")
                if not ready:
                    try:
                        did_open = self._send_process_did_open(
                            current,
                            process,
                            deadline=deadline,
                        )
                    except (ProtocolViolation, RuntimeError, TimeoutError):
                        did_open = False
                    self._mark_protocol_initialized(
                        did_open=did_open,
                        deadline=deadline,
                    )
                    if not did_open:
                        return current
                    self._probe_document(current, process, deadline=deadline)
                return current

            document = OpenDocument(source, content, digest, 1)
            did_open = self._did_open_params(document)
            try:
                encode_frame(
                    {
                        "jsonrpc": "2.0",
                        "method": "textDocument/didOpen",
                        "params": did_open,
                    }
                )
            except ProtocolViolation as error:
                raise ValueError("Pyright source document exceeds the LSP frame") from error
            with self._lock:
                if len(self._documents) >= _MAX_OPEN_DOCUMENTS:
                    raise RuntimeError("Pyright open document count limit exceeded")
                document_bytes = self._document_bytes + len(content)
                if document_bytes > _MAX_OPEN_DOCUMENT_BYTES:
                    raise RuntimeError(
                        "Pyright open document source bytes limit exceeded"
                    )
                self._documents[source.uri] = document
                self._document_bytes = document_bytes
                self._readiness_target_uri = source.uri
                self._ready_uri_generations.pop(source.uri, None)
            try:
                sent = self._send_process_did_open(
                    document,
                    process,
                    deadline=deadline,
                )
            except (ProtocolViolation, RuntimeError, TimeoutError):
                self._mark_protocol_initialized(did_open=False, deadline=deadline)
                return document
            self._mark_protocol_initialized(did_open=sent, deadline=deadline)
            if not sent:
                return document
            self._probe_document(document, process, deadline=deadline)
            return document

    def _normalize_location(self, value: object) -> LspLocation | None:
        if not isinstance(value, dict):
            return None
        if "targetUri" in value or "targetSelectionRange" in value:
            uri = value.get("targetUri")
            range_value = value.get("targetSelectionRange")
            if _lsp_range(value.get("targetRange")) is None:
                return None
        else:
            uri = value.get("uri")
            range_value = value.get("range")
        if not isinstance(uri, str):
            return None
        range_ = _lsp_range(range_value)
        if range_ is None:
            return None
        source = normalize_provider_uri(self._repository, uri)
        if source is None:
            return None
        return LspLocation(source.uri, range_)

    def _normalize_locations(
        self,
        result: object,
    ) -> tuple[tuple[LspLocation, ...], bool]:
        if result is None:
            return (), False
        if isinstance(result, dict):
            raw = [result]
        elif isinstance(result, list):
            raw = result
        else:
            return (), True
        partial = len(raw) > MAX_LOCATIONS
        locations: list[LspLocation] = []
        seen: set[tuple[object, ...]] = set()
        for value in raw[:MAX_LOCATIONS]:
            location = self._normalize_location(value)
            if location is None:
                partial = True
                continue
            key = _location_key(location)
            if key in seen:
                continue
            seen.add(key)
            locations.append(location)
        return tuple(locations), partial

    def _location_feature(
        self,
        anchor: SourceAnchor,
        *,
        capability: str,
        method: str,
        deadline: float,
        references: bool = False,
    ) -> ProviderLocations:
        if not isinstance(anchor, SourceAnchor):
            raise TypeError("anchor must be a SourceAnchor")
        deadline = _validated_deadline(deadline)
        if time.monotonic() >= deadline:
            raise TimeoutError("Pyright semantic request deadline expired")
        with self._operation():
            self.start(deadline=deadline)
            with self._lock:
                process = self._process
                encoding = self._position_encoding
                supported = self._capabilities.get(capability, False)
                if process is None or encoding is None:
                    return ProviderLocations((), "not_ready", True)
            if not supported:
                return ProviderLocations((), "unsupported", True)
            document = self.open_document(anchor.path, deadline=deadline)
            with self._lock:
                ready = self._document_ready_locked(document.source.uri)
                encoding = self._position_encoding
                process = self._process
            if not ready or encoding is None or process is None:
                return ProviderLocations((), "not_ready", True)
            source_document = SourceDocument.from_bytes(
                document.source.relative_path,
                document.content,
            )
            position = source_document.to_lsp(anchor, encoding)
            params: dict[str, object] = {
                "textDocument": {"uri": document.source.uri},
                "position": {
                    "line": position.line,
                    "character": position.character,
                },
            }
            if references:
                params["context"] = {"includeDeclaration": True}
            result = process.request(method, params, deadline=deadline)
            with self._lock:
                if not self._document_ready_locked(document.source.uri):
                    return ProviderLocations((), "not_ready", True)
            locations, filtered = self._normalize_locations(result)
            return ProviderLocations(
                locations,
                "provider_reported",
                True if references else filtered,
            )

    def definition(
        self,
        anchor: SourceAnchor,
        *,
        deadline: float,
    ) -> ProviderLocations:
        return self._location_feature(
            anchor,
            capability="definition",
            method="textDocument/definition",
            deadline=deadline,
        )

    def references(
        self,
        anchor: SourceAnchor,
        *,
        deadline: float,
    ) -> ProviderLocations:
        return self._location_feature(
            anchor,
            capability="references",
            method="textDocument/references",
            deadline=deadline,
            references=True,
        )

    def implementations(
        self,
        anchor: SourceAnchor,
        *,
        deadline: float,
    ) -> ProviderLocations:
        return self._location_feature(
            anchor,
            capability="implementations",
            method="textDocument/implementation",
            deadline=deadline,
        )

    def type_definition(
        self,
        anchor: SourceAnchor,
        *,
        deadline: float,
    ) -> ProviderLocations:
        return self._location_feature(
            anchor,
            capability="type_definition",
            method="textDocument/typeDefinition",
            deadline=deadline,
        )

    def _normalize_document_symbols(
        self,
        result: object,
        uri: str,
    ) -> tuple[tuple[LspLocation, ...], bool]:
        if result is None:
            return (), False
        if not isinstance(result, list):
            return (), True
        locations: list[LspLocation] = []
        seen: set[tuple[object, ...]] = set()
        seen_nodes: set[int] = set()
        partial = len(result) > MAX_LOCATIONS
        stack: list[object] = list(reversed(result[:MAX_LOCATIONS]))
        visited = 0
        while stack and visited < MAX_LOCATIONS:
            value = stack.pop()
            visited += 1
            if not isinstance(value, dict):
                partial = True
                continue
            node_id = id(value)
            if node_id in seen_nodes:
                partial = True
                continue
            seen_nodes.add(node_id)
            children = value.get("children")
            if children is not None:
                if isinstance(children, list):
                    remaining = max(0, MAX_LOCATIONS - visited - len(stack))
                    if len(children) > remaining:
                        partial = True
                    stack.extend(reversed(children[:remaining]))
                else:
                    partial = True
            name = _bounded_text(value.get("name"), _MAX_CALL_ITEM_TEXT_BYTES)
            kind = _lsp_coordinate(value.get("kind"))
            if name is None or kind in {None, 0}:
                partial = True
                continue
            detail = value.get("detail")
            if detail is not None and _bounded_text(
                detail,
                _MAX_CALL_ITEM_TEXT_BYTES,
            ) is None:
                partial = True
                continue
            container_name = value.get("containerName")
            if container_name is not None and _bounded_text(
                container_name,
                _MAX_CALL_ITEM_TEXT_BYTES,
            ) is None:
                partial = True
                continue
            deprecated = value.get("deprecated")
            if deprecated is not None and not isinstance(deprecated, bool):
                partial = True
                continue
            tags = value.get("tags")
            if tags is not None and (
                not isinstance(tags, list)
                or len(tags) > 32
                or any(_lsp_coordinate(tag) is None for tag in tags)
            ):
                partial = True
                continue
            if "location" in value:
                location = self._normalize_location(value.get("location"))
            else:
                range_ = _lsp_range(value.get("range"))
                selection = _lsp_range(value.get("selectionRange"))
                if (
                    range_ is None
                    or selection is None
                    or (
                        selection.start.line,
                        selection.start.character,
                    )
                    < (range_.start.line, range_.start.character)
                    or (selection.end.line, selection.end.character)
                    > (range_.end.line, range_.end.character)
                ):
                    location = None
                else:
                    location = LspLocation(uri, selection)
            if location is None:
                partial = True
                continue
            key = _location_key(location)
            if key not in seen:
                seen.add(key)
                locations.append(location)
        if stack:
            partial = True
        return tuple(locations), partial

    def document_symbols(self, path: str, *, deadline: float) -> ProviderLocations:
        deadline = _validated_deadline(deadline)
        with self._operation():
            self.start(deadline=deadline)
            with self._lock:
                process = self._process
                initialized = self._position_encoding is not None
                supported = self._capabilities.get("document_symbols", False)
                if process is None or not initialized:
                    return ProviderLocations((), "not_ready", True)
            if not supported:
                return ProviderLocations((), "unsupported", True)
            document = self.open_document(path, deadline=deadline)
            with self._lock:
                ready = self._document_ready_locked(document.source.uri)
                process = self._process
            if not ready or process is None:
                return ProviderLocations((), "not_ready", True)
            result = process.request(
                "textDocument/documentSymbol",
                {"textDocument": {"uri": document.source.uri}},
                deadline=deadline,
            )
            with self._lock:
                if not self._document_ready_locked(document.source.uri):
                    return ProviderLocations((), "not_ready", True)
            locations, partial = self._normalize_document_symbols(
                result,
                document.source.uri,
            )
            return ProviderLocations(locations, "provider_reported", partial)

    def workspace_symbols(
        self,
        query: str,
        *,
        deadline: float,
    ) -> ProviderLocations:
        if not isinstance(query, str):
            raise TypeError("query must be a string")
        if len(query.encode("utf-8", errors="strict")) > 4096:
            raise ValueError("query exceeds 4096 bytes")
        deadline = _validated_deadline(deadline)
        with self._operation():
            self.start(deadline=deadline)
            with self._lock:
                supported = self._capabilities.get("workspace_symbols", False)
                readiness = self._readiness
                process = self._process
                initialized = self._position_encoding is not None
            if process is None or not initialized:
                return ProviderLocations((), "not_ready", True)
            if not supported:
                return ProviderLocations((), "unsupported", True)
            if readiness != "query_ready":
                return ProviderLocations((), "not_ready", True)
            result = process.request(
                "workspace/symbol",
                {"query": query},
                deadline=deadline,
            )
            if result is None:
                return ProviderLocations((), "provider_reported", False)
            if not isinstance(result, list):
                return ProviderLocations((), "provider_reported", True)
            values: list[object] = []
            partial = len(result) > MAX_LOCATIONS
            for symbol in result[:MAX_LOCATIONS]:
                if not isinstance(symbol, dict) or "location" not in symbol:
                    partial = True
                    continue
                values.append(symbol["location"])
            locations, filtered = self._normalize_locations(values)
            return ProviderLocations(
                locations,
                "provider_reported",
                partial or filtered,
            )

    def hover(self, anchor: SourceAnchor, *, deadline: float) -> ProviderHover:
        if not isinstance(anchor, SourceAnchor):
            raise TypeError("anchor must be a SourceAnchor")
        deadline = _validated_deadline(deadline)
        with self._operation():
            self.start(deadline=deadline)
            with self._lock:
                process = self._process
                encoding = self._position_encoding
                supported = self._capabilities.get("hover", False)
                if process is None or encoding is None:
                    return ProviderHover(None, None, True)
            if not supported:
                return ProviderHover(None, None, True)
            document = self.open_document(anchor.path, deadline=deadline)
            with self._lock:
                ready = self._document_ready_locked(document.source.uri)
                encoding = self._position_encoding
                process = self._process
            if not ready or encoding is None or process is None:
                return ProviderHover(None, None, True)
            source_document = SourceDocument.from_bytes(
                document.source.relative_path,
                document.content,
            )
            position = source_document.to_lsp(anchor, encoding)
            result = process.request(
                "textDocument/hover",
                {
                    "textDocument": {"uri": document.source.uri},
                    "position": {
                        "line": position.line,
                        "character": position.character,
                    },
                },
                deadline=deadline,
            )
            with self._lock:
                if not self._document_ready_locked(document.source.uri):
                    return ProviderHover(None, None, True)
            if result is None:
                return ProviderHover(None, None, False)
            if not isinstance(result, dict) or "contents" not in result:
                return ProviderHover(None, None, True)
            contents, partial = _hover_contents(result["contents"])
            range_ = None
            if "range" in result:
                range_ = _lsp_range(result["range"])
                if range_ is None:
                    partial = True
            return ProviderHover(contents, range_, partial)

    def _sanitize_call_item(self, value: object) -> dict[str, object] | None:
        if not isinstance(value, dict):
            return None
        name = _bounded_text(value.get("name"), _MAX_CALL_ITEM_TEXT_BYTES)
        kind = _lsp_coordinate(value.get("kind"))
        uri = value.get("uri")
        range_ = _lsp_range(value.get("range"))
        selection = _lsp_range(value.get("selectionRange"))
        if (
            not name
            or kind is None
            or kind == 0
            or not isinstance(uri, str)
            or range_ is None
            or selection is None
        ):
            return None
        if (
            (selection.start.line, selection.start.character)
            < (range_.start.line, range_.start.character)
            or (selection.end.line, selection.end.character)
            > (range_.end.line, range_.end.character)
        ):
            return None
        source = normalize_provider_uri(self._repository, uri)
        if source is None:
            return None
        item: dict[str, object] = {
            "name": name,
            "kind": kind,
            "uri": source.uri,
            "range": _range_json(range_),
            "selectionRange": _range_json(selection),
        }
        detail = value.get("detail")
        if detail is not None:
            detail = _bounded_text(detail, _MAX_CALL_ITEM_TEXT_BYTES)
            if detail is None:
                return None
            item["detail"] = detail
        tags = value.get("tags")
        if tags is not None:
            if (
                not isinstance(tags, list)
                or len(tags) > 32
                or any(_lsp_coordinate(tag) is None for tag in tags)
            ):
                return None
            item["tags"] = list(tags)
        if "data" in value:
            item["data"] = value["data"]
        return item

    def _call_location(self, value: object) -> LspLocation | None:
        if not isinstance(value, dict) or not isinstance(value.get("uri"), str):
            return None
        source = normalize_provider_uri(self._repository, value["uri"])
        if source is None:
            return None
        range_ = _lsp_range(value.get("selectionRange"))
        if range_ is None:
            range_ = _lsp_range(value.get("range"))
        if range_ is None:
            return None
        return LspLocation(source.uri, range_)

    def _calls(
        self,
        anchor: SourceAnchor,
        *,
        direction: str,
        deadline: float,
    ) -> ProviderCalls:
        if not isinstance(anchor, SourceAnchor):
            raise TypeError("anchor must be a SourceAnchor")
        deadline = _validated_deadline(deadline)
        with self._operation():
            self.start(deadline=deadline)
            with self._lock:
                process = self._process
                encoding = self._position_encoding
                supported = self._capabilities.get("calls", False)
                if process is None or encoding is None:
                    return ProviderCalls(direction, (), "not_ready", True)
            if not supported:
                return ProviderCalls(direction, (), "unsupported", True)
            document = self.open_document(anchor.path, deadline=deadline)
            with self._lock:
                ready = self._document_ready_locked(document.source.uri)
                encoding = self._position_encoding
                process = self._process
            if not ready or encoding is None or process is None:
                return ProviderCalls(direction, (), "not_ready", True)
            source_document = SourceDocument.from_bytes(
                document.source.relative_path,
                document.content,
            )
            position = source_document.to_lsp(anchor, encoding)
            prepared = process.request(
                "textDocument/prepareCallHierarchy",
                {
                    "textDocument": {"uri": document.source.uri},
                    "position": {
                        "line": position.line,
                        "character": position.character,
                    },
                },
                deadline=deadline,
            )
            with self._lock:
                if not self._document_ready_locked(document.source.uri):
                    return ProviderCalls(direction, (), "not_ready", True)
            if prepared is None:
                return ProviderCalls(direction, (), "provider_reported", True)
            if not isinstance(prepared, list):
                return ProviderCalls(direction, (), "provider_reported", True)
            items = [
                item
                for value in prepared[:_MAX_PREPARED_CALL_ITEMS]
                if (item := self._sanitize_call_item(value)) is not None
            ]
            method = (
                "callHierarchy/incomingCalls"
                if direction == "incoming"
                else "callHierarchy/outgoingCalls"
            )
            result_key = "from" if direction == "incoming" else "to"
            locations: list[LspLocation] = []
            seen: set[tuple[object, ...]] = set()
            for item in items:
                result = process.request(
                    method,
                    {"item": item},
                    deadline=deadline,
                )
                if not isinstance(result, list):
                    continue
                for call in result[:MAX_LOCATIONS]:
                    if not isinstance(call, dict):
                        continue
                    location = self._call_location(call.get(result_key))
                    if location is None:
                        continue
                    key = _location_key(location)
                    if key not in seen and len(locations) < MAX_LOCATIONS:
                        seen.add(key)
                        locations.append(location)
            with self._lock:
                if not self._document_ready_locked(document.source.uri):
                    return ProviderCalls(direction, (), "not_ready", True)
            return ProviderCalls(
                direction,
                tuple(locations),
                "provider_reported",
                True,
            )

    def incoming_calls(
        self,
        anchor: SourceAnchor,
        *,
        deadline: float,
    ) -> ProviderCalls:
        return self._calls(anchor, direction="incoming", deadline=deadline)

    def outgoing_calls(
        self,
        anchor: SourceAnchor,
        *,
        deadline: float,
    ) -> ProviderCalls:
        return self._calls(anchor, direction="outgoing", deadline=deadline)

    def diagnostics(self, path: str, *, deadline: float) -> ProviderDiagnostics:
        deadline = _validated_deadline(deadline)
        with self._operation():
            self.start(deadline=deadline)
            with self._lock:
                initialized = self._process is not None and self._position_encoding is not None
            if not initialized:
                return ProviderDiagnostics((), None, True)
            document = self.open_document(path, deadline=deadline)
            with self._lock:
                if not self._document_ready_locked(document.source.uri):
                    return ProviderDiagnostics((), None, True)
                while True:
                    if not self._document_ready_locked(document.source.uri):
                        return ProviderDiagnostics((), None, True)
                    snapshot = self._diagnostics.get(document.source.uri)
                    if (
                        snapshot is not None
                        and snapshot.document_version == document.version
                    ):
                        return ProviderDiagnostics(
                            snapshot.diagnostics,
                            snapshot.document_version,
                            snapshot.partial,
                        )
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        if snapshot is not None and snapshot.document_version is None:
                            return ProviderDiagnostics(
                                snapshot.diagnostics,
                                None,
                                True,
                            )
                        return ProviderDiagnostics((), None, True)
                    self._condition.wait(remaining)

    def close(self, *, deadline: float) -> None:
        deadline = _validated_deadline(deadline)
        with self._lock:
            while self._starting:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self._condition.wait(remaining):
                    raise TimeoutError("Pyright startup did not finish before close")
            cleanup_error = self._startup_cleanup_error
            startup_process = self._startup_process
            if (
                self._closed
                and self._process is None
                and cleanup_error is None
                and startup_process is None
            ):
                return
            process = self._process
            self._closing = True
        try:
            if cleanup_error is not None:
                cleanup_error.retry_cleanup(deadline)
            if startup_process is not None:
                startup_process.close(deadline)
            if process is not None:
                process.close(deadline)
        except BaseException:
            with self._lock:
                self._closing = False
                self._condition.notify_all()
            raise
        with self._lock:
            if self._startup_cleanup_error is cleanup_error:
                self._startup_cleanup_error = None
            if self._startup_process is startup_process:
                self._startup_process = None
            self._sync_startup_atexit_locked()
            if self._process is process:
                self._process = None
            self._readiness = "not_ready"
            self._readiness_evidence = ()
            self._position_encoding = None
            self._capabilities = {}
            self._documents.clear()
            self._document_bytes = 0
            self._readiness_target_uri = None
            self._generation_nonce = None
            self._ready_uri_generations.clear()
            self._diagnostics.clear()
            self._diagnostic_bytes = 0
            self._progress_events.clear()
            self._progress_bytes = 0
            self._clear_wire_state()
            self._closing = False
            self._closed = True
            self._condition.notify_all()
