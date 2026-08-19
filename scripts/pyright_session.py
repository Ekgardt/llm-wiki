"""Bounded, capability-honest Pyright language-server session."""

import atexit
import dataclasses
import hashlib
import math
import os
import queue
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
from workspace_revision import (
    WorkspaceDelta,
    WorkspaceRevision,
    diff_workspace_revisions,
)

STARTUP_SECONDS = 60.0
MAX_LSP_PROCESSES = 4

_OWNER_CLEANUP_SECONDS = 2.0
_LOCK_POLL_SECONDS = 0.01
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
_WINDOWS_STAT_CREATION_TIME = os.name == "nt"
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
        if isinstance(current, PermissionError):
            cause = current.__cause__
            if (
                cause is not None
                and cause.__class__.__name__ == "TimeoutExpired"
            ):
                return "pyright_startup_timeout"
            if "deadline expired" in str(current):
                return "pyright_startup_timeout"
        current = current.__cause__
    return "pyright_startup_failed"


def _startup_interruption(
    error: BaseException,
) -> KeyboardInterrupt | SystemExit | None:
    pending: list[BaseException] = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, (KeyboardInterrupt, SystemExit)):
            return current
        if current.__context__ is not None:
            pending.append(current.__context__)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
    return None


def _exception_reaches(error: BaseException | None, target: BaseException) -> bool:
    pending = [error] if error is not None else []
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current is target:
            return True
        if id(current) in seen:
            continue
        seen.add(id(current))
        if current.__context__ is not None:
            pending.append(current.__context__)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
    return False


def _raise_collected_errors(
    errors: tuple[BaseException, ...] | list[BaseException],
    *,
    prior_error: BaseException | None = None,
) -> None:
    ordered = ((prior_error,) if prior_error is not None else ()) + tuple(errors)
    source: BaseException | None = None
    interruption: KeyboardInterrupt | SystemExit | None = None
    for error in ordered:
        interruption = _startup_interruption(error)
        if interruption is not None:
            source = error
            break
    if interruption is not None:
        secondary = next(
            (
                error
                for error in ordered
                if error is not source and error is not interruption
            ),
            None,
        )
        if secondary is None and source is not interruption:
            secondary = source
        if secondary is not None:
            if _exception_reaches(secondary.__cause__, interruption):
                secondary.__cause__ = None
            if _exception_reaches(secondary.__context__, interruption):
                secondary.__context__ = None
            if _exception_reaches(secondary, interruption):
                secondary = None
        try:
            if secondary is not None:
                raise interruption.with_traceback(
                    interruption.__traceback__
                ) from secondary
            raise interruption.with_traceback(interruption.__traceback__)
        except (KeyboardInterrupt, SystemExit) as raised:
            if raised is not interruption:
                raise
            if _exception_reaches(interruption.__cause__, interruption):
                interruption.__cause__ = None
            if _exception_reaches(interruption.__context__, interruption):
                interruption.__context__ = None
            if interruption.__context__ is interruption.__cause__:
                interruption.__context__ = None
            raise
    if errors:
        if prior_error is not None:
            raise errors[0] from prior_error
        raise errors[0]
    if prior_error is not None:
        raise prior_error


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
        0 if _WINDOWS_STAT_CREATION_TIME else info.st_ctime_ns,
    )


def _diagnostic_severity(value: object) -> tuple[int | None, bool]:
    """The severity, and whether the field was readable at all."""
    if value is None:
        return None, True
    if isinstance(value, bool) or not isinstance(value, int):
        return None, False
    if value not in {1, 2, 3, 4}:
        return None, False
    return value, True


def _diagnostic_int_code(value: object) -> tuple[str | None, bool]:
    """A numeric diagnostic code rendered as text, when it fits in 32 bits."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None, False
    if not -(2**31) <= value <= 2**31 - 1:
        return None, False
    return str(value), True


def _diagnostic_code(value: object) -> tuple[str | None, bool]:
    """The code as text, and whether the field was readable at all."""
    if value is None:
        return None, True
    if isinstance(value, str):
        code = _bounded_text(value, 4096)
        return code, code is not None
    return _diagnostic_int_code(value)


@dataclass
class _SymbolWalk:
    """A bounded depth-first pass over a document-symbol tree."""

    stack: list[object]
    partial: bool
    visited: int = 0
    seen_nodes: set[int] = dataclasses.field(default_factory=set)
    seen_keys: set[tuple[object, ...]] = dataclasses.field(default_factory=set)
    locations: list[LspLocation] = dataclasses.field(default_factory=list)

    def drop(self) -> None:
        """Record that something in the tree could not be used."""
        self.partial = True

    def add(self, location: LspLocation) -> None:
        key = _location_key(location)
        if key in self.seen_keys:
            return
        self.seen_keys.add(key)
        self.locations.append(location)

    def first_visit(self, value: object) -> bool:
        """True the first time this exact node is seen."""
        node_id = id(value)
        if node_id in self.seen_nodes:
            return False
        self.seen_nodes.add(node_id)
        return True


def _push_symbol_children(walk: _SymbolWalk, value: Mapping[str, object]) -> None:
    """Queue the node's children, dropping whatever exceeds the walk's bound."""
    children = value.get("children")
    if children is None:
        return
    if not isinstance(children, list):
        walk.drop()
        return
    remaining = max(0, MAX_LOCATIONS - walk.visited - len(walk.stack))
    if len(children) > remaining:
        walk.drop()
    walk.stack.extend(reversed(children[:remaining]))


def _bounded_optional_text(value: object) -> bool:
    """An absent field is fine; a present one has to fit the text bound."""
    if value is None:
        return True
    return _bounded_text(value, _MAX_CALL_ITEM_TEXT_BYTES) is not None


def _symbol_tags_ok(tags: object) -> bool:
    if not isinstance(tags, list) or len(tags) > 32:
        return False
    return all(_lsp_coordinate(tag) is not None for tag in tags)


def _symbol_flags_ok(value: Mapping[str, object]) -> bool:
    """The deprecated flag and the tag list, when present, are well formed."""
    deprecated = value.get("deprecated")
    if deprecated is not None and not isinstance(deprecated, bool):
        return False
    tags = value.get("tags")
    if tags is None:
        return True
    return _symbol_tags_ok(tags)


def _symbol_fields_ok(value: Mapping[str, object]) -> bool:
    """Every scalar field of the symbol is present and within bounds."""
    name = _bounded_text(value.get("name"), _MAX_CALL_ITEM_TEXT_BYTES)
    kind = _lsp_coordinate(value.get("kind"))
    if name is None or kind in {None, 0}:
        return False
    if not _bounded_optional_text(value.get("detail")):
        return False
    if not _bounded_optional_text(value.get("containerName")):
        return False
    return _symbol_flags_ok(value)


def _range_contains(outer: LspRange, inner: LspRange) -> bool:
    """The inner range starts no earlier and ends no later than the outer one."""
    starts_inside = (inner.start.line, inner.start.character) >= (
        outer.start.line,
        outer.start.character,
    )
    ends_inside = (inner.end.line, inner.end.character) <= (
        outer.end.line,
        outer.end.character,
    )
    return starts_inside and ends_inside


_CALL_METHODS = MappingProxyType(
    {
        "incoming": "callHierarchy/incomingCalls",
        "outgoing": "callHierarchy/outgoingCalls",
    }
)
_CALL_RESULT_KEYS = MappingProxyType({"incoming": "from", "outgoing": "to"})


@dataclass
class _BoundedLocations:
    """Distinct locations, up to the shared MAX_LOCATIONS bound."""

    locations: list[LspLocation] = dataclasses.field(default_factory=list)
    seen: set[tuple[object, ...]] = dataclasses.field(default_factory=set)

    def add(self, location: LspLocation) -> None:
        key = _location_key(location)
        if key in self.seen or len(self.locations) >= MAX_LOCATIONS:
            return
        self.seen.add(key)
        self.locations.append(location)


@dataclass(frozen=True)
class _CallQuery:
    """The process, generation, document and encoding one query is bound to."""

    process: LspProcess
    generation: str
    document: OpenDocument
    epoch: int
    encoding: PositionEncoding


@dataclass(frozen=True)
class _SyncPlan:
    """Everything one synchronize pass settled before touching the server."""

    process: LspProcess
    generation: str
    prior: WorkspaceRevision | None
    revision: WorkspaceRevision
    deadline: float
    documents_snapshot: dict[str, OpenDocument]
    next_documents: dict[str, OpenDocument]
    projected_document_bytes: int
    closed_documents: list[OpenDocument]
    changed_replacements: list[tuple[OpenDocument, OpenDocument, dict[str, object]]]
    close_notifications: list[tuple[OpenDocument, dict[str, object]]]
    watched_params: dict[str, object] | None


@dataclass
class _WireAttempt:
    """Whether any notification of this pass has already reached the wire."""

    started: bool = False


def _without_wire_uri(
    keys: set[tuple[object, ...]], generation: str, uri: str
) -> set[tuple[object, ...]]:
    """The wire keys with every entry for this generation's URI removed."""
    return {key for key in keys if not (key[0] == generation and key[1] == uri)}


def _drop_superseded_diagnostics(
    diagnostics: dict[str, _DiagnosticSnapshot],
    document: OpenDocument,
    replacement: OpenDocument,
) -> None:
    """A snapshot older than the replacement no longer describes the document."""
    snapshot = diagnostics.get(document.source.uri)
    if snapshot is None or snapshot.document_version is None:
        return
    if snapshot.document_version < replacement.version:
        diagnostics.pop(document.source.uri, None)


@dataclass(frozen=True)
class _SyncSnapshot:
    """The session state one synchronize pass plans against."""

    process: LspProcess
    generation: str
    prior: WorkspaceRevision | None
    documents_snapshot: dict[str, OpenDocument]
    open_by_path: dict[str, OpenDocument]


def _first_sync_delta(
    open_by_path: Mapping[str, OpenDocument],
    entries: Mapping[str, object],
) -> WorkspaceDelta:
    """Without a prior revision, open documents are compared to this one."""
    changed = tuple(
        sorted(
            path
            for path, document in open_by_path.items()
            if _entry_supersedes(entries.get(path), document)
        )
    )
    deleted = tuple(
        sorted(path for path in open_by_path if _entry_missing(entries.get(path)))
    )
    return WorkspaceDelta((), changed, (), deleted, False)


def _entry_missing(entry: object) -> bool:
    """The revision does not describe a usable file at this path."""
    return entry is None or getattr(entry, "sha256", None) is None


def _entry_supersedes(entry: object, document: OpenDocument) -> bool:
    """The revision describes different bytes than the open document holds."""
    if _entry_missing(entry):
        return False
    return entry.sha256 != document.source_sha256


def _validated_entry_size(entry: object) -> int:
    """The entry's byte count, refused when it is not a sane size."""
    size = entry.size
    if isinstance(size, bool) or not isinstance(size, int):
        raise RuntimeError("Pyright source document byte limit exceeded")
    if size < 0 or size > _MAX_DOCUMENT_BYTES:
        raise RuntimeError("Pyright source document byte limit exceeded")
    return size


def _retained_document_bytes(entry: object, document: OpenDocument) -> int:
    """The revision's size for a document that must stay open."""
    if _entry_missing(entry):
        raise RuntimeError(
            "Pyright open document is absent from the workspace revision"
        )
    size = _validated_entry_size(entry)
    if entry.sha256 == document.source_sha256 and size != len(document.content):
        raise RuntimeError("Pyright source document size differs from the revision")
    return size


def _verified_document_content(
    entry: object, document: OpenDocument, *, deadline: float
) -> bytes:
    """The document re-read from disk, checked against the revision's entry."""
    content = read_stable_bytes(
        document.source.absolute_path,
        _MAX_DOCUMENT_BYTES,
        label="Pyright retained source document",
        deadline=deadline,
    )
    digest = hashlib.sha256(content).hexdigest()
    if entry.sha256 != digest or entry.size != len(content):
        raise RuntimeError(
            "Pyright retained source document hash differs from the revision"
        )
    return content


def _check_changed_entry(entry: object, digest: str, content: bytes) -> None:
    """A changed document has to match what the revision says it became."""
    if entry is None or entry.sha256 != digest or entry.size != len(content):
        raise RuntimeError(
            "Pyright changed source document hash differs from the revision"
        )


def _check_encodable(method: str, params: dict[str, object]) -> None:
    """Refuse now what the wire would refuse later."""
    encode_frame({"jsonrpc": "2.0", "method": method, "params": params})


def _collect_closed(
    document: OpenDocument | None,
    closed: list[OpenDocument],
    closed_uris: set[str],
) -> None:
    """Record a document to close, once per URI."""
    if document is None or document.source.uri in closed_uris:
        return
    closed.append(document)
    closed_uris.add(document.source.uri)


def _close_notifications(
    closed: list[OpenDocument],
) -> list[tuple[OpenDocument, dict[str, object]]]:
    """The didClose params for every document being closed."""
    notifications = [
        (document, {"textDocument": {"uri": document.source.uri}})
        for document in closed
    ]
    for _document, params in notifications:
        _check_encodable("textDocument/didClose", params)
    return notifications


def _watched_params(watched: list[dict[str, object]]) -> dict[str, object] | None:
    """The watched-files params, or None when nothing was watched."""
    if not watched:
        return None
    params: dict[str, object] = {"changes": list(watched)}
    _check_encodable("workspace/didChangeWatchedFiles", params)
    return params


def _next_documents(
    documents_snapshot: dict[str, OpenDocument],
    closed: list[OpenDocument],
    replacements: list[tuple[OpenDocument, OpenDocument, dict[str, object]]],
) -> dict[str, OpenDocument]:
    """The open-document map after the planned closes and replacements."""
    next_documents = dict(documents_snapshot)
    for document in closed:
        next_documents.pop(document.source.uri, None)
    for document, replacement, _params in replacements:
        next_documents[document.source.uri] = replacement
    return next_documents


class _LaunchServerGuard:
    def __init__(
        self,
        path: Path,
        expected_sha256: str,
        *,
        command: tuple[str, ...],
        owner_root: Path,
        deadline: float,
    ) -> None:
        if not isinstance(owner_root, Path):
            raise TypeError("owner_root must be a Path")
        self._path = path
        self._expected_sha256 = expected_sha256
        self._command = command
        self._owner_root = owner_root
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
                snapshot_descriptor, snapshot_name = tempfile.mkstemp(
                    prefix=".launch-",
                    suffix=".tmp",
                    dir=self._owner_root,
                )
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
        errors: list[BaseException] = []
        if snapshot is not None:
            try:
                snapshot.close()
            except BaseException as error:
                errors.append(error)
        if snapshot_path is not None:
            try:
                os.unlink(snapshot_path)
            except FileNotFoundError:
                pass
            except BaseException as error:
                errors.append(error)
        if launch_descriptor is not None:
            try:
                os.close(launch_descriptor)
            except BaseException as error:
                errors.append(error)
        if descriptor is not None:
            try:
                os.close(descriptor)
            except BaseException as error:
                errors.append(error)
        _raise_collected_errors(errors)

    def __exit__(self, error_type: object, *error_info: object) -> None:
        operation_error = next(
            (item for item in error_info if isinstance(item, BaseException)),
            None,
        )
        if error_type is None:
            try:
                self.verify()
            except BaseException as error:
                operation_error = error
        try:
            self.close()
        except BaseException as cleanup_error:
            if operation_error is not None:
                _raise_collected_errors(
                    [cleanup_error],
                    prior_error=operation_error,
                )
            raise
        if error_type is None and operation_error is not None:
            raise operation_error.with_traceback(operation_error.__traceback__)


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
        self._close_lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._document_lock = threading.RLock()
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
        self._workspace_revision: WorkspaceRevision | None = None
        self._synchronize_epoch = 0
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
        self._bootstrap_owner_nonce: str | None = None
        self._bootstrap_generation_owners: dict[str, str] = {}
        self._startup_atexit_registered = False
        self._closing = False
        self._closed = False
        self._capacity_locked = False

    @property
    def identity(self) -> PyrightIdentity:
        return self._identity

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

    def _acquire_state_lock(self, deadline: float, message: str) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._lock.acquire(timeout=remaining):
            raise TimeoutError(message)

    @contextmanager
    def _document_operation_lock(self, deadline: float) -> Iterator[None]:
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._document_lock.acquire(timeout=remaining):
                raise TimeoutError("Pyright document lock deadline expired")
            yield
        finally:
            try:
                self._document_lock.release()
            except RuntimeError:
                pass

    @contextmanager
    def _operation(self) -> Iterator[None]:
        now = time.monotonic()
        with self._lock:
            if self._closed or self._closing:
                raise RuntimeError("Pyright session is closing")
            self._active_operations += 1
            self._last_used_monotonic = now
        try:
            yield
        finally:
            with self._lock:
                self._active_operations -= 1
                self._last_used_monotonic = time.monotonic()
                self._condition.notify_all()

    @contextmanager
    def _synchronize_semantic_fence(self) -> Iterator[None]:
        with self._lock:
            if self._synchronize_epoch % 2 != 0:
                raise RuntimeError("Pyright synchronization fence is already active")
            self._synchronize_epoch += 1
            active_epoch = self._synchronize_epoch
        try:
            yield
        finally:
            with self._lock:
                if self._synchronize_epoch != active_epoch:
                    raise RuntimeError("Pyright synchronization fence changed unexpectedly")
                self._synchronize_epoch += 1
                self._condition.notify_all()

    def _reserve_idle_close(self, deadline: float) -> bool:
        deadline = _validated_deadline(deadline)
        self._acquire_state_lock(
            deadline,
            "Pyright session reservation state lock deadline expired",
        )
        try:
            if (
                self._closed
                or self._closing
                or self._starting
                or self._active_operations != 0
            ):
                return False
            self._closing = True
            self._condition.notify_all()
            return True
        finally:
            self._lock.release()

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

    def _diagnostic_core(
        self, value: Mapping[str, object]
    ) -> tuple[LspRange, str, int | None, str | None] | None:
        """Range, message, severity and code, or None when one is unreadable."""
        range_ = _lsp_range(value.get("range"))
        message = _bounded_text(value.get("message"), _MAX_DIAGNOSTIC_TEXT_BYTES)
        if range_ is None or message is None:
            return None
        severity, severity_ok = _diagnostic_severity(value.get("severity"))
        if not severity_ok:
            return None
        code, code_ok = _diagnostic_code(value.get("code"))
        if not code_ok:
            return None
        return range_, message, severity, code

    def _related_entry(
        self, relation: object
    ) -> tuple[LspLocation, str | None] | None:
        """One related location, or None when it cannot be read."""
        if not isinstance(relation, dict):
            return None
        location = self._normalize_location(relation.get("location"))
        if location is None:
            return None
        raw_message = relation.get("message")
        if raw_message is None:
            return location, None
        message = _bounded_text(raw_message, _MAX_DIAGNOSTIC_TEXT_BYTES)
        if message is None:
            return None
        return location, message

    def _related_information(
        self, value: object
    ) -> tuple[list[tuple[LspLocation, str | None]], bool]:
        """The related locations, and whether anything had to be dropped."""
        if value is None:
            return [], False
        if not isinstance(value, list):
            return [], True
        partial = len(value) > MAX_LOCATIONS
        related: list[tuple[LspLocation, str | None]] = []
        for relation in value[:MAX_LOCATIONS]:
            entry = self._related_entry(relation)
            if entry is None:
                partial = True
                continue
            related.append(entry)
        return related, partial

    def _parse_diagnostic(
        self,
        value: object,
        uri: str,
    ) -> tuple[LspDiagnostic | None, bool]:
        if not isinstance(value, dict):
            return None, True
        core = self._diagnostic_core(value)
        if core is None:
            return None, True
        range_, message, severity, code = core
        related, partial = self._related_information(value.get("relatedInformation"))
        diagnostic = LspDiagnostic(
            uri,
            range_,
            severity,
            code,
            message,
            tuple(related),
        )
        return diagnostic, partial

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

    def _bootstrap_owned_generation(
        self,
        owner_nonce: str,
        protocol: LspProtocol,
        process_id: int,
        generation_nonce: str,
        deadline: float,
    ) -> ProcessState:
        with self._lock:
            if self._bootstrap_owner_nonce != owner_nonce:
                raise RuntimeError("Pyright process owner is no longer accepted")
            self._bootstrap_generation_owners[generation_nonce] = owner_nonce
        try:
            return self._bootstrap_generation(
                protocol,
                process_id,
                generation_nonce,
                deadline,
            )
        finally:
            with self._lock:
                if self._bootstrap_generation_owners.get(generation_nonce) == owner_nonce:
                    self._bootstrap_generation_owners.pop(generation_nonce, None)

    def _bootstrap_generation(
        self,
        protocol: LspProtocol,
        _process_id: int,
        _generation_nonce: str,
        deadline: float,
    ) -> ProcessState:
        with self._lock:
            owner_nonce = self._bootstrap_generation_owners.get(_generation_nonce)
            if owner_nonce is None or self._bootstrap_owner_nonce != owner_nonce:
                raise RuntimeError("Pyright process owner is no longer accepted")
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
            with self._lock:
                if self._bootstrap_owner_nonce != owner_nonce:
                    raise RuntimeError("Pyright process owner is no longer accepted")
            self._begin_wire_generation(_generation_nonce)
            with self._lock:
                if self._bootstrap_owner_nonce != owner_nonce:
                    raise RuntimeError("Pyright process owner is no longer accepted")
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
                with self._lock:
                    if self._bootstrap_owner_nonce != owner_nonce:
                        raise RuntimeError("Pyright process owner is no longer accepted")
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
                if self._capacity_locked:
                    self._readiness = "not_ready"
                    self._readiness_evidence = ()
                    self._position_encoding = None
                    self._capabilities = {}
                    self._degradation_codes = tuple(
                        sorted(
                            {
                                *self._degradation_codes,
                                "pyright_capacity_exhausted",
                            }
                        )
                    )
                    return
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
            bootstrap_owner_nonce: str | None = None
            try:
                if retained_cleanup is not None:
                    try:
                        retained_cleanup.retry_cleanup(startup_deadline)
                    except BaseException as error:
                        interruption = _startup_interruption(error)
                        if interruption is not None:
                            if interruption is error:
                                raise
                            _raise_collected_errors([], prior_error=error)
                        if isinstance(error, (OSError, RuntimeError, TimeoutError)):
                            return
                        raise
                    with self._lock:
                        if self._startup_cleanup_error is retained_cleanup:
                            self._startup_cleanup_error = None
                            self._sync_startup_atexit_locked()

                if retained_process is not None:
                    try:
                        retained_process.close(startup_deadline)
                    except BaseException as error:
                        interruption = _startup_interruption(error)
                        if interruption is not None:
                            if interruption is error:
                                raise
                            _raise_collected_errors([], prior_error=error)
                        if isinstance(error, (OSError, RuntimeError, TimeoutError)):
                            return
                        raise
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
                    bootstrap_owner_nonce = owner.name
                    with self._lock:
                        self._bootstrap_owner_nonce = bootstrap_owner_nonce
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
                        generation_bootstrap=(
                            lambda protocol,
                            process_id,
                            generation_nonce,
                            generation_deadline: self._bootstrap_owned_generation(
                                owner.name,
                                protocol,
                                process_id,
                                generation_nonce,
                                generation_deadline,
                            )
                        ),
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
                                    owner_root=owner,
                                    deadline=generation_deadline,
                                )
                            )
                        ),
                    )
                except BaseException as error:
                    with self._lock:
                        if self._bootstrap_owner_nonce == bootstrap_owner_nonce:
                            self._bootstrap_owner_nonce = None
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
                        _raise_collected_errors([], prior_error=error)
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
                cleanup_error: BaseException | None = None
                if process is not None:
                    cleanup_deadline = min(
                        startup_deadline,
                        time.monotonic() + _OWNER_CLEANUP_SECONDS,
                    )
                    retained_process_owner: LspProcess | None = None
                    try:
                        process.close(cleanup_deadline)
                    except BaseException as close_error:
                        retained_process_owner = process
                        cleanup_error = close_error
                    with self._lock:
                        if self._process is process:
                            self._process = None
                        if self._bootstrap_owner_nonce == bootstrap_owner_nonce:
                            self._bootstrap_owner_nonce = None
                        self._startup_process = retained_process_owner
                        self._sync_startup_atexit_locked()
                        self._readiness = "not_ready"
                        self._readiness_evidence = ()
                        self._position_encoding = None
                        self._capabilities = {}
                        self._generation_nonce = None
                        self._ready_uri_generations.clear()
                        self._workspace_revision = None
                        self._diagnostics.clear()
                        self._diagnostic_bytes = 0
                        self._clear_wire_state()
                original_interruption = _startup_interruption(error)
                cleanup_interruption = (
                    _startup_interruption(cleanup_error)
                    if cleanup_error is not None
                    else None
                )
                interruption = original_interruption or cleanup_interruption
                if interruption is not None:
                    with self._lock:
                        self._startup_attempted = False
                    if cleanup_error is not None:
                        _raise_collected_errors(
                            [cleanup_error],
                            prior_error=error,
                        )
                    if interruption is error:
                        raise
                    _raise_collected_errors([], prior_error=error)
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

    def _semantic_query_epoch_locked(self) -> int | None:
        epoch = self._synchronize_epoch
        return epoch if epoch % 2 == 0 else None

    def _semantic_query_epoch_current_locked(self, epoch: int) -> bool:
        return epoch % 2 == 0 and self._synchronize_epoch == epoch

    def _document_query_current_locked(
        self,
        process: LspProcess,
        generation: str,
        document: OpenDocument,
        synchronize_epoch: int,
    ) -> bool:
        uri = document.source.uri
        return (
            self._semantic_query_epoch_current_locked(synchronize_epoch)
            and self._process is process
            and self._generation_nonce == generation
            and self._documents.get(uri) is document
            and self._document_ready_locked(uri)
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
        self._workspace_revision = None
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
        with self._document_operation_lock(deadline), self._operation():
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
            with self._lock:
                synchronize_epoch = self._semantic_query_epoch_locked()
            if synchronize_epoch is None:
                return ProviderLocations((), "not_ready", True)
            self.start(deadline=deadline)
            with self._lock:
                process = self._process
                encoding = self._position_encoding
                supported = self._capabilities.get(capability, False)
                if (
                    process is None
                    or encoding is None
                    or not self._semantic_query_epoch_current_locked(
                        synchronize_epoch
                    )
                ):
                    return ProviderLocations((), "not_ready", True)
            if not supported:
                return ProviderLocations((), "unsupported", True)
            document = self.open_document(anchor.path, deadline=deadline)
            with self._lock:
                encoding = self._position_encoding
                process = self._process
                generation = self._generation_nonce
                ready = (
                    process is not None
                    and generation is not None
                    and self._document_query_current_locked(
                        process,
                        generation,
                        document,
                        synchronize_epoch,
                    )
                )
            if not ready or encoding is None or process is None or generation is None:
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
            with self._lock:
                if not self._document_query_current_locked(
                    process,
                    generation,
                    document,
                    synchronize_epoch,
                ):
                    return ProviderLocations((), "not_ready", True)
            result = process.request(method, params, deadline=deadline)
            locations, filtered = self._normalize_locations(result)
            response = ProviderLocations(
                locations,
                "provider_reported",
                True if references else filtered,
            )
            with self._lock:
                if not self._document_query_current_locked(
                    process,
                    generation,
                    document,
                    synchronize_epoch,
                ):
                    return ProviderLocations((), "not_ready", True)
                return response

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

    def _symbol_location(
        self, value: Mapping[str, object], uri: str
    ) -> LspLocation | None:
        """Where the symbol is: an explicit location, or its selection range."""
        if "location" in value:
            return self._normalize_location(value.get("location"))
        range_ = _lsp_range(value.get("range"))
        selection = _lsp_range(value.get("selectionRange"))
        if range_ is None or selection is None:
            return None
        if not _range_contains(range_, selection):
            return None
        return LspLocation(uri, selection)

    def _visit_symbol(self, walk: _SymbolWalk, uri: str) -> None:
        """Take one node off the walk and collect the location it names."""
        value = walk.stack.pop()
        walk.visited += 1
        if not isinstance(value, dict) or not walk.first_visit(value):
            walk.drop()
            return
        _push_symbol_children(walk, value)
        if not _symbol_fields_ok(value):
            walk.drop()
            return
        location = self._symbol_location(value, uri)
        if location is None:
            walk.drop()
            return
        walk.add(location)

    def _walk_document_symbols(self, walk: _SymbolWalk, uri: str) -> None:
        """Visit up to the bound, then record whatever is left unvisited."""
        while walk.stack and walk.visited < MAX_LOCATIONS:
            self._visit_symbol(walk, uri)
        if walk.stack:
            walk.drop()

    def _normalize_document_symbols(
        self,
        result: object,
        uri: str,
    ) -> tuple[tuple[LspLocation, ...], bool]:
        if result is None:
            return (), False
        if not isinstance(result, list):
            return (), True
        walk = _SymbolWalk(
            stack=list(reversed(result[:MAX_LOCATIONS])),
            partial=len(result) > MAX_LOCATIONS,
        )
        self._walk_document_symbols(walk, uri)
        return tuple(walk.locations), walk.partial

    def document_symbols(self, path: str, *, deadline: float) -> ProviderLocations:
        deadline = _validated_deadline(deadline)
        with self._operation():
            with self._lock:
                synchronize_epoch = self._semantic_query_epoch_locked()
            if synchronize_epoch is None:
                return ProviderLocations((), "not_ready", True)
            self.start(deadline=deadline)
            with self._lock:
                process = self._process
                initialized = self._position_encoding is not None
                supported = self._capabilities.get("document_symbols", False)
                if (
                    process is None
                    or not initialized
                    or not self._semantic_query_epoch_current_locked(
                        synchronize_epoch
                    )
                ):
                    return ProviderLocations((), "not_ready", True)
            if not supported:
                return ProviderLocations((), "unsupported", True)
            document = self.open_document(path, deadline=deadline)
            with self._lock:
                process = self._process
                generation = self._generation_nonce
                ready = (
                    process is not None
                    and generation is not None
                    and self._document_query_current_locked(
                        process,
                        generation,
                        document,
                        synchronize_epoch,
                    )
                )
            if not ready or process is None or generation is None:
                return ProviderLocations((), "not_ready", True)
            result = process.request(
                "textDocument/documentSymbol",
                {"textDocument": {"uri": document.source.uri}},
                deadline=deadline,
            )
            locations, partial = self._normalize_document_symbols(
                result,
                document.source.uri,
            )
            response = ProviderLocations(locations, "provider_reported", partial)
            with self._lock:
                if not self._document_query_current_locked(
                    process,
                    generation,
                    document,
                    synchronize_epoch,
                ):
                    return ProviderLocations((), "not_ready", True)
                return response

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
            with self._lock:
                synchronize_epoch = self._semantic_query_epoch_locked()
            if synchronize_epoch is None:
                return ProviderLocations((), "not_ready", True)
            self.start(deadline=deadline)
            with self._lock:
                supported = self._capabilities.get("workspace_symbols", False)
                readiness = self._readiness
                process = self._process
                generation = self._generation_nonce
                initialized = self._position_encoding is not None
                current = self._semantic_query_epoch_current_locked(synchronize_epoch)
            if (
                process is None
                or generation is None
                or not initialized
                or not current
            ):
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
                response = ProviderLocations((), "provider_reported", False)
            elif not isinstance(result, list):
                response = ProviderLocations((), "provider_reported", True)
            else:
                values: list[object] = []
                partial = len(result) > MAX_LOCATIONS
                for symbol in result[:MAX_LOCATIONS]:
                    if not isinstance(symbol, dict) or "location" not in symbol:
                        partial = True
                        continue
                    values.append(symbol["location"])
                locations, filtered = self._normalize_locations(values)
                response = ProviderLocations(
                    locations,
                    "provider_reported",
                    partial or filtered,
                )
            with self._lock:
                if (
                    self._process is not process
                    or self._generation_nonce != generation
                    or self._readiness != readiness
                    or not self._semantic_query_epoch_current_locked(
                        synchronize_epoch
                    )
                ):
                    return ProviderLocations((), "not_ready", True)
                return response

    def hover(self, anchor: SourceAnchor, *, deadline: float) -> ProviderHover:
        if not isinstance(anchor, SourceAnchor):
            raise TypeError("anchor must be a SourceAnchor")
        deadline = _validated_deadline(deadline)
        with self._operation():
            with self._lock:
                synchronize_epoch = self._semantic_query_epoch_locked()
            if synchronize_epoch is None:
                return ProviderHover(None, None, True)
            self.start(deadline=deadline)
            with self._lock:
                process = self._process
                encoding = self._position_encoding
                supported = self._capabilities.get("hover", False)
                if (
                    process is None
                    or encoding is None
                    or not self._semantic_query_epoch_current_locked(
                        synchronize_epoch
                    )
                ):
                    return ProviderHover(None, None, True)
            if not supported:
                return ProviderHover(None, None, True)
            document = self.open_document(anchor.path, deadline=deadline)
            with self._lock:
                encoding = self._position_encoding
                process = self._process
                generation = self._generation_nonce
                ready = (
                    process is not None
                    and generation is not None
                    and self._document_query_current_locked(
                        process,
                        generation,
                        document,
                        synchronize_epoch,
                    )
                )
            if not ready or encoding is None or process is None or generation is None:
                return ProviderHover(None, None, True)
            source_document = SourceDocument.from_bytes(
                document.source.relative_path,
                document.content,
            )
            position = source_document.to_lsp(anchor, encoding)
            with self._lock:
                if not self._document_query_current_locked(
                    process,
                    generation,
                    document,
                    synchronize_epoch,
                ):
                    return ProviderHover(None, None, True)
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
            if result is None:
                response = ProviderHover(None, None, False)
            elif not isinstance(result, dict) or "contents" not in result:
                response = ProviderHover(None, None, True)
            else:
                contents, partial = _hover_contents(result["contents"])
                range_ = None
                if "range" in result:
                    range_ = _lsp_range(result["range"])
                    if range_ is None:
                        partial = True
                response = ProviderHover(contents, range_, partial)
            with self._lock:
                if not self._document_query_current_locked(
                    process,
                    generation,
                    document,
                    synchronize_epoch,
                ):
                    return ProviderHover(None, None, True)
                return response

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

    def _semantic_query_epoch(self) -> int | None:
        with self._lock:
            return self._semantic_query_epoch_locked()

    def _query_ready_locked(
        self,
        process: LspProcess | None,
        generation: str | None,
        document: OpenDocument,
        epoch: int,
    ) -> bool:
        if process is None or generation is None:
            return False
        return self._document_query_current_locked(
            process, generation, document, epoch
        )

    def _query_still_current(self, query: _CallQuery) -> bool:
        """Whether the query still refers to the workspace it started on."""
        with self._lock:
            return self._document_query_current_locked(
                query.process, query.generation, query.document, query.epoch
            )

    def _call_support_status(self, epoch: int) -> str | None:
        """A refusal status for call hierarchy, or None when it may proceed."""
        with self._lock:
            process = self._process
            encoding = self._position_encoding
            supported = self._capabilities.get("calls", False)
            current = self._semantic_query_epoch_current_locked(epoch)
        if process is None or encoding is None or not current:
            return "not_ready"
        if not supported:
            return "unsupported"
        return None

    def _call_query(self, document: OpenDocument, epoch: int) -> _CallQuery | None:
        """The bound query, or None when the workspace moved under it."""
        with self._lock:
            encoding = self._position_encoding
            process = self._process
            generation = self._generation_nonce
            ready = self._query_ready_locked(process, generation, document, epoch)
        if not ready or encoding is None or process is None or generation is None:
            return None
        return _CallQuery(process, generation, document, epoch, encoding)

    def _prepare_call_hierarchy(
        self, anchor: SourceAnchor, query: _CallQuery, *, deadline: float
    ) -> tuple[bool, object]:
        """Whether the query held, and what the server prepared for the anchor."""
        source_document = SourceDocument.from_bytes(
            query.document.source.relative_path,
            query.document.content,
        )
        position = source_document.to_lsp(anchor, query.encoding)
        if not self._query_still_current(query):
            return False, None
        prepared = query.process.request(
            "textDocument/prepareCallHierarchy",
            {
                "textDocument": {"uri": query.document.source.uri},
                "position": {
                    "line": position.line,
                    "character": position.character,
                },
            },
            deadline=deadline,
        )
        if not self._query_still_current(query):
            return False, None
        return True, prepared

    def _sanitized_call_items(self, prepared: list[object]) -> list[object]:
        items: list[object] = []
        for value in prepared[:_MAX_PREPARED_CALL_ITEMS]:
            item = self._sanitize_call_item(value)
            if item is not None:
                items.append(item)
        return items

    def _call_result(
        self, item: object, query: _CallQuery, *, method: str, deadline: float
    ) -> tuple[bool, object]:
        """Whether the query held across the request, and what came back."""
        if not self._query_still_current(query):
            return False, None
        result = query.process.request(method, {"item": item}, deadline=deadline)
        if not self._query_still_current(query):
            return False, None
        return True, result

    def _call_entry_location(
        self, call: object, result_key: str
    ) -> LspLocation | None:
        if not isinstance(call, dict):
            return None
        return self._call_location(call.get(result_key))

    def _collect_call_locations(
        self, result: object, result_key: str, collected: _BoundedLocations
    ) -> None:
        if not isinstance(result, list):
            return
        for call in result[:MAX_LOCATIONS]:
            location = self._call_entry_location(call, result_key)
            if location is not None:
                collected.add(location)

    def _call_locations(
        self,
        items: list[object],
        query: _CallQuery,
        *,
        direction: str,
        deadline: float,
    ) -> list[LspLocation] | None:
        """Every location the calls name, or None when the query went stale."""
        method = _CALL_METHODS[direction]
        result_key = _CALL_RESULT_KEYS[direction]
        collected = _BoundedLocations()
        for item in items:
            current, result = self._call_result(
                item, query, method=method, deadline=deadline
            )
            if not current:
                return None
            self._collect_call_locations(result, result_key, collected)
        if not self._query_still_current(query):
            return None
        return collected.locations

    def _collect_calls(
        self,
        anchor: SourceAnchor,
        query: _CallQuery,
        *,
        direction: str,
        deadline: float,
    ) -> ProviderCalls:
        current, prepared = self._prepare_call_hierarchy(
            anchor, query, deadline=deadline
        )
        if not current:
            return ProviderCalls(direction, (), "not_ready", True)
        if prepared is None or not isinstance(prepared, list):
            return ProviderCalls(direction, (), "provider_reported", True)
        items = self._sanitized_call_items(prepared)
        locations = self._call_locations(
            items, query, direction=direction, deadline=deadline
        )
        if locations is None:
            return ProviderCalls(direction, (), "not_ready", True)
        return ProviderCalls(direction, tuple(locations), "provider_reported", True)

    def _calls_within_operation(
        self, anchor: SourceAnchor, *, direction: str, deadline: float
    ) -> ProviderCalls:
        epoch = self._semantic_query_epoch()
        if epoch is None:
            return ProviderCalls(direction, (), "not_ready", True)
        self.start(deadline=deadline)
        status = self._call_support_status(epoch)
        if status is not None:
            return ProviderCalls(direction, (), status, True)
        document = self.open_document(anchor.path, deadline=deadline)
        query = self._call_query(document, epoch)
        if query is None:
            return ProviderCalls(direction, (), "not_ready", True)
        return self._collect_calls(
            anchor, query, direction=direction, deadline=deadline
        )

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
            return self._calls_within_operation(
                anchor, direction=direction, deadline=deadline
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
            with self._lock:
                synchronize_epoch = self._semantic_query_epoch_locked()
            if synchronize_epoch is None:
                return ProviderDiagnostics((), None, True)
            self.start(deadline=deadline)
            with self._lock:
                initialized = self._process is not None and self._position_encoding is not None
                current = self._semantic_query_epoch_current_locked(synchronize_epoch)
            if not initialized or not current:
                return ProviderDiagnostics((), None, True)
            document = self.open_document(path, deadline=deadline)
            with self._lock:
                process = self._process
                generation = self._generation_nonce
                if (
                    process is None
                    or generation is None
                    or not self._document_query_current_locked(
                        process,
                        generation,
                        document,
                        synchronize_epoch,
                    )
                ):
                    return ProviderDiagnostics((), None, True)
                while True:
                    if not self._document_query_current_locked(
                        process,
                        generation,
                        document,
                        synchronize_epoch,
                    ):
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

    def _watched_uri(self, relative_path: str) -> str:
        absolute = Path(self._repository.checkout_root, relative_path)
        return path_to_file_uri(absolute)

    def _synchronize_snapshot_replayed_locked(self, process: LspProcess) -> bool:
        generation = self._generation_nonce
        return (
            self._process is process
            and process.state not in {ProcessState.DEGRADED, ProcessState.FAILED}
            and generation is not None
            and generation == process.generation_nonce
            and self._position_encoding is not None
            and all(
                self._wire_document_opened(document, generation)
                for document in self._documents.values()
            )
        )

    def _recover_synchronize_snapshot(
        self,
        process: LspProcess,
        failed_generation: str,
        *,
        deadline: float,
    ) -> None:
        recovery_serialized = False
        bootstrap_owner_nonce: str | None = None
        with self._lock:
            already_replayed = (
                self._generation_nonce != failed_generation
                and self._synchronize_snapshot_replayed_locked(process)
            )
            if already_replayed:
                return
            self._readiness = "not_ready"
            self._readiness_evidence = ()
            self._ready_uri_generations.clear()
            self._diagnostics.clear()
            self._diagnostic_bytes = 0
            if self._process is not process:
                raise RuntimeError(
                    "Pyright synchronization process changed before recovery"
                )
            if (
                self._startup_process is not None
                and self._startup_process is not process
            ):
                raise RuntimeError(
                    "Pyright synchronization cleanup owner is unavailable"
                )
            bootstrap_owner_nonce = self._bootstrap_owner_nonce
            self._process = None
            self._startup_process = process
            self._starting = True
            recovery_serialized = True
            self._position_encoding = None
            self._capabilities = {}
            self._generation_nonce = None
            self._clear_wire_state()
            self._sync_startup_atexit_locked()
            self._condition.notify_all()
        try:
            process.restart(deadline)
            with self._lock:
                if self._startup_process is process and self._process is None:
                    self._process = process
                    if self._synchronize_snapshot_replayed_locked(process):
                        self._startup_process = None
                        self._sync_startup_atexit_locked()
                        return
                    self._process = None
            raise RuntimeError(
                "Pyright synchronization recovery could not prove the prior snapshot"
            )
        except BaseException:
            with self._lock:
                if self._process is process:
                    self._process = None
                if self._startup_process is None:
                    self._startup_process = process
                if self._bootstrap_owner_nonce == bootstrap_owner_nonce:
                    self._bootstrap_owner_nonce = None
                self._readiness = "not_ready"
                self._readiness_evidence = ()
                self._position_encoding = None
                self._capabilities = {}
                self._generation_nonce = None
                self._ready_uri_generations.clear()
                self._diagnostics.clear()
                self._diagnostic_bytes = 0
                self._clear_wire_state()
                self._sync_startup_atexit_locked()
                self._condition.notify_all()
            raise
        finally:
            if recovery_serialized:
                with self._lock:
                    self._starting = False
                    self._condition.notify_all()

    def _notify_or_fail(
        self,
        plan: _SyncPlan,
        method: str,
        params: dict[str, object],
        message: str,
    ) -> None:
        """Send one notification; an explicit refusal fails the synchronize."""
        delivered = plan.process.notify_generation(
            method,
            params,
            generation_nonce=plan.generation,
            deadline=plan.deadline,
        )
        if delivered is False:
            raise RuntimeError(message)

    def _send_change(
        self, plan: _SyncPlan, document: OpenDocument, params: dict[str, object]
    ) -> None:
        """A changed document is opened once, then sent its change."""
        opened = self._send_did_open_once(
            document,
            plan.generation,
            deadline=plan.deadline,
            notify=lambda: plan.process.notify_generation(
                "textDocument/didOpen",
                self._did_open_params(document),
                generation_nonce=plan.generation,
                deadline=plan.deadline,
            ),
        )
        if not opened:
            raise RuntimeError("Pyright didOpen notification was not delivered")
        self._notify_or_fail(
            plan,
            "textDocument/didChange",
            params,
            "Pyright didChange notification was not delivered",
        )

    def _send_synchronize_notifications(
        self, plan: _SyncPlan, attempt: _WireAttempt
    ) -> None:
        for _document, params in plan.close_notifications:
            attempt.started = True
            self._notify_or_fail(
                plan,
                "textDocument/didClose",
                params,
                "Pyright didClose notification was not delivered",
            )
        for document, _replacement, params in plan.changed_replacements:
            attempt.started = True
            self._send_change(plan, document, params)
        if plan.watched_params is not None:
            attempt.started = True
            self._notify_or_fail(
                plan,
                "workspace/didChangeWatchedFiles",
                plan.watched_params,
                "Pyright watched-files notification was not delivered",
            )

    def _recover_after_wire_failure(
        self, plan: _SyncPlan, notification_error: BaseException
    ) -> None:
        """Put the server back to the snapshot it had before this pass."""
        try:
            self._recover_synchronize_snapshot(
                plan.process,
                plan.generation,
                deadline=plan.deadline,
            )
        except BaseException as recovery_error:
            if (
                _startup_interruption(notification_error) is not None
                or _startup_interruption(recovery_error) is not None
            ):
                _raise_collected_errors(
                    [recovery_error],
                    prior_error=notification_error,
                )
            raise RuntimeError(
                "Pyright synchronization notification recovery failed"
            ) from recovery_error

    def _deliver_synchronize(self, plan: _SyncPlan) -> None:
        """Send the planned notifications, restoring the snapshot if one fails."""
        attempt = _WireAttempt()
        try:
            self._send_synchronize_notifications(plan, attempt)
        except BaseException as notification_error:
            if attempt.started:
                self._recover_after_wire_failure(plan, notification_error)
            raise

    def _documents_unchanged_locked(
        self, snapshot: dict[str, OpenDocument]
    ) -> bool:
        """No document was opened, closed or replaced while the plan was built."""
        if len(self._documents) != len(snapshot):
            return False
        return all(
            self._documents.get(uri) is document
            for uri, document in snapshot.items()
        )

    def _commit_identity_changed_locked(self, plan: _SyncPlan) -> bool:
        """The process, generation or revision the plan was built on has moved."""
        if self._process is not plan.process:
            return True
        if self._workspace_revision is not plan.prior:
            return True
        if self._generation_nonce != plan.generation:
            return True
        return plan.process.generation_nonce != plan.generation

    def _commit_state_changed_locked(self, plan: _SyncPlan) -> bool:
        if self._closed or self._closing:
            return True
        if self._commit_identity_changed_locked(plan):
            return True
        return not self._documents_unchanged_locked(plan.documents_snapshot)

    def _projected_readiness(
        self, plan: _SyncPlan
    ) -> tuple[dict[str, _DiagnosticSnapshot], dict[str, str], str | None]:
        """Diagnostics, readiness and target after the planned closes and changes."""
        next_diagnostics = dict(self._diagnostics)
        next_ready = dict(self._ready_uri_generations)
        next_target = self._readiness_target_uri
        for document in plan.closed_documents:
            next_ready.pop(document.source.uri, None)
            next_diagnostics.pop(document.source.uri, None)
            if next_target == document.source.uri:
                next_target = None
        for document, replacement, _params in plan.changed_replacements:
            _drop_superseded_diagnostics(next_diagnostics, document, replacement)
        return next_diagnostics, next_ready, next_target

    def _projected_wire_state(
        self, plan: _SyncPlan
    ) -> tuple[set[tuple[object, ...]], set[tuple[object, ...]]]:
        """The wire bookkeeping after the planned closes and version bumps."""
        opened = set(self._wire_opened)
        failed = set(self._wire_failed)
        for document in plan.closed_documents:
            uri = document.source.uri
            opened = _without_wire_uri(opened, plan.generation, uri)
            failed = _without_wire_uri(failed, plan.generation, uri)
        for document, replacement, _params in plan.changed_replacements:
            opened.discard((plan.generation, document.source.uri, document.version))
            opened.add(
                (plan.generation, replacement.source.uri, replacement.version)
            )
        return opened, failed

    def _relax_readiness_locked(
        self, next_documents: dict[str, OpenDocument], next_target: str | None
    ) -> None:
        """With no readiness target left, the session falls back to initialized."""
        if next_target is not None or self._readiness != "query_ready":
            return
        self._readiness = "protocol_initialized"
        self._readiness_evidence = (
            "initialize",
            "initialized",
            "configuration",
            *(("didOpen",) if next_documents else ()),
        )

    def _apply_synchronize_commit_locked(self, plan: _SyncPlan) -> None:
        """Publish the planned state; the caller holds the session and wire locks."""
        next_diagnostics, next_ready, next_target = self._projected_readiness(plan)
        self._wire_opened, self._wire_failed = self._projected_wire_state(plan)
        self._wire_condition.notify_all()
        self._documents = plan.next_documents
        self._document_bytes = plan.projected_document_bytes
        self._ready_uri_generations = next_ready
        self._diagnostics = next_diagnostics
        self._diagnostic_bytes = sum(
            snapshot.retained_bytes for snapshot in next_diagnostics.values()
        )
        self._readiness_target_uri = next_target
        self._relax_readiness_locked(plan.next_documents, next_target)
        self._workspace_revision = plan.revision
        self._condition.notify_all()

    def _commit_synchronize(self, plan: _SyncPlan) -> RuntimeError | None:
        """Publish the planned state, or say why it could not be published."""
        with self._lock:
            if self._commit_state_changed_locked(plan):
                return RuntimeError(
                    "Pyright synchronization state changed before commit"
                )
            with self._wire_condition:
                if self._wire_generation != plan.generation:
                    return RuntimeError(
                        "Pyright synchronization generation changed before commit"
                    )
                self._apply_synchronize_commit_locked(plan)
        return None

    def _recover_after_commit_failure(
        self, plan: _SyncPlan, commit_error: RuntimeError
    ) -> None:
        """Restore the server's snapshot, then report why the commit failed."""
        try:
            self._recover_synchronize_snapshot(
                plan.process,
                plan.generation,
                deadline=plan.deadline,
            )
        except BaseException as recovery_error:
            if _startup_interruption(recovery_error) is not None:
                _raise_collected_errors(
                    [recovery_error],
                    prior_error=commit_error,
                )
            raise RuntimeError(
                "Pyright synchronization commit recovery failed"
            ) from recovery_error
        raise commit_error

    def _synchronize_snapshot(self) -> _SyncSnapshot:
        """The process, generation and open documents this pass plans against."""
        with self._lock:
            process = self._process
            prior = self._workspace_revision
            documents_snapshot = dict(self._documents)
            generation = self._generation_nonce
        if process is None or generation is None:
            raise RuntimeError("Pyright session is not protocol initialized")
        open_by_path = {
            document.source.relative_path: document
            for document in documents_snapshot.values()
        }
        return _SyncSnapshot(
            process, generation, prior, documents_snapshot, open_by_path
        )

    def _synchronize_delta(
        self,
        snapshot: _SyncSnapshot,
        revision: WorkspaceRevision,
        entries: Mapping[str, object],
    ) -> WorkspaceDelta:
        if snapshot.prior is None:
            return _first_sync_delta(snapshot.open_by_path, entries)
        return diff_workspace_revisions(snapshot.prior, revision)

    def _projected_document_bytes(
        self,
        snapshot: _SyncSnapshot,
        entries: Mapping[str, object],
        closing_paths: set[str],
    ) -> int:
        """Bytes the retained documents will occupy, refused when they overrun."""
        total = 0
        for path, document in snapshot.open_by_path.items():
            if path in closing_paths:
                continue
            total += _retained_document_bytes(entries.get(path), document)
            if total > _MAX_OPEN_DOCUMENT_BYTES:
                raise RuntimeError("Pyright open document source bytes limit exceeded")
        return total

    def _verified_retained_contents(
        self,
        snapshot: _SyncSnapshot,
        entries: Mapping[str, object],
        closing_paths: set[str],
        *,
        deadline: float,
    ) -> dict[str, bytes]:
        """Re-read every retained document and check it against the revision."""
        contents: dict[str, bytes] = {}
        for path, document in snapshot.open_by_path.items():
            if path in closing_paths:
                continue
            contents[path] = _verified_document_content(
                entries[path], document, deadline=deadline
            )
        return contents

    def _changed_replacement(
        self,
        document: OpenDocument,
        entry: object,
        verified: bytes | None,
        *,
        deadline: float,
    ) -> tuple[OpenDocument, dict[str, object]]:
        """The next version of a changed document and its didChange params."""
        content = verified
        if content is None:
            content = read_stable_bytes(
                document.source.absolute_path,
                _MAX_DOCUMENT_BYTES,
                label="Pyright changed source document",
                deadline=deadline,
            )
        text = content.decode("utf-8", errors="strict")
        digest = hashlib.sha256(content).hexdigest()
        _check_changed_entry(entry, digest, content)
        replacement = OpenDocument(
            document.source,
            content,
            digest,
            document.version + 1,
        )
        change_params: dict[str, object] = {
            "textDocument": {
                "uri": document.source.uri,
                "version": replacement.version,
            },
            "contentChanges": [{"text": text}],
        }
        _check_encodable("textDocument/didChange", change_params)
        return replacement, change_params

    def _planned_changes(
        self,
        snapshot: _SyncSnapshot,
        delta: WorkspaceDelta,
        entries: Mapping[str, object],
        verified: Mapping[str, bytes],
        *,
        deadline: float,
    ) -> tuple[list[dict[str, object]], list[tuple[OpenDocument, OpenDocument, dict[str, object]]]]:
        """Watched-file events and the replacements for open changed documents."""
        watched = [
            {"uri": self._watched_uri(path), "type": 1} for path in delta.created
        ]
        replacements: list[
            tuple[OpenDocument, OpenDocument, dict[str, object]]
        ] = []
        for path in delta.changed:
            document = snapshot.open_by_path.get(path)
            if document is None:
                watched.append({"uri": self._watched_uri(path), "type": 2})
                continue
            replacement, params = self._changed_replacement(
                document, entries.get(path), verified.get(path), deadline=deadline
            )
            replacements.append((document, replacement, params))
        return watched, replacements

    def _planned_closes(
        self,
        snapshot: _SyncSnapshot,
        delta: WorkspaceDelta,
        watched: list[dict[str, object]],
    ) -> list[OpenDocument]:
        """Documents to close, adding the watched-file events they imply."""
        closed: list[OpenDocument] = []
        closed_uris: set[str] = set()
        for path in delta.deleted:
            _collect_closed(snapshot.open_by_path.get(path), closed, closed_uris)
            watched.append({"uri": self._watched_uri(path), "type": 3})
        for old_path, new_path in delta.renamed:
            _collect_closed(snapshot.open_by_path.get(old_path), closed, closed_uris)
            watched.append({"uri": self._watched_uri(old_path), "type": 3})
            watched.append({"uri": self._watched_uri(new_path), "type": 1})
        return closed

    def _synchronize_plan(
        self, revision: WorkspaceRevision, *, deadline: float
    ) -> tuple[_SyncPlan, WorkspaceDelta]:
        """Everything this pass will do, worked out before the server is told."""
        snapshot = self._synchronize_snapshot()
        entries = {entry.path: entry for entry in revision.entries}
        delta = self._synchronize_delta(snapshot, revision, entries)
        closing_paths = set(delta.deleted)
        closing_paths.update(old_path for old_path, _new_path in delta.renamed)
        projected_bytes = self._projected_document_bytes(
            snapshot, entries, closing_paths
        )
        verified = self._verified_retained_contents(
            snapshot, entries, closing_paths, deadline=deadline
        )
        watched, replacements = self._planned_changes(
            snapshot, delta, entries, verified, deadline=deadline
        )
        closed = self._planned_closes(snapshot, delta, watched)
        plan = _SyncPlan(
            process=snapshot.process,
            generation=snapshot.generation,
            prior=snapshot.prior,
            revision=revision,
            deadline=deadline,
            documents_snapshot=snapshot.documents_snapshot,
            next_documents=_next_documents(
                snapshot.documents_snapshot, closed, replacements
            ),
            projected_document_bytes=projected_bytes,
            closed_documents=closed,
            changed_replacements=replacements,
            close_notifications=_close_notifications(closed),
            watched_params=_watched_params(watched),
        )
        return plan, delta

    def synchronize(
        self,
        revision: WorkspaceRevision,
        *,
        deadline: float,
    ) -> WorkspaceDelta:
        deadline = _validated_deadline(deadline)
        self._check_synchronize_revision(revision, deadline)
        with (
            self._document_operation_lock(deadline),
            self._operation(),
            self._synchronize_semantic_fence(),
        ):
            self.start(deadline=deadline)
            plan, delta = self._synchronize_plan(revision, deadline=deadline)
            self._deliver_synchronize(plan)
            commit_error = self._commit_synchronize(plan)
            if commit_error is not None:
                self._recover_after_commit_failure(plan, commit_error)
            return delta

    def _check_synchronize_revision(
        self, revision: WorkspaceRevision, deadline: float
    ) -> None:
        if not isinstance(revision, WorkspaceRevision):
            raise TypeError("revision must be a WorkspaceRevision")
        if (
            revision.repository_id != self._repository.repository_id
            or revision.checkout_id != self._repository.checkout_id
        ):
            raise ValueError("workspace revision must describe this checkout")
        if time.monotonic() >= deadline:
            raise TimeoutError("Pyright synchronize deadline expired")

    def close(self, *, deadline: float) -> None:
        deadline = _validated_deadline(deadline)
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._close_lock.acquire(timeout=remaining):
            raise TimeoutError("Pyright close serialization deadline expired")
        try:
            self._close_owned(deadline)
        finally:
            self._close_lock.release()

    def _close_owned(self, deadline: float) -> None:
        while True:
            self._acquire_state_lock(
                deadline,
                "Pyright close state lock deadline expired",
            )
            try:
                cleanup_error = self._startup_cleanup_error
                startup_process = self._startup_process
                if (
                    self._closed
                    and self._process is None
                    and cleanup_error is None
                    and startup_process is None
                ):
                    return
                if not self._closing:
                    self._closing = True
                    self._condition.notify_all()
                if not self._starting and self._active_operations == 0:
                    process = self._process
                    break
            finally:
                self._lock.release()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Pyright operations did not finish before close")
            time.sleep(min(_LOCK_POLL_SECONDS, remaining))
        try:
            if cleanup_error is not None:
                cleanup_error.retry_cleanup(deadline)
            if startup_process is not None:
                startup_process.close(deadline)
            if process is not None:
                process.close(deadline)
        except BaseException as error:
            _raise_collected_errors([], prior_error=error)
        self._acquire_state_lock(
            deadline,
            "Pyright close final state lock deadline expired",
        )
        try:
            if self._startup_cleanup_error is cleanup_error:
                self._startup_cleanup_error = None
            if self._startup_process is startup_process:
                self._startup_process = None
            self._sync_startup_atexit_locked()
            if self._process is process:
                self._process = None
            self._bootstrap_owner_nonce = None
            self._readiness = "not_ready"
            self._readiness_evidence = ()
            self._position_encoding = None
            self._capabilities = {}
            self._documents.clear()
            self._document_bytes = 0
            self._readiness_target_uri = None
            self._workspace_revision = None
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
        finally:
            self._lock.release()


class _KeyLockState:
    __slots__ = ("lock", "reference_lock", "references")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reference_lock = threading.Lock()
        self.references = 0


class PyrightSessionManager:
    """Bound live Pyright sessions to four processes per owning MCP process."""

    def __init__(self, *, state_root: Path) -> None:
        if not isinstance(state_root, Path):
            raise TypeError("state_root must be a Path")
        self._state_root = state_root
        self._lock = threading.RLock()
        self._sessions: dict[tuple[str, PyrightIdentity], PyrightSession] = {}
        self._key_locks: dict[tuple[str, PyrightIdentity], _KeyLockState] = {}
        self._key_lock_releases: queue.SimpleQueue[
            tuple[tuple[str, PyrightIdentity], _KeyLockState]
        ] = queue.SimpleQueue()
        self._atexit_registered = False
        self._closed = False

    @staticmethod
    def _profile_key(
        repository: RepositoryScope,
        identity: PyrightIdentity,
    ) -> tuple[str, PyrightIdentity]:
        return repository.checkout_id, identity

    def _register_atexit_locked(self) -> None:
        if not self._atexit_registered:
            atexit.register(self._atexit_close_all)
            self._atexit_registered = True

    def _atexit_close_all(self) -> None:
        try:
            self.close_all(deadline=time.monotonic() + _OWNER_CLEANUP_SECONDS)
        except BaseException:
            pass

    def _acquire_manager(self, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._lock.acquire(timeout=remaining):
            raise TimeoutError("Pyright session manager lock deadline expired")

    @staticmethod
    def _acquire_key_lock(lock: threading.Lock, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not lock.acquire(timeout=remaining):
            raise TimeoutError("Pyright session key lock deadline expired")

    def _drain_key_lock_releases_locked(self) -> None:
        deferred: list[
            tuple[tuple[str, PyrightIdentity], _KeyLockState]
        ] = []
        while True:
            try:
                key, state = self._key_lock_releases.get_nowait()
            except queue.Empty:
                break
            if not state.reference_lock.acquire(blocking=False):
                deferred.append((key, state))
                continue
            try:
                state.references -= 1
                if state.references < 0:
                    raise RuntimeError(
                        "Pyright session key lock reference underflow"
                    )
                if state.references == 0 and self._key_locks.get(key) is state:
                    self._key_locks.pop(key, None)
            finally:
                state.reference_lock.release()
        for release in deferred:
            self._key_lock_releases.put(release)

    def _prune_key_locks_locked(self) -> None:
        self._drain_key_lock_releases_locked()
        for key, state in tuple(self._key_locks.items()):
            if not state.reference_lock.acquire(blocking=False):
                continue
            try:
                if state.references == 0 and self._key_locks.get(key) is state:
                    self._key_locks.pop(key, None)
            finally:
                state.reference_lock.release()

    def _retain_key_lock_locked(
        self,
        key: tuple[str, PyrightIdentity],
        deadline: float,
    ) -> _KeyLockState:
        self._prune_key_locks_locked()
        state = self._key_locks.get(key)
        if state is None:
            state = _KeyLockState()
            self._key_locks[key] = state
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not state.reference_lock.acquire(timeout=remaining):
            if state.references == 0 and self._key_locks.get(key) is state:
                self._key_locks.pop(key, None)
            raise TimeoutError(
                "Pyright session key lock reference deadline expired"
            )
        try:
            state.references += 1
        finally:
            state.reference_lock.release()
        return state

    def _release_key_lock_reference(
        self,
        key: tuple[str, PyrightIdentity],
        state: _KeyLockState,
    ) -> None:
        self._key_lock_releases.put((key, state))
        if not self._lock.acquire(blocking=False):
            return
        try:
            self._prune_key_locks_locked()
        finally:
            self._lock.release()

    def _wait_for_key_lock_releases(self, deadline: float) -> None:
        while True:
            self._acquire_manager(deadline)
            try:
                self._prune_key_locks_locked()
                if not self._key_locks and self._key_lock_releases.empty():
                    return
            finally:
                self._lock.release()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    "Pyright session key references did not release before deadline"
                )
            time.sleep(min(_LOCK_POLL_SECONDS, remaining))

    @staticmethod
    def _session_state(
        session: PyrightSession,
        deadline: float,
    ) -> tuple[bool, bool, bool, int, float]:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not session._lock.acquire(timeout=remaining):
            raise TimeoutError("Pyright session state lock deadline expired")
        try:
            return (
                session._closed,
                session._closing,
                session._starting,
                session._active_operations,
                session._last_used_monotonic,
            )
        finally:
            session._lock.release()

    def _live_entries_locked(
        self,
        deadline: float,
    ) -> list[tuple[tuple[str, PyrightIdentity], PyrightSession]]:
        live: list[tuple[tuple[str, PyrightIdentity], PyrightSession]] = []
        for key, session in tuple(self._sessions.items()):
            closed, _closing, _starting, _active, _last_used = self._session_state(
                session,
                deadline,
            )
            if closed:
                if self._sessions.get(key) is session:
                    self._sessions.pop(key, None)
                continue
            live.append((key, session))
        return live

    def _reserve_lru_idle_locked(
        self,
        live: list[tuple[tuple[str, PyrightIdentity], PyrightSession]],
        deadline: float,
    ) -> tuple[tuple[str, PyrightIdentity], PyrightSession] | None:
        idle: list[
            tuple[float, tuple[str, PyrightIdentity], PyrightSession]
        ] = []
        for key, session in live:
            closed, closing, starting, active, last_used = self._session_state(
                session,
                deadline,
            )
            if not closed and not closing and not starting and active == 0:
                idle.append((last_used, key, session))
        for _last_used, key, session in sorted(idle, key=lambda item: item[0]):
            if session._reserve_idle_close(deadline):
                return key, session
        return None

    @staticmethod
    def _wait_for_session_close(session: PyrightSession, deadline: float) -> None:
        while True:
            closed, closing, _starting, _active, _last_used = (
                PyrightSessionManager._session_state(session, deadline)
            )
            if closed or not closing:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Pyright session close wait deadline expired")
            time.sleep(min(_LOCK_POLL_SECONDS, remaining))

    def get(
        self,
        repository: RepositoryScope,
        *,
        deadline: float,
    ) -> PyrightSession:
        deadline = _validated_deadline(deadline)
        if not isinstance(repository, RepositoryScope):
            raise TypeError("repository must be a RepositoryScope")
        from pyright_profile import discover_pyright

        identity = discover_pyright(
            repository,
            state_root=self._state_root,
            deadline=deadline,
        )
        self._acquire_manager(deadline)
        try:
            if self._closed:
                raise RuntimeError("Pyright session manager is closed")
            if not identity.qualified:
                return PyrightSession(
                    repository,
                    identity,
                    state_root=self._state_root,
                )
            key = self._profile_key(repository, identity)
            key_lock_state = self._retain_key_lock_locked(key, deadline)
        finally:
            self._lock.release()

        key_lock_acquired = False
        try:
            self._acquire_key_lock(key_lock_state.lock, deadline)
            key_lock_acquired = True
            while True:
                wait_for: PyrightSession | None = None
                reserved: tuple[
                    tuple[str, PyrightIdentity], PyrightSession
                ] | None = None
                self._acquire_manager(deadline)
                try:
                    if self._closed:
                        raise RuntimeError("Pyright session manager is closed")
                    existing = self._sessions.get(key)
                    if existing is not None:
                        closed, closing, _starting, _active, _last_used = (
                            self._session_state(existing, deadline)
                        )
                        if closed:
                            if self._sessions.get(key) is existing:
                                self._sessions.pop(key, None)
                        elif closing:
                            wait_for = existing
                        else:
                            return existing
                    if wait_for is None:
                        live = self._live_entries_locked(deadline)
                        if len(live) < MAX_LSP_PROCESSES:
                            session = PyrightSession(
                                repository,
                                identity,
                                state_root=self._state_root,
                            )
                            self._sessions[key] = session
                            self._register_atexit_locked()
                            return session
                        reserved = self._reserve_lru_idle_locked(live, deadline)
                        if reserved is None:
                            denied = PyrightSession(
                                repository,
                                identity,
                                state_root=self._state_root,
                            )
                            denied._capacity_locked = True
                            return denied
                finally:
                    self._lock.release()

                if wait_for is not None:
                    self._wait_for_session_close(wait_for, deadline)
                    continue

                assert reserved is not None
                evicted_key, evicted = reserved
                evicted.close(deadline=deadline)

                self._acquire_manager(deadline)
                try:
                    closed, _closing, _starting, _active, _last_used = (
                        self._session_state(evicted, deadline)
                    )
                    if not closed:
                        raise RuntimeError(
                            "Pyright eviction close did not release the session"
                        )
                    if self._sessions.get(evicted_key) is evicted:
                        self._sessions.pop(evicted_key, None)
                    if self._closed:
                        raise RuntimeError("Pyright session manager is closed")
                    live = self._live_entries_locked(deadline)
                    if len(live) >= MAX_LSP_PROCESSES:
                        continue
                    session = PyrightSession(
                        repository,
                        identity,
                        state_root=self._state_root,
                    )
                    self._sessions[key] = session
                    self._register_atexit_locked()
                    return session
                finally:
                    self._lock.release()
        finally:
            if key_lock_acquired:
                key_lock_state.lock.release()
            self._release_key_lock_reference(key, key_lock_state)

    def close_all(self, *, deadline: float) -> None:
        deadline = _validated_deadline(deadline)
        self._acquire_manager(deadline)
        try:
            self._closed = True
            self._prune_key_locks_locked()
            sessions = tuple(self._sessions.items())
        finally:
            self._lock.release()

        errors: list[BaseException] = []
        for key, session in sessions:
            try:
                session.close(deadline=deadline)
            except BaseException as error:
                errors.append(error)
                continue

            try:
                self._acquire_manager(deadline)
                try:
                    closed, _closing, _starting, _active, _last_used = (
                        self._session_state(session, deadline)
                    )
                    if not closed:
                        raise RuntimeError(
                            "Pyright close_all close did not release the session"
                        )
                    if self._sessions.get(key) is session:
                        self._sessions.pop(key, None)
                finally:
                    self._lock.release()
            except BaseException as error:
                errors.append(error)

        try:
            self._wait_for_key_lock_releases(deadline)
        except BaseException as error:
            errors.append(error)
        _raise_collected_errors(errors)
