"""Pinned, read-only discovery for an explicitly installed Pyright runtime."""

from __future__ import annotations

import atexit
import json
import math
import os
import re
import shutil
import stat
import subprocess as _subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

import lsp_process_tree as _lsp_process_tree
from bounded_io import read_stable_bytes
from lsp_paths import PYRIGHT_VERSION, managed_pyright_root
from reliable_memory import _known_network_path, canonical_json_bytes, sha256_bytes
from repository_scope import RepositoryScope

ProcessTree = _lsp_process_tree.ProcessTree

PYRIGHT_PACKAGE_URL = "https://registry.npmjs.org/pyright/-/pyright-1.1.411.tgz"
PYRIGHT_PACKAGE_SHA256 = (
    "bd5c488fc20fa237a944279bf32cae2f986cf10d5d5d9e8705819859daeb2f4a"
)
PYRIGHT_PACKAGE_INTEGRITY = (
    "sha512-03S/vmS5lF1S/tVbKc2WNXCMq8JWCwta/qIYjj1jvqbQhoy+N3NgBzHTSmUlbYD6DJwqQ5XHf108QujoqeURvw=="
)
QUALIFIED_NODE_MAJOR = 22
PYRIGHT_SERVER_RELATIVE = Path("package/langserver.index.js")


def _freeze_pyright_profile_value(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze_pyright_profile_value(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_pyright_profile_value(item) for item in value)
    return value


def _thaw_mapping(value: Mapping) -> dict:
    return {key: thaw_pyright_profile_value(item) for key, item in value.items()}


def _thaw_tuple(value: tuple) -> list:
    return [thaw_pyright_profile_value(item) for item in value]


def thaw_pyright_profile_value(value: object) -> object:
    """Return a mutable JSON-domain copy of an immutable profile value."""
    if isinstance(value, Mapping):
        return _thaw_mapping(value)
    if isinstance(value, tuple):
        return _thaw_tuple(value)
    if value is None or isinstance(value, (bool, int, str)):
        return value
    raise TypeError(f"unsupported Pyright profile value: {type(value).__name__}")


PYRIGHT_CONFIGURATION = _freeze_pyright_profile_value({
    "python": {
        "analysis": {
            "autoSearchPaths": True,
            "diagnosticMode": "openFilesOnly",
            "logLevel": "Error",
            "useLibraryCodeForTypes": True,
        }
    },
    "pyright": {
        "disableLanguageServices": False,
        "disableOrganizeImports": True,
        "disableTaggedHints": False,
    },
})
PYRIGHT_INITIALIZATION_OPTIONS = _freeze_pyright_profile_value(
    {"files": {"exclude": []}}
)

PYRIGHT_INSTALL_MANIFEST_SCHEMA = "pyright-install/v1"
PYRIGHT_CONFIGURATION_SHA256 = sha256_bytes(
    canonical_json_bytes(thaw_pyright_profile_value(PYRIGHT_CONFIGURATION))
)
PYRIGHT_INITIALIZATION_OPTIONS_SHA256 = sha256_bytes(
    canonical_json_bytes(thaw_pyright_profile_value(PYRIGHT_INITIALIZATION_OPTIONS))
)

MAX_PACKAGE_JSON_BYTES = 64 * 1024
MAX_PACKAGE_LOCK_BYTES = 8 * 1024 * 1024
MAX_INSTALL_MANIFEST_BYTES = 16 * 1024
MAX_PYRIGHT_CONFIG_BYTES = 256 * 1024
MAX_PYRIGHT_CONFIG_TOTAL_BYTES = 512 * 1024
MAX_PYRIGHT_CONFIG_FILES = 9
MAX_PYRIGHT_CONFIG_EXTENDS_DEPTH = 8
MAX_PYRIGHT_CONFIG_DOMAIN_DEPTH = 64
MAX_PYRIGHT_CONFIG_DOMAIN_NODES = 65_536
MAX_PYRIGHT_MANIFEST_DOMAIN_DEPTH = 64
MAX_PYRIGHT_MANIFEST_DOMAIN_NODES = 4096
MAX_SERVER_BYTES = 64 * 1024 * 1024
MAX_NODE_VERSION_BYTES = 128
NODE_PROBE_TIMEOUT_SECONDS = 2.0
NODE_PROBE_CLEANUP_SECONDS = 0.5
_MAX_NODE_PROBE_OWNERS = 8

_NODE_ENV_ALLOWLIST = frozenset(
    {
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "WINDIR",
    }
)

_HEX_SHA256 = re.compile(r"[0-9a-f]{64}")
_PACKAGE_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_NODE_VERSION = re.compile(rb"v([0-9]+)\.([0-9]+)\.([0-9]+)(?:\r?\n)?")
_NODE_PROBE_ERRORS = (
    OSError,
    TypeError,
    ValueError,
    RuntimeError,
    _subprocess.SubprocessError,
)
_MANIFEST_KEYS = frozenset(
    {
        "configuration_sha256",
        "initialization_options_sha256",
        "package_integrity",
        "package_sha256",
        "package_url",
        "schema_version",
        "server_relative_path",
        "server_sha256",
        "version",
    }
)


class _SubprocessFacade:
    Popen = _subprocess.Popen
    DEVNULL = _subprocess.DEVNULL
    PIPE = _subprocess.PIPE
    STDOUT = _subprocess.STDOUT
    TimeoutExpired = _subprocess.TimeoutExpired


subprocess = _SubprocessFacade()

_NODE_PROBE_OWNERS_LOCK = threading.Lock()
_NODE_PROBE_DRAIN_LOCK = threading.Lock()
_NODE_PROBE_OWNERS: set[object] = set()
_PENDING_NODE_PROBE_CLEANUPS: dict[object, object] = {}


@dataclass(frozen=True, slots=True)
class PyrightIdentity:
    status: str
    source: str | None
    version: str | None
    node_executable: Path | None
    node_version: str | None
    node_major: int | None
    server_executable: Path | None
    executable_sha256: str | None
    package_sha256: str | None
    initialization_options_sha256: str
    configuration_sha256: str
    qualified: bool
    degradation_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PyrightCandidates:
    project_local: tuple[Path, ...]
    managed: tuple[Path, ...]
    system: tuple[Path, ...]


class _MetadataError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _ManifestValidationError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def build_pyright_install_manifest(*, server_sha256: str) -> dict[str, str]:
    """Build the closed canonical receipt value used by the explicit installer."""
    if not isinstance(server_sha256, str) or _HEX_SHA256.fullmatch(server_sha256) is None:
        raise ValueError("server_sha256 must be a lowercase SHA-256 digest")
    return {
        "configuration_sha256": PYRIGHT_CONFIGURATION_SHA256,
        "initialization_options_sha256": PYRIGHT_INITIALIZATION_OPTIONS_SHA256,
        "package_integrity": PYRIGHT_PACKAGE_INTEGRITY,
        "package_sha256": PYRIGHT_PACKAGE_SHA256,
        "package_url": PYRIGHT_PACKAGE_URL,
        "schema_version": PYRIGHT_INSTALL_MANIFEST_SCHEMA,
        "server_relative_path": PYRIGHT_SERVER_RELATIVE.as_posix(),
        "server_sha256": server_sha256,
        "version": PYRIGHT_VERSION,
    }


_MANIFEST_CHECKS = (
    ("schema_version", "pyright_manifest_schema_mismatch"),
    ("version", "pyright_version_mismatch"),
    ("package_url", "pyright_package_url_mismatch"),
    ("package_sha256", "pyright_package_sha256_mismatch"),
    ("package_integrity", "pyright_integrity_mismatch"),
    ("server_relative_path", "pyright_server_relative_mismatch"),
    ("configuration_sha256", "pyright_configuration_mismatch"),
    ("initialization_options_sha256", "pyright_initialization_options_mismatch"),
)


def _manifest_shape_is_valid(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != _MANIFEST_KEYS:
        return False
    if any(not isinstance(item, str) for item in value.values()):
        return False
    return _HEX_SHA256.fullmatch(value["server_sha256"]) is not None


def validate_pyright_install_manifest(value: object) -> dict[str, str]:
    """Validate the install receipt's closed pinned domain."""
    if not _manifest_shape_is_valid(value):
        raise _ManifestValidationError("pyright_manifest_malformed")
    expected = build_pyright_install_manifest(server_sha256=value["server_sha256"])
    for field, code in _MANIFEST_CHECKS:
        if value[field] != expected[field]:
            raise _ManifestValidationError(code)
    return {field: value[field] for field in sorted(_MANIFEST_KEYS)}


def _validated_deadline(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    if (
        isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or not math.isfinite(deadline)
    ):
        raise ValueError("deadline must be a finite monotonic timestamp or None")
    return float(deadline)


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("Pyright discovery deadline expired")


def _require_environment_entry(name: object, value: object) -> None:
    if not isinstance(name, str) or not isinstance(value, str):
        raise TypeError("environment names and values must be strings")
    if "\0" in name or "\0" in value:
        raise ValueError("environment names and values must not contain NUL")


def _require_windows_system_root(values: Mapping[str, str]) -> None:
    if os.name != "nt":
        return
    system_root = values.get("SYSTEMROOT")
    if not system_root or not Path(system_root).is_absolute() or not Path(system_root).is_dir():
        raise ValueError("SYSTEMROOT must be an inherited existing directory on Windows")


def _node_environment() -> dict[str, str]:
    values = os.environ
    for name, value in values.items():
        _require_environment_entry(name, value)
    _require_windows_system_root(values)
    return {name: values[name] for name in sorted(_NODE_ENV_ALLOWLIST) if name in values}


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_number(_value: str) -> object:
    raise ValueError("non-integral JSON number")


def _strict_json_object(
    raw: bytes,
    malformed_code: str,
    *,
    recursion_code: str | None = None,
) -> dict[str, object]:
    try:
        text = raw.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_float=_reject_json_number,
            parse_constant=_reject_json_number,
        )
    except RecursionError as exc:
        raise _MetadataError(recursion_code or malformed_code) from exc
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _MetadataError(malformed_code) from exc
    if not isinstance(value, dict):
        raise _MetadataError(malformed_code)
    return value


def _blank_unless_newline(character: str) -> str:
    if character in "\r\n":
        return character
    return " "


class _CommentStripper:
    """Replace JSONC comments with spaces (newlines kept) outside strings."""

    def __init__(self, text: str, malformed_code: str) -> None:
        self.text = text
        self.code = malformed_code
        self.out: list[str] = []
        self.index = 0
        self.in_string = False
        self.escaped = False

    def run(self) -> list[str]:
        while self.index < len(self.text):
            self._step()
        return self.out

    def _keep(self, character: str) -> None:
        self.out.append(character)
        self.index += 1

    def _advance_string_state(self, character: str) -> None:
        if self.escaped:
            self.escaped = False
            return
        if character == "\\":
            self.escaped = True
            return
        if character == '"':
            self.in_string = False

    def _step(self) -> None:
        character = self.text[self.index]
        if self.in_string:
            self._advance_string_state(character)
            self._keep(character)
            return
        if character == '"':
            self.in_string = True
            self._keep(character)
            return
        if character != "/" or self.index + 1 >= len(self.text):
            self._keep(character)
            return
        self._comment_or_slash(self.text[self.index + 1], character)

    def _comment_or_slash(self, marker: str, character: str) -> None:
        if marker == "/":
            self._skip_line_comment()
            return
        if marker != "*":
            self._keep(character)
            return
        self._skip_block_comment()

    def _skip_line_comment(self) -> None:
        self.out.extend((" ", " "))
        self.index += 2
        while self.index < len(self.text) and self.text[self.index] not in "\r\n":
            self.out.append(" ")
            self.index += 1

    def _at_block_end(self) -> bool:
        return self.text[self.index] == "*" and self.text[self.index + 1 : self.index + 2] == "/"

    def _skip_block_comment(self) -> None:
        self.out.extend((" ", " "))
        self.index += 2
        while self.index < len(self.text):
            if self._at_block_end():
                self.out.extend((" ", " "))
                self.index += 2
                return
            self.out.append(_blank_unless_newline(self.text[self.index]))
            self.index += 1
        raise _MetadataError(self.code)


class _TrailingCommaPass:
    """Blank a comma that directly precedes `}` or `]` outside strings."""

    def __init__(self, normalized: list[str]) -> None:
        self.normalized = normalized
        self.pending_comma: int | None = None
        self.previous: str | None = None
        self.in_string = False
        self.escaped = False

    def run(self) -> list[str]:
        for index, character in enumerate(self.normalized):
            self._step(index, character)
        return self.normalized

    def _string_character(self, character: str) -> None:
        if self.escaped:
            self.escaped = False
            return
        if character == "\\":
            self.escaped = True
            return
        if character == '"':
            self.in_string = False
            self.previous = character

    def _comma(self, index: int) -> None:
        if self.previous in {None, "{", "[", ",", ":"}:
            self.pending_comma = None
        else:
            self.pending_comma = index
        self.previous = ","

    def _significant(self, character: str) -> None:
        if character in "}]" and self.pending_comma is not None:
            self.normalized[self.pending_comma] = " "
        self.pending_comma = None
        self.previous = character

    def _step(self, index: int, character: str) -> None:
        if self.in_string:
            self._string_character(character)
            return
        if character == '"':
            self.pending_comma = None
            self.previous = character
            self.in_string = True
            return
        if character == ",":
            self._comma(index)
            return
        if character not in " \t\r\n":
            self._significant(character)


def _normalize_jsonc(raw: bytes, malformed_code: str) -> bytes:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _MetadataError(malformed_code) from exc
    normalized = _CommentStripper(text, malformed_code).run()
    return "".join(_TrailingCommaPass(normalized).run()).encode("utf-8")


def _read_json_object(
    path: Path,
    max_bytes: int,
    *,
    prefix: str,
    deadline: float | None,
    recursion_code: str | None = None,
) -> tuple[dict[str, object], bytes]:
    _check_deadline(deadline)
    try:
        raw = read_stable_bytes(path, max_bytes, label=prefix.replace("_", " "))
    except FileNotFoundError as exc:
        raise _MetadataError(f"{prefix}_missing") from exc
    except PermissionError as exc:
        raise _MetadataError(f"{prefix}_unsafe") from exc
    except ValueError as exc:
        raise _MetadataError(f"{prefix}_oversized") from exc
    except OSError as exc:
        raise _MetadataError(f"{prefix}_unreadable") from exc
    _check_deadline(deadline)
    return (
        _strict_json_object(
            raw,
            f"{prefix}_malformed",
            recursion_code=recursion_code,
        ),
        raw,
    )


def _require_domain_bounds(nodes: int, depth: int, prefix: str, max_depth: int, max_nodes: int) -> None:
    if nodes > max_nodes:
        raise _MetadataError(f"{prefix}_too_many_nodes")
    if depth > max_depth:
        raise _MetadataError(f"{prefix}_too_deep")


def _object_children(item: dict, prefix: str) -> tuple:
    if any(not isinstance(key, str) for key in item):
        raise _MetadataError(f"{prefix}_unsupported_value")
    return tuple(item.values())


def _domain_children(item: object, prefix: str) -> tuple | None:
    """The children of a container; None for a scalar; a refusal for anything else."""
    if item is None or isinstance(item, (bool, int, str)):
        return None
    if isinstance(item, dict):
        return _object_children(item, prefix)
    if isinstance(item, list):
        return tuple(item)
    raise _MetadataError(f"{prefix}_unsupported_value")


def _push_domain_children(
    stack: list, children: tuple, nodes: int, depth: int, prefix: str, max_nodes: int
) -> None:
    if nodes + len(stack) + len(children) > max_nodes:
        raise _MetadataError(f"{prefix}_too_many_nodes")
    stack.extend((child, depth + 1) for child in reversed(children))


def _validate_canonical_domain(
    value: object,
    *,
    prefix: str,
    max_depth: int,
    max_nodes: int,
    deadline: float | None,
) -> None:
    stack: list[tuple[object, int]] = [(value, 0)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        _require_domain_bounds(nodes, depth, prefix, max_depth, max_nodes)
        if nodes & 255 == 0:
            _check_deadline(deadline)
        children = _domain_children(item, prefix)
        if children is None:
            continue
        _push_domain_children(stack, children, nodes, depth, prefix, max_nodes)
    _check_deadline(deadline)


def _server_digest(path: Path, deadline: float | None) -> tuple[str | None, str | None]:
    _check_deadline(deadline)
    try:
        content = read_stable_bytes(path, MAX_SERVER_BYTES, label="Pyright server")
    except FileNotFoundError:
        return None, "pyright_server_missing"
    except PermissionError:
        return None, "pyright_server_unsafe"
    except ValueError:
        return None, "pyright_server_oversized"
    except OSError:
        return None, "pyright_server_unreadable"
    _check_deadline(deadline)
    return sha256_bytes(content), None


def _repository_config_entrypoint(
    repository_root: Path,
    deadline: float | None,
) -> Path | None:
    for path in (
        repository_root / "pyrightconfig.json",
        repository_root / "pyproject.toml",
    ):
        _check_deadline(deadline)
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        except PermissionError as exc:
            raise _MetadataError("pyright_repository_config_unsafe") from exc
        except OSError as exc:
            raise _MetadataError("pyright_repository_config_unreadable") from exc
        return path
    return None


def _read_config_bytes(path: Path) -> bytes:
    try:
        return read_stable_bytes(path, MAX_PYRIGHT_CONFIG_BYTES, label="Pyright repository config")
    except FileNotFoundError as exc:
        raise _MetadataError("pyright_repository_config_missing") from exc
    except PermissionError as exc:
        raise _MetadataError("pyright_repository_config_unsafe") from exc
    except ValueError as exc:
        raise _MetadataError("pyright_repository_config_oversized") from exc
    except OSError as exc:
        raise _MetadataError("pyright_repository_config_unreadable") from exc


def _validate_repository_domain(configuration: dict[str, object], deadline: float | None) -> None:
    _validate_canonical_domain(
        configuration,
        prefix="pyright_repository_config",
        max_depth=MAX_PYRIGHT_CONFIG_DOMAIN_DEPTH,
        max_nodes=MAX_PYRIGHT_CONFIG_DOMAIN_NODES,
        deadline=deadline,
    )


def _json_repository_config(raw: bytes, deadline: float | None) -> dict[str, object]:
    configuration = _strict_json_object(
        _normalize_jsonc(raw, "pyright_repository_config_malformed"),
        "pyright_repository_config_malformed",
        recursion_code="pyright_repository_config_too_deep",
    )
    _validate_repository_domain(configuration, deadline)
    return configuration


def _toml_document(raw: bytes) -> dict:
    try:
        return tomllib.loads(raw.decode("utf-8", errors="strict"))
    except RecursionError as exc:
        raise _MetadataError("pyright_repository_config_too_deep") from exc
    except (UnicodeDecodeError, ValueError) as exc:
        raise _MetadataError("pyright_repository_config_malformed") from exc


def _toml_pyright_table(document: dict, root_pyproject: bool) -> dict[str, object]:
    tool = document.get("tool")
    configuration = tool.get("pyright") if isinstance(tool, dict) else None
    if isinstance(configuration, dict):
        return configuration
    if root_pyproject:
        raise _MetadataError("pyright_repository_config_ancestor_search")
    raise _MetadataError("pyright_repository_config_malformed")


def _toml_repository_config(
    raw: bytes, root_pyproject: bool, deadline: float | None
) -> dict[str, object]:
    configuration = _toml_pyright_table(_toml_document(raw), root_pyproject)
    _validate_repository_domain(configuration, deadline)
    return configuration


def _read_repository_config(
    path: Path,
    *,
    root_pyproject: bool,
    deadline: float | None,
) -> tuple[dict[str, object], bytes]:
    suffix = path.suffix.casefold()
    if suffix not in {".json", ".toml"}:
        raise _MetadataError("pyright_repository_config_unsupported_format")
    raw = _read_config_bytes(path)
    _check_deadline(deadline)
    if suffix == ".json":
        return _json_repository_config(raw, deadline), raw
    return _toml_repository_config(raw, root_pyproject, deadline), raw


def _relative_extends_path(value: str) -> Path:
    if not value or "\0" in value:
        raise _MetadataError("pyright_repository_config_extends_invalid")
    try:
        relative = Path(value)
    except (TypeError, ValueError) as exc:
        raise _MetadataError("pyright_repository_config_extends_invalid") from exc
    if relative.is_absolute():
        raise _MetadataError("pyright_repository_config_extends_absolute")
    return relative


def _contained_repository_config_path(
    value: str,
    *,
    current: Path,
    repository_root: Path,
) -> Path:
    relative = _relative_extends_path(value)
    candidate = _lexical_absolute_path(current.parent / relative)
    try:
        candidate.relative_to(repository_root)
    except ValueError as exc:
        raise _MetadataError("pyright_repository_config_outside_repository") from exc
    if candidate.suffix.casefold() not in {".json", ".toml"}:
        raise _MetadataError("pyright_repository_config_unsupported_format")
    return candidate


class _ConfigChain:
    """The configurations read so far, the files visited, and their total size."""

    def __init__(self) -> None:
        self.configurations: list[dict[str, object]] = []
        self.visited: set[Path] = set()
        self.total_bytes = 0


def _require_chain_step(chain: _ConfigChain, current: Path) -> None:
    if current in chain.visited:
        raise _MetadataError("pyright_repository_config_extends_cycle")
    if len(chain.configurations) >= MAX_PYRIGHT_CONFIG_FILES:
        raise _MetadataError("pyright_repository_config_extends_too_deep")


def _record_configuration(
    chain: _ConfigChain, current: Path, repository_root: Path, configuration: dict, raw: bytes
) -> None:
    chain.total_bytes += len(raw)
    if chain.total_bytes > MAX_PYRIGHT_CONFIG_TOTAL_BYTES:
        raise _MetadataError("pyright_repository_config_total_oversized")
    relative_path = current.relative_to(repository_root)
    chain.configurations.append(
        {
            "configuration": configuration,
            "source_directory": relative_path.parent.as_posix(),
            "source_path": relative_path.as_posix(),
        }
    )


def _next_extends(
    chain: _ConfigChain, configuration: dict, current: Path, repository_root: Path
) -> Path | None:
    extends = configuration.get("extends")
    if extends is None:
        return None
    if not isinstance(extends, str):
        raise _MetadataError("pyright_repository_config_extends_invalid")
    if len(chain.configurations) - 1 >= MAX_PYRIGHT_CONFIG_EXTENDS_DEPTH:
        raise _MetadataError("pyright_repository_config_extends_too_deep")
    return _contained_repository_config_path(
        extends, current=current, repository_root=repository_root
    )


def _repository_configuration_chain(
    repository: RepositoryScope,
    deadline: float | None,
) -> list[dict[str, object]] | None:
    repository_root = _lexical_absolute_path(Path(repository.checkout_root))
    current = _repository_config_entrypoint(repository_root, deadline)
    if current is None:
        return None
    chain = _ConfigChain()
    while current is not None:
        _check_deadline(deadline)
        _require_chain_step(chain, current)
        chain.visited.add(current)
        root_pyproject = not chain.configurations and current.name == "pyproject.toml"
        configuration, raw = _read_repository_config(
            current, root_pyproject=root_pyproject, deadline=deadline
        )
        _record_configuration(chain, current, repository_root, configuration, raw)
        current = _next_extends(chain, configuration, current, repository_root)
    chain.configurations.reverse()
    return chain.configurations


def _repository_configuration_identity(
    repository: RepositoryScope,
    deadline: float | None,
) -> tuple[str, set[str]]:
    _check_deadline(deadline)
    try:
        configuration_chain = _repository_configuration_chain(repository, deadline)
    except _MetadataError as exc:
        return PYRIGHT_CONFIGURATION_SHA256, {exc.code}
    if configuration_chain is None:
        return PYRIGHT_CONFIGURATION_SHA256, set()
    envelope = {
        "base_lsp_configuration": thaw_pyright_profile_value(PYRIGHT_CONFIGURATION),
        "repository_configuration_chain": configuration_chain,
    }
    try:
        fingerprint = sha256_bytes(canonical_json_bytes(envelope))
    except RecursionError:
        return PYRIGHT_CONFIGURATION_SHA256, {"pyright_repository_config_too_deep"}
    except (TypeError, ValueError):
        return PYRIGHT_CONFIGURATION_SHA256, {
            "pyright_repository_config_unsupported_value"
        }
    _check_deadline(deadline)
    return fingerprint, set()


def _package_version_codes(observed: object) -> tuple[str | None, set[str]]:
    if not isinstance(observed, str) or _PACKAGE_VERSION.fullmatch(observed) is None:
        return None, {"pyright_package_json_malformed"}
    if observed != PYRIGHT_VERSION:
        return observed, {"pyright_version_mismatch"}
    return observed, set()


def _package_identity(
    server: Path,
    deadline: float | None,
) -> tuple[str | None, set[str]]:
    try:
        package, _raw = _read_json_object(
            server.with_name("package.json"),
            MAX_PACKAGE_JSON_BYTES,
            prefix="pyright_package_json",
            deadline=deadline,
        )
    except _MetadataError as exc:
        return None, {exc.code}
    codes: set[str] = set()
    name = package.get("name")
    if not isinstance(name, str) or name != "pyright":
        codes.add("pyright_package_mismatch")
    version, version_codes = _package_version_codes(package.get("version"))
    return version, codes | version_codes


def _lockfile_path(source: str, server: Path, repository: RepositoryScope) -> Path | None:
    if source == "project-local":
        return Path(repository.checkout_root) / "package-lock.json"
    package_root = server.parent
    if package_root.name != "pyright" or package_root.parent.name != "node_modules":
        return None
    return package_root.parent.parent / "package-lock.json"


def _lockfile_entries(value: dict, lockfile_version: int) -> tuple[object, str] | None:
    """(the entry table, the pyright key) for a supported lockfile version."""
    if lockfile_version == 1:
        return value.get("dependencies"), "pyright"
    if lockfile_version in {2, 3}:
        return value.get("packages"), "node_modules/pyright"
    return None


def _pyright_lockfile_entry(entries: object, key: str) -> dict | str:
    if not isinstance(entries, dict):
        return "pyright_lockfile_malformed"
    entry = entries.get(key)
    if not isinstance(entry, dict):
        return "pyright_lockfile_entry_missing"
    if "link" in entry:
        return "pyright_lockfile_link"
    return entry


def _lockfile_entry(value: dict) -> dict | str:
    """The pyright entry of a lockfile, or the degradation code that stands for it."""
    lockfile_version = value.get("lockfileVersion")
    if isinstance(lockfile_version, bool) or not isinstance(lockfile_version, int):
        return "pyright_lockfile_malformed"
    located = _lockfile_entries(value, lockfile_version)
    if located is None:
        return "pyright_lockfile_unsupported"
    return _pyright_lockfile_entry(*located)


def _entry_field_codes(entry: dict) -> set[str]:
    codes: set[str] = set()
    version = entry.get("version")
    integrity = entry.get("integrity")
    if not isinstance(version, str):
        codes.add("pyright_lockfile_malformed")
    elif version != PYRIGHT_VERSION:
        codes.add("pyright_version_mismatch")
    if not isinstance(integrity, str):
        codes.add("pyright_lockfile_malformed")
    elif integrity != PYRIGHT_PACKAGE_INTEGRITY:
        codes.add("pyright_integrity_mismatch")
    return codes


def _lockfile_codes(
    source: str,
    server: Path,
    repository: RepositoryScope,
    deadline: float | None,
) -> set[str]:
    lockfile = _lockfile_path(source, server, repository)
    if lockfile is None:
        return {"pyright_lockfile_missing"}
    try:
        value, _raw = _read_json_object(
            lockfile, MAX_PACKAGE_LOCK_BYTES, prefix="pyright_lockfile", deadline=deadline
        )
    except _MetadataError as exc:
        return {exc.code}
    entry = _lockfile_entry(value)
    if isinstance(entry, str):
        return {entry}
    return _entry_field_codes(entry)


def _read_managed_manifest(root: Path, deadline: float | None) -> tuple[dict[str, object], bytes]:
    value, raw = _read_json_object(
        root / "install-manifest.json",
        MAX_INSTALL_MANIFEST_BYTES,
        prefix="pyright_manifest",
        deadline=deadline,
        recursion_code="pyright_manifest_too_deep",
    )
    _validate_canonical_domain(
        value,
        prefix="pyright_manifest",
        max_depth=MAX_PYRIGHT_MANIFEST_DOMAIN_DEPTH,
        max_nodes=MAX_PYRIGHT_MANIFEST_DOMAIN_NODES,
        deadline=deadline,
    )
    return value, raw


def _canonical_form_codes(value: dict, raw: bytes) -> set[str]:
    try:
        if canonical_json_bytes(value) != raw:
            return {"pyright_manifest_noncanonical"}
    except RecursionError:
        return {"pyright_manifest_too_deep"}
    except (TypeError, ValueError):
        return {"pyright_manifest_unsupported_value"}
    return set()


def _manifest_validation_codes(value: dict) -> set[str]:
    try:
        validate_pyright_install_manifest(value)
    except _ManifestValidationError as exc:
        return {exc.code}
    return set()


def _hex_digest_or_none(value: object) -> str | None:
    if isinstance(value, str) and _HEX_SHA256.fullmatch(value) is not None:
        return value
    return None


def _executable_digest_codes(receipt_server_sha256: object, executable_sha256: str | None) -> set[str]:
    receipt = _hex_digest_or_none(receipt_server_sha256)
    if receipt is None or executable_sha256 is None or receipt == executable_sha256:
        return set()
    return {"pyright_executable_digest_mismatch"}


def _managed_manifest(
    server: Path,
    executable_sha256: str | None,
    deadline: float | None,
) -> tuple[str | None, set[str]]:
    codes: set[str] = set()
    if Path(server.parent.name) / server.name != PYRIGHT_SERVER_RELATIVE:
        codes.add("pyright_server_relative_mismatch")
    try:
        value, raw = _read_managed_manifest(server.parent.parent, deadline)
    except _MetadataError as exc:
        return None, {exc.code, *codes}
    codes |= _canonical_form_codes(value, raw)
    codes |= _manifest_validation_codes(value)
    codes |= _executable_digest_codes(value.get("server_sha256"), executable_sha256)
    return _hex_digest_or_none(value.get("package_sha256")), codes


def _reserve_node_probe_owner() -> object | None:
    owner = object()
    with _NODE_PROBE_OWNERS_LOCK:
        if len(_NODE_PROBE_OWNERS) >= _MAX_NODE_PROBE_OWNERS:
            return None
        _NODE_PROBE_OWNERS.add(owner)
    return owner


def _release_node_probe_owner(owner: object) -> None:
    with _NODE_PROBE_OWNERS_LOCK:
        _PENDING_NODE_PROBE_CLEANUPS.pop(owner, None)
        _NODE_PROBE_OWNERS.discard(owner)


def _retain_node_probe_owner(owner: object, owned: object) -> None:
    with _NODE_PROBE_OWNERS_LOCK:
        if owner not in _NODE_PROBE_OWNERS:
            raise RuntimeError("Node probe cleanup owner was not reserved")
        _PENDING_NODE_PROBE_CLEANUPS[owner] = owned


def _pending_node_probe_cleanup_snapshot() -> tuple[object, ...]:
    with _NODE_PROBE_OWNERS_LOCK:
        return tuple(_PENDING_NODE_PROBE_CLEANUPS.values())


def _pending_node_probe_cleanup_items() -> tuple[tuple[object, object], ...]:
    with _NODE_PROBE_OWNERS_LOCK:
        return tuple(_PENDING_NODE_PROBE_CLEANUPS.items())


def _terminate_node_probe_tree(tree: object, cleanup_deadline: float) -> bool:
    try:
        tree.terminate(deadline=cleanup_deadline)
    except _NODE_PROBE_ERRORS:
        return False
    return True


def _close_process_stream(process: object, name: str) -> bool:
    try:
        stream = getattr(process, name)
    except _NODE_PROBE_ERRORS:
        return False
    if stream is None or getattr(stream, "closed", False):
        return True
    try:
        stream.close()
    except _NODE_PROBE_ERRORS:
        return False
    return True


def _release_node_probe_tree(tree: object) -> bool:
    process = tree.process
    closed = [_close_process_stream(process, name) for name in ("stdin", "stdout", "stderr")]
    if not all(closed):
        return False
    try:
        tree.close()
    except _NODE_PROBE_ERRORS:
        return False
    return True


def _cleanup_node_probe_owner(owned: object, cleanup_deadline: float) -> bool:
    if isinstance(owned, _lsp_process_tree._ProcessTreeSpawnError):
        job = owned.windows_job
        if job is None:
            return True
        try:
            _lsp_process_tree._close_windows_handle(job)
        except _NODE_PROBE_ERRORS:
            return False
        owned.windows_job = None
        return True
    if not _terminate_node_probe_tree(owned, cleanup_deadline):
        return False
    return _release_node_probe_tree(owned)


def _retry_one_cleanup(owner: object, owned: object, cleanup_deadline: float) -> None:
    try:
        released = _cleanup_node_probe_owner(owned, cleanup_deadline)
    except BaseException:
        return
    if released:
        _release_node_probe_owner(owner)


def _drain_pending_cleanups(cleanup_deadline: float) -> None:
    for owner, owned in _pending_node_probe_cleanup_items():
        if time.monotonic() >= cleanup_deadline:
            return
        _retry_one_cleanup(owner, owned, cleanup_deadline)


def _retry_node_probe_cleanups(cleanup_deadline: float | None = None) -> None:
    """Retry all retained probes within one shared cleanup budget."""
    if not _pending_node_probe_cleanup_snapshot():
        return
    if not _NODE_PROBE_DRAIN_LOCK.acquire(blocking=False):
        return
    try:
        if cleanup_deadline is None:
            cleanup_deadline = time.monotonic() + NODE_PROBE_CLEANUP_SECONDS
        _drain_pending_cleanups(cleanup_deadline)
    finally:
        _NODE_PROBE_DRAIN_LOCK.release()


def _atexit_cleanup_node_probes() -> None:
    try:
        _retry_node_probe_cleanups()
    except BaseException:
        pass


def _lstat_is_plain(info: os.stat_result, kind_check) -> bool:
    """Not a link, not a reparse point, and of the expected kind."""
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        return False
    return bool(kind_check(info.st_mode))


def _parents_are_plain_directories(node: Path, deadline: float | None) -> bool:
    for parent in node.parents:
        if parent == Path(parent.anchor):
            return True
        _check_deadline(deadline)
        if not _lstat_is_plain(parent.lstat(), stat.S_ISDIR):
            return False
    return True


def _node_path_is_plain(node: Path, deadline: float | None) -> bool:
    if not _parents_are_plain_directories(node, deadline):
        return False
    _check_deadline(deadline)
    if not _lstat_is_plain(node.lstat(), stat.S_ISREG):
        return False
    return not _known_network_path(node)


def _node_shape_is_safe(node: Path) -> bool:
    if not _is_local_absolute_path(node):
        return False
    return not (os.name == "nt" and node.suffix.casefold() in {".bat", ".cmd"})


def _node_executable_is_safe(node: Path, deadline: float | None) -> bool:
    if not _node_shape_is_safe(node):
        return False
    try:
        plain = _node_path_is_plain(node, deadline)
    except (OSError, TypeError, ValueError, RuntimeError):
        return False
    if not plain:
        return False
    _check_deadline(deadline)
    return True


def _node_from_path(environment: dict[str, str], deadline: float | None) -> tuple[Path | None, str | None]:
    """(node path, degradation code); the code is set when the probe stops here."""
    try:
        found = shutil.which("node", path=environment.get("PATH", ""))
    except _NODE_PROBE_ERRORS:
        return None, "pyright_node_probe_failed"
    _check_deadline(deadline)
    if found is None:
        return None, "pyright_node_missing"
    try:
        node = Path(found)
    except (TypeError, ValueError):
        return None, "pyright_node_probe_failed"
    if not _node_executable_is_safe(node, deadline):
        return node, "pyright_node_executable_unsafe"
    return node, None


def _located_node(deadline: float | None) -> tuple[dict[str, str] | None, Path | None, str | None]:
    try:
        environment = _node_environment()
    except (OSError, TypeError, ValueError):
        return None, None, "pyright_node_probe_failed"
    node, code = _node_from_path(environment, deadline)
    return environment, node, code


def _probe_window(deadline: float | None) -> tuple[float, float] | None:
    """(probe deadline, hard cleanup deadline), or None when no time is left."""
    now = time.monotonic()
    probe_deadline = now + NODE_PROBE_TIMEOUT_SECONDS
    if deadline is not None:
        probe_deadline = min(probe_deadline, deadline)
    if probe_deadline - now <= 0:
        return None
    hard_cleanup_deadline = probe_deadline + NODE_PROBE_CLEANUP_SECONDS
    _retry_node_probe_cleanups(
        min(time.monotonic() + NODE_PROBE_CLEANUP_SECONDS, hard_cleanup_deadline)
    )
    if time.monotonic() >= probe_deadline:
        return None
    return probe_deadline, hard_cleanup_deadline


def _spawn_probe(node: Path, environment: dict[str, str], probe_deadline: float, owner: object):
    """The spawned tree, or None; the owner is released or retained as the failure demands."""
    try:
        return ProcessTree.spawn_with_deadline(
            [str(node), "--version"],
            cwd=node.parent,
            env=environment,
            deadline=probe_deadline,
        )
    except _lsp_process_tree._ProcessTreeSpawnError as error:
        _retain_node_probe_owner(owner, error.tree if error.tree is not None else error)
        return None
    except _NODE_PROBE_ERRORS:
        _release_node_probe_owner(owner)
        return None
    except BaseException:
        _release_node_probe_owner(owner)
        raise


def _output_code(output: object) -> str | None:
    if not isinstance(output, bytes):
        return "pyright_node_probe_failed"
    if len(output) > MAX_NODE_VERSION_BYTES:
        return "pyright_node_output_oversized"
    return None


def _node_version_result(node: Path, output: bytes) -> tuple[Path, str | None, int | None, set[str]]:
    match = _NODE_VERSION.fullmatch(output)
    if match is None:
        return node, None, None, {"pyright_node_version_malformed"}
    version = output.decode("ascii").rstrip("\r\n")
    major = int(match.group(1))
    codes: set[str] = set()
    if major != QUALIFIED_NODE_MAJOR:
        codes.add("pyright_node_major_mismatch")
    return node, version, major, codes


class _ProbeRun:
    """One spawned `node --version`: observe it, end its tree, grade the outcome."""

    def __init__(self, tree: object, probe_deadline: float, hard_cleanup_deadline: float) -> None:
        self.tree = tree
        self.process = tree.process
        self.probe_deadline = probe_deadline
        self.hard_cleanup_deadline = hard_cleanup_deadline
        self.output: bytes | None = None
        self.degradation_code: str | None = None
        self.stream = None
        self.parent_returncode: int | None = None
        self.parent_exited = False
        self.had_live_descendants = False
        self.tree_empty = False
        self.cleanup_attempted = False

    def observe(self) -> None:
        self._take_stdout()
        self._wait_parent()
        if self.parent_exited:
            self._check_descendants()
        self._terminate()
        if self.degradation_code is None:
            self._read_output()

    def _take_stdout(self) -> None:
        try:
            self.stream = self.process.stdout
        except _NODE_PROBE_ERRORS:
            self.degradation_code = "pyright_node_probe_failed"

    def _wait_parent(self) -> None:
        try:
            self.parent_returncode = self.process.wait(
                timeout=max(0.0, self.probe_deadline - time.monotonic())
            )
        except subprocess.TimeoutExpired:
            if self.degradation_code is None:
                self.degradation_code = "pyright_node_probe_timeout"
            return
        except _NODE_PROBE_ERRORS:
            self.degradation_code = "pyright_node_probe_failed"
            return
        self._record_returncode()

    def _record_returncode(self) -> None:
        try:
            observed = self.process.returncode
        except _NODE_PROBE_ERRORS:
            self.degradation_code = "pyright_node_probe_failed"
            return
        self.parent_exited = observed is not None
        self.parent_returncode = observed

    def _check_descendants(self) -> None:
        try:
            self.had_live_descendants = self.tree.has_live_descendants()
        except _NODE_PROBE_ERRORS:
            self.degradation_code = "pyright_node_probe_failed"

    def _cleanup_deadline(self) -> float:
        return min(time.monotonic() + NODE_PROBE_CLEANUP_SECONDS, self.hard_cleanup_deadline)

    def _timed_out(self) -> bool:
        return self.had_live_descendants or time.monotonic() >= self.probe_deadline

    def _terminate(self) -> None:
        self.cleanup_attempted = True
        self.tree_empty = _terminate_node_probe_tree(self.tree, self._cleanup_deadline())
        if not self.tree_empty:
            self.degradation_code = "pyright_node_probe_failed"
            return
        if self.degradation_code is None and self._timed_out():
            self.degradation_code = "pyright_node_probe_timeout"

    def _read_output(self) -> None:
        if self.parent_returncode != 0 or self.stream is None:
            self.degradation_code = "pyright_node_probe_failed"
            return
        try:
            self.output = self.stream.read(MAX_NODE_VERSION_BYTES + 1)
        except _NODE_PROBE_ERRORS:
            self.degradation_code = "pyright_node_probe_failed"
            return
        self.degradation_code = _output_code(self.output)

    def terminate_on_error(self) -> None:
        if self.tree_empty or self.cleanup_attempted:
            return
        try:
            self.cleanup_attempted = True
            self.tree_empty = _terminate_node_probe_tree(self.tree, self._cleanup_deadline())
        except BaseException:
            pass

    def settle(self, owner: object) -> None:
        """Release the ended tree and its owner, or retain both for a later retry."""
        if not self.tree_empty:
            _retain_node_probe_owner(owner, self.tree)
            return
        released = False
        try:
            released = _release_node_probe_tree(self.tree)
        finally:
            if released:
                _release_node_probe_owner(owner)
            else:
                _retain_node_probe_owner(owner, self.tree)
                self.degradation_code = "pyright_node_probe_failed"

    def result(self, node: Path) -> tuple[Path, str | None, int | None, set[str]]:
        if self.degradation_code is not None:
            return node, None, None, {self.degradation_code}
        if self.output is None:
            return node, None, None, {"pyright_node_probe_failed"}
        return _node_version_result(node, self.output)


def _run_probe(tree: object, owner: object, probe_deadline: float, hard_cleanup_deadline: float) -> _ProbeRun:
    run = _ProbeRun(tree, probe_deadline, hard_cleanup_deadline)
    try:
        try:
            run.observe()
        except BaseException:
            run.terminate_on_error()
            raise
    finally:
        run.settle(owner)
    return run


def _probe_node(
    deadline: float | None,
) -> tuple[Path | None, str | None, int | None, set[str]]:
    """Probe Node within its deadline plus one fixed tree-cleanup allowance."""
    _check_deadline(deadline)
    environment, node, code = _located_node(deadline)
    if code is not None:
        return node, None, None, {code}
    window = _probe_window(deadline)
    if window is None:
        return node, None, None, {"pyright_node_probe_timeout"}
    owner = _reserve_node_probe_owner()
    if owner is None:
        return node, None, None, {"pyright_node_probe_failed"}
    tree = _spawn_probe(node, environment, window[0], owner)
    if tree is None:
        return node, None, None, {"pyright_node_probe_failed"}
    return _run_probe(tree, owner, window[0], window[1]).result(node)


def _candidate_exists(path: Path, deadline: float | None) -> tuple[bool, str | None]:
    _check_deadline(deadline)
    try:
        path.lstat()
    except FileNotFoundError:
        return False, None
    except OSError:
        return True, "pyright_candidate_unreadable"
    _check_deadline(deadline)
    return True, None


def _path_exists_no_follow(path: Path, deadline: float | None) -> bool:
    _check_deadline(deadline)
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    _check_deadline(deadline)
    return True


def _lock_mentions_pyright(
    path: Path,
    deadline: float | None,
) -> bool:
    try:
        value, _raw = _read_json_object(
            path,
            MAX_PACKAGE_LOCK_BYTES,
            prefix="pyright_lockfile",
            deadline=deadline,
        )
    except _MetadataError:
        return False
    lockfile_version = value.get("lockfileVersion")
    if lockfile_version == 1:
        entries = value.get("dependencies")
        key = "pyright"
    elif lockfile_version in {2, 3}:
        entries = value.get("packages")
        key = "node_modules/pyright"
    else:
        return False
    return isinstance(entries, dict) and key in entries


def _project_local_evidence_present(server: Path, repository: RepositoryScope, deadline: float | None) -> bool:
    evidence = (server.parent, server.with_name("package.json"))
    if any(_path_exists_no_follow(path, deadline) for path in evidence):
        return True
    return _lock_mentions_pyright(Path(repository.checkout_root) / "package-lock.json", deadline)


def _managed_evidence_present(server: Path, deadline: float | None) -> bool:
    root = server.parent.parent
    evidence = (root, server.parent, server.with_name("package.json"), root / "install-manifest.json")
    return any(_path_exists_no_follow(path, deadline) for path in evidence)


def _candidate_is_present(
    source: str,
    server: Path,
    repository: RepositoryScope,
    state_root: Path,
    deadline: float | None,
) -> tuple[bool, str | None]:
    exists, code = _candidate_exists(server, deadline)
    if exists:
        return True, code
    expected = _expected_source_server(source, repository, state_root)
    if expected is None or server != expected:
        return False, None
    if source == "project-local":
        return _project_local_evidence_present(server, repository, deadline), None
    return _managed_evidence_present(server, deadline), None


def _is_path_tuple(values: object) -> bool:
    return isinstance(values, tuple) and all(isinstance(path, Path) for path in values)


def _validate_candidates(candidates: PyrightCandidates) -> None:
    if not isinstance(candidates, PyrightCandidates):
        raise TypeError("candidates must be a PyrightCandidates instance or None")
    for values in (candidates.project_local, candidates.managed, candidates.system):
        if not _is_path_tuple(values):
            raise TypeError("Pyright candidate categories must be tuples of Paths")


def _expected_source_server(
    source: str,
    repository: RepositoryScope,
    state_root: Path,
) -> Path | None:
    if source == "project-local":
        return Path(repository.checkout_root) / "node_modules/pyright/langserver.index.js"
    if source == "managed":
        return managed_pyright_root(state_root) / PYRIGHT_SERVER_RELATIVE
    return None


def _path_is_reserved(path: Path, raw: str) -> bool:
    is_reserved = getattr(os.path, "isreserved", None)
    if is_reserved is not None:
        return bool(is_reserved(raw))
    return path.is_reserved()


def _is_local_absolute_path(path: Path) -> bool:
    raw = os.fspath(path)
    if not path.is_absolute() or raw.startswith(("\\\\", "//")):
        return False
    if "\0" in raw or ".." in path.parts:
        return False
    return not _path_is_reserved(path, raw)


def _lexical_absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.path.normpath(os.fspath(path))))


def _mismatch() -> set[str]:
    """A fresh set: callers add the presence code to what they receive."""
    return {"pyright_source_path_mismatch"}


def _is_node_modules_server(candidate: Path) -> bool:
    if candidate.name != "langserver.index.js" or candidate.parent.name != "pyright":
        return False
    return candidate.parent.parent.name == "node_modules"


def _cmd_shim_server(candidate: Path) -> Path | None:
    """The server a `.cmd` shim stands for, or None when the shim is misplaced."""
    if candidate.parent.name != ".bin":
        return candidate.parent / "node_modules/pyright/langserver.index.js"
    node_modules = candidate.parent.parent
    if node_modules.name != "node_modules":
        return None
    return node_modules / "pyright/langserver.index.js"


def _cmd_shim_result(candidate: Path) -> tuple[Path | None, set[str], bool]:
    server = _cmd_shim_server(candidate)
    if server is None:
        return None, _mismatch(), False
    try:
        info = candidate.lstat()
    except FileNotFoundError:
        return server, set(), False
    except OSError:
        return None, _mismatch(), False
    if not _lstat_is_plain(info, stat.S_ISREG):
        return None, _mismatch(), False
    return server, set(), False


def _symlink_expected_server(candidate: Path) -> Path | None:
    if candidate.parent.name == ".bin":
        node_modules = candidate.parent.parent
        if node_modules.name != "node_modules":
            return None
        return node_modules / "pyright/langserver.index.js"
    if candidate.parent.name == "bin":
        return candidate.parent.parent / "lib/node_modules/pyright/langserver.index.js"
    return None


def _symlink_is_pyright(candidate: Path) -> bool:
    try:
        info = candidate.lstat()
    except OSError:
        return False
    return stat.S_ISLNK(info.st_mode)


def _resolved_symlink_target(candidate: Path, deadline: float | None) -> Path | None:
    _check_deadline(deadline)
    try:
        raw_target = os.readlink(candidate)
    except (OSError, ValueError):
        return None
    target = Path(raw_target)
    if not target.is_absolute():
        target = candidate.parent / target
    target = _lexical_absolute_path(target)
    _check_deadline(deadline)
    return target


def _target_matches(target: Path | None, expected: Path) -> bool:
    if target is None or not _is_local_absolute_path(target):
        return False
    return target == expected


def _symlink_result(candidate: Path, deadline: float | None) -> tuple[Path | None, set[str], bool]:
    if not _symlink_is_pyright(candidate):
        return None, _mismatch(), False
    expected = _symlink_expected_server(candidate)
    if expected is None:
        return None, _mismatch(), False
    target = _resolved_symlink_target(candidate, deadline)
    if not _target_matches(target, expected):
        return None, _mismatch(), False
    return expected, set(), False


def _system_candidate_server(
    candidate: Path,
    deadline: float | None,
) -> tuple[Path | None, set[str], bool]:
    if not _is_local_absolute_path(candidate):
        return None, _mismatch(), True
    if _is_node_modules_server(candidate):
        return candidate, set(), False
    if candidate.name.casefold() == "pyright-langserver.cmd":
        return _cmd_shim_result(candidate)
    if candidate.name != "pyright-langserver":
        return None, _mismatch(), False
    return _symlink_result(candidate, deadline)


def _approved_servers(repository: RepositoryScope, state_root: Path) -> set[Path | None]:
    return {
        _expected_source_server("project-local", repository, state_root),
        _expected_source_server("managed", repository, state_root),
    }


def _system_candidate(
    candidate: Path, repository: RepositoryScope, state_root: Path, deadline: float | None
) -> tuple[Path | None, set[str], bool]:
    approved = _approved_servers(repository, state_root)
    if candidate in approved:
        return None, _mismatch(), True
    server, codes, force_present = _system_candidate_server(candidate, deadline)
    if server in approved:
        return None, _mismatch(), True
    return server, codes, force_present


def _normalize_candidate(
    source: str,
    candidate: Path,
    repository: RepositoryScope,
    state_root: Path,
    deadline: float | None,
) -> tuple[Path | None, set[str], bool]:
    if source == "system":
        return _system_candidate(candidate, repository, state_root, deadline)
    expected = _expected_source_server(source, repository, state_root)
    if expected is None:
        raise AssertionError(f"unsupported Pyright source: {source}")
    if candidate != expected:
        return None, _mismatch(), not _is_local_absolute_path(candidate)
    return expected, set(), False


def _system_server_evidence(server: Path, repository: RepositoryScope, deadline: float | None) -> bool:
    evidence = (server.parent, server.with_name("package.json"))
    if any(_path_exists_no_follow(path, deadline) for path in evidence):
        return True
    lockfile = _lockfile_path("system", server, repository)
    return lockfile is not None and _lock_mentions_pyright(lockfile, deadline)


def _system_candidate_present(
    candidate: Path, server: Path, repository: RepositoryScope, deadline: float | None
) -> tuple[bool, str | None]:
    exists, code = _candidate_exists(candidate, deadline)
    if exists:
        return True, code
    exists, code = _candidate_exists(server, deadline)
    if exists:
        return True, code
    return _system_server_evidence(server, repository, deadline), None


def _normalized_candidate_is_present(
    source: str,
    candidate: Path,
    server: Path | None,
    force_present: bool,
    repository: RepositoryScope,
    state_root: Path,
    deadline: float | None,
) -> tuple[bool, str | None]:
    if force_present:
        return True, None
    if server is None:
        return _candidate_exists(candidate, deadline)
    if source != "system":
        return _candidate_is_present(source, server, repository, state_root, deadline)
    return _system_candidate_present(candidate, server, repository, deadline)


def _default_paths(
    source: str,
    repository: RepositoryScope,
    state_root: Path,
    deadline: float | None,
) -> tuple[Path, ...]:
    _check_deadline(deadline)
    expected = _expected_source_server(source, repository, state_root)
    if expected is not None:
        result = (expected,)
    else:
        try:
            environment = _node_environment()
        except (OSError, TypeError, ValueError):
            return ()
        found = shutil.which("pyright-langserver", path=environment.get("PATH", ""))
        result = () if found is None else (Path(found),)
    _check_deadline(deadline)
    return result


def _missing_identity(
    configuration_sha256: str,
    profile_codes: set[str],
) -> PyrightIdentity:
    return PyrightIdentity(
        status="missing",
        source=None,
        version=None,
        node_executable=None,
        node_version=None,
        node_major=None,
        server_executable=None,
        executable_sha256=None,
        package_sha256=None,
        initialization_options_sha256=PYRIGHT_INITIALIZATION_OPTIONS_SHA256,
        configuration_sha256=configuration_sha256,
        qualified=False,
        degradation_codes=tuple(sorted({"pyright_missing", *profile_codes})),
    )


def _inspect_candidate(
    repository: RepositoryScope,
    source: str,
    server: Path | None,
    initial_codes: set[str],
    configuration_sha256: str,
    profile_codes: set[str],
    deadline: float | None,
) -> PyrightIdentity:
    codes = {*initial_codes, *profile_codes}
    version: str | None = None
    executable_sha256: str | None = None
    package_sha256: str | None = None
    if server is not None:
        version, package_codes = _package_identity(server, deadline)
        codes.update(package_codes)
        executable_sha256, digest_code = _server_digest(server, deadline)
        if digest_code is not None:
            codes.add(digest_code)
        if source == "managed":
            package_sha256, manifest_codes = _managed_manifest(
                server, executable_sha256, deadline
            )
            codes.update(manifest_codes)
        else:
            codes.update(_lockfile_codes(source, server, repository, deadline))

    node_executable, node_version, node_major, node_codes = _probe_node(deadline)
    codes.update(node_codes)
    degradation_codes = tuple(sorted(codes))
    qualified = not degradation_codes
    return PyrightIdentity(
        status="qualified" if qualified else "degraded",
        source=source,
        version=version,
        node_executable=node_executable,
        node_version=node_version,
        node_major=node_major,
        server_executable=server,
        executable_sha256=executable_sha256,
        package_sha256=package_sha256,
        initialization_options_sha256=PYRIGHT_INITIALIZATION_OPTIONS_SHA256,
        configuration_sha256=configuration_sha256,
        qualified=qualified,
        degradation_codes=degradation_codes,
    )


_SOURCE_ATTRIBUTES = (
    ("project-local", "project_local"),
    ("managed", "managed"),
    ("system", "system"),
)


def _candidate_paths(
    source: str,
    attribute: str,
    candidates: PyrightCandidates | None,
    repository: RepositoryScope,
    state_root: Path,
    deadline: float | None,
) -> tuple[Path, ...]:
    if candidates is not None:
        return getattr(candidates, attribute)
    return _default_paths(source, repository, state_root, deadline)


def _present_candidate(
    source: str,
    candidate: Path,
    repository: RepositoryScope,
    state_root: Path,
    deadline: float | None,
) -> tuple[Path | None, set[str]] | None:
    """(server, initial codes) when the candidate is present, else None."""
    server, initial_codes, force_present = _normalize_candidate(
        source, candidate, repository, state_root, deadline
    )
    exists, initial_code = _normalized_candidate_is_present(
        source, candidate, server, force_present, repository, state_root, deadline
    )
    if not exists:
        return None
    if initial_code is not None:
        initial_codes.add(initial_code)
    return server, initial_codes


def _first_present_in_source(
    source: str,
    paths: tuple[Path, ...],
    repository: RepositoryScope,
    state_root: Path,
    deadline: float | None,
) -> tuple[Path | None, set[str]] | None:
    for candidate in paths:
        found = _present_candidate(source, candidate, repository, state_root, deadline)
        if found is not None:
            return found
    return None


def _first_present_candidate(
    candidates: PyrightCandidates | None,
    repository: RepositoryScope,
    state_root: Path,
    deadline: float | None,
) -> tuple[str, Path | None, set[str]] | None:
    for source, attribute in _SOURCE_ATTRIBUTES:
        paths = _candidate_paths(source, attribute, candidates, repository, state_root, deadline)
        found = _first_present_in_source(source, paths, repository, state_root, deadline)
        if found is not None:
            return source, found[0], found[1]
    return None


def discover_pyright(
    repository: RepositoryScope,
    *,
    state_root: Path,
    candidates: PyrightCandidates | None = None,
    deadline: float | None = None,
) -> PyrightIdentity:
    """Discover one candidate by fixed precedence without mutation or installation."""
    if not isinstance(repository, RepositoryScope):
        raise TypeError("repository must be a RepositoryScope")
    if not isinstance(state_root, Path):
        raise TypeError("state_root must be a Path")
    deadline = _validated_deadline(deadline)
    _check_deadline(deadline)
    configuration_sha256, profile_codes = _repository_configuration_identity(
        repository, deadline
    )
    if candidates is not None:
        _validate_candidates(candidates)
    found = _first_present_candidate(candidates, repository, state_root, deadline)
    if found is None:
        return _missing_identity(configuration_sha256, profile_codes)
    source, server, initial_codes = found
    return _inspect_candidate(
        repository, source, server, initial_codes, configuration_sha256, profile_codes, deadline
    )


atexit.register(_atexit_cleanup_node_probes)
