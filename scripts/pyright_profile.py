"""Pinned, read-only discovery for an explicitly installed Pyright runtime."""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import shutil
import subprocess as _subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from bounded_io import read_stable_bytes
from lsp_paths import PYRIGHT_VERSION, managed_pyright_root
from reliable_memory import canonical_json_bytes, sha256_bytes
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

PYRIGHT_CONFIGURATION = {
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
}
PYRIGHT_INITIALIZATION_OPTIONS = {"files": {"exclude": []}}

PYRIGHT_INSTALL_MANIFEST_SCHEMA = "pyright-install/v1"
PYRIGHT_CONFIGURATION_SHA256 = sha256_bytes(canonical_json_bytes(PYRIGHT_CONFIGURATION))
PYRIGHT_INITIALIZATION_OPTIONS_SHA256 = sha256_bytes(
    canonical_json_bytes(PYRIGHT_INITIALIZATION_OPTIONS)
)

MAX_PACKAGE_JSON_BYTES = 64 * 1024
MAX_PACKAGE_LOCK_BYTES = 8 * 1024 * 1024
MAX_INSTALL_MANIFEST_BYTES = 16 * 1024
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


def _strict_json_object(raw: bytes, malformed_code: str) -> dict[str, object]:
    try:
        text = raw.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_float=_reject_json_number,
            parse_constant=_reject_json_number,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise _MetadataError(malformed_code) from exc
    if not isinstance(value, dict):
        raise _MetadataError(malformed_code)
    return value


def _read_json_object(
    path: Path,
    max_bytes: int,
    *,
    prefix: str,
    deadline: float | None,
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
    return _strict_json_object(raw, f"{prefix}_malformed"), raw


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
        )
    except _MetadataError as exc:
        return None, {exc.code, *codes}

    try:
        if canonical_json_bytes(value) != raw:
            codes.add("pyright_manifest_noncanonical")
    except (TypeError, ValueError):
        codes.add("pyright_manifest_malformed")

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


def _bounded_node_output(
    process: object,
) -> tuple[threading.Thread, list[bytes], list[BaseException], threading.Event]:
    output: list[bytes] = []
    errors: list[BaseException] = []
    oversized = threading.Event()

    def read_output() -> None:
        try:
            stream = process.stdout
            if stream is None:
                raise OSError("Node stdout pipe is unavailable")
            content = stream.read(MAX_NODE_VERSION_BYTES + 1)
            if not isinstance(content, bytes):
                raise TypeError("Node stdout must be bytes")
            output.append(content)
            if len(content) > MAX_NODE_VERSION_BYTES:
                oversized.set()
                with contextlib.suppress(OSError):
                    process.kill()
        except BaseException as exc:
            errors.append(exc)

    reader = threading.Thread(
        target=read_output,
        name="pyright-node-version-reader",
        daemon=True,
    )
    reader.start()
    return reader, output, errors, oversized


def _probe_node(
    deadline: float | None,
) -> tuple[Path | None, str | None, int | None, set[str]]:
    _check_deadline(deadline)
    try:
        environment = _node_environment()
    except (OSError, TypeError, ValueError):
        return None, None, None, {"pyright_node_probe_failed"}
    found = shutil.which("node", path=environment.get("PATH", ""))
    _check_deadline(deadline)
    if found is None:
        return None, None, None, {"pyright_node_missing"}
    node = Path(found)
    if os.name == "nt" and node.suffix.casefold() in {".bat", ".cmd"}:
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
            stderr=subprocess.DEVNULL,
            shell=False,
            env=environment,
        )
    except OSError:
        return node, None, None, {"pyright_node_probe_failed"}

    reader, output, read_errors, oversized = _bounded_node_output(process)
    timed_out = False
    try:
        process.wait(timeout=max(0.0, probe_deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        timed_out = True
        with contextlib.suppress(OSError):
            process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=max(0.0, probe_deadline - time.monotonic()))
    reader.join(timeout=max(0.0, probe_deadline - time.monotonic()))
    if reader.is_alive():
        timed_out = True
        with contextlib.suppress(OSError):
            process.kill()
    stream = getattr(process, "stdout", None)
    if stream is not None and not reader.is_alive():
        with contextlib.suppress(OSError):
            stream.close()

    if timed_out:
        return node, None, None, {"pyright_node_probe_timeout"}
    if oversized.is_set():
        return node, None, None, {"pyright_node_output_oversized"}
    if read_errors or process.returncode != 0 or len(output) != 1:
        return node, None, None, {"pyright_node_probe_failed"}
    match = _NODE_VERSION.fullmatch(output[0])
    if match is None:
        return node, None, None, {"pyright_node_version_malformed"}
    version = output[0].decode("ascii").rstrip("\r\n")
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


def _validate_candidates(candidates: PyrightCandidates) -> None:
    if not isinstance(candidates, PyrightCandidates):
        raise TypeError("candidates must be a PyrightCandidates instance or None")
    for values in (candidates.project_local, candidates.managed, candidates.system):
        if not isinstance(values, tuple) or any(not isinstance(path, Path) for path in values):
            raise TypeError("Pyright candidate categories must be tuples of Paths")


def _default_paths(
    source: str,
    repository: RepositoryScope,
    state_root: Path,
    deadline: float | None,
) -> tuple[Path, ...]:
    _check_deadline(deadline)
    if source == "project-local":
        result = (
            Path(repository.checkout_root) / "node_modules/pyright/langserver.index.js",
        )
    elif source == "managed":
        result = (managed_pyright_root(state_root) / PYRIGHT_SERVER_RELATIVE,)
    else:
        try:
            environment = _node_environment()
        except (OSError, TypeError, ValueError):
            return ()
        found = shutil.which("pyright-langserver", path=environment.get("PATH", ""))
        result = () if found is None else (Path(found),)
    _check_deadline(deadline)
    return result


def _missing_identity() -> PyrightIdentity:
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
        configuration_sha256=PYRIGHT_CONFIGURATION_SHA256,
        qualified=False,
        degradation_codes=("pyright_missing",),
    )


def _inspect_candidate(
    repository: RepositoryScope,
    source: str,
    server: Path,
    initial_code: str | None,
    deadline: float | None,
) -> PyrightIdentity:
    codes = set() if initial_code is None else {initial_code}
    version, package_codes = _package_identity(server, deadline)
    codes.update(package_codes)
    executable_sha256, digest_code = _server_digest(server, deadline)
    if digest_code is not None:
        codes.add(digest_code)

    package_sha256: str | None = None
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
        configuration_sha256=PYRIGHT_CONFIGURATION_SHA256,
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
        for server in paths:
            exists, initial_code = _candidate_exists(server, deadline)
            if exists:
                return _inspect_candidate(
                    repository,
                    source,
                    server,
                    initial_code,
                    deadline,
                )
    return _missing_identity()
