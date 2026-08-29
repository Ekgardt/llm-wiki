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
from interruption import (
    interruption_in_chain as _startup_interruption,
)
from interruption import (
    raise_collected_errors as _raise_collected_errors,
)
from lsp_identity import discover_managed_server
from lsp_paths import lsp_owner_root
from lsp_positions import (
    LspPosition,
    LspRange,
    SourceAnchor,
    SourceDocument,
    path_to_file_uri,
)
from lsp_process import GenerationLaunch, LspProcess, ProcessState, StartupCleanupError
from lsp_profiles import PYRIGHT_PROFILE
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
from lsp_server_profile import LanguageServerProfile, thaw_profile_value
from pyright_profile import (
    MAX_SERVER_BYTES,
    PYRIGHT_CONFIGURATION,
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


def _check_parent_directories(path: Path, message: str, *, deadline: float) -> None:
    """Every ancestor below the anchor has to be a real directory."""
    for parent in path.parents:
        if parent == Path(parent.anchor):
            return
        _require_startup_deadline(deadline)
        info = _path_identity(parent)
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(message)


def _check_local_path_shape(value: Path, label: str) -> None:
    """An absolute local path: no UNC prefix, no NUL, no parent traversal."""
    raw = os.fspath(value)
    if not value.is_absolute() or "\0" in raw:
        raise ValueError(f"{label} must be an absolute local path")
    if raw.startswith(("\\\\", "//")) or ".." in value.parts:
        raise ValueError(f"{label} must be an absolute local path")


def _validated_local_file(value: object, label: str, *, deadline: float) -> Path:
    _require_startup_deadline(deadline)
    if not isinstance(value, Path):
        raise TypeError(f"{label} must be a Path")
    _check_local_path_shape(value, label)
    _check_parent_directories(
        value, f"{label} parent must be a directory", deadline=deadline
    )
    _require_startup_deadline(deadline)
    info = _path_identity(value)
    if not stat.S_ISREG(info.st_mode) or _known_network_path(value):
        raise ValueError(f"{label} must be a local regular file")
    _require_startup_deadline(deadline)
    return value


def _ensure_owned_directory(
    parent: Path, name: str, message: str, *, deadline: float
) -> Path:
    """The named child directory, created owner-only when it is missing."""
    child = parent / name
    _require_startup_deadline(deadline)
    try:
        child.mkdir(mode=0o700)
    except FileExistsError:
        pass
    _require_startup_deadline(deadline)
    info = _path_identity(child)
    if not stat.S_ISDIR(info.st_mode):
        raise PermissionError(message)
    return child


def _ensure_lsp_parent(state_root: Path, *, deadline: float) -> Path:
    _require_startup_deadline(deadline)
    if not state_root.is_absolute() or _known_network_path(state_root):
        raise ValueError("state_root must be an absolute local path")
    _check_parent_directories(
        state_root, "state_root parent must be a directory", deadline=deadline
    )
    _require_startup_deadline(deadline)
    root_info = _path_identity(state_root)
    if not stat.S_ISDIR(root_info.st_mode):
        raise NotADirectoryError(state_root)
    run_root = _ensure_owned_directory(
        state_root, "run", "LSP run parent must be a directory", deadline=deadline
    )
    parent = _ensure_owned_directory(
        run_root, "lsp", "LSP owner parent must be a directory", deadline=deadline
    )
    _require_startup_deadline(deadline)
    _restrict_owner_only(parent, 0o700)
    _require_startup_deadline(deadline)
    _verify_owner_only(parent, 0o700)
    _require_startup_deadline(deadline)
    return parent


def _provider_supported(value: object, label: str, prefix: str) -> bool:
    if value is None or value is False:
        return False
    if value is True or isinstance(value, dict):
        return True
    raise _BootstrapDegradation(f"{prefix}_{label}_capability_invalid")


_POSITION_ENCODINGS = MappingProxyType(
    {
        "utf-8": PositionEncoding.UTF8,
        "utf-16": PositionEncoding.UTF16,
        "utf-32": PositionEncoding.UTF32,
    }
)


def _server_position_encoding(
    server: Mapping[str, object], prefix: str
) -> PositionEncoding:
    """The encoding the server asked for, from the three we can serve."""
    value = server.get("positionEncoding", "utf-16")
    if not isinstance(value, str) or value not in _POSITION_ENCODINGS:
        raise _BootstrapDegradation(f"{prefix}_position_encoding_unsupported")
    return _POSITION_ENCODINGS[value]


def _parse_server_capabilities(
    result: object,
    prefix: str = "pyright",
) -> tuple[dict[str, bool], PositionEncoding]:
    if not isinstance(result, dict) or not isinstance(result.get("capabilities"), dict):
        raise _BootstrapDegradation(f"{prefix}_initialize_result_invalid")
    server = result["capabilities"]
    encoding = _server_position_encoding(server, prefix)
    capabilities = {
        name: _provider_supported(server.get(field), name, prefix)
        for name, field in _CAPABILITY_FIELDS.items()
    }
    capabilities["diagnostics"] = True
    return dict(sorted(capabilities.items())), encoding


def _permission_startup_code(error: PermissionError, prefix: str) -> str | None:
    """A refused permission that is really a timeout in disguise."""
    cause = error.__cause__
    if cause is not None and cause.__class__.__name__ == "TimeoutExpired":
        return f"{prefix}_startup_timeout"
    if "deadline expired" in str(error):
        return f"{prefix}_startup_timeout"
    return None


def _timeout_startup_code(error: BaseException, prefix: str) -> str | None:
    """A timeout, or a permission refusal that is really one."""
    if isinstance(error, TimeoutError):
        return f"{prefix}_startup_timeout"
    if isinstance(error, PermissionError):
        return _permission_startup_code(error, prefix)
    return None


def _startup_code_of(error: BaseException, prefix: str) -> str | None:
    """The code this one error names, if it names one."""
    if isinstance(error, _BootstrapDegradation):
        return error.code
    return _timeout_startup_code(error, prefix)


def _startup_code(error: BaseException, prefix: str = "pyright") -> str:
    current: BaseException | None = error
    while current is not None:
        code = _startup_code_of(current, prefix)
        if code is not None:
            return code
        current = current.__cause__
    return f"{prefix}_startup_failed"






















def _lsp_coordinate(value: object) -> int | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= _LSP_UINTEGER_MAX
    ):
        return None
    return value


def _lsp_position(value: object) -> LspPosition | None:
    """One protocol position, when both coordinates are usable."""
    if not isinstance(value, dict):
        return None
    line = _lsp_coordinate(value.get("line"))
    character = _lsp_coordinate(value.get("character"))
    if line is None or character is None:
        return None
    return LspPosition(line, character)


def _ordered_lsp_range(
    start: LspPosition | None, end: LspPosition | None
) -> LspRange | None:
    """A range only exists when both ends read and the end is not before the start."""
    if start is None or end is None:
        return None
    if (end.line, end.character) < (start.line, start.character):
        return None
    return LspRange(start, end)


def _lsp_range(value: object) -> LspRange | None:
    if not isinstance(value, dict):
        return None
    return _ordered_lsp_range(
        _lsp_position(value.get("start")), _lsp_position(value.get("end"))
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


def _hover_fragment_labelled(value: Mapping[str, object], text: str) -> str | None:
    """A fragment is usable when its kind or its language names something real."""
    if "kind" in value:
        if value.get("kind") in {"plaintext", "markdown"}:
            return text
        return None
    language = value.get("language")
    if not isinstance(language, str) or not language:
        return None
    return text


def _hover_fragment_mapping(value: Mapping[str, object]) -> str | None:
    text = _bounded_hover_string(value.get("value"))
    if text is None:
        return None
    return _hover_fragment_labelled(value, text)


def _hover_fragment(value: object) -> str | None:
    if isinstance(value, str):
        return _bounded_hover_string(value)
    if not isinstance(value, dict):
        return None
    return _hover_fragment_mapping(value)


def _hover_fragments(items: list[object]) -> tuple[list[str], bool]:
    """Every readable fragment, and whether any had to be dropped."""
    fragments: list[str] = []
    partial = False
    for item in items:
        fragment = _hover_fragment(item)
        if fragment is None:
            partial = True
            continue
        fragments.append(fragment)
    return fragments, partial


def _bounded_join(fragments: list[str], partial: bool) -> tuple[str | None, bool]:
    joined = "\n\n".join(fragments)
    if _bounded_hover_string(joined) is None:
        return None, True
    return joined, partial


def _joined_hover_contents(value: list[object]) -> tuple[str | None, bool]:
    """The list form of hover contents, joined and bounded."""
    if len(value) > 1024:
        return None, True
    fragments, partial = _hover_fragments(value)
    if not fragments:
        return None, True
    return _bounded_join(fragments, partial)


def _hover_contents(value: object) -> tuple[str | None, bool]:
    if isinstance(value, list):
        return _joined_hover_contents(value)
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


_DIAGNOSTIC_SEVERITIES = frozenset({1, 2, 3, 4})


def _known_severity(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return value in _DIAGNOSTIC_SEVERITIES


def _diagnostic_severity(value: object) -> tuple[int | None, bool]:
    """The severity, and whether the field was readable at all."""
    if value is None:
        return None, True
    if not _known_severity(value):
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
    _push_bounded_children(walk, children)


def _push_bounded_children(walk: _SymbolWalk, children: list[object]) -> None:
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
    return (
        _bounded_optional_text(value.get("detail"))
        and _bounded_optional_text(value.get("containerName"))
        and _symbol_flags_ok(value)
    )


def _contained_selection(
    range_: LspRange | None, selection: LspRange | None, uri: str
) -> LspLocation | None:
    """The selection range, but only when the symbol's own range contains it."""
    if range_ is None or selection is None:
        return None
    if not _range_contains(range_, selection):
        return None
    return LspLocation(uri, selection)


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
class _DocumentQuery:
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


_STARTUP_DEGRADING_ERRORS = (
    JsonRpcResponseError,
    OSError,
    ProtocolViolation,
    RuntimeError,
    TimeoutError,
)

# `$/progress` is the specification's; the rest a profile declares for itself.
_NEUTRAL_PROGRESS_METHOD = "$/progress"

# How long a query waits between checks for the work-done-progress `end` that a
# progress-gated profile makes readiness out of. Short because the whole gate is
# measured at 0.67-0.82 s on a small project and the caller holds a deadline.
_PROGRESS_POLL_SECONDS = 0.02

# At most this many outstanding work-done tokens are remembered per generation.
# A server that opens more than this without ending them is not one we can gate
# on, and the bound keeps a misbehaving one from growing the session.
_MAX_WORK_DONE_TOKENS = 64


@dataclass
class _StartupAttempt:
    """The owner nonce one startup attempt published, if it got that far."""

    owner_nonce: str | None = None


def _cleanup_interruption(cleanup_error: BaseException | None):
    """The interruption travelling inside a cleanup failure, if there is one."""
    if cleanup_error is None:
        return None
    return _startup_interruption(cleanup_error)


def _reraise_startup_interruption(
    error: BaseException, interruption: BaseException
) -> None:
    """An interruption always propagates; a wrapped one keeps its context."""
    if interruption is error:
        raise error
    _raise_collected_errors([], prior_error=error)


def _progress_handler(
    session: "LanguageServerSession", method: str
) -> Callable[[object], None]:
    """One notification handler bound to the progress method it reports."""
    return lambda params: session._progress(method, params)


@dataclass(frozen=True)
class _WorkspaceState:
    """The session state a workspace-wide query was admitted against."""

    process: LspProcess | None
    generation: str | None
    readiness: str
    supported: bool
    initialized: bool
    current: bool


@dataclass(frozen=True)
class _WorkspaceQuery:
    """What one workspace-wide query is bound to."""

    process: LspProcess
    generation: str
    readiness: str
    epoch: int


def _location_payload(result: object) -> list[object] | None:
    """The list of raw locations a provider returned, in either shape."""
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        return [result]
    return None


def _call_item_range_value(value: Mapping[str, object]) -> object:
    """A call item names its selection range, or falls back to its range."""
    if _lsp_range(value.get("selectionRange")) is not None:
        return value.get("selectionRange")
    return value.get("range")


def _location_fields(value: Mapping[str, object]) -> tuple[object, object] | None:
    """The URI and range of a location, in either of the protocol's two shapes."""
    if "targetUri" not in value and "targetSelectionRange" not in value:
        return value.get("uri"), value.get("range")
    if _lsp_range(value.get("targetRange")) is None:
        return None
    return value.get("targetUri"), value.get("targetSelectionRange")


def _check_qualified_identity(
    identity: PyrightIdentity, profile: LanguageServerProfile = PYRIGHT_PROFILE
) -> None:
    """A qualified identity has to be internally consistent before it is used."""
    if identity.status != "qualified" or identity.degradation_codes:
        raise ValueError("qualified language server identity is internally inconsistent")
    if identity.initialization_options_sha256 != _profile_options_digest(profile):
        raise ValueError("language server initialization options identity is inconsistent")
    _check_identity_digest(
        identity.configuration_sha256, "Pyright configuration identity is invalid"
    )
    _check_identity_digest(
        identity.executable_sha256, "Pyright executable identity is invalid"
    )


def _profile_options_digest(profile: LanguageServerProfile) -> str:
    """Pyright keeps its precomputed constant; anything else derives its own."""
    if profile is PYRIGHT_PROFILE:
        return PYRIGHT_INITIALIZATION_OPTIONS_SHA256
    from lsp_identity import profile_initialization_options_sha256

    return profile_initialization_options_sha256(profile)


def _check_identity_digest(value: object, message: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(message)


def _workspace_query_ready(state: _WorkspaceState) -> bool:
    """The session holds a current, initialized generation to query."""
    if state.process is None or state.generation is None:
        return False
    return state.initialized and state.current


def _workspace_query_status(state: _WorkspaceState) -> str | None:
    """The status refusing a workspace query, or None when it may proceed."""
    if not _workspace_query_ready(state):
        return "not_ready"
    if not state.supported:
        return "unsupported"
    return None if state.readiness == "query_ready" else "not_ready"


def _check_symbol_query(query: str) -> None:
    if not isinstance(query, str):
        raise TypeError("query must be a string")
    if len(query.encode("utf-8", errors="strict")) > 4096:
        raise ValueError("query exceeds 4096 bytes")


def _symbol_locations_payload(result: list[object]) -> tuple[list[object], bool]:
    """Every well-formed symbol's location, and whether any had to be dropped."""
    values: list[object] = []
    partial = len(result) > MAX_LOCATIONS
    for symbol in result[:MAX_LOCATIONS]:
        if not isinstance(symbol, dict) or "location" not in symbol:
            partial = True
            continue
        values.append(symbol["location"])
    return values, partial


def _hover_range(result: Mapping[str, object]) -> tuple[LspRange | None, bool]:
    """The hover's range, and whether it was readable."""
    if "range" not in result:
        return None, True
    range_ = _lsp_range(result["range"])
    return range_, range_ is not None


def _hover_response(result: object) -> ProviderHover:
    """The hover the server reported, or an explicitly partial answer."""
    if result is None:
        return ProviderHover(None, None, False)
    if not isinstance(result, dict) or "contents" not in result:
        return ProviderHover(None, None, True)
    contents, partial = _hover_contents(result["contents"])
    range_, range_ok = _hover_range(result)
    return ProviderHover(contents, range_, partial or not range_ok)


def _position_params(
    query: _DocumentQuery, position: LspPosition
) -> dict[str, object]:
    """The textDocument/position pair every anchored request sends."""
    return {
        "textDocument": {"uri": query.document.source.uri},
        "position": {
            "line": position.line,
            "character": position.character,
        },
    }


def _diagnostic_classification(
    value: Mapping[str, object],
) -> tuple[int | None, str | None] | None:
    """Severity and code together, or None when either is present but unreadable."""
    severity, severity_ok = _diagnostic_severity(value.get("severity"))
    if not severity_ok:
        return None
    code, code_ok = _diagnostic_code(value.get("code"))
    if not code_ok:
        return None
    return severity, code


def _related_message(
    location: LspLocation, raw_message: object
) -> tuple[LspLocation, str | None] | None:
    """A related entry keeps its location; an unreadable message drops the entry."""
    if raw_message is None:
        return location, None
    message = _bounded_text(raw_message, _MAX_DIAGNOSTIC_TEXT_BYTES)
    if message is None:
        return None
    return location, message


def _versioned_diagnostic_target(
    uri: str, values: list[object], version_value: object
) -> tuple[str, list[object], int | None] | None:
    version, version_ok = _published_version(version_value)
    if not version_ok:
        return None
    return uri, values, version


def _published_diagnostics_params(
    params: object,
) -> tuple[str, list[object], object] | None:
    """The URI, diagnostics and version of a publish notification, when readable."""
    if not isinstance(params, dict):
        return None
    uri_value = params.get("uri")
    diagnostics_value = params.get("diagnostics")
    if not isinstance(uri_value, str) or not isinstance(diagnostics_value, list):
        return None
    return uri_value, diagnostics_value, params.get("version")


def _published_version(value: object) -> tuple[int | None, bool]:
    """The document version, and whether the field was readable at all."""
    if value is None:
        return None, True
    version = _lsp_coordinate(value)
    return version, version is not None


def _usable_kind(kind: int | None) -> bool:
    """A symbol kind the protocol actually assigns."""
    return kind is not None and kind != 0


def _call_item_ranges(
    value: Mapping[str, object],
) -> tuple[LspRange, LspRange] | None:
    """The item's range and selection range, when the selection sits inside it."""
    range_ = _lsp_range(value.get("range"))
    selection = _lsp_range(value.get("selectionRange"))
    if range_ is None or selection is None:
        return None
    if not _range_contains(range_, selection):
        return None
    return range_, selection


def _call_item_core(
    value: Mapping[str, object],
) -> tuple[str, int, str, LspRange, LspRange] | None:
    """The required fields of a call item, when all are present and consistent."""
    name = _bounded_text(value.get("name"), _MAX_CALL_ITEM_TEXT_BYTES)
    kind = _lsp_coordinate(value.get("kind"))
    uri = value.get("uri")
    if not name or not _usable_kind(kind) or not isinstance(uri, str):
        return None
    ranges = _call_item_ranges(value)
    if ranges is None:
        return None
    return name, kind, uri, ranges[0], ranges[1]


def _call_item_detail(
    value: Mapping[str, object], item: dict[str, object]
) -> bool:
    """Copy the optional detail; False when it is present but unusable."""
    detail = value.get("detail")
    if detail is None:
        return True
    bounded = _bounded_text(detail, _MAX_CALL_ITEM_TEXT_BYTES)
    if bounded is None:
        return False
    item["detail"] = bounded
    return True


def _call_item_tags(value: Mapping[str, object], item: dict[str, object]) -> bool:
    """Copy the optional tags; False when they are present but unusable."""
    tags = value.get("tags")
    if tags is None:
        return True
    if not _symbol_tags_ok(tags):
        return False
    item["tags"] = list(tags)
    return True


def _call_item_optionals(
    value: Mapping[str, object], item: dict[str, object]
) -> bool:
    """Copy detail, tags and opaque data; False when one is present but unusable."""
    if not _call_item_detail(value, item):
        return False
    if not _call_item_tags(value, item):
        return False
    return _call_item_data(value, item)


def _call_item_data(value: Mapping[str, object], item: dict[str, object]) -> bool:
    """The opaque round-trip field, copied through untouched when present."""
    if "data" in value:
        item["data"] = value["data"]
    return True


def _progress_token(value: object) -> str | int | None:
    """The progress token, bounded when textual; None when it is unusable."""
    if isinstance(value, str):
        return _bounded_text(value, 256)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _progress_text(value: Mapping[str, object], kind: str) -> tuple[str | None, bool]:
    """The progress text, and whether the field was readable at all."""
    raw = value.get("title") if kind == "begin" else value.get("message")
    if raw is None:
        return "", True
    text = _bounded_text(raw, _MAX_PROGRESS_TEXT_BYTES)
    return text, text is not None


def _progress_payload(params: object) -> tuple[object, Mapping[str, object]] | None:
    """The token and value of a `$/progress` payload, when it is well formed."""
    if not isinstance(params, dict) or set(params) != {"token", "value"}:
        return None
    value = params["value"]
    if not isinstance(value, dict):
        return None
    return params["token"], value


def _progress_record(method: str, params: object) -> tuple[object, ...] | None:
    """The `$/progress` record to retain, or None when the payload is unusable."""
    payload = _progress_payload(params)
    if payload is None:
        return None
    raw_token, value = payload
    token = _progress_token(raw_token)
    kind = value.get("kind")
    if token is None or kind not in {"begin", "report", "end"}:
        return None
    return _progress_text_record(method, token, kind, value)


def _progress_text_record(
    method: str, token: object, kind: object, value: Mapping[str, object]
) -> tuple[object, ...] | None:
    text, readable = _progress_text(value, kind)
    if not readable:
        return None
    return method, token, kind, text


def _work_done_end_token(params: object) -> object | None:
    """The token a `$/progress` `end` closes, when the payload is well formed."""
    payload = _progress_payload(params)
    if payload is None:
        return None
    raw_token, value = payload
    if value.get("kind") != "end":
        return None
    return _progress_token(raw_token)


def _pyright_marker_record(method: str, params: object) -> tuple[object, ...] | None:
    """A begin/end marker carries no payload of its own."""
    if params is None or params == {}:
        return (method,)
    return None


def _pyright_report_record(method: str, params: object) -> tuple[object, ...] | None:
    """A progress report carries one bounded message."""
    value = params.get("message") if isinstance(params, dict) else params
    text = _bounded_text(value, _MAX_PROGRESS_TEXT_BYTES)
    if text is None:
        return None
    return method, text


_PROGRESS_RECORDS = MappingProxyType(
    {
        "$/progress": _progress_record,
        "pyright/beginProgress": _pyright_marker_record,
        "pyright/endProgress": _pyright_marker_record,
        "pyright/reportProgress": _pyright_report_record,
    }
)


def _progress_notification_record(
    method: str, params: object
) -> tuple[object, ...] | None:
    """What to retain for this progress notification, if anything."""
    build = _PROGRESS_RECORDS.get(method)
    if build is None:
        return None
    return build(method, params)


def _configuration_items(params: object) -> list[object] | None:
    """The configuration items requested, when the request is well formed."""
    if not isinstance(params, dict) or set(params) - {"items"}:
        return None
    items = params.get("items")
    if not isinstance(items, list) or len(items) > _MAX_CONFIGURATION_ITEMS:
        return None
    return items


def _usable_scope_uri(scope_uri: object) -> bool:
    """An absent scope is fine; a present one has to be bounded text."""
    if scope_uri is None:
        return True
    if not isinstance(scope_uri, str):
        return False
    return len(scope_uri.encode("utf-8", errors="strict")) <= 16 * 1024


def _usable_section(section: object) -> bool:
    """A section name has to be non-empty, encodable and bounded."""
    if not isinstance(section, str) or not section:
        return False
    try:
        size = len(section.encode("utf-8", errors="strict"))
    except UnicodeEncodeError:
        return False
    return size <= _MAX_CONFIGURATION_SECTION_BYTES


def _configuration_item_ok(item: object) -> bool:
    """The item names only fields we accept, and carries a usable scope."""
    if not isinstance(item, dict) or set(item) - {"scopeUri", "section"}:
        return False
    return _usable_scope_uri(item.get("scopeUri"))


def _configuration_section(item: object) -> tuple[str | None, bool]:
    """The section requested, and whether the item was usable at all."""
    if not _configuration_item_ok(item):
        return None, False
    assert isinstance(item, dict)
    return _usable_configuration_section(item.get("section"))


def _usable_configuration_section(section: object) -> tuple[str | None, bool]:
    """An absent section is usable; a present one has to be bounded text."""
    if section is None:
        return None, True
    if not _usable_section(section):
        return None, False
    return section, True


def _configuration_value(settings: Mapping[str, object], section: str) -> object:
    """The setting the dotted section names, or None when it is absent."""
    current: object = settings
    for component in section.split("."):
        if not component or not isinstance(current, dict):
            return None
        current = current.get(component)
    return current


def _configuration_result(settings: Mapping[str, object], item: object) -> object:
    """What to answer for one requested configuration item."""
    section, usable = _configuration_section(item)
    if not usable:
        return None
    if section is None:
        return thaw_pyright_profile_value(PYRIGHT_CONFIGURATION)
    return _configuration_value(settings, section)


@dataclass(frozen=True)
class _CloseTargets:
    """What a close has to shut down, captured under the state lock."""

    cleanup_error: StartupCleanupError | None
    startup_process: LspProcess | None
    process: LspProcess | None


def _check_document_unchanged(
    current: OpenDocument,
    source: RepositorySource,
    content: bytes,
    digest: str,
) -> None:
    """An open document must not change behind the session's back."""
    if (
        current.source != source
        or current.content != content
        or current.source_sha256 != digest
    ):
        raise RuntimeError(
            "open Pyright document changed without synchronization"
        )


def _check_did_open_encodable(params: dict[str, object]) -> None:
    """Refuse a document the wire could never carry."""
    try:
        encode_frame(
            {
                "jsonrpc": "2.0",
                "method": "textDocument/didOpen",
                "params": params,
            }
        )
    except ProtocolViolation as error:
        raise ValueError(
            "Pyright source document exceeds the LSP frame"
        ) from error


def _matching_diagnostics(
    snapshot: _DiagnosticSnapshot | None, version: int
) -> ProviderDiagnostics | None:
    """The snapshot, when it describes exactly this document version."""
    if snapshot is None or snapshot.document_version != version:
        return None
    return ProviderDiagnostics(
        snapshot.diagnostics,
        snapshot.document_version,
        snapshot.partial,
    )


def _expired_diagnostics(
    snapshot: _DiagnosticSnapshot | None,
) -> ProviderDiagnostics:
    """What to answer when the wait ran out: a versionless snapshot, or nothing."""
    if snapshot is not None and snapshot.document_version is None:
        return ProviderDiagnostics(snapshot.diagnostics, None, True)
    return ProviderDiagnostics((), None, True)


_PROTOCOL_EVIDENCE = ("initialize", "initialized", "configuration")
_DID_OPEN_EVIDENCE = (*_PROTOCOL_EVIDENCE, "didOpen")
_QUERY_READY_EVIDENCE = (*_DID_OPEN_EVIDENCE, "documentSymbol")


def _released_error(release: Callable[[], object]) -> BaseException | None:
    """Run one release step; the error it raised, if it raised one."""
    try:
        release()
    except BaseException as error:
        return error
    return None


def _unlink_error(path: Path | None) -> BaseException | None:
    """Remove a file that may already be gone; the error worth reporting."""
    if path is None:
        return None
    try:
        os.unlink(path)
    except FileNotFoundError:
        return None
    except BaseException as error:
        return error
    return None


def _closed_descriptor_error(descriptor: int | None) -> BaseException | None:
    if descriptor is None:
        return None
    return _released_error(lambda: os.close(descriptor))


class _LaunchServerGuard:
    def __init__(
        self,
        path: Path,
        expected_sha256: str,
        *,
        command: tuple[str, ...],
        owner_root: Path,
        deadline: float,
        degradation_prefix: str = "pyright",
    ) -> None:
        if not isinstance(owner_root, Path):
            raise TypeError("owner_root must be a Path")
        self._degradation_prefix = degradation_prefix
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

    def _digest_mismatch(self) -> _BootstrapDegradation:
        """One refusal, named for whichever server this guard is launching."""
        return _BootstrapDegradation(
            f"{self._degradation_prefix}_executable_digest_mismatch"
        )

    def _open_source_descriptor(self) -> int:
        """Open the server file exclusively, the way this platform allows."""
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        if os.name != "nt":
            return os.open(
                self._path,
                flags | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
        import msvcrt

        handle = _windows_workspace.open_exclusive_readonly_source_file(self._path)
        try:
            return msvcrt.open_osfhandle(handle, flags)
        except BaseException:
            _windows_workspace.close_handle(handle)
            raise

    def _check_unchanged(self, before: os.stat_result, descriptor: int) -> None:
        """What we opened has to be the same regular file we just measured."""
        opened = os.fstat(descriptor)
        state = _launch_file_state(opened)
        if _launch_file_state(before) != state or not stat.S_ISREG(opened.st_mode):
            raise self._digest_mismatch()
        self._state = state

    def _open_snapshot(self) -> BinaryIO:
        """A private copy of the server, under our own owner root."""
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
        return snapshot

    def _check_snapshot_launchable(
        self,
        launch_info: os.stat_result,
        snapshot_info: os.stat_result,
        before: os.stat_result,
    ) -> None:
        """The descriptor we will launch has to name our verified copy."""
        launchable = (
            stat.S_ISREG(launch_info.st_mode)
            and launch_info.st_size == before.st_size
            and _launch_file_state(launch_info) == _launch_file_state(snapshot_info)
        )
        if not launchable:
            raise self._digest_mismatch()

    def _posix_launch(self, before: os.stat_result) -> GenerationLaunch:
        """Copy the server aside, verify it, and launch from the copy."""
        snapshot = self._open_snapshot()
        self._verify_digest(self._copy_snapshot(snapshot))
        launch_descriptor = os.open(
            self._snapshot_path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        self._launch_descriptor = launch_descriptor
        self._check_snapshot_launchable(
            os.fstat(launch_descriptor), os.fstat(snapshot.fileno()), before
        )
        os.unlink(self._snapshot_path)
        self._snapshot_path = None
        snapshot.close()
        self._snapshot = None
        return GenerationLaunch(
            self._posix_launch_command(launch_descriptor),
            (launch_descriptor,),
        )

    def __enter__(self) -> "_LaunchServerGuard | GenerationLaunch":
        _require_startup_deadline(self._deadline)
        before = _path_identity(self._path)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_SERVER_BYTES:
            raise self._digest_mismatch()
        _require_startup_deadline(self._deadline)
        self._descriptor = self._open_source_descriptor()
        try:
            _require_startup_deadline(self._deadline)
            self._check_unchanged(before, self._descriptor)
            if os.name == "posix":
                return self._posix_launch(before)
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
                raise self._digest_mismatch()
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
                raise self._digest_mismatch()
            digest.update(chunk)
        return digest.hexdigest()

    def verify(self) -> None:
        _require_startup_deadline(self._deadline)
        descriptor = self._descriptor
        state = self._state
        if descriptor is None or state is None:
            raise RuntimeError("Pyright launch server guard is not open")
        if _launch_file_state(os.fstat(descriptor)) != state:
            raise self._digest_mismatch()
        actual = self._digest()
        self._verify_digest(actual)

    def _verify_digest(self, actual: str) -> None:
        descriptor = self._descriptor
        state = self._state
        if descriptor is None or state is None:
            raise RuntimeError("launch server guard is not open")
        _require_startup_deadline(self._deadline)
        if actual != self._expected_sha256:
            raise self._digest_mismatch()
        self._require_unchanged_state(descriptor, state)
        _require_startup_deadline(self._deadline)

    def _require_unchanged_state(self, descriptor: int, state: tuple) -> None:
        """The open descriptor and the path still name the file we digested."""
        observed = (
            _launch_file_state(os.fstat(descriptor)),
            _launch_file_state(_path_identity(self._path)),
        )
        if observed != (state, state):
            raise self._digest_mismatch()

    def _take_owned_resources(self) -> tuple[object, Path | None, int | None, int | None]:
        """Hand over everything the guard holds, leaving it holding nothing."""
        snapshot, self._snapshot = self._snapshot, None
        snapshot_path, self._snapshot_path = self._snapshot_path, None
        launch_descriptor, self._launch_descriptor = self._launch_descriptor, None
        descriptor, self._descriptor = self._descriptor, None
        return snapshot, snapshot_path, launch_descriptor, descriptor

    def close(self) -> None:
        snapshot, snapshot_path, launch_descriptor, descriptor = (
            self._take_owned_resources()
        )
        errors = [
            _released_error(snapshot.close) if snapshot is not None else None,
            _unlink_error(snapshot_path),
            _closed_descriptor_error(launch_descriptor),
            _closed_descriptor_error(descriptor),
        ]
        _raise_collected_errors([item for item in errors if item is not None])

    def _verified_operation_error(
        self, error_type: object, error_info: tuple[object, ...]
    ) -> BaseException | None:
        """The error the block carried, or the one verification found."""
        for item in error_info:
            if isinstance(item, BaseException):
                return item
        if error_type is not None:
            return None
        try:
            self.verify()
        except BaseException as error:
            return error
        return None

    def __exit__(self, error_type: object, *error_info: object) -> None:
        operation_error = self._verified_operation_error(error_type, error_info)
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


def _snapshot_supersedes(
    existing: _DiagnosticSnapshot | None, version: int | None
) -> bool:
    """The snapshot already describes a newer version than the update does."""
    if existing is None or existing.document_version is None:
        return False
    if version is None:
        return True
    return version < existing.document_version


def _require_session_arguments(
    repository: object,
    identity: object,
    state_root: object,
    profile: object,
) -> None:
    """Everything a session is built from, checked before anything is retained."""
    for value, expected, label in (
        (repository, RepositoryScope, "repository"),
        (identity, PyrightIdentity, "identity"),
        (state_root, Path, "state_root"),
        (profile, LanguageServerProfile, "profile"),
    ):
        if not isinstance(value, expected):
            raise TypeError(f"{label} must be a {expected.__name__}")


class LanguageServerSession:
    """Own one repository-scoped language-server protocol lifecycle.

    One class, one `profile` field -- not a base class with a subclass per
    language. Measurement (`docs/research/2026-08-28-precise-navigation-beyond-python.md`)
    put 171 Pyright-mentioning lines inside 5,028: the other 4,857 are one
    implementation of containment, leases, generations and wire state, and a
    base/subclass split would invite a second override of exactly those.
    Everything language-shaped is read from `self._profile`.

    `PyrightSession` remains as an alias below, because that is what the
    existing Pyright tests and `code_navigation` type-check against.
    """

    def __init__(
        self,
        repository: RepositoryScope,
        identity: PyrightIdentity,
        *,
        state_root: Path,
        profile: LanguageServerProfile = PYRIGHT_PROFILE,
    ) -> None:
        _require_session_arguments(repository, identity, state_root, profile)
        self._repository = repository
        self._identity = identity
        self._profile = profile
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
        # The work-done tokens the server asked us to create, and the one
        # generation whose project load has been declared finished. Only the
        # profiles that gate on progress consult these.
        self._work_done_tokens: set[object] = set()
        self._progress_ready_generation: str | None = None
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
    def profile(self) -> LanguageServerProfile:
        """The pinned managed server this session drives."""
        return self._profile

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
        """The exit hook is registered exactly while something is retained."""
        retained = (
            self._startup_cleanup_error is not None
            or self._startup_process is not None
        )
        if retained == self._startup_atexit_registered:
            return
        if retained:
            atexit.register(self._atexit_cleanup)
        else:
            atexit.unregister(self._atexit_cleanup)
        self._startup_atexit_registered = retained

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
            if not self._is_idle_locked():
                return False
            self._closing = True
            self._condition.notify_all()
            return True
        finally:
            self._lock.release()

    def _is_idle_locked(self) -> bool:
        """Nothing is using this session and nothing is starting or stopping it."""
        if self._closed or self._closing or self._starting:
            return False
        return self._active_operations == 0

    def _configuration(self, params: object) -> object:
        settings = self._profile.wire_configuration()
        assert isinstance(settings, dict)
        items = _configuration_items(params)
        if items is None:
            return []
        return [_configuration_result(settings, item) for item in items]

    def _work_done_progress_create(self, params: object) -> None:
        """Answer the create request, and remember the token it names.

        Answering is what makes the server send `$/progress` at all -- the
        specification only permits server-initiated progress once the client
        declares `window.workDoneProgress` *and* replies here. Remembering the
        token is what lets a progress-gated profile tell its project load from
        any other progress the server may report.
        """
        self._benign_server_request(params)
        assert isinstance(params, dict)
        token = _progress_token(params.get("token"))
        self._retain_work_done_token(token)

    def _retain_work_done_token(self, token: object) -> None:
        if token is None:
            return
        with self._lock:
            if len(self._work_done_tokens) < _MAX_WORK_DONE_TOKENS:
                self._work_done_tokens.add(token)

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

    def _rearm_progress_gate(self) -> None:
        """A new server process must load the project again before answering."""
        with self._lock:
            self._work_done_tokens.clear()
            self._progress_ready_generation = None

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
        claimed = self._claim_did_open(key, generation_nonce, deadline)
        if claimed is not None:
            return claimed
        sent = False
        try:
            sent = notify() is not False
        except BaseException:
            self._record_did_open_failure(key, generation_nonce)
            raise
        finally:
            retained = self._release_did_open(key, generation_nonce, sent=sent)
        return retained

    def _await_did_open_gate_locked(self, key: tuple, deadline: float) -> None:
        """Wait out another sender of the same key; caller holds the condition."""
        while key in self._wire_sending:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._wire_condition.wait(remaining):
                raise TimeoutError("Pyright didOpen send gate deadline expired")

    def _claim_did_open(
        self, key: tuple, generation_nonce: str, deadline: float
    ) -> bool | None:
        """The settled answer for this key, or None when this call must send."""
        with self._wire_condition:
            self._await_did_open_gate_locked(key, deadline)
            settled = self._settled_did_open_locked(key, generation_nonce)
            if settled is None:
                self._wire_sending.add(key)
        return settled

    def _settled_did_open_locked(
        self, key: tuple, generation_nonce: str
    ) -> bool | None:
        """The answer already on record for this key, or None to send it."""
        if self._wire_generation != generation_nonce:
            return False
        if key in self._wire_opened:
            return True
        return False if key in self._wire_failed else None

    def _record_did_open_failure(self, key: tuple, generation_nonce: str) -> None:
        with self._wire_condition:
            if self._wire_generation == generation_nonce:
                self._wire_failed.add(key)

    def _release_did_open(
        self, key: tuple, generation_nonce: str, *, sent: bool
    ) -> bool:
        """Leave the gate, recording the open only if it still belongs to us."""
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
            generation_nonce = self._next_untried_generation(attempted)
            if generation_nonce is None:
                return False
            settled = self._did_open_generation_outcome(
                document, process, generation_nonce, deadline
            )
            if settled is not None:
                return settled

    def _did_open_generation_outcome(
        self,
        document: OpenDocument,
        process: LspProcess,
        generation_nonce: str,
        deadline: float,
    ) -> bool | None:
        """True sent, False the generation is still current and refused, None retry."""
        if self._send_did_open_to_generation(
            document, process, generation_nonce, deadline=deadline
        ):
            return True
        if self._current_generation_nonce() == generation_nonce:
            return False
        return None

    def _next_untried_generation(self, attempted: set[str]) -> str | None:
        """The current generation, unless we have already tried it."""
        generation_nonce = self._current_generation_nonce()
        if generation_nonce is None or generation_nonce in attempted:
            return None
        attempted.add(generation_nonce)
        return generation_nonce

    def _current_generation_nonce(self) -> str | None:
        with self._lock:
            return self._generation_nonce

    def _send_did_open_to_generation(
        self,
        document: OpenDocument,
        process: LspProcess,
        generation_nonce: str,
        *,
        deadline: float,
    ) -> bool:
        params = self._did_open_params(document)
        return self._send_did_open_once(
            document,
            generation_nonce,
            deadline=deadline,
            notify=lambda: process.notify_generation(
                "textDocument/didOpen",
                params,
                generation_nonce=generation_nonce,
                deadline=deadline,
            ),
        )

    def _progress(self, method: str, params: object) -> None:
        record = _progress_notification_record(method, params)
        if record is not None:
            self._retain_progress(record)
        self._note_work_done_end(method, params)

    def _note_work_done_end(self, method: str, params: object) -> None:
        """An `end` on a token the server opened closes this generation's load.

        This is the measured difference between a right and a wrong answer:
        ungated, typescript-language-server answers go-to-definition with the
        import binding rather than the declaration -- 0/12 correct against 12/12
        gated. See `docs/research/2026-08-28-precise-navigation-beyond-python.md`,
        Finding 4.
        """
        if method != _NEUTRAL_PROGRESS_METHOD:
            return
        token = _work_done_end_token(params)
        if token is None:
            return
        self._mark_progress_ready(token)

    def _mark_progress_ready(self, token: object) -> None:
        with self._lock:
            if token not in self._work_done_tokens:
                return
            self._progress_ready_generation = self._generation_nonce
            self._condition.notify_all()

    def _progress_gate_satisfied_locked(self) -> bool:
        """Whether this profile's readiness precondition is met right now."""
        if not self._profile.gates_on_progress():
            return True
        generation = self._generation_nonce
        return generation is not None and self._progress_ready_generation == generation

    def _await_progress_gate(self, deadline: float) -> None:
        """Block until the project load is declared finished, or the deadline."""
        if not self._profile.gates_on_progress():
            return
        with self._lock:
            while not self._progress_gate_satisfied_locked():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self._condition.wait(min(remaining, _PROGRESS_POLL_SECONDS))

    def _diagnostic_core(
        self, value: Mapping[str, object]
    ) -> tuple[LspRange, str, int | None, str | None] | None:
        """Range, message, severity and code, or None when one is unreadable."""
        range_ = _lsp_range(value.get("range"))
        message = _bounded_text(value.get("message"), _MAX_DIAGNOSTIC_TEXT_BYTES)
        if range_ is None or message is None:
            return None
        classified = _diagnostic_classification(value)
        if classified is None:
            return None
        severity, code = classified
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
        return _related_message(location, relation.get("message"))

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
        return _snapshot_supersedes(existing, version)

    def _diagnostic_update_admissible_locked(
        self, uri: str, version: int | None
    ) -> bool:
        """Whether a diagnostic update for this URI is still worth keeping."""
        document = self._documents.get(uri)
        if document is None:
            return False
        existing = self._diagnostics.get(uri)
        if self._diagnostic_update_is_stale(document, existing, version):
            return False
        return existing is not None or len(self._diagnostics) < _MAX_DIAGNOSTIC_URIS

    def _parsed_diagnostics(
        self, values: list[object], uri: str
    ) -> tuple[list[LspDiagnostic], bool]:
        """Every readable diagnostic, and whether anything had to be dropped."""
        partial = len(values) > MAX_LOCATIONS
        diagnostics: list[LspDiagnostic] = []
        for value in values[:MAX_LOCATIONS]:
            diagnostic, filtered = self._parse_diagnostic(value, uri)
            partial = partial or filtered
            if diagnostic is not None:
                diagnostics.append(diagnostic)
        return diagnostics, partial

    def _diagnostic_snapshot(
        self, values: list[object], uri: str, version: int | None
    ) -> _DiagnosticSnapshot | None:
        """The snapshot to publish, or None when it would not fit the budget."""
        diagnostics, partial = self._parsed_diagnostics(values, uri)
        retained_bytes = _DIAGNOSTIC_BASE_BYTES + sum(
            self._diagnostic_retained_bytes(diagnostic) for diagnostic in diagnostics
        )
        if retained_bytes > _MAX_DIAGNOSTIC_BYTES:
            return None
        return _DiagnosticSnapshot(
            tuple(diagnostics),
            version,
            partial,
            retained_bytes,
        )

    def _store_diagnostics(
        self, uri: str, version: int | None, snapshot: _DiagnosticSnapshot
    ) -> None:
        """Publish the snapshot, unless the session moved on or ran out of budget."""
        with self._lock:
            if not self._diagnostic_update_admissible_locked(uri, version):
                return
            existing = self._diagnostics.get(uri)
            previous_bytes = existing.retained_bytes if existing is not None else 0
            aggregate_bytes = (
                self._diagnostic_bytes - previous_bytes + snapshot.retained_bytes
            )
            if aggregate_bytes > _MAX_DIAGNOSTIC_BYTES:
                return
            self._diagnostics[uri] = snapshot
            self._diagnostic_bytes = aggregate_bytes
            self._condition.notify_all()

    def _diagnostic_target(
        self, params: object
    ) -> tuple[str, list[object], int | None] | None:
        """The URI, diagnostics and version to publish, when all are readable."""
        published = _published_diagnostics_params(params)
        if published is None:
            return None
        uri_value, values, version_value = published
        source = normalize_provider_uri(self._repository, uri_value)
        if source is None:
            return None
        return _versioned_diagnostic_target(source.uri, values, version_value)

    def _publish_diagnostics(self, params: object) -> None:
        target = self._diagnostic_target(params)
        if target is None:
            return
        uri, values, version = target
        with self._lock:
            if not self._diagnostic_update_admissible_locked(uri, version):
                return
        snapshot = self._diagnostic_snapshot(values, uri, version)
        if snapshot is None:
            return
        self._store_diagnostics(uri, version, snapshot)

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

    def _bootstrap_owner(self, generation_nonce: str) -> str:
        """The owner nonce this bootstrap belongs to, while it is still accepted."""
        with self._lock:
            owner_nonce = self._bootstrap_generation_owners.get(generation_nonce)
            if owner_nonce is None or self._bootstrap_owner_nonce != owner_nonce:
                raise RuntimeError("Pyright process owner is no longer accepted")
        return owner_nonce

    def _require_bootstrap_owner(self, owner_nonce: str) -> None:
        """The owner that started this bootstrap must still be the accepted one."""
        with self._lock:
            if self._bootstrap_owner_nonce != owner_nonce:
                raise RuntimeError("Pyright process owner is no longer accepted")

    def _initialize_protocol(
        self, protocol: LspProtocol, deadline: float
    ) -> tuple[dict[str, bool], PositionEncoding]:
        """Initialize the server, then hand it our configuration."""
        root = Path(self._repository.checkout_root)
        root_uri = path_to_file_uri(root)
        result = protocol.request(
            "initialize",
            {
                "processId": os.getpid(),
                "clientInfo": {"name": "llm-wiki"},
                "rootUri": root_uri,
                "workspaceFolders": [{"uri": root_uri, "name": root.name}],
                "initializationOptions": self._profile.wire_initialization_options(
                    self._state_root
                ),
                "capabilities": thaw_profile_value(_CLIENT_CAPABILITIES),
            },
            deadline=deadline,
        )
        capabilities, encoding = _parse_server_capabilities(
            result, self._profile.degradation_prefix
        )
        protocol.notify("initialized", {}, deadline=deadline)
        protocol.notify(
            "workspace/didChangeConfiguration",
            {"settings": self._profile.wire_configuration()},
            deadline=deadline,
        )
        return capabilities, encoding

    def _publish_generation_locked(
        self,
        generation_nonce: str,
        capabilities: dict[str, bool],
        encoding: PositionEncoding,
    ) -> tuple[OpenDocument, ...]:
        """Make the new generation current and report the documents to reopen."""
        documents = tuple(self._documents.values())
        self._generation_nonce = generation_nonce
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
        return documents

    def _adopt_generation(
        self,
        owner_nonce: str,
        generation_nonce: str,
        capabilities: dict[str, bool],
        encoding: PositionEncoding,
    ) -> tuple[OpenDocument, ...]:
        self._require_bootstrap_owner(owner_nonce)
        self._begin_wire_generation(generation_nonce)
        with self._lock:
            if self._bootstrap_owner_nonce != owner_nonce:
                raise RuntimeError("Pyright process owner is no longer accepted")
            return self._publish_generation_locked(
                generation_nonce, capabilities, encoding
            )

    def _reopen_documents(
        self,
        documents: tuple[OpenDocument, ...],
        protocol: LspProtocol,
        owner_nonce: str,
        generation_nonce: str,
        deadline: float,
    ) -> None:
        """Every document we hold open is opened again on the new generation."""
        for document in documents:
            self._require_bootstrap_owner(owner_nonce)
            if not self._send_protocol_did_open(
                document,
                protocol,
                generation_nonce,
                deadline=deadline,
            ):
                raise RuntimeError("Pyright generation changed during didOpen")

    def _mark_document_ready(self, uri: str, generation_nonce: str) -> None:
        with self._lock:
            if self._generation_nonce == generation_nonce:
                self._ready_uri_generations[uri] = generation_nonce

    def _prime_one_document(
        self,
        document: OpenDocument,
        protocol: LspProtocol,
        generation_nonce: str,
        deadline: float,
    ) -> None:
        """A document whose symbols come back complete counts as query-ready."""
        try:
            symbols = protocol.request(
                "textDocument/documentSymbol",
                {"textDocument": {"uri": document.source.uri}},
                deadline=deadline,
            )
        except JsonRpcResponseError:
            return
        if self._normalize_document_symbols(symbols, document.source.uri)[1]:
            return
        self._mark_document_ready(document.source.uri, generation_nonce)

    def _prime_document_readiness(
        self,
        documents: tuple[OpenDocument, ...],
        protocol: LspProtocol,
        generation_nonce: str,
        deadline: float,
    ) -> None:
        for document in documents:
            self._prime_one_document(document, protocol, generation_nonce, deadline)

    def _bootstrap_ready_state(self, deadline: float) -> ProcessState:
        with self._lock:
            self._refresh_readiness_locked(deadline=deadline)
            ready = self._readiness == "query_ready"
        if ready:
            return ProcessState.WORKSPACE_READY
        return ProcessState.PROTOCOL_INITIALIZED

    def _fail_bootstrap_locked(self) -> None:
        """A failed re-bootstrap leaves the session degraded, not quietly ready."""
        self._readiness = "not_ready"
        self._readiness_evidence = ()
        self._ready_uri_generations.clear()
        self._degradation_codes = tuple(
            sorted(
                {
                    *self._degradation_codes,
                    self._profile.degradation_code("restart_bootstrap_failed"),
                }
            )
        )

    def _fail_generation_bootstrap(
        self, documents: tuple[OpenDocument, ...], generation_nonce: str
    ) -> None:
        if documents:
            with self._lock:
                self._fail_bootstrap_locked()
        self._discard_wire_generation(generation_nonce)

    def _bootstrap_generation(
        self,
        protocol: LspProtocol,
        _process_id: int,
        _generation_nonce: str,
        deadline: float,
    ) -> ProcessState:
        owner_nonce = self._bootstrap_owner(_generation_nonce)
        self._rearm_progress_gate()
        capabilities, encoding = self._initialize_protocol(protocol, deadline)
        documents: tuple[OpenDocument, ...] = ()
        try:
            documents = self._adopt_generation(
                owner_nonce, _generation_nonce, capabilities, encoding
            )
            self._reopen_documents(
                documents, protocol, owner_nonce, _generation_nonce, deadline
            )
            if capabilities["document_symbols"]:
                self._prime_document_readiness(
                    documents, protocol, _generation_nonce, deadline
                )
                return self._bootstrap_ready_state(deadline)
        except BaseException:
            self._fail_generation_bootstrap(documents, _generation_nonce)
            raise
        return ProcessState.PROTOCOL_INITIALIZED

    def _validated_qualified_paths(self, *, deadline: float) -> tuple[Path, Path]:
        identity = self._identity
        _check_qualified_identity(identity, self._profile)
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

    def _reset_readiness_locked(self) -> None:
        """Forget what a running server told us about itself."""
        self._readiness = "not_ready"
        self._readiness_evidence = ()
        self._position_encoding = None
        self._capabilities = {}

    def _degrade_locked(self, code: str) -> None:
        """Record a startup degradation and drop what it invalidates."""
        self._reset_readiness_locked()
        self._degradation_codes = tuple(sorted({*self._degradation_codes, code}))

    def _forget_generation_locked(self) -> None:
        """Forget everything tied to a generation that never became ours."""
        self._work_done_tokens.clear()
        self._progress_ready_generation = None
        self._generation_nonce = None
        self._ready_uri_generations.clear()
        self._workspace_revision = None
        self._diagnostics.clear()
        self._diagnostic_bytes = 0
        self._clear_wire_state()

    def _await_startup_locked(self, startup_deadline: float) -> None:
        """Wait out another caller's startup; the caller holds the lock."""
        while self._starting:
            remaining = startup_deadline - time.monotonic()
            if remaining <= 0 or not self._condition.wait(remaining):
                raise TimeoutError("Pyright startup did not finish before deadline")

    def _startup_needed_locked(self) -> bool:
        """A start is warranted only for a qualified, idle session."""
        if self._process is not None or self._readiness != "not_ready":
            return False
        return self._identity.qualified

    def _claim_startup_locked(
        self,
    ) -> tuple[StartupCleanupError | None, LspProcess | None] | None:
        """The retained owners to clear first, or None when not to start at all."""
        self._reconcile_process_state_locked()
        if not self._startup_needed_locked():
            return None
        retained = (self._startup_cleanup_error, self._startup_process)
        nothing_retained = retained == (None, None)
        if self._startup_attempted and nothing_retained:
            return None
        self._begin_startup_locked(nothing_retained)
        return retained

    def _begin_startup_locked(self, nothing_retained: bool) -> None:
        """Take the startup claim; a first attempt also records that it happened."""
        self._starting = True
        if nothing_retained:
            self._startup_attempted = True

    def _admit_startup(
        self, startup_deadline: float, bootstrap_timeout_seconds: float
    ) -> tuple[StartupCleanupError | None, LspProcess | None] | None:
        """Claim the right to start, or None when this call must not start one."""
        with self._lock:
            if self._closed or self._closing:
                raise RuntimeError("Pyright session is closed")
            if self._startup_refused_locked(bootstrap_timeout_seconds):
                return None
            self._await_startup_locked(startup_deadline)
            return self._claim_startup_locked()

    def _startup_refused_locked(self, bootstrap_timeout_seconds: float) -> bool:
        """Whether this session may not start at all; the reason is recorded."""
        if self._capacity_locked:
            self._degrade_locked(self._profile.degradation_code("capacity_exhausted"))
            return True
        if bootstrap_timeout_seconds <= 0:
            self._degrade_locked(self._profile.degradation_code("startup_timeout"))
            return True
        return False

    def _startup_retry_ok(self, action: Callable[[], None]) -> bool:
        """Retry a retained owner; False when it failed in an expected way."""
        try:
            action()
        except BaseException as error:
            interruption = _startup_interruption(error)
            if interruption is not None:
                _reraise_startup_interruption(error, interruption)
            if isinstance(error, (OSError, RuntimeError, TimeoutError)):
                return False
            raise
        return True

    def _clear_retained_cleanup(self, retained: StartupCleanupError) -> None:
        with self._lock:
            if self._startup_cleanup_error is retained:
                self._startup_cleanup_error = None
                self._sync_startup_atexit_locked()

    def _clear_retained_process(self, retained: LspProcess) -> None:
        with self._lock:
            if self._startup_process is retained:
                self._startup_process = None
                self._sync_startup_atexit_locked()

    def _clear_retained_owners(
        self,
        retained_cleanup: StartupCleanupError | None,
        retained_process: LspProcess | None,
        startup_deadline: float,
    ) -> bool:
        """Clear what a previous attempt left; False when one refused again."""
        if retained_cleanup is not None:
            if not self._startup_retry_ok(
                lambda: retained_cleanup.retry_cleanup(startup_deadline)
            ):
                return False
            self._clear_retained_cleanup(retained_cleanup)
        if retained_process is not None:
            if not self._startup_retry_ok(
                lambda: retained_process.close(startup_deadline)
            ):
                return False
            self._clear_retained_process(retained_process)
        return True

    def _clear_bootstrap_nonce_locked(self, attempt: _StartupAttempt) -> None:
        if self._bootstrap_owner_nonce == attempt.owner_nonce:
            self._bootstrap_owner_nonce = None

    def _clear_bootstrap_nonce(self, attempt: _StartupAttempt) -> None:
        with self._lock:
            self._clear_bootstrap_nonce_locked(attempt)

    def _server_request_handlers(self) -> dict[str, object]:
        return {
            "client/registerCapability": self._benign_server_request,
            "client/unregisterCapability": self._benign_server_request,
            "window/workDoneProgress/create": self._work_done_progress_create,
            "workspace/configuration": self._configuration,
        }

    def _progress_methods(self) -> tuple[str, ...]:
        """`$/progress` plus this profile's own vendor progress notifications."""
        identity = self._profile.identity_notification
        vendor = tuple(
            method
            for method in sorted(self._profile.server_notifications)
            if identity is None or method != identity.method
        )
        return (_NEUTRAL_PROGRESS_METHOD, *vendor)

    def _server_notification_handlers(self) -> dict[str, object]:
        handlers: dict[str, object] = {
            method: _progress_handler(self, method)
            for method in self._progress_methods()
        }
        handlers["textDocument/publishDiagnostics"] = self._publish_diagnostics
        self._add_identity_handler(handlers)
        return handlers

    def _add_identity_handler(self, handlers: dict[str, object]) -> None:
        """Register the post-initialize identity assertion, where a profile has one.

        Honest limitation, measured 2026-08-28: `lsp_protocol.SERVER_NOTIFICATIONS`
        is a module-level allowlist and does not carry `$/typescriptVersion`, so
        this handler is registered but not yet reached -- the transport drops the
        method with one "unknown notification" warning and nothing else. Widening
        that allowlist means editing `lsp_protocol.py`, which the complexity gate
        refuses wholesale over roughly thirty pre-existing findings in the
        transport hot path; that is a separate piece of work.

        The guarantee is not lost in the meantime, it is taken earlier:
        `lsp_identity` digests the pinned engine at `tsserver.path` against the
        install receipt *before* the process starts, so the file the server is
        pointed at is known to be ours. What is missing is the server's own
        confirmation that it used it rather than something else.
        """
        identity = self._profile.identity_notification
        if identity is None:
            return
        handlers[identity.method] = self._record_server_identity

    def _record_server_identity(self, params: object) -> None:
        identity = self._profile.identity_notification
        if identity is None:
            return
        version, confirmed = identity.confirmed(params)
        self._retain_progress((identity.method, str(version), str(confirmed)))

    def _prepare_owner(
        self, attempt: _StartupAttempt, *, startup_deadline: float
    ) -> tuple[Path, Path, Path]:
        """The node and server paths, and a fresh owner root for this attempt."""
        node, server = self._validated_qualified_paths(deadline=startup_deadline)
        _ensure_lsp_parent(self._state_root, deadline=startup_deadline)
        owner = lsp_owner_root(self._state_root, secrets.token_hex(16))
        attempt.owner_nonce = owner.name
        with self._lock:
            self._bootstrap_owner_nonce = owner.name
        return node, server, owner

    def _start_configured_process(
        self,
        node: Path,
        server: Path,
        owner: Path,
        *,
        bootstrap_timeout_seconds: float,
        startup_deadline: float,
    ) -> LspProcess:
        """Start the pinned server under its owner root, with our handlers."""
        command = self._profile.launch_command(node, server, owner)
        return LspProcess.start_configured(
            command,
            cwd=Path(self._repository.checkout_root),
            owner_root=owner,
            deadline=startup_deadline,
            server_request_handlers=self._server_request_handlers(),
            server_notification_handlers=self._server_notification_handlers(),
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
                lambda _generation_nonce, generation_deadline: _LaunchServerGuard(
                    server,
                    self._identity.executable_sha256,
                    command=command,
                    owner_root=owner,
                    deadline=generation_deadline,
                    degradation_prefix=self._profile.degradation_prefix,
                )
            ),
        )

    def _retry_startup_cleanup(
        self, error: BaseException, startup_deadline: float
    ) -> StartupCleanupError | None:
        """Retry an owner cleanup the launch left behind; what still needs keeping."""
        if not isinstance(error, StartupCleanupError):
            return None
        try:
            error.retry_cleanup(
                min(startup_deadline, time.monotonic() + _OWNER_CLEANUP_SECONDS)
            )
        except (KeyboardInterrupt, SystemExit):
            with self._lock:
                self._retain_startup_cleanup_locked(error)
            raise
        except BaseException:
            return error
        return None

    def _record_retained_cleanup_locked(
        self, retained_error: StartupCleanupError | None
    ) -> None:
        if retained_error is not None:
            self._retain_startup_cleanup_locked(retained_error)
            return
        self._startup_cleanup_error = None
        self._sync_startup_atexit_locked()

    def _record_retained_cleanup(
        self, retained_error: StartupCleanupError | None
    ) -> None:
        with self._lock:
            self._record_retained_cleanup_locked(retained_error)

    def _allow_startup_retry(self) -> None:
        with self._lock:
            self._startup_attempted = False

    def _record_startup_degradation(
        self, error: BaseException, retained_error: StartupCleanupError | None
    ) -> None:
        """A launch that failed in a known way leaves the session degraded."""
        code = _startup_code(error, self._profile.degradation_prefix)
        with self._lock:
            self._process = None
            self._record_retained_cleanup_locked(retained_error)
            self._degrade_locked(code)

    def _handle_launch_failure(
        self,
        error: BaseException,
        attempt: _StartupAttempt,
        *,
        startup_deadline: float,
    ) -> None:
        """Record a failed launch; returns only when it counts as degradation."""
        self._clear_bootstrap_nonce(attempt)
        interruption = _startup_interruption(error)
        retained_error = self._retry_startup_cleanup(error, startup_deadline)
        if interruption is not None:
            self._record_retained_cleanup(retained_error)
            _reraise_startup_interruption(error, interruption)
        self._require_degrading_launch_error(error)
        self._record_startup_degradation(error, retained_error)

    def _require_degrading_launch_error(self, error: BaseException) -> None:
        """Propagate the failures that are not this session's to absorb."""
        if isinstance(error, (TypeError, ValueError)):
            self._allow_startup_retry()
            raise error
        if not isinstance(error, _STARTUP_DEGRADING_ERRORS):
            raise error

    def _launch_server(
        self,
        attempt: _StartupAttempt,
        *,
        bootstrap_timeout_seconds: float,
        startup_deadline: float,
    ) -> LspProcess | None:
        """The started process, or None when the failure was recorded as degraded."""
        try:
            node, server, owner = self._prepare_owner(
                attempt, startup_deadline=startup_deadline
            )
            return self._start_configured_process(
                node,
                server,
                owner,
                bootstrap_timeout_seconds=bootstrap_timeout_seconds,
                startup_deadline=startup_deadline,
            )
        except BaseException as error:
            self._handle_launch_failure(
                error, attempt, startup_deadline=startup_deadline
            )
            return None

    def _reset_after_failed_start(
        self,
        process: LspProcess,
        retained_owner: LspProcess | None,
        attempt: _StartupAttempt,
    ) -> None:
        """Forget everything the failed attempt might have published."""
        with self._lock:
            if self._process is process:
                self._process = None
            self._clear_bootstrap_nonce_locked(attempt)
            self._startup_process = retained_owner
            self._sync_startup_atexit_locked()
            self._reset_readiness_locked()
            self._forget_generation_locked()

    def _close_failed_process(
        self,
        process: LspProcess | None,
        attempt: _StartupAttempt,
        *,
        startup_deadline: float,
    ) -> BaseException | None:
        """Close a process that never became ours; the close error, if any."""
        if process is None:
            return None
        cleanup_deadline = min(
            startup_deadline, time.monotonic() + _OWNER_CLEANUP_SECONDS
        )
        retained_owner: LspProcess | None = None
        cleanup_error: BaseException | None = None
        try:
            process.close(cleanup_deadline)
        except BaseException as close_error:
            retained_owner = process
            cleanup_error = close_error
        self._reset_after_failed_start(process, retained_owner, attempt)
        return cleanup_error

    def _abandon_startup(
        self,
        error: BaseException,
        process: LspProcess | None,
        attempt: _StartupAttempt,
        *,
        startup_deadline: float,
    ) -> None:
        """Undo a startup that raised, then propagate what stopped it."""
        cleanup_error = self._close_failed_process(
            process, attempt, startup_deadline=startup_deadline
        )
        interruption = _startup_interruption(error) or _cleanup_interruption(
            cleanup_error
        )
        if interruption is None:
            raise error
        self._allow_startup_retry()
        if cleanup_error is not None:
            _raise_collected_errors([cleanup_error], prior_error=error)
        _reraise_startup_interruption(error, interruption)

    def _start_owned(
        self,
        retained: tuple[StartupCleanupError | None, LspProcess | None],
        *,
        bootstrap_timeout_seconds: float,
        startup_deadline: float,
    ) -> None:
        """Start a server while this caller holds the starting flag."""
        retained_cleanup, retained_process = retained
        attempt = _StartupAttempt()
        process: LspProcess | None = None
        try:
            if not self._clear_retained_owners(
                retained_cleanup, retained_process, startup_deadline
            ):
                return
            with self._lock:
                self._startup_attempted = True
            process = self._launch_server(
                attempt,
                bootstrap_timeout_seconds=bootstrap_timeout_seconds,
                startup_deadline=startup_deadline,
            )
            if process is None:
                return
            with self._lock:
                self._process = process
                self._startup_process = None
                self._sync_startup_atexit_locked()
        except BaseException as error:
            self._abandon_startup(
                error, process, attempt, startup_deadline=startup_deadline
            )

    def start(self, *, deadline: float) -> None:
        caller_deadline = _validated_deadline(deadline)
        startup_started = time.monotonic()
        startup_deadline = min(caller_deadline, startup_started + STARTUP_SECONDS)
        bootstrap_timeout_seconds = startup_deadline - startup_started
        with self._operation():
            retained = self._admit_startup(
                startup_deadline, bootstrap_timeout_seconds
            )
            if retained is None:
                return
            try:
                self._start_owned(
                    retained,
                    bootstrap_timeout_seconds=bootstrap_timeout_seconds,
                    startup_deadline=startup_deadline,
                )
            finally:
                with self._lock:
                    self._starting = False
                    self._condition.notify_all()

    def _document_ready_locked(self, uri: str) -> bool:
        # The progress gate belongs here rather than in
        # `_refresh_readiness_locked`: readiness reporting goes through that
        # one, but a query is admitted through `_document_query` ->
        # `_query_ready_locked` -> `_document_query_current_locked` -> here.
        # Gating only the reporting path would leave a session that calls
        # itself not ready and answers anyway.
        generation = self._generation_nonce
        document = self._documents.get(uri)
        return (
            generation is not None
            and document is not None
            and self._progress_gate_satisfied_locked()
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
        if not self._process_state_needs_reconciling():
            return
        self._reset_readiness_locked()
        self._generation_nonce = None
        self._ready_uri_generations.clear()
        self._workspace_revision = None
        self._diagnostics.clear()
        self._diagnostic_bytes = 0
        self._clear_wire_state()

    def _process_state_needs_reconciling(self) -> bool:
        """The process has failed or degraded out from under this session."""
        process = self._process
        if process is None:
            return False
        if process.state is ProcessState.FAILED:
            return True
        return self._degraded_generation_is_ours(process)

    def _degraded_generation_is_ours(self, process: LspProcess) -> bool:
        """A degraded process only reconciles when the failed generation is ours."""
        if process.state is not ProcessState.DEGRADED:
            return False
        generation = self._generation_nonce
        return generation is None or generation == process.generation_nonce

    def _demote_target_locked(self, target: str) -> None:
        """The target is open but could not be promoted to query-ready."""
        self._ready_uri_generations.pop(target, None)
        self._readiness = "protocol_initialized"
        self._readiness_evidence = _DID_OPEN_EVIDENCE

    def _promote_target_locked(
        self,
        target: str,
        process: LspProcess,
        generation: str,
        deadline: float,
    ) -> bool:
        """Whether the server confirmed its workspace is ready."""
        try:
            return process.promote_workspace_ready(
                generation_nonce=generation,
                deadline=deadline,
            )
        except BaseException:
            self._demote_target_locked(target)
            raise

    def _target_ready_locked(self, target: str, deadline: float) -> bool:
        """Whether the ready target may be reported as query-ready."""
        process = self._process
        generation = self._generation_nonce
        if process is None or generation is None:
            return True
        if process.generation_nonce != generation:
            return True
        return self._promoted_or_demoted_locked(target, process, generation, deadline)

    def _promoted_or_demoted_locked(
        self, target: str, process: LspProcess, generation: str, deadline: float
    ) -> bool:
        if self._promote_target_locked(target, process, generation, deadline):
            return True
        self._demote_target_locked(target)
        return False

    def _initialized_readiness_locked(self) -> None:
        """Without a ready target the session is initialized, not query-ready."""
        generation = self._generation_nonce
        self._readiness = "protocol_initialized"
        did_open = any(
            self._wire_document_opened(document, generation)
            for document in self._documents.values()
        )
        self._readiness_evidence = (
            *_PROTOCOL_EVIDENCE,
            *(("didOpen",) if did_open else ()),
        )

    def _refresh_readiness_locked(self, *, deadline: float) -> None:
        if self._position_encoding is None:
            self._readiness = "not_ready"
            self._readiness_evidence = ()
            return
        target = self._readiness_target_uri
        if target is None or not self._document_ready_locked(target):
            self._initialized_readiness_locked()
            return
        self._promote_readiness_locked(target, deadline)

    def _promote_readiness_locked(self, target: str, deadline: float) -> None:
        if not self._target_ready_locked(target, deadline):
            return
        self._readiness = "query_ready"
        self._readiness_evidence = self._query_ready_evidence()

    def _query_ready_evidence(self) -> tuple[str, ...]:
        if not self._profile.gates_on_progress():
            return _QUERY_READY_EVIDENCE
        return (*_QUERY_READY_EVIDENCE, "workDoneProgress/end")

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

    def _document_source_bytes(
        self, path: str, deadline: float
    ) -> tuple[RepositorySource, bytes, str]:
        """The resolved source, its verified bytes, and their digest."""
        source = resolve_repository_source(self._repository, path)
        content = read_stable_bytes(
            source.absolute_path,
            _MAX_DOCUMENT_BYTES,
            label="Pyright source document",
            deadline=deadline,
        )
        content.decode("utf-8", errors="strict")
        return source, content, hashlib.sha256(content).hexdigest()

    def _try_process_did_open(
        self, document: OpenDocument, process: LspProcess, deadline: float
    ) -> bool:
        """Announce the document; a refusal is an answer, not a failure."""
        try:
            return self._send_process_did_open(document, process, deadline=deadline)
        except (ProtocolViolation, RuntimeError, TimeoutError):
            return False

    def _reopen_existing_document(
        self,
        current: OpenDocument,
        process: LspProcess,
        *,
        ready: bool,
        deadline: float,
    ) -> OpenDocument:
        """A document we already hold; re-announce it if the server lost it."""
        if ready:
            return current
        did_open = self._try_process_did_open(current, process, deadline)
        self._mark_protocol_initialized(did_open=did_open, deadline=deadline)
        if not did_open:
            return current
        self._probe_document(current, process, deadline=deadline)
        return current

    def _register_document_locked(
        self, document: OpenDocument, content: bytes
    ) -> None:
        """Take the document into the open set, within its count and byte bounds."""
        if len(self._documents) >= _MAX_OPEN_DOCUMENTS:
            raise RuntimeError("Pyright open document count limit exceeded")
        document_bytes = self._document_bytes + len(content)
        if document_bytes > _MAX_OPEN_DOCUMENT_BYTES:
            raise RuntimeError("Pyright open document source bytes limit exceeded")
        uri = document.source.uri
        self._documents[uri] = document
        self._document_bytes = document_bytes
        self._readiness_target_uri = uri
        self._ready_uri_generations.pop(uri, None)

    def _open_new_document(
        self,
        source: RepositorySource,
        content: bytes,
        digest: str,
        process: LspProcess,
        deadline: float,
    ) -> OpenDocument:
        document = OpenDocument(source, content, digest, 1)
        _check_did_open_encodable(self._did_open_params(document))
        with self._lock:
            self._register_document_locked(document, content)
        sent = self._try_process_did_open(document, process, deadline)
        self._mark_protocol_initialized(did_open=sent, deadline=deadline)
        if not sent:
            return document
        self._probe_document(document, process, deadline=deadline)
        return document

    def _open_document_within_operation(
        self, path: str, deadline: float
    ) -> OpenDocument:
        self.start(deadline=deadline)
        source, content, digest = self._document_source_bytes(path, deadline)
        with self._lock:
            current = self._documents.get(source.uri)
            process = self._process
            ready = self._document_ready_locked(source.uri)
        if process is None:
            raise RuntimeError("Pyright session is not protocol initialized")
        if current is None:
            return self._open_new_document(source, content, digest, process, deadline)
        _check_document_unchanged(current, source, content, digest)
        return self._reopen_existing_document(
            current, process, ready=ready, deadline=deadline
        )

    def open_document(self, path: str, *, deadline: float) -> OpenDocument:
        deadline = _validated_deadline(deadline)
        if time.monotonic() >= deadline:
            raise TimeoutError("Pyright document deadline expired")
        with self._document_operation_lock(deadline), self._operation():
            return self._open_document_within_operation(path, deadline)

    def _located_at(self, uri: object, range_value: object) -> LspLocation | None:
        """One location from a URI this repository owns and a usable range."""
        if not isinstance(uri, str):
            return None
        range_ = _lsp_range(range_value)
        if range_ is None:
            return None
        return self._owned_location(uri, range_)

    def _owned_location(self, uri: str, range_: LspRange) -> LspLocation | None:
        source = normalize_provider_uri(self._repository, uri)
        if source is None:
            return None
        return LspLocation(source.uri, range_)

    def _normalize_location(self, value: object) -> LspLocation | None:
        if not isinstance(value, dict):
            return None
        located = _location_fields(value)
        if located is None:
            return None
        return self._located_at(located[0], located[1])

    def _normalize_locations(
        self,
        result: object,
    ) -> tuple[tuple[LspLocation, ...], bool]:
        raw = _location_payload(result)
        if raw is None:
            return (), result is not None
        partial = len(raw) > MAX_LOCATIONS
        collected = _BoundedLocations()
        for value in raw[:MAX_LOCATIONS]:
            location = self._normalize_location(value)
            if location is None:
                partial = True
                continue
            collected.add(location)
        return tuple(collected.locations), partial

    def _location_params(
        self, query: _DocumentQuery, anchor: SourceAnchor, *, references: bool
    ) -> dict[str, object]:
        params = _position_params(query, self._anchor_position(query, anchor))
        if references:
            params["context"] = {"includeDeclaration": True}
        return params

    def _location_feature_within_operation(
        self,
        anchor: SourceAnchor,
        *,
        capability: str,
        method: str,
        deadline: float,
        references: bool,
    ) -> ProviderLocations:
        query, status = self._begin_document_query(
            capability, anchor.path, deadline=deadline
        )
        if query is None:
            return ProviderLocations((), status, True)
        params = self._location_params(query, anchor, references=references)
        return self._location_response(
            query, method, params, deadline=deadline, references=references
        )

    def _location_response(
        self,
        query: _DocumentQuery,
        method: str,
        params: dict[str, object],
        *,
        deadline: float,
        references: bool,
    ) -> ProviderLocations:
        """Ask, then publish only if the workspace has not moved either side of it."""
        if not self._query_still_current(query):
            return ProviderLocations((), "not_ready", True)
        result = query.process.request(method, params, deadline=deadline)
        locations, filtered = self._normalize_locations(result)
        response = ProviderLocations(
            locations, "provider_reported", references or filtered
        )
        if not self._query_still_current(query):
            return ProviderLocations((), "not_ready", True)
        return response

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
            return self._location_feature_within_operation(
                anchor,
                capability=capability,
                method=method,
                deadline=deadline,
                references=references,
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

    def _symbol_location(
        self, value: Mapping[str, object], uri: str
    ) -> LspLocation | None:
        """Where the symbol is: an explicit location, or its selection range."""
        if "location" in value:
            return self._normalize_location(value.get("location"))
        return _contained_selection(
            _lsp_range(value.get("range")),
            _lsp_range(value.get("selectionRange")),
            uri,
        )

    def _visit_symbol(self, walk: _SymbolWalk, uri: str) -> None:
        """Take one node off the walk and collect the location it names."""
        value = walk.stack.pop()
        walk.visited += 1
        if not isinstance(value, dict) or not walk.first_visit(value):
            walk.drop()
            return
        _push_symbol_children(walk, value)
        self._collect_symbol(walk, value, uri)

    def _collect_symbol(
        self, walk: _SymbolWalk, value: Mapping[str, object], uri: str
    ) -> None:
        """Add the location this node names, or record that it could not be read."""
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

    def _document_symbols_within_operation(
        self, path: str, *, deadline: float
    ) -> ProviderLocations:
        query, status = self._begin_document_query(
            "document_symbols", path, deadline=deadline
        )
        if query is None:
            return ProviderLocations((), status, True)
        result = query.process.request(
            "textDocument/documentSymbol",
            {"textDocument": {"uri": query.document.source.uri}},
            deadline=deadline,
        )
        locations, partial = self._normalize_document_symbols(
            result, query.document.source.uri
        )
        response = ProviderLocations(locations, "provider_reported", partial)
        if not self._query_still_current(query):
            return ProviderLocations((), "not_ready", True)
        return response

    def document_symbols(self, path: str, *, deadline: float) -> ProviderLocations:
        deadline = _validated_deadline(deadline)
        with self._operation():
            return self._document_symbols_within_operation(path, deadline=deadline)


    def _anchor_position(
        self, query: _DocumentQuery, anchor: SourceAnchor
    ) -> LspPosition:
        """The anchor as a position in the document this query is bound to."""
        source_document = SourceDocument.from_bytes(
            query.document.source.relative_path,
            query.document.content,
        )
        return source_document.to_lsp(anchor, query.encoding)

    def _begin_document_query(
        self, capability: str, path: str, *, deadline: float
    ) -> tuple[_DocumentQuery | None, str]:
        """The bound query for this path; the status matters only when it is None."""
        epoch = self._semantic_query_epoch()
        if epoch is None:
            return None, "not_ready"
        self.start(deadline=deadline)
        status = self._capability_status(capability, epoch)
        if status is not None:
            return None, status
        document = self.open_document(path, deadline=deadline)
        # The server begins loading the project on didOpen, so the wait has to
        # come after it, not after `start`.
        self._await_progress_gate(deadline)
        return self._document_query(document, epoch), "not_ready"

    def _hover_within_operation(
        self, anchor: SourceAnchor, *, deadline: float
    ) -> ProviderHover:
        query, _status = self._begin_document_query(
            "hover", anchor.path, deadline=deadline
        )
        if query is None:
            return ProviderHover(None, None, True)
        position = self._anchor_position(query, anchor)
        return self._hover_response(query, position, deadline=deadline)

    def _hover_response(
        self, query: _DocumentQuery, position: LspPosition, *, deadline: float
    ) -> ProviderHover:
        if not self._query_still_current(query):
            return ProviderHover(None, None, True)
        result = query.process.request(
            "textDocument/hover",
            _position_params(query, position),
            deadline=deadline,
        )
        response = _hover_response(result)
        if not self._query_still_current(query):
            return ProviderHover(None, None, True)
        return response

    def hover(self, anchor: SourceAnchor, *, deadline: float) -> ProviderHover:
        if not isinstance(anchor, SourceAnchor):
            raise TypeError("anchor must be a SourceAnchor")
        deadline = _validated_deadline(deadline)
        with self._operation():
            return self._hover_within_operation(anchor, deadline=deadline)

    def _workspace_state(self, capability: str, epoch: int) -> _WorkspaceState:
        with self._lock:
            return _WorkspaceState(
                process=self._process,
                generation=self._generation_nonce,
                readiness=self._readiness,
                supported=self._capabilities.get(capability, False),
                initialized=self._position_encoding is not None,
                current=self._semantic_query_epoch_current_locked(epoch),
            )

    def _workspace_query_current(self, query: _WorkspaceQuery) -> bool:
        """Whether the session still stands where the workspace query began."""
        with self._lock:
            return (
                self._process is query.process
                and self._generation_nonce == query.generation
                and self._readiness == query.readiness
                and self._semantic_query_epoch_current_locked(query.epoch)
            )

    def _workspace_symbol_response(self, result: object) -> ProviderLocations:
        """The locations the server reported for a workspace symbol query."""
        if result is None:
            return ProviderLocations((), "provider_reported", False)
        if not isinstance(result, list):
            return ProviderLocations((), "provider_reported", True)
        values, partial = _symbol_locations_payload(result)
        locations, filtered = self._normalize_locations(values)
        return ProviderLocations(locations, "provider_reported", partial or filtered)

    def _workspace_symbols_within_operation(
        self, query: str, *, deadline: float
    ) -> ProviderLocations:
        epoch = self._semantic_query_epoch()
        if epoch is None:
            return ProviderLocations((), "not_ready", True)
        self.start(deadline=deadline)
        state = self._workspace_state("workspace_symbols", epoch)
        status = _workspace_query_status(state)
        if status is not None:
            return ProviderLocations((), status, True)
        bound = _WorkspaceQuery(
            state.process, state.generation, state.readiness, epoch
        )
        return self._workspace_symbol_result(bound, query, deadline=deadline)

    def _workspace_symbol_result(
        self, bound: _WorkspaceQuery, query: str, *, deadline: float
    ) -> ProviderLocations:
        result = bound.process.request(
            "workspace/symbol", {"query": query}, deadline=deadline
        )
        response = self._workspace_symbol_response(result)
        if not self._workspace_query_current(bound):
            return ProviderLocations((), "not_ready", True)
        return response

    def workspace_symbols(
        self,
        query: str,
        *,
        deadline: float,
    ) -> ProviderLocations:
        _check_symbol_query(query)
        deadline = _validated_deadline(deadline)
        with self._operation():
            return self._workspace_symbols_within_operation(query, deadline=deadline)


    def _sanitize_call_item(self, value: object) -> dict[str, object] | None:
        if not isinstance(value, dict):
            return None
        core = _call_item_core(value)
        if core is None:
            return None
        return self._call_item_from_core(value, core)

    def _call_item_from_core(
        self, value: Mapping[str, object], core: tuple
    ) -> dict[str, object] | None:
        name, kind, uri, range_, selection = core
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
        return item if _call_item_optionals(value, item) else None

    def _call_location(self, value: object) -> LspLocation | None:
        if not isinstance(value, dict):
            return None
        return self._located_at(value.get("uri"), _call_item_range_value(value))

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

    def _query_still_current(self, query: _DocumentQuery) -> bool:
        """Whether the query still refers to the workspace it started on."""
        with self._lock:
            return self._document_query_current_locked(
                query.process, query.generation, query.document, query.epoch
            )

    def _capability_status(self, capability: str, epoch: int) -> str | None:
        """A refusal status for this capability, or None when it may proceed."""
        with self._lock:
            process = self._process
            encoding = self._position_encoding
            supported = self._capabilities.get(capability, False)
            current = self._semantic_query_epoch_current_locked(epoch)
        if process is None or encoding is None or not current:
            return "not_ready"
        if not supported:
            return "unsupported"
        return None

    def _document_query(self, document: OpenDocument, epoch: int) -> _DocumentQuery | None:
        """The bound query, or None when the workspace moved under it."""
        with self._lock:
            encoding = self._position_encoding
            process = self._process
            generation = self._generation_nonce
            ready = self._query_ready_locked(process, generation, document, epoch)
        if not ready or encoding is None or process is None or generation is None:
            return None
        return _DocumentQuery(process, generation, document, epoch, encoding)

    def _prepare_call_hierarchy(
        self, anchor: SourceAnchor, query: _DocumentQuery, *, deadline: float
    ) -> tuple[bool, object]:
        """Whether the query held, and what the server prepared for the anchor."""
        position = self._anchor_position(query, anchor)
        if not self._query_still_current(query):
            return False, None
        prepared = query.process.request(
            "textDocument/prepareCallHierarchy",
            _position_params(query, position),
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
        self, item: object, query: _DocumentQuery, *, method: str, deadline: float
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
        query: _DocumentQuery,
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
        query: _DocumentQuery,
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
        return self._calls_from_prepared(
            prepared, query, direction=direction, deadline=deadline
        )

    def _calls_from_prepared(
        self,
        prepared: list[object],
        query: _DocumentQuery,
        *,
        direction: str,
        deadline: float,
    ) -> ProviderCalls:
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
        status = self._capability_status("calls", epoch)
        if status is not None:
            return ProviderCalls(direction, (), status, True)
        return self._calls_for_open_document(
            anchor, epoch, direction=direction, deadline=deadline
        )

    def _calls_for_open_document(
        self, anchor: SourceAnchor, epoch: int, *, direction: str, deadline: float
    ) -> ProviderCalls:
        document = self.open_document(anchor.path, deadline=deadline)
        self._await_progress_gate(deadline)
        query = self._document_query(document, epoch)
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

    def _diagnostics_ready(self, epoch: int) -> bool:
        """The session can answer for diagnostics at this synchronize epoch."""
        with self._lock:
            initialized = (
                self._process is not None and self._position_encoding is not None
            )
            current = self._semantic_query_epoch_current_locked(epoch)
        return initialized and current

    def _await_diagnostics_locked(
        self,
        document: OpenDocument,
        process: LspProcess,
        generation: str,
        epoch: int,
        deadline: float,
    ) -> ProviderDiagnostics:
        """Wait for a snapshot of this document version; the caller holds the lock."""
        while True:
            settled = self._settled_diagnostics_locked(
                document, process, generation, epoch, deadline
            )
            if settled is not None:
                return settled
            self._condition.wait(deadline - time.monotonic())

    def _settled_diagnostics_locked(
        self,
        document: OpenDocument,
        process: LspProcess,
        generation: str,
        epoch: int,
        deadline: float,
    ) -> ProviderDiagnostics | None:
        """The answer this wait can already give, or None to keep waiting."""
        if not self._document_query_current_locked(
            process, generation, document, epoch
        ):
            return ProviderDiagnostics((), None, True)
        snapshot = self._diagnostics.get(document.source.uri)
        matched = _matching_diagnostics(snapshot, document.version)
        if matched is not None:
            return matched
        return _expired_diagnostics(snapshot) if deadline <= time.monotonic() else None

    def _diagnostics_for_document(
        self, document: OpenDocument, epoch: int, deadline: float
    ) -> ProviderDiagnostics:
        with self._lock:
            process = self._process
            generation = self._generation_nonce
            if process is None or generation is None:
                return ProviderDiagnostics((), None, True)
            if not self._document_query_current_locked(
                process, generation, document, epoch
            ):
                return ProviderDiagnostics((), None, True)
            return self._await_diagnostics_locked(
                document, process, generation, epoch, deadline
            )

    def _diagnostics_within_operation(
        self, path: str, deadline: float
    ) -> ProviderDiagnostics:
        epoch = self._semantic_query_epoch()
        if epoch is None:
            return ProviderDiagnostics((), None, True)
        self.start(deadline=deadline)
        if not self._diagnostics_ready(epoch):
            return ProviderDiagnostics((), None, True)
        document = self.open_document(path, deadline=deadline)
        return self._diagnostics_for_document(document, epoch, deadline)

    def diagnostics(self, path: str, *, deadline: float) -> ProviderDiagnostics:
        deadline = _validated_deadline(deadline)
        with self._operation():
            return self._diagnostics_within_operation(path, deadline)

    def _watched_uri(self, relative_path: str) -> str:
        absolute = Path(self._repository.checkout_root, relative_path)
        return path_to_file_uri(absolute)

    def _replay_generation_current(self, process: LspProcess) -> str | None:
        """The generation both we and the process agree is current, if any."""
        generation = self._generation_nonce
        if self._process is not process or generation is None:
            return None
        usable = (
            process.state not in {ProcessState.DEGRADED, ProcessState.FAILED}
            and generation == process.generation_nonce
        )
        return generation if usable else None

    def _synchronize_snapshot_replayed_locked(self, process: LspProcess) -> bool:
        generation = self._replay_generation_current(process)
        if generation is None or self._position_encoding is None:
            return False
        return all(
            self._wire_document_opened(document, generation)
            for document in self._documents.values()
        )

    def _recovery_unnecessary_locked(
        self, process: LspProcess, failed_generation: str
    ) -> bool:
        """Another generation already replayed the snapshot for us."""
        return (
            self._generation_nonce != failed_generation
            and self._synchronize_snapshot_replayed_locked(process)
        )

    def _forget_readiness_for_recovery_locked(self) -> None:
        self._readiness = "not_ready"
        self._readiness_evidence = ()
        self._ready_uri_generations.clear()
        self._diagnostics.clear()
        self._diagnostic_bytes = 0

    def _check_recovery_owners_locked(self, process: LspProcess) -> None:
        """Only the process we are recovering may be under our own ownership."""
        if self._process is not process:
            raise RuntimeError(
                "Pyright synchronization process changed before recovery"
            )
        if (
            self._startup_process is not None
            and self._startup_process is not process
        ):
            raise RuntimeError("Pyright synchronization cleanup owner is unavailable")

    def _take_recovery_ownership_locked(self, process: LspProcess) -> None:
        """Hold the process as a startup owner while it restarts."""
        self._process = None
        self._startup_process = process
        self._starting = True
        self._position_encoding = None
        self._capabilities = {}
        self._generation_nonce = None
        self._clear_wire_state()
        self._sync_startup_atexit_locked()
        self._condition.notify_all()

    def _claim_recovery(
        self, process: LspProcess, failed_generation: str
    ) -> tuple[bool, str | None]:
        """Whether this call owns the recovery, and the bootstrap owner it took."""
        with self._lock:
            if self._recovery_unnecessary_locked(process, failed_generation):
                return False, None
            self._forget_readiness_for_recovery_locked()
            self._check_recovery_owners_locked(process)
            bootstrap_owner_nonce = self._bootstrap_owner_nonce
            self._take_recovery_ownership_locked(process)
            return True, bootstrap_owner_nonce

    def _replayed_after_restart(self, process: LspProcess) -> bool:
        """Whether the restarted process proves the snapshot it had before."""
        with self._lock:
            if self._startup_process is not process or self._process is not None:
                return False
            self._process = process
            if self._synchronize_snapshot_replayed_locked(process):
                self._startup_process = None
                self._sync_startup_atexit_locked()
                return True
            self._process = None
            return False

    def _forget_recovery_state_locked(self) -> None:
        self._reset_readiness_locked()
        self._generation_nonce = None
        self._ready_uri_generations.clear()
        self._diagnostics.clear()
        self._diagnostic_bytes = 0
        self._clear_wire_state()

    def _abandon_recovery(
        self, process: LspProcess, bootstrap_owner_nonce: str | None
    ) -> None:
        """A recovery that failed leaves the process to be cleaned up, not used."""
        with self._lock:
            self._detach_recovered_process_locked(process, bootstrap_owner_nonce)
            self._forget_recovery_state_locked()
            self._sync_startup_atexit_locked()
            self._condition.notify_all()

    def _detach_recovered_process_locked(
        self, process: LspProcess, bootstrap_owner_nonce: str | None
    ) -> None:
        """Hand the process to the cleanup path and drop the owner that made it."""
        if self._process is process:
            self._process = None
        if self._startup_process is None:
            self._startup_process = process
        self._release_bootstrap_owner_locked(bootstrap_owner_nonce)

    def _release_bootstrap_owner_locked(self, owner_nonce: str | None) -> None:
        if self._bootstrap_owner_nonce == owner_nonce:
            self._bootstrap_owner_nonce = None

    def _release_recovery_serialization(self) -> None:
        with self._lock:
            self._starting = False
            self._condition.notify_all()

    def _recover_synchronize_snapshot(
        self,
        process: LspProcess,
        failed_generation: str,
        *,
        deadline: float,
    ) -> None:
        claimed, bootstrap_owner_nonce = self._claim_recovery(
            process, failed_generation
        )
        if not claimed:
            return
        try:
            process.restart(deadline)
            if self._replayed_after_restart(process):
                return
            raise RuntimeError(
                "Pyright synchronization recovery could not prove the prior snapshot"
            )
        except BaseException:
            self._abandon_recovery(process, bootstrap_owner_nonce)
            raise
        finally:
            self._release_recovery_serialization()

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
        return (
            self._process is not plan.process
            or self._workspace_revision is not plan.prior
            or self._generation_nonce != plan.generation
            or plan.process.generation_nonce != plan.generation
        )

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
        self._require_matching_revision(revision)
        if time.monotonic() >= deadline:
            raise TimeoutError("Pyright synchronize deadline expired")

    def _require_matching_revision(self, revision: WorkspaceRevision) -> None:
        if not isinstance(revision, WorkspaceRevision):
            raise TypeError("revision must be a WorkspaceRevision")
        if (
            revision.repository_id != self._repository.repository_id
            or revision.checkout_id != self._repository.checkout_id
        ):
            raise ValueError("workspace revision must describe this checkout")

    def close(self, *, deadline: float) -> None:
        deadline = _validated_deadline(deadline)
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._close_lock.acquire(timeout=remaining):
            raise TimeoutError("Pyright close serialization deadline expired")
        try:
            self._close_owned(deadline)
        finally:
            self._close_lock.release()

    def _close_finished_locked(
        self,
        cleanup_error: StartupCleanupError | None,
        startup_process: LspProcess | None,
    ) -> bool:
        """Nothing of ours is left to close."""
        return (
            self._closed
            and self._process is None
            and cleanup_error is None
            and startup_process is None
        )

    def _close_attempt_locked(self) -> tuple[bool, _CloseTargets | None]:
        """Whether the close already happened, and what is ready to be closed."""
        cleanup_error = self._startup_cleanup_error
        startup_process = self._startup_process
        if self._close_finished_locked(cleanup_error, startup_process):
            return True, None
        self._reserve_close_locked()
        if self._starting or self._active_operations:
            return False, None
        return False, _CloseTargets(cleanup_error, startup_process, self._process)

    def _reserve_close_locked(self) -> None:
        """Claim the close before waiting, so nothing new starts behind it."""
        if not self._closing:
            self._closing = True
            self._condition.notify_all()

    def _close_attempt(self, deadline: float) -> tuple[bool, _CloseTargets | None]:
        self._acquire_state_lock(
            deadline,
            "Pyright close state lock deadline expired",
        )
        try:
            return self._close_attempt_locked()
        finally:
            self._lock.release()

    def _await_close_targets(self, deadline: float) -> _CloseTargets | None:
        """What to close, or None when this session was already closed."""
        while True:
            finished, targets = self._close_attempt(deadline)
            if finished:
                return None
            if targets is not None:
                return targets
            _sleep_before_close_retry(deadline)

    @staticmethod
    def _shut_down_targets(targets: _CloseTargets, deadline: float) -> None:
        """Close what this session owns, in the order it took them on."""
        try:
            _close_owned_targets(targets, deadline)
        except BaseException as error:
            _raise_collected_errors([], prior_error=error)

    def _forget_closed_owners_locked(self, targets: _CloseTargets) -> None:
        """Drop the owners we just closed, if they are still the current ones."""
        self._forget_closed_startup_locked(targets)
        self._sync_startup_atexit_locked()
        if self._process is targets.process:
            self._process = None
        self._bootstrap_owner_nonce = None

    def _forget_closed_startup_locked(self, targets: _CloseTargets) -> None:
        if self._startup_cleanup_error is targets.cleanup_error:
            self._startup_cleanup_error = None
        if self._startup_process is targets.startup_process:
            self._startup_process = None

    def _forget_session_state_locked(self) -> None:
        """Everything a running session accumulated is dropped on close."""
        self._reset_readiness_locked()
        self._documents.clear()
        self._document_bytes = 0
        self._readiness_target_uri = None
        self._forget_generation_locked()
        self._progress_events.clear()
        self._progress_bytes = 0

    def _finish_close(self, targets: _CloseTargets, deadline: float) -> None:
        self._acquire_state_lock(
            deadline,
            "Pyright close final state lock deadline expired",
        )
        try:
            self._forget_closed_owners_locked(targets)
            self._forget_session_state_locked()
            self._closing = False
            self._closed = True
            self._condition.notify_all()
        finally:
            self._lock.release()

    def _close_owned(self, deadline: float) -> None:
        targets = self._await_close_targets(deadline)
        if targets is None:
            return
        self._shut_down_targets(targets, deadline)
        self._finish_close(targets, deadline)


# The name the Pyright tests, `code_navigation` and the MCP server type-check
# against. One class drives every profile; keeping the alias leaves the Python
# path's own vocabulary intact rather than rewriting 5,000 lines for a second
# language. It is placed here, before the manager, so the manager's annotations
# resolve at definition time -- this module has no `from __future__ import
# annotations`.
PyrightSession = LanguageServerSession


def _sleep_before_close_retry(deadline: float) -> None:
    """Back off once while an operation finishes, or give up on the deadline."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Pyright operations did not finish before close")
    time.sleep(min(_LOCK_POLL_SECONDS, remaining))


def _close_owned_targets(targets: _CloseTargets, deadline: float) -> None:
    """Close what the session owns, in the order it took them on."""
    if targets.cleanup_error is not None:
        targets.cleanup_error.retry_cleanup(deadline)
    _close_owned_processes(targets, deadline)


def _close_owned_processes(targets: _CloseTargets, deadline: float) -> None:
    for process in (targets.startup_process, targets.process):
        if process is not None:
            process.close(deadline)


class _KeyLockState:
    __slots__ = ("lock", "reference_lock", "references")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reference_lock = threading.Lock()
        self.references = 0


@dataclass(frozen=True)
class _SessionLookup:
    """What one locked attempt at getting a session decided."""

    session: LanguageServerSession | None = None
    wait_for: LanguageServerSession | None = None
    reserved: tuple[object, PyrightSession] | None = None


def _require_get_arguments(
    repository: RepositoryScope, profile: LanguageServerProfile
) -> None:
    if not isinstance(repository, RepositoryScope):
        raise TypeError("repository must be a RepositoryScope")
    if not isinstance(profile, LanguageServerProfile):
        raise TypeError("profile must be a LanguageServerProfile")


def _discovered_identity(
    profile: LanguageServerProfile,
    repository: RepositoryScope,
    state_root: Path,
    deadline: float,
) -> PyrightIdentity:
    """Ask the right discovery for this profile, without installing anything.

    Pyright keeps its own, which accepts a project-local or system candidate and
    re-derives the whole identity for whichever wins. Every other profile is
    managed-root only; `scripts/lsp_identity.py` says why.
    """
    if profile is PYRIGHT_PROFILE:
        from pyright_profile import discover_pyright

        return discover_pyright(repository, state_root=state_root, deadline=deadline)
    return discover_managed_server(
        profile, repository, state_root=state_root, deadline=deadline
    )


class LanguageServerSessionManager:
    """Bound live language-server sessions to four processes per MCP process."""

    def __init__(self, *, state_root: Path) -> None:
        if not isinstance(state_root, Path):
            raise TypeError("state_root must be a Path")
        self._state_root = state_root
        self._lock = threading.RLock()
        self._sessions: dict[tuple[str, PyrightIdentity], LanguageServerSession] = {}
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
        profile: LanguageServerProfile = PYRIGHT_PROFILE,
    ) -> tuple[str, str, PyrightIdentity]:
        """One live process per (checkout, language, identity).

        The profile name is in the key because two languages in one checkout are
        two servers, not one contended session: without it a Python and a
        TypeScript request would collide on the same slot and evict each other.
        """
        return repository.checkout_id, profile.name, identity

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

    def _drop_key_lock_reference(
        self, key: tuple[str, PyrightIdentity], state: _KeyLockState
    ) -> None:
        """Give back one reference; the last one retires the lock."""
        state.references -= 1
        if state.references < 0:
            raise RuntimeError("Pyright session key lock reference underflow")
        if state.references == 0 and self._key_locks.get(key) is state:
            self._key_locks.pop(key, None)

    def _release_key_lock_reference_once(
        self, key: tuple[str, PyrightIdentity], state: _KeyLockState
    ) -> bool:
        """False when another thread holds the reference lock right now."""
        if not state.reference_lock.acquire(blocking=False):
            return False
        try:
            self._drop_key_lock_reference(key, state)
        finally:
            state.reference_lock.release()
        return True

    def _drain_key_lock_releases_locked(self) -> None:
        deferred: list[
            tuple[tuple[str, PyrightIdentity], _KeyLockState]
        ] = []
        while True:
            try:
                key, state = self._key_lock_releases.get_nowait()
            except queue.Empty:
                break
            if not self._release_key_lock_reference_once(key, state):
                deferred.append((key, state))
        for release in deferred:
            self._key_lock_releases.put(release)

    def _prune_one_key_lock(
        self, key: tuple[str, PyrightIdentity], state: _KeyLockState
    ) -> None:
        """Retire an unreferenced key lock, if nobody is touching it now."""
        if not state.reference_lock.acquire(blocking=False):
            return
        try:
            self._forget_unreferenced_key_lock(key, state)
        finally:
            state.reference_lock.release()

    def _prune_key_locks_locked(self) -> None:
        self._drain_key_lock_releases_locked()
        for key, state in tuple(self._key_locks.items()):
            self._prune_one_key_lock(key, state)

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
            self._forget_unreferenced_key_lock(key, state)
            raise TimeoutError(
                "Pyright session key lock reference deadline expired"
            )
        try:
            state.references += 1
        finally:
            state.reference_lock.release()
        return state

    def _forget_unreferenced_key_lock(
        self, key: tuple[str, PyrightIdentity], state: _KeyLockState
    ) -> None:
        if state.references == 0 and self._key_locks.get(key) is state:
            self._key_locks.pop(key, None)

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

    def _key_locks_released(self, deadline: float) -> bool:
        self._acquire_manager(deadline)
        try:
            self._prune_key_locks_locked()
            return not self._key_locks and self._key_lock_releases.empty()
        finally:
            self._lock.release()

    def _wait_for_key_lock_releases(self, deadline: float) -> None:
        while True:
            if self._key_locks_released(deadline):
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    "Pyright session key references did not release before deadline"
                )
            time.sleep(min(_LOCK_POLL_SECONDS, remaining))

    @staticmethod
    def _session_state(
        session: LanguageServerSession,
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
    ) -> list[tuple[tuple[str, PyrightIdentity], LanguageServerSession]]:
        live: list[tuple[tuple[str, PyrightIdentity], LanguageServerSession]] = []
        for key, session in tuple(self._sessions.items()):
            closed, _closing, _starting, _active, _last_used = self._session_state(
                session,
                deadline,
            )
            if closed:
                self._forget_session_locked(key, session)
                continue
            live.append((key, session))
        return live

    def _idle_entry(
        self,
        key: tuple[str, PyrightIdentity],
        session: LanguageServerSession,
        deadline: float,
    ) -> tuple[float, tuple[str, PyrightIdentity], LanguageServerSession] | None:
        """The session's last-used time, when it is idle enough to evict."""
        closed, closing, starting, active, last_used = self._session_state(
            session,
            deadline,
        )
        if closed or closing or starting:
            return None
        if active != 0:
            return None
        return last_used, key, session

    def _reserve_lru_idle_locked(
        self,
        live: list[tuple[tuple[str, PyrightIdentity], LanguageServerSession]],
        deadline: float,
    ) -> tuple[tuple[str, PyrightIdentity], LanguageServerSession] | None:
        idle: list[
            tuple[float, tuple[str, PyrightIdentity], LanguageServerSession]
        ] = []
        for key, session in live:
            entry = self._idle_entry(key, session, deadline)
            if entry is not None:
                idle.append(entry)
        for _last_used, key, session in sorted(idle, key=lambda item: item[0]):
            if session._reserve_idle_close(deadline):
                return key, session
        return None

    @staticmethod
    def _wait_for_session_close(session: LanguageServerSession, deadline: float) -> None:
        while True:
            closed, closing, _starting, _active, _last_used = (
                LanguageServerSessionManager._session_state(session, deadline)
            )
            if closed or not closing:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Pyright session close wait deadline expired")
            time.sleep(min(_LOCK_POLL_SECONDS, remaining))

    def _admit_manager_locked(self) -> None:
        if self._closed:
            raise RuntimeError("Pyright session manager is closed")

    def _forget_session_locked(self, key: object, session: LanguageServerSession) -> None:
        if self._sessions.get(key) is session:
            self._sessions.pop(key, None)

    def _new_session_locked(
        self,
        key: object,
        repository: RepositoryScope,
        identity: PyrightIdentity,
        profile: LanguageServerProfile,
    ) -> LanguageServerSession:
        session = LanguageServerSession(
            repository, identity, state_root=self._state_root, profile=profile
        )
        self._sessions[key] = session
        self._register_atexit_locked()
        return session

    def _capacity_denied_session(
        self,
        repository: RepositoryScope,
        identity: PyrightIdentity,
        profile: LanguageServerProfile,
    ) -> LanguageServerSession:
        """A session that refuses to start because the manager is at capacity."""
        denied = LanguageServerSession(
            repository, identity, state_root=self._state_root, profile=profile
        )
        denied._capacity_locked = True
        return denied

    def _existing_session_locked(
        self, key: object, deadline: float
    ) -> _SessionLookup | None:
        """What to do about a session already registered under this key."""
        existing = self._sessions.get(key)
        if existing is None:
            return None
        closed, closing, _starting, _active, _last_used = self._session_state(
            existing, deadline
        )
        if closed:
            self._forget_session_locked(key, existing)
            return None
        return _SessionLookup(wait_for=existing) if closing else _SessionLookup(
            session=existing
        )

    def _lookup_session_locked(
        self,
        key: object,
        repository: RepositoryScope,
        identity: PyrightIdentity,
        profile: LanguageServerProfile,
        deadline: float,
    ) -> _SessionLookup:
        """One locked attempt: a session, one to wait for, or one to evict."""
        self._admit_manager_locked()
        existing = self._existing_session_locked(key, deadline)
        if existing is not None:
            return existing
        live = self._live_entries_locked(deadline)
        if len(live) < MAX_LSP_PROCESSES:
            return _SessionLookup(
                session=self._new_session_locked(key, repository, identity, profile)
            )
        return self._crowded_lookup_locked(live, repository, identity, profile, deadline)

    def _crowded_lookup_locked(
        self,
        live: list,
        repository: RepositoryScope,
        identity: PyrightIdentity,
        profile: LanguageServerProfile,
        deadline: float,
    ) -> _SessionLookup:
        """At capacity: evict the least recently used idle session, or refuse."""
        reserved = self._reserve_lru_idle_locked(live, deadline)
        if reserved is None:
            return _SessionLookup(
                session=self._capacity_denied_session(repository, identity, profile)
            )
        return _SessionLookup(reserved=reserved)

    def _lookup_session(
        self,
        key: object,
        repository: RepositoryScope,
        identity: PyrightIdentity,
        profile: LanguageServerProfile,
        deadline: float,
    ) -> _SessionLookup:
        self._acquire_manager(deadline)
        try:
            return self._lookup_session_locked(
                key, repository, identity, profile, deadline
            )
        finally:
            self._lock.release()

    def _adopt_after_eviction_locked(
        self,
        evicted_key: object,
        evicted: LanguageServerSession,
        key: object,
        repository: RepositoryScope,
        identity: PyrightIdentity,
        profile: LanguageServerProfile,
        deadline: float,
    ) -> LanguageServerSession | None:
        """The session replacing the evicted one, or None to look again."""
        closed, _closing, _starting, _active, _last_used = self._session_state(
            evicted, deadline
        )
        if not closed:
            raise RuntimeError("eviction close did not release the session")
        self._forget_session_locked(evicted_key, evicted)
        self._admit_manager_locked()
        live = self._live_entries_locked(deadline)
        if len(live) >= MAX_LSP_PROCESSES:
            return None
        return self._new_session_locked(key, repository, identity, profile)

    def _evict_and_adopt(
        self,
        reserved: tuple[object, LanguageServerSession],
        key: object,
        repository: RepositoryScope,
        identity: PyrightIdentity,
        profile: LanguageServerProfile,
        deadline: float,
    ) -> LanguageServerSession | None:
        """Close the reserved session, then take its place if one is free."""
        evicted_key, evicted = reserved
        evicted.close(deadline=deadline)
        self._acquire_manager(deadline)
        try:
            return self._adopt_after_eviction_locked(
                evicted_key, evicted, key, repository, identity, profile, deadline
            )
        finally:
            self._lock.release()

    def _session_for_key(
        self,
        key: object,
        repository: RepositoryScope,
        identity: PyrightIdentity,
        profile: LanguageServerProfile,
        deadline: float,
    ) -> LanguageServerSession:
        """The session for this key, waiting or evicting until there is one."""
        while True:
            lookup = self._lookup_session(key, repository, identity, profile, deadline)
            if lookup.session is not None:
                return lookup.session
            adopted = self._advance_lookup(
                lookup, key, repository, identity, profile, deadline
            )
            if adopted is not None:
                return adopted

    def _advance_lookup(
        self,
        lookup: _SessionLookup,
        key: object,
        repository: RepositoryScope,
        identity: PyrightIdentity,
        profile: LanguageServerProfile,
        deadline: float,
    ) -> LanguageServerSession | None:
        """Wait out a closing session, or evict a reserved one; None to look again."""
        if lookup.wait_for is not None:
            self._wait_for_session_close(lookup.wait_for, deadline)
            return None
        assert lookup.reserved is not None
        return self._evict_and_adopt(
            lookup.reserved, key, repository, identity, profile, deadline
        )

    def _admit_get(
        self,
        repository: RepositoryScope,
        identity: PyrightIdentity,
        profile: LanguageServerProfile,
        deadline: float,
    ) -> LanguageServerSession | tuple[object, object]:
        """The key and its lock, or an unqualified session that needs neither."""
        self._acquire_manager(deadline)
        try:
            self._admit_manager_locked()
            if not identity.qualified:
                return LanguageServerSession(
                    repository, identity, state_root=self._state_root, profile=profile
                )
            key = self._profile_key(repository, identity, profile)
            return key, self._retain_key_lock_locked(key, deadline)
        finally:
            self._lock.release()

    def get(
        self,
        repository: RepositoryScope,
        *,
        deadline: float,
        profile: LanguageServerProfile = PYRIGHT_PROFILE,
    ) -> LanguageServerSession:
        deadline = _validated_deadline(deadline)
        _require_get_arguments(repository, profile)
        identity = _discovered_identity(profile, repository, self._state_root, deadline)
        admitted = self._admit_get(repository, identity, profile, deadline)
        if isinstance(admitted, LanguageServerSession):
            return admitted
        key, key_lock_state = admitted
        key_lock_acquired = False
        try:
            self._acquire_key_lock(key_lock_state.lock, deadline)
            key_lock_acquired = True
            return self._session_for_key(
                key, repository, identity, profile, deadline
            )
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
            error = self._close_one_session(key, session, deadline)
            if error is not None:
                errors.append(error)
        error = _released_error(
            lambda: self._wait_for_key_lock_releases(deadline)
        )
        if error is not None:
            errors.append(error)
        _raise_collected_errors(errors)

    def _forget_closed_session(
        self, key: object, session: LanguageServerSession, deadline: float
    ) -> None:
        """Drop a session we just closed, refusing one that did not release."""
        self._acquire_manager(deadline)
        try:
            closed, _closing, _starting, _active, _last_used = self._session_state(
                session, deadline
            )
            if not closed:
                raise RuntimeError(
                    "Pyright close_all close did not release the session"
                )
            self._forget_session_locked(key, session)
        finally:
            self._lock.release()

    def _close_one_session(
        self, key: object, session: LanguageServerSession, deadline: float
    ) -> BaseException | None:
        """Close one session and forget it; the error worth reporting, if any."""
        error = _released_error(lambda: session.close(deadline=deadline))
        if error is not None:
            return error
        return _released_error(
            lambda: self._forget_closed_session(key, session, deadline)
        )


# The manager name `scripts/mcp_server.py` imports. Same object.
PyrightSessionManager = LanguageServerSessionManager
