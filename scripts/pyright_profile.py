"""Pinned, read-only discovery for an explicitly installed Pyright runtime."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import stat
import subprocess as _subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from bounded_io import read_stable_bytes
from lsp_paths import PYRIGHT_VERSION, managed_pyright_root
from reliable_memory import _known_network_path, canonical_json_bytes, sha256_bytes
from repository_scope import RepositoryScope

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


def thaw_pyright_profile_value(value: object) -> object:
    """Return a mutable JSON-domain copy of an immutable profile value."""
    if isinstance(value, Mapping):
        return {key: thaw_pyright_profile_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_pyright_profile_value(item) for item in value]
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


def validate_pyright_install_manifest(value: object) -> dict[str, str]:
    """Validate the install receipt's closed pinned domain."""
    if not isinstance(value, dict) or set(value) != _MANIFEST_KEYS:
        raise _ManifestValidationError("pyright_manifest_malformed")
    if any(not isinstance(item, str) for item in value.values()):
        raise _ManifestValidationError("pyright_manifest_malformed")
    server_sha256 = value["server_sha256"]
    if _HEX_SHA256.fullmatch(server_sha256) is None:
        raise _ManifestValidationError("pyright_manifest_malformed")
    expected = build_pyright_install_manifest(server_sha256=server_sha256)
    checks = (
        ("schema_version", "pyright_manifest_schema_mismatch"),
        ("version", "pyright_version_mismatch"),
        ("package_url", "pyright_package_url_mismatch"),
        ("package_sha256", "pyright_package_sha256_mismatch"),
        ("package_integrity", "pyright_integrity_mismatch"),
        ("server_relative_path", "pyright_server_relative_mismatch"),
        ("configuration_sha256", "pyright_configuration_mismatch"),
        (
            "initialization_options_sha256",
            "pyright_initialization_options_mismatch",
        ),
    )
    for field, code in checks:
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


def _node_environment() -> dict[str, str]:
    values = os.environ
    for name, value in values.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise TypeError("environment names and values must be strings")
        if "\0" in name or "\0" in value:
            raise ValueError("environment names and values must not contain NUL")
    if os.name == "nt":
        system_root = values.get("SYSTEMROOT")
        if (
            not system_root
            or not Path(system_root).is_absolute()
            or not Path(system_root).is_dir()
        ):
            raise ValueError("SYSTEMROOT must be an inherited existing directory on Windows")
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


def _normalize_jsonc(raw: bytes, malformed_code: str) -> bytes:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _MetadataError(malformed_code) from exc

    normalized: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        character = text[index]
        if in_string:
            normalized.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue

        if character == '"':
            normalized.append(character)
            in_string = True
            index += 1
            continue
        if character != "/" or index + 1 >= len(text):
            normalized.append(character)
            index += 1
            continue

        marker = text[index + 1]
        if marker == "/":
            normalized.extend((" ", " "))
            index += 2
            while index < len(text) and text[index] not in "\r\n":
                normalized.append(" ")
                index += 1
            continue
        if marker != "*":
            normalized.append(character)
            index += 1
            continue

        normalized.extend((" ", " "))
        index += 2
        while index < len(text):
            if text[index] == "*" and index + 1 < len(text) and text[index + 1] == "/":
                normalized.extend((" ", " "))
                index += 2
                break
            normalized.append(text[index] if text[index] in "\r\n" else " ")
            index += 1
        else:
            raise _MetadataError(malformed_code)

    pending_comma: int | None = None
    previous_significant: str | None = None
    in_string = False
    escaped = False
    for index, character in enumerate(normalized):
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
                previous_significant = character
            continue
        if character == '"':
            pending_comma = None
            previous_significant = character
            in_string = True
        elif character == ",":
            pending_comma = (
                index
                if previous_significant not in {None, "{", "[", ",", ":"}
                else None
            )
            previous_significant = character
        elif character not in " \t\r\n":
            if character in "}]" and pending_comma is not None:
                normalized[pending_comma] = " "
            pending_comma = None
            previous_significant = character
    return "".join(normalized).encode("utf-8")


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
        if nodes > max_nodes:
            raise _MetadataError(f"{prefix}_too_many_nodes")
        if depth > max_depth:
            raise _MetadataError(f"{prefix}_too_deep")
        if nodes & 255 == 0:
            _check_deadline(deadline)

        if item is None or isinstance(item, (bool, int, str)):
            continue
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise _MetadataError(f"{prefix}_unsupported_value")
            children = tuple(item.values())
        elif isinstance(item, list):
            children = tuple(item)
        else:
            raise _MetadataError(f"{prefix}_unsupported_value")
        if nodes + len(stack) + len(children) > max_nodes:
            raise _MetadataError(f"{prefix}_too_many_nodes")
        stack.extend((child, depth + 1) for child in reversed(children))
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


def _read_repository_config(
    path: Path,
    *,
    root_pyproject: bool,
    deadline: float | None,
) -> tuple[dict[str, object], bytes]:
    suffix = path.suffix.casefold()
    if suffix not in {".json", ".toml"}:
        raise _MetadataError("pyright_repository_config_unsupported_format")
    try:
        raw = read_stable_bytes(
            path,
            MAX_PYRIGHT_CONFIG_BYTES,
            label="Pyright repository config",
        )
    except FileNotFoundError as exc:
        raise _MetadataError("pyright_repository_config_missing") from exc
    except PermissionError as exc:
        raise _MetadataError("pyright_repository_config_unsafe") from exc
    except ValueError as exc:
        raise _MetadataError("pyright_repository_config_oversized") from exc
    except OSError as exc:
        raise _MetadataError("pyright_repository_config_unreadable") from exc
    _check_deadline(deadline)

    if suffix == ".json":
        configuration = _strict_json_object(
            _normalize_jsonc(raw, "pyright_repository_config_malformed"),
            "pyright_repository_config_malformed",
            recursion_code="pyright_repository_config_too_deep",
        )
        _validate_canonical_domain(
            configuration,
            prefix="pyright_repository_config",
            max_depth=MAX_PYRIGHT_CONFIG_DOMAIN_DEPTH,
            max_nodes=MAX_PYRIGHT_CONFIG_DOMAIN_NODES,
            deadline=deadline,
        )
        return configuration, raw
    try:
        document = tomllib.loads(raw.decode("utf-8", errors="strict"))
    except RecursionError as exc:
        raise _MetadataError("pyright_repository_config_too_deep") from exc
    except (UnicodeDecodeError, ValueError) as exc:
        raise _MetadataError("pyright_repository_config_malformed") from exc
    tool = document.get("tool")
    configuration = tool.get("pyright") if isinstance(tool, dict) else None
    if not isinstance(configuration, dict):
        if root_pyproject:
            raise _MetadataError("pyright_repository_config_ancestor_search")
        raise _MetadataError("pyright_repository_config_malformed")
    _validate_canonical_domain(
        configuration,
        prefix="pyright_repository_config",
        max_depth=MAX_PYRIGHT_CONFIG_DOMAIN_DEPTH,
        max_nodes=MAX_PYRIGHT_CONFIG_DOMAIN_NODES,
        deadline=deadline,
    )
    return configuration, raw


def _contained_repository_config_path(
    value: str,
    *,
    current: Path,
    repository_root: Path,
) -> Path:
    if not value or "\0" in value:
        raise _MetadataError("pyright_repository_config_extends_invalid")
    try:
        relative = Path(value)
    except (TypeError, ValueError) as exc:
        raise _MetadataError("pyright_repository_config_extends_invalid") from exc
    if relative.is_absolute():
        raise _MetadataError("pyright_repository_config_extends_absolute")
    candidate = _lexical_absolute_path(current.parent / relative)
    try:
        candidate.relative_to(repository_root)
    except ValueError as exc:
        raise _MetadataError("pyright_repository_config_outside_repository") from exc
    if candidate.suffix.casefold() not in {".json", ".toml"}:
        raise _MetadataError("pyright_repository_config_unsupported_format")
    return candidate


def _repository_configuration_chain(
    repository: RepositoryScope,
    deadline: float | None,
) -> list[dict[str, object]] | None:
    repository_root = _lexical_absolute_path(Path(repository.checkout_root))
    current = _repository_config_entrypoint(repository_root, deadline)
    if current is None:
        return None

    configurations: list[dict[str, object]] = []
    visited: set[Path] = set()
    total_bytes = 0
    while current is not None:
        _check_deadline(deadline)
        if current in visited:
            raise _MetadataError("pyright_repository_config_extends_cycle")
        if len(configurations) >= MAX_PYRIGHT_CONFIG_FILES:
            raise _MetadataError("pyright_repository_config_extends_too_deep")
        visited.add(current)
        configuration, raw = _read_repository_config(
            current,
            root_pyproject=not configurations and current.name == "pyproject.toml",
            deadline=deadline,
        )
        total_bytes += len(raw)
        if total_bytes > MAX_PYRIGHT_CONFIG_TOTAL_BYTES:
            raise _MetadataError("pyright_repository_config_total_oversized")
        relative_path = current.relative_to(repository_root)
        source_directory = relative_path.parent.as_posix()
        configurations.append(
            {
                "configuration": configuration,
                "source_directory": source_directory,
                "source_path": relative_path.as_posix(),
            }
        )
        extends = configuration.get("extends")
        if extends is None:
            current = None
            continue
        if not isinstance(extends, str):
            raise _MetadataError("pyright_repository_config_extends_invalid")
        if len(configurations) - 1 >= MAX_PYRIGHT_CONFIG_EXTENDS_DEPTH:
            raise _MetadataError("pyright_repository_config_extends_too_deep")
        current = _contained_repository_config_path(
            extends,
            current=current,
            repository_root=repository_root,
        )

    configurations.reverse()
    return configurations


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


def _package_identity(
    server: Path,
    deadline: float | None,
) -> tuple[str | None, set[str]]:
    codes: set[str] = set()
    try:
        package, _raw = _read_json_object(
            server.with_name("package.json"),
            MAX_PACKAGE_JSON_BYTES,
            prefix="pyright_package_json",
            deadline=deadline,
        )
    except _MetadataError as exc:
        return None, {exc.code}

    name = package.get("name")
    if not isinstance(name, str) or name != "pyright":
        codes.add("pyright_package_mismatch")
    observed = package.get("version")
    if not isinstance(observed, str) or _PACKAGE_VERSION.fullmatch(observed) is None:
        codes.add("pyright_package_json_malformed")
        version = None
    else:
        version = observed
        if version != PYRIGHT_VERSION:
            codes.add("pyright_version_mismatch")
    return version, codes


def _lockfile_path(source: str, server: Path, repository: RepositoryScope) -> Path | None:
    if source == "project-local":
        return Path(repository.checkout_root) / "package-lock.json"
    package_root = server.parent
    if package_root.name != "pyright" or package_root.parent.name != "node_modules":
        return None
    return package_root.parent.parent / "package-lock.json"


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
            lockfile,
            MAX_PACKAGE_LOCK_BYTES,
            prefix="pyright_lockfile",
            deadline=deadline,
        )
    except _MetadataError as exc:
        return {exc.code}

    lockfile_version = value.get("lockfileVersion")
    if isinstance(lockfile_version, bool) or not isinstance(lockfile_version, int):
        return {"pyright_lockfile_malformed"}
    if lockfile_version == 1:
        entries = value.get("dependencies")
        key = "pyright"
    elif lockfile_version in {2, 3}:
        entries = value.get("packages")
        key = "node_modules/pyright"
    else:
        return {"pyright_lockfile_unsupported"}
    if not isinstance(entries, dict):
        return {"pyright_lockfile_malformed"}
    entry = entries.get(key)
    if not isinstance(entry, dict):
        return {"pyright_lockfile_entry_missing"}
    if "link" in entry:
        return {"pyright_lockfile_link"}

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


def _managed_manifest(
    server: Path,
    executable_sha256: str | None,
    deadline: float | None,
) -> tuple[str | None, set[str]]:
    root = server.parent.parent
    codes: set[str] = set()
    if Path(server.parent.name) / server.name != PYRIGHT_SERVER_RELATIVE:
        codes.add("pyright_server_relative_mismatch")
    try:
        value, raw = _read_json_object(
            root / "install-manifest.json",
            MAX_INSTALL_MANIFEST_BYTES,
            prefix="pyright_manifest",
            deadline=deadline,
            recursion_code="pyright_manifest_too_deep",
        )
    except _MetadataError as exc:
        return None, {exc.code, *codes}

    try:
        _validate_canonical_domain(
            value,
            prefix="pyright_manifest",
            max_depth=MAX_PYRIGHT_MANIFEST_DOMAIN_DEPTH,
            max_nodes=MAX_PYRIGHT_MANIFEST_DOMAIN_NODES,
            deadline=deadline,
        )
    except _MetadataError as exc:
        return None, {exc.code, *codes}

    try:
        if canonical_json_bytes(value) != raw:
            codes.add("pyright_manifest_noncanonical")
    except RecursionError:
        codes.add("pyright_manifest_too_deep")
    except (TypeError, ValueError):
        codes.add("pyright_manifest_unsupported_value")

    try:
        validate_pyright_install_manifest(value)
    except _ManifestValidationError as exc:
        codes.add(exc.code)

    package_sha256 = value.get("package_sha256")
    if not isinstance(package_sha256, str) or _HEX_SHA256.fullmatch(package_sha256) is None:
        package_sha256 = None
    receipt_server_sha256 = value.get("server_sha256")
    if (
        isinstance(receipt_server_sha256, str)
        and _HEX_SHA256.fullmatch(receipt_server_sha256) is not None
        and executable_sha256 is not None
        and receipt_server_sha256 != executable_sha256
    ):
        codes.add("pyright_executable_digest_mismatch")
    return package_sha256, codes


def _stop_node_probe(process: object, probe_deadline: float) -> bool:
    cleanup_ok = True
    try:
        process.kill()
    except _NODE_PROBE_ERRORS:
        cleanup_ok = False
    try:
        process.wait(timeout=max(0.0, probe_deadline - time.monotonic()))
    except _NODE_PROBE_ERRORS:
        cleanup_ok = False
    return cleanup_ok


def _node_executable_is_safe(node: Path, deadline: float | None) -> bool:
    if not _is_local_absolute_path(node):
        return False
    if os.name == "nt" and node.suffix.casefold() in {".bat", ".cmd"}:
        return False
    try:
        for parent in node.parents:
            if parent == Path(parent.anchor):
                break
            _check_deadline(deadline)
            info = parent.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400
                or not stat.S_ISDIR(info.st_mode)
            ):
                return False
        _check_deadline(deadline)
        info = node.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400
            or not stat.S_ISREG(info.st_mode)
        ):
            return False
        if _known_network_path(node):
            return False
    except (OSError, TypeError, ValueError, RuntimeError):
        return False
    _check_deadline(deadline)
    return True


def _probe_node(
    deadline: float | None,
) -> tuple[Path | None, str | None, int | None, set[str]]:
    _check_deadline(deadline)
    try:
        environment = _node_environment()
    except (OSError, TypeError, ValueError):
        return None, None, None, {"pyright_node_probe_failed"}
    try:
        found = shutil.which("node", path=environment.get("PATH", ""))
    except _NODE_PROBE_ERRORS:
        return None, None, None, {"pyright_node_probe_failed"}
    _check_deadline(deadline)
    if found is None:
        return None, None, None, {"pyright_node_missing"}
    try:
        node = Path(found)
    except (TypeError, ValueError):
        return None, None, None, {"pyright_node_probe_failed"}
    if not _node_executable_is_safe(node, deadline):
        return node, None, None, {"pyright_node_executable_unsafe"}

    now = time.monotonic()
    probe_deadline = now + NODE_PROBE_TIMEOUT_SECONDS
    if deadline is not None:
        probe_deadline = min(probe_deadline, deadline)
    remaining = probe_deadline - now
    if remaining <= 0:
        return node, None, None, {"pyright_node_probe_timeout"}
    try:
        process = subprocess.Popen(
            [str(node), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=False,
            env=environment,
        )
    except _NODE_PROBE_ERRORS:
        return node, None, None, {"pyright_node_probe_failed"}

    output: bytes | None = None
    degradation_code: str | None = None
    stream = None
    try:
        try:
            stream = process.stdout
        except _NODE_PROBE_ERRORS:
            degradation_code = "pyright_node_probe_failed"
        try:
            process.wait(timeout=max(0.0, probe_deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            if degradation_code is None:
                degradation_code = "pyright_node_probe_timeout"
            if not _stop_node_probe(process, probe_deadline):
                degradation_code = "pyright_node_probe_failed"
        except _NODE_PROBE_ERRORS:
            degradation_code = "pyright_node_probe_failed"
            _stop_node_probe(process, probe_deadline)
        if degradation_code is None:
            try:
                returncode = process.returncode
            except _NODE_PROBE_ERRORS:
                degradation_code = "pyright_node_probe_failed"
            else:
                if returncode != 0 or stream is None:
                    degradation_code = "pyright_node_probe_failed"
                else:
                    try:
                        output = stream.read(MAX_NODE_VERSION_BYTES + 1)
                    except _NODE_PROBE_ERRORS:
                        degradation_code = "pyright_node_probe_failed"
                    else:
                        if not isinstance(output, bytes):
                            degradation_code = "pyright_node_probe_failed"
                        elif len(output) > MAX_NODE_VERSION_BYTES:
                            degradation_code = "pyright_node_output_oversized"
    finally:
        if stream is not None:
            try:
                stream.close()
            except _NODE_PROBE_ERRORS:
                degradation_code = "pyright_node_probe_failed"

    if degradation_code is not None:
        return node, None, None, {degradation_code}
    if output is None:
        return node, None, None, {"pyright_node_probe_failed"}
    match = _NODE_VERSION.fullmatch(output)
    if match is None:
        return node, None, None, {"pyright_node_version_malformed"}
    version = output.decode("ascii").rstrip("\r\n")
    major = int(match.group(1))
    codes = set()
    if major != QUALIFIED_NODE_MAJOR:
        codes.add("pyright_node_major_mismatch")
    return node, version, major, codes


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
        evidence = (
            server.parent,
            server.with_name("package.json"),
        )
        return (
            any(_path_exists_no_follow(path, deadline) for path in evidence)
            or _lock_mentions_pyright(
                Path(repository.checkout_root) / "package-lock.json", deadline
            ),
            None,
        )
    root = server.parent.parent
    evidence = (
        root,
        server.parent,
        server.with_name("package.json"),
        root / "install-manifest.json",
    )
    return any(_path_exists_no_follow(path, deadline) for path in evidence), None


def _validate_candidates(candidates: PyrightCandidates) -> None:
    if not isinstance(candidates, PyrightCandidates):
        raise TypeError("candidates must be a PyrightCandidates instance or None")
    for values in (candidates.project_local, candidates.managed, candidates.system):
        if not isinstance(values, tuple) or any(not isinstance(path, Path) for path in values):
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


def _is_local_absolute_path(path: Path) -> bool:
    raw = os.fspath(path)
    is_reserved = getattr(os.path, "isreserved", None)
    reserved = is_reserved(raw) if is_reserved is not None else path.is_reserved()
    return (
        path.is_absolute()
        and not raw.startswith(("\\\\", "//"))
        and "\0" not in raw
        and ".." not in path.parts
        and not reserved
    )


def _lexical_absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.path.normpath(os.fspath(path))))


def _system_candidate_server(
    candidate: Path,
    deadline: float | None,
) -> tuple[Path | None, set[str], bool]:
    mismatch = {"pyright_source_path_mismatch"}
    if not _is_local_absolute_path(candidate):
        return None, mismatch, True
    if (
        candidate.name == "langserver.index.js"
        and candidate.parent.name == "pyright"
        and candidate.parent.parent.name == "node_modules"
    ):
        return candidate, set(), False

    if candidate.name.casefold() == "pyright-langserver.cmd":
        if candidate.parent.name == ".bin":
            node_modules = candidate.parent.parent
            if node_modules.name != "node_modules":
                return None, mismatch, False
            server = node_modules / "pyright/langserver.index.js"
        else:
            server = candidate.parent / "node_modules/pyright/langserver.index.js"
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            return server, set(), False
        except OSError:
            return None, mismatch, False
        if (
            stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400
            or not stat.S_ISREG(info.st_mode)
        ):
            return None, mismatch, False
        return server, set(), False

    if candidate.name != "pyright-langserver":
        return None, mismatch, False
    try:
        info = candidate.lstat()
    except OSError:
        return None, mismatch, False
    if not stat.S_ISLNK(info.st_mode):
        return None, mismatch, False
    if candidate.parent.name == ".bin":
        node_modules = candidate.parent.parent
        if node_modules.name != "node_modules":
            return None, mismatch, False
        expected = node_modules / "pyright/langserver.index.js"
    elif candidate.parent.name == "bin":
        expected = candidate.parent.parent / "lib/node_modules/pyright/langserver.index.js"
    else:
        return None, mismatch, False
    _check_deadline(deadline)
    try:
        raw_target = os.readlink(candidate)
    except (OSError, ValueError):
        return None, mismatch, False
    target = Path(raw_target)
    if not target.is_absolute():
        target = candidate.parent / target
    target = _lexical_absolute_path(target)
    _check_deadline(deadline)
    if not _is_local_absolute_path(target) or target != expected:
        return None, mismatch, False
    return expected, set(), False


def _normalize_candidate(
    source: str,
    candidate: Path,
    repository: RepositoryScope,
    state_root: Path,
    deadline: float | None,
) -> tuple[Path | None, set[str], bool]:
    if source == "system":
        approved_servers = {
            _expected_source_server("project-local", repository, state_root),
            _expected_source_server("managed", repository, state_root),
        }
        if candidate in approved_servers:
            return None, {"pyright_source_path_mismatch"}, True
        server, codes, force_present = _system_candidate_server(candidate, deadline)
        if server in approved_servers:
            return None, {"pyright_source_path_mismatch"}, True
        return server, codes, force_present
    expected = _expected_source_server(source, repository, state_root)
    if expected is not None:
        if candidate != expected:
            return (
                None,
                {"pyright_source_path_mismatch"},
                not _is_local_absolute_path(candidate),
            )
        return expected, set(), False
    raise AssertionError(f"unsupported Pyright source: {source}")


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
        return _candidate_is_present(
            source,
            server,
            repository,
            state_root,
            deadline,
        )
    exists, code = _candidate_exists(candidate, deadline)
    if exists:
        return True, code
    exists, code = _candidate_exists(server, deadline)
    if exists:
        return True, code
    evidence = (server.parent, server.with_name("package.json"))
    if any(_path_exists_no_follow(path, deadline) for path in evidence):
        return True, None
    lockfile = _lockfile_path("system", server, repository)
    return (
        lockfile is not None and _lock_mentions_pyright(lockfile, deadline),
        None,
    )


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

    for source, attribute in (
        ("project-local", "project_local"),
        ("managed", "managed"),
        ("system", "system"),
    ):
        paths = (
            getattr(candidates, attribute)
            if candidates is not None
            else _default_paths(source, repository, state_root, deadline)
        )
        for candidate in paths:
            server, initial_codes, force_present = _normalize_candidate(
                source,
                candidate,
                repository,
                state_root,
                deadline,
            )
            exists, initial_code = _normalized_candidate_is_present(
                source,
                candidate,
                server,
                force_present,
                repository,
                state_root,
                deadline,
            )
            if exists:
                if initial_code is not None:
                    initial_codes.add(initial_code)
                return _inspect_candidate(
                    repository,
                    source,
                    server,
                    initial_codes,
                    configuration_sha256,
                    profile_codes,
                    deadline,
                )
    return _missing_identity(configuration_sha256, profile_codes)
