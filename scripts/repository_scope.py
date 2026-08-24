"""Canonical local repository and checkout identities."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = "repository-scope/v1"
GIT_TIMEOUT_SECONDS = 2.0
MAX_GIT_OUTPUT_BYTES = 8192
MAX_PATH_LENGTH = 4096

_SCOPE_KEYS = {
    "schema_version",
    "repository_id",
    "checkout_id",
    "checkout_root",
    "git_common_dir",
    "git_commit",
}
_REPOSITORY_ID_RE = re.compile(r"repository:[0-9a-f]{64}")
_CHECKOUT_ID_RE = re.compile(r"checkout:[0-9a-f]{64}")
_COMMIT_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_LOCAL_GIT_ENVIRONMENT = {
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CONFIG",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_NOSYSTEM",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_SYSTEM",
    "GIT_DIR",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_DIFF_OPTS",
    "GIT_GRAFT_FILE",
    "GIT_EXTERNAL_DIFF",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_NAMESPACE",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_OBJECT_DIRECTORY",
    "GIT_PREFIX",
    "GIT_QUARANTINE_PATH",
    "GIT_REPLACE_REF_BASE",
    "GIT_SHALLOW_FILE",
    "GIT_WORK_TREE",
}
_LOCAL_GIT_ENVIRONMENT_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")


def sanitized_git_environment() -> dict[str, str]:
    """Return an environment with ambient repository/config selectors removed."""
    environment = os.environ.copy()
    for name in tuple(environment):
        if name in _LOCAL_GIT_ENVIRONMENT or name.startswith(
            _LOCAL_GIT_ENVIRONMENT_PREFIXES
        ):
            environment.pop(name)
    environment.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    return environment


class RepositoryScopeUnavailable(OSError):
    """Raised when a checkout marker exists but Git identity is unavailable."""


def _identity(prefix: str, purpose: str, values: Sequence[str]) -> str:
    payload = "\0".join((SCHEMA_VERSION, purpose, *values)).encode(
        "utf-8", errors="strict"
    )
    return f"{prefix}:{hashlib.sha256(payload).hexdigest()}"


def _bounded_path_text(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > MAX_PATH_LENGTH:
        return False
    return not any(character in value for character in "\x00\r\n")


def _windows_components(name: str, value: str) -> list[str]:
    if re.fullmatch(r"[A-Z]:/(?:[^/\\]+(?:/[^/\\]+)*)?", value) is None:
        raise ValueError(f"{name} must be a canonical drive-letter absolute path")
    if len(value) > 3:
        return value[3:].split("/")
    return []


def _posix_components(name: str, value: str) -> list[str]:
    if not value.startswith("/") or value.startswith("//"):
        raise ValueError(f"{name} must be a canonical POSIX absolute path")
    if len(value) > 1:
        return value[1:].split("/")
    return []


def _path_components(name: str, value: str) -> list[str]:
    if re.match(r"[A-Za-z]:", value):
        return _windows_components(name, value)
    return _posix_components(name, value)


def _serialized_path(name: str, value: object) -> str:
    if not _bounded_path_text(value):
        raise ValueError(f"{name} must be a bounded canonical absolute path")
    components = _path_components(name, str(value))
    if any(component in {"", ".", ".."} for component in components):
        raise ValueError(f"{name} must not contain noncanonical path components")
    return str(value)


def _local_serialized_path(path: Path, *, strict: bool) -> str:
    resolved = path.resolve(strict=strict)
    value = str(resolved)
    if re.match(r"[A-Za-z]:[\\/]", value):
        value = value[0].upper() + value[1:].replace("\\", "/")
    return _serialized_path("repository scope path", value)


def _validated_path(name: str, value: object, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    return _serialized_path(name, value)


def _validated_id(name: str, value: object, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{name} must be a canonical SHA-256 identity")
    return value


def _validated_commit(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _COMMIT_RE.fullmatch(value) is None:
        raise ValueError("git_commit must be a full lowercase Git object ID or null")
    return value


def derive_repository_id(*, checkout_root: str, git_common_dir: str | None) -> str:
    """Derive the v1 repository identity from canonical serialized paths."""
    checkout_root = _serialized_path("checkout_root", checkout_root)
    if git_common_dir is None:
        return _identity("repository", "repository/non-git-root", [checkout_root])
    common = _serialized_path("git_common_dir", git_common_dir)
    return _identity("repository", "repository/git-common-dir", [common])


def derive_checkout_id(repository_id: str, checkout_root: str) -> str:
    """Derive the v1 checkout identity from its repository and serialized root."""
    repository_id = _validated_id("repository_id", repository_id, _REPOSITORY_ID_RE)
    checkout_root = _serialized_path("checkout_root", checkout_root)
    return _identity("checkout", "checkout/root", [repository_id, checkout_root])


@dataclass(frozen=True)
class RepositoryScope:
    """Closed identity for one repository and one of its checkouts."""

    schema_version: str
    repository_id: str
    checkout_id: str
    checkout_root: str
    git_common_dir: str | None
    git_commit: str | None

    def __post_init__(self) -> None:
        self._require_fields()
        self._require_derived_identity()

    def _require_fields(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {SCHEMA_VERSION}")
        _validated_id("repository_id", self.repository_id, _REPOSITORY_ID_RE)
        _validated_id("checkout_id", self.checkout_id, _CHECKOUT_ID_RE)
        _validated_path("checkout_root", self.checkout_root)
        _validated_path("git_common_dir", self.git_common_dir, optional=True)
        _validated_commit(self.git_commit)
        if self.git_common_dir is None and self.git_commit is not None:
            raise ValueError("non-Git repository scope must not contain git_commit")

    def _require_derived_identity(self) -> None:
        expected_repository = derive_repository_id(
            checkout_root=self.checkout_root,
            git_common_dir=self.git_common_dir,
        )
        expected_checkout = derive_checkout_id(expected_repository, self.checkout_root)
        if not hmac.compare_digest(self.repository_id, expected_repository):
            raise ValueError("repository_id is not derived from the canonical repository path")
        if not hmac.compare_digest(self.checkout_id, expected_checkout):
            raise ValueError("checkout_id is not derived from the canonical checkout path")

    def identity(self) -> tuple[str, str, str, str, str | None]:
        """Which repository and checkout this is — the part a commit cannot change.

        `git_commit` is provenance: it says what a generation was built from, not
        which repository it belongs to. Comparing whole scopes made every
        generation ineligible after the next commit, which on a vault that commits
        its own runtime meant semantic retrieval fell back to the legacy index
        almost always. See NEW-65.
        """
        return (
            self.schema_version,
            self.repository_id,
            self.checkout_id,
            self.checkout_root,
            self.git_common_dir,
        )

    def same_repository(self, other: object) -> bool:
        """True when both scopes name the same repository checkout."""
        if not isinstance(other, RepositoryScope):
            return False
        return self.identity() == other.identity()

    def as_dict(self) -> dict[str, str | None]:
        """Return the canonical closed JSON object."""
        return {
            "schema_version": self.schema_version,
            "repository_id": self.repository_id,
            "checkout_id": self.checkout_id,
            "checkout_root": self.checkout_root,
            "git_common_dir": self.git_common_dir,
            "git_commit": self.git_commit,
        }

    @classmethod
    def from_dict(cls, value: object) -> RepositoryScope:
        """Validate and load a canonical closed JSON object."""
        if not isinstance(value, Mapping) or set(value) != _SCOPE_KEYS:
            raise ValueError("repository scope must be a closed object with all required fields")
        return cls(
            schema_version=value["schema_version"],
            repository_id=value["repository_id"],
            checkout_id=value["checkout_id"],
            checkout_root=value["checkout_root"],
            git_common_dir=value["git_common_dir"],
            git_commit=value["git_commit"],
        )


def _finite_deadline(deadline: object) -> bool:
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        return False
    return float("-inf") < deadline < float("inf")


def _require_stop_arguments(deadline: float | None, cancelled: object) -> None:
    if deadline is not None and not _finite_deadline(deadline):
        raise ValueError("deadline must be a finite monotonic timestamp or None")
    if cancelled is not None and not callable(cancelled):
        raise TypeError("cancelled must be callable or None")


def _check_stop(
    deadline: float | None,
    cancelled: object,
) -> None:
    _require_stop_arguments(deadline, cancelled)
    if cancelled is not None and cancelled():
        raise TimeoutError("repository scope resolution cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("repository scope deadline reached")


def _has_git_marker(requested: Path) -> bool:
    for directory in (requested, *requested.parents):
        marker = directory / ".git"
        if marker.exists() or marker.is_symlink():
            return True
    return False


def _git_probe_process(root: Path, command: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        shell=False,
        env=sanitized_git_environment(),
    )


class _GitWatch:
    """Kills the probe when its deadline passes or the caller cancels."""

    def __init__(self, process: subprocess.Popen, effective_deadline: float, cancelled) -> None:
        self.process = process
        self.deadline = effective_deadline
        self.cancelled = cancelled
        self.stopped = threading.Event()
        self.reason: list[str] = []
        self.thread = threading.Thread(target=self._monitor, daemon=True)

    def _stop_reason(self) -> str | None:
        if self.cancelled is not None and self.cancelled():
            return "cancelled"
        if self.deadline - time.monotonic() <= 0:
            return "deadline"
        return None

    def _monitor(self) -> None:
        while not self.stopped.is_set() and self.process.poll() is None:
            reason = self._stop_reason()
            if reason is not None:
                self.reason.append(reason)
                self.process.kill()
                return
            self.stopped.wait(min(0.01, max(self.deadline - time.monotonic(), 0.0)))

    def start(self) -> None:
        self.thread.start()

    def finish(self) -> None:
        self.stopped.set()
        self.thread.join(timeout=0.1)


def _read_probe_output(process: subprocess.Popen) -> bytes:
    assert process.stdout is not None
    output = process.stdout.read(MAX_GIT_OUTPUT_BYTES + 1)
    if len(output) > MAX_GIT_OUTPUT_BYTES and process.poll() is None:
        process.kill()
    process.wait()
    return output


def _close_probe(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.kill()
        process.wait()
    close = getattr(process.stdout, "close", None)
    if close is not None:
        close()


def _require_probe_success(
    watch: _GitWatch, process: subprocess.Popen, command: list[str], output: bytes
) -> None:
    if watch.reason:
        raise TimeoutError(f"repository scope {watch.reason[0]} reached during Git probe")
    if len(output) > MAX_GIT_OUTPUT_BYTES:
        raise ValueError("Git command output exceeds the byte ceiling")
    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, command)


def _clean_probe_text(value: str) -> bool:
    return len(value) <= MAX_PATH_LENGTH and "\x00" not in value and "\n" not in value


def _probe_value(output: bytes, allow_empty: bool) -> str:
    value = output.decode("utf-8", errors="strict").strip()
    if not value and not allow_empty:
        raise ValueError("Git command returned an invalid value")
    if not _clean_probe_text(value):
        raise ValueError("Git command returned an invalid value")
    return value


def _git_output(
    root: Path,
    subcommand: str,
    *arguments: str,
    deadline: float | None = None,
    cancelled=None,
    allow_empty: bool = False,
) -> str:
    _check_stop(deadline, cancelled)
    command = ["git", "-C", str(root), subcommand, *arguments]
    process = _git_probe_process(root, command)
    local_deadline = time.monotonic() + GIT_TIMEOUT_SECONDS
    effective = local_deadline if deadline is None else min(local_deadline, deadline)
    watch = _GitWatch(process, effective, cancelled)
    watch.start()
    output = b""
    try:
        output = _read_probe_output(process)
    finally:
        watch.stopped.set()
        _close_probe(process)
        watch.finish()
    _require_probe_success(watch, process, command, output)
    return _probe_value(output, allow_empty)


def _git_value(
    root: Path,
    *arguments: str,
    deadline: float | None = None,
    cancelled=None,
) -> str:
    return _git_output(
        root,
        "rev-parse",
        *arguments,
        deadline=deadline,
        cancelled=cancelled,
    )


def _non_git_scope(local_root: str) -> RepositoryScope:
    repository_id = derive_repository_id(checkout_root=local_root, git_common_dir=None)
    return RepositoryScope(
        schema_version=SCHEMA_VERSION,
        repository_id=repository_id,
        checkout_id=derive_checkout_id(repository_id, local_root),
        checkout_root=local_root,
        git_common_dir=None,
        git_commit=None,
    )


def _git_roots(
    requested: Path, deadline: float | None, cancelled
) -> tuple[str, str]:
    """(checkout root, common dir) for the checkout that contains `requested`."""
    checkout_path = Path(
        _git_value(
            requested,
            "--path-format=absolute",
            "--show-toplevel",
            deadline=deadline,
            cancelled=cancelled,
        )
    ).resolve(strict=True)
    if not checkout_path.is_dir():
        raise ValueError("Git checkout root must be a directory")
    requested.relative_to(checkout_path)
    common = Path(
        _git_value(
            requested,
            "--path-format=absolute",
            "--git-common-dir",
            deadline=deadline,
            cancelled=cancelled,
        )
    )
    return (
        _local_serialized_path(checkout_path, strict=True),
        _local_serialized_path(common, strict=True),
    )


def _commit_without_head(
    requested: Path, head_error: Exception, deadline: float | None, cancelled
) -> None:
    """An empty repository has no commit; anything else is an uncertain identity."""
    try:
        ref = _git_output(
            requested,
            "for-each-ref",
            "--count=1",
            "--format=%(objectname)",
            "refs",
            deadline=deadline,
            cancelled=cancelled,
            allow_empty=True,
        )
    except TimeoutError:
        raise
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError) as exc:
        raise RepositoryScopeUnavailable(
            "Git commit identity is uncertain because refs could not be inspected"
        ) from exc
    if ref:
        raise RepositoryScopeUnavailable(
            "Git commit identity is uncertain because HEAD failed while refs exist"
        ) from head_error
    return None


def _git_commit_of(requested: Path, deadline: float | None, cancelled) -> str | None:
    try:
        git_commit = _git_value(
            requested,
            "--verify",
            "HEAD^{commit}",
            deadline=deadline,
            cancelled=cancelled,
        )
        _validated_commit(git_commit)
        return git_commit
    except subprocess.CalledProcessError as head_error:
        return _commit_without_head(requested, head_error, deadline, cancelled)
    except TimeoutError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise RepositoryScopeUnavailable("Git commit identity is unavailable") from exc


def _requested_directory(directory: Path) -> Path:
    requested = Path(directory).resolve(strict=True)
    if not requested.is_dir():
        raise NotADirectoryError(requested)
    return requested


def resolve_repository_scope(
    directory: Path,
    *,
    deadline: float | None = None,
    cancelled=None,
) -> RepositoryScope:
    """Resolve a directory to a stable local repository and checkout scope."""
    _check_stop(deadline, cancelled)
    requested = _requested_directory(directory)
    local_root = _local_serialized_path(requested, strict=True)
    try:
        checkout_root, git_common_dir = _git_roots(requested, deadline, cancelled)
    except TimeoutError:
        raise
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError) as exc:
        if _has_git_marker(requested):
            raise RepositoryScopeUnavailable(
                "Git checkout marker exists but repository identity is unavailable"
            ) from exc
        return _non_git_scope(local_root)
    git_commit = _git_commit_of(requested, deadline, cancelled)
    repository_id = derive_repository_id(
        checkout_root=checkout_root,
        git_common_dir=git_common_dir,
    )
    return RepositoryScope(
        schema_version=SCHEMA_VERSION,
        repository_id=repository_id,
        checkout_id=derive_checkout_id(repository_id, checkout_root),
        checkout_root=checkout_root,
        git_common_dir=git_common_dir,
        git_commit=git_commit,
    )
