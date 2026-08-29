"""Discover the managed language server for one profile, without installing it.

`pyright_profile.discover_pyright` answers this question for Pyright, and does
more than a second language needs: it accepts a project-local or system
candidate and re-derives the identity from a lockfile. That precedence is
defensible for Pyright because the whole identity -- package version, executable
digest, lockfile evidence -- is re-derived for whichever candidate wins.

For every other profile this module looks in exactly one place: the managed root
the profile names. The reason is measured rather than tidy-minded. A
`typescript-language-server` found on `PATH` would still be handed *our* pinned
`tsserver.js` through `initializationOptions`, because that is how the profile
makes the answer reproducible at all (see
`docs/research/2026-08-28-precise-navigation-beyond-python.md`, Finding 3). The
combination "unpinned server driving a pinned engine" is a configuration nobody
measured, so it is not offered. One installed shape, or none.

Absence is a normal steady state, not a failure. Installation is a separate
explicit operator action under
`knowledge/notes/read-only-lsp-navigation-engine-decision.md`; when the artifact
is not there this returns a `missing` identity whose single degradation code is
named in the profile's own namespace, the session never starts, and the caller
falls back to structural evidence.
"""

from __future__ import annotations

import json
from pathlib import Path

from bounded_io import read_stable_bytes
from lsp_server_profile import LanguageServerProfile, is_sha256, thaw_profile_value
from pyright_profile import (
    MAX_INSTALL_MANIFEST_BYTES,
    MAX_SERVER_BYTES,
    PyrightIdentity,
    _check_deadline,
    _probe_node,
    _validated_deadline,
)
from reliable_memory import canonical_json_bytes, sha256_bytes
from repository_scope import RepositoryScope

INSTALL_MANIFEST_NAME = "install-manifest.json"

# The repository-side configuration whose bytes take part in a profile's
# identity. A changed project configuration is a changed session, the same way
# a changed `pyrightconfig.json` is for Pyright.
_REPOSITORY_CONFIG_NAMES = {
    "typescript": "tsconfig.json",
}
MAX_REPOSITORY_CONFIG_BYTES = 256 * 1024

# `_probe_node` reports in Pyright's namespace because that is the only caller
# it has ever had. Its checks -- node present, safe, probeable, right major --
# are not Pyright's, so the codes are re-prefixed rather than duplicated.
_PYRIGHT_CODE_PREFIX = "pyright_"

_MANIFEST_KEYS = frozenset(
    {
        "configuration_sha256",
        "initialization_options_sha256",
        "package_integrity",
        "package_url",
        "runtime_integrity",
        "runtime_relative_path",
        "runtime_sha256",
        "runtime_url",
        "schema_version",
        "server_relative_path",
        "server_sha256",
        "version",
    }
)


class ManifestError(ValueError):
    """The install receipt does not describe the artifact the profile pins."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def profile_initialization_options_sha256(profile: LanguageServerProfile) -> str:
    """Digest of the options as the profile declares them, before substitution.

    Deliberately the template rather than the wire form: the wire form carries
    an absolute managed path for profiles with a runtime option, which would
    make the digest a property of where the vault happens to live rather than of
    what this build sends.
    """
    return sha256_bytes(
        canonical_json_bytes(thaw_profile_value(profile.initialization_options))
    )


def profile_configuration_sha256(profile: LanguageServerProfile) -> str:
    return sha256_bytes(canonical_json_bytes(profile.wire_configuration()))


def _runtime_relative_text(profile: LanguageServerProfile) -> str:
    runtime = profile.runtime_option
    if runtime is None:
        return ""
    return runtime.sibling_relative.as_posix()


def _runtime_field(profile: LanguageServerProfile, attribute: str) -> str:
    runtime = profile.runtime_option
    if runtime is None:
        return ""
    return getattr(runtime, attribute) or ""


def build_install_manifest(
    profile: LanguageServerProfile,
    *,
    server_sha256: str,
    runtime_sha256: str = "",
) -> dict[str, str]:
    """The closed canonical receipt the explicit installer writes."""
    if not is_sha256(server_sha256):
        raise ValueError("server_sha256 must be a lowercase SHA-256 digest")
    return {
        "configuration_sha256": profile_configuration_sha256(profile),
        "initialization_options_sha256": profile_initialization_options_sha256(profile),
        "package_integrity": profile.package_integrity,
        "package_url": profile.package_url,
        "runtime_integrity": _runtime_field(profile, "package_integrity"),
        "runtime_relative_path": _runtime_relative_text(profile),
        "runtime_sha256": runtime_sha256,
        "runtime_url": _runtime_field(profile, "package_url"),
        "schema_version": profile.install_manifest_schema,
        "server_relative_path": profile.server_relative.as_posix(),
        "server_sha256": server_sha256,
        "version": profile.version,
    }


def _manifest_shape_ok(value: object) -> bool:
    """A closed set of string keys, one of which is a real digest."""
    if not isinstance(value, dict) or set(value) != _MANIFEST_KEYS:
        return False
    if any(not isinstance(item, str) for item in value.values()):
        return False
    return is_sha256(value["server_sha256"])


def _require_manifest_shape(value: object) -> None:
    if not _manifest_shape_ok(value):
        raise ManifestError("manifest_malformed")


_MANIFEST_FIELD_CODES = (
    ("schema_version", "manifest_schema_mismatch"),
    ("version", "version_mismatch"),
    ("package_url", "package_url_mismatch"),
    ("package_integrity", "integrity_mismatch"),
    ("server_relative_path", "server_relative_mismatch"),
    ("runtime_relative_path", "runtime_relative_mismatch"),
    ("runtime_url", "runtime_url_mismatch"),
    ("runtime_integrity", "runtime_integrity_mismatch"),
    ("configuration_sha256", "configuration_mismatch"),
    ("initialization_options_sha256", "initialization_options_mismatch"),
)


def _require_manifest_field(
    profile: LanguageServerProfile,
    value: dict,
    expected: dict,
    field: str,
    reason: str,
) -> None:
    if value[field] != expected[field]:
        raise ManifestError(profile.degradation_code(reason))


def _require_manifest_fields(
    profile: LanguageServerProfile, value: dict, expected: dict
) -> None:
    for field, reason in _MANIFEST_FIELD_CODES:
        _require_manifest_field(profile, value, expected, field, reason)


def validate_install_manifest(
    profile: LanguageServerProfile, value: object
) -> dict[str, str]:
    """Check the receipt against the pins this build carries."""
    _require_manifest_shape(value)
    assert isinstance(value, dict)
    expected = build_install_manifest(
        profile,
        server_sha256=value["server_sha256"],
        runtime_sha256=value["runtime_sha256"],
    )
    _require_manifest_fields(profile, value, expected)
    return {field: value[field] for field in sorted(_MANIFEST_KEYS)}


def _read_json_document(path: Path, max_bytes: int, deadline: float | None) -> object:
    raw = read_stable_bytes(path, max_bytes, label="install manifest", deadline=deadline)
    return json.loads(raw.decode("utf-8", errors="strict"))


def _digest_of(path: Path, max_bytes: int, deadline: float | None) -> tuple[str, str]:
    """(digest, reason) for one managed file; reason is empty when it is fine."""
    try:
        content = read_stable_bytes(path, max_bytes, label="server", deadline=deadline)
    except FileNotFoundError:
        return "", "missing"
    except PermissionError:
        return "", "unsafe"
    except ValueError:
        return "", "oversized"
    except OSError:
        return "", "unreadable"
    return sha256_bytes(content), ""


def _reprefixed_one(profile: LanguageServerProfile, code: str) -> str:
    if not code.startswith(_PYRIGHT_CODE_PREFIX):
        return code
    return profile.degradation_code(code[len(_PYRIGHT_CODE_PREFIX):])


def _reprefixed(profile: LanguageServerProfile, codes: set[str]) -> set[str]:
    return {_reprefixed_one(profile, code) for code in codes}


def _node_minor(version: str | None) -> int | None:
    """The minor component of a `vMAJOR.MINOR.PATCH` string, when readable."""
    if not isinstance(version, str):
        return None
    parts = version.lstrip("v").split(".")
    if len(parts) < 2 or not parts[1].isdecimal():
        return None
    return int(parts[1])


def _node_floor_codes(
    profile: LanguageServerProfile, version: str | None, major: int | None
) -> set[str]:
    """`engines.node >= 22.22.2` needs a minor floor Pyright never needed."""
    if profile.node_minor_floor <= 0 or major != profile.node_major:
        return set()
    minor = _node_minor(version)
    if minor is None or minor < profile.node_minor_floor:
        return {profile.degradation_code("node_minor_below_floor")}
    return set()


def repository_configuration_digest(
    profile: LanguageServerProfile,
    repository: RepositoryScope,
    deadline: float | None = None,
) -> str:
    """Digest of the project configuration this profile reads, or of nothing.

    A missing configuration is deliberately **not** a degradation code. The
    measurement says an absent `tsconfig.json` leaves tsserver inferring a
    project and still answering, and any degradation code disqualifies the whole
    session -- so treating absence as a fault would turn "slightly weaker
    resolution" into "no precise navigation at all", which is the wrong trade
    and not what the contract asks for.
    """
    name = _REPOSITORY_CONFIG_NAMES.get(profile.name)
    if name is None:
        return sha256_bytes(b"")
    digest, _reason = _digest_of(
        Path(repository.checkout_root) / name, MAX_REPOSITORY_CONFIG_BYTES, deadline
    )
    return digest or sha256_bytes(b"")


def _missing_identity(
    profile: LanguageServerProfile, configuration_sha256: str, reason: str
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
        initialization_options_sha256=profile_initialization_options_sha256(profile),
        configuration_sha256=configuration_sha256,
        qualified=False,
        degradation_codes=(profile.degradation_code(reason),),
    )


def _receipt_digest_codes(
    profile: LanguageServerProfile,
    manifest: dict[str, str],
    server_sha256: str,
    runtime_sha256: str,
) -> set[str]:
    codes: set[str] = set()
    if manifest["server_sha256"] != server_sha256:
        codes.add(profile.degradation_code("executable_digest_mismatch"))
    if manifest["runtime_sha256"] != runtime_sha256:
        codes.add(profile.degradation_code("runtime_digest_mismatch"))
    return codes


def _validated_manifest_codes(
    profile: LanguageServerProfile,
    value: object,
    server_sha256: str,
    runtime_sha256: str,
) -> set[str]:
    try:
        manifest = validate_install_manifest(profile, value)
    except ManifestError as exc:
        return {_reprefixed_one(profile, exc.code)}
    return _receipt_digest_codes(profile, manifest, server_sha256, runtime_sha256)


def _manifest_codes(
    profile: LanguageServerProfile,
    root: Path,
    server_sha256: str,
    runtime_sha256: str,
    deadline: float | None,
) -> set[str]:
    try:
        value = _read_json_document(
            root / INSTALL_MANIFEST_NAME, MAX_INSTALL_MANIFEST_BYTES, deadline
        )
    except (OSError, UnicodeDecodeError, ValueError):
        return {profile.degradation_code("manifest_unreadable")}
    return _validated_manifest_codes(profile, value, server_sha256, runtime_sha256)


def _runtime_digest(
    profile: LanguageServerProfile, state_root: Path, deadline: float | None
) -> tuple[str, set[str]]:
    """Digest the pinned engine the profile hands the server, if it has one.

    This is the *precondition* half of the identity assertion the profile
    describes: the server resolves its engine at runtime and reports the outcome
    afterwards, but the file we point it at is ours to check before it starts.
    """
    runtime = profile.runtime_option
    if runtime is None:
        return "", set()
    path = runtime.resolved_path(profile.managed_root(state_root))
    digest, reason = _digest_of(path, MAX_SERVER_BYTES, deadline)
    if reason:
        return digest, {profile.degradation_code(f"runtime_{reason}")}
    return digest, set()


def _built_identity(
    profile: LanguageServerProfile,
    node: tuple[Path | None, str | None, int | None],
    server: tuple[Path, str | None],
    configuration_sha256: str,
    codes: tuple[str, ...],
) -> PyrightIdentity:
    node_executable, node_version, node_major = node
    server_executable, server_sha256 = server
    return PyrightIdentity(
        status="degraded" if codes else "qualified",
        source="managed",
        version=profile.version,
        node_executable=node_executable,
        node_version=node_version,
        node_major=node_major,
        server_executable=server_executable,
        executable_sha256=server_sha256,
        package_sha256=None,
        initialization_options_sha256=profile_initialization_options_sha256(profile),
        configuration_sha256=configuration_sha256,
        qualified=not codes,
        degradation_codes=codes,
    )


def _artifact_codes(
    profile: LanguageServerProfile,
    state_root: Path,
    server_digest: tuple[str, str],
    deadline: float | None,
) -> set[str]:
    """Every degradation the installed tree itself accounts for."""
    server_sha256, reason = server_digest
    codes = {profile.degradation_code(f"server_{reason}")} if reason else set()
    runtime_sha256, runtime_codes = _runtime_digest(profile, state_root, deadline)
    codes.update(runtime_codes)
    codes.update(
        _manifest_codes(
            profile,
            profile.managed_root(state_root),
            server_sha256,
            runtime_sha256,
            deadline,
        )
    )
    return codes


def _inspected_identity(
    profile: LanguageServerProfile,
    state_root: Path,
    server: Path,
    server_digest: tuple[str, str],
    configuration_sha256: str,
    deadline: float | None,
) -> PyrightIdentity:
    codes = _artifact_codes(profile, state_root, server_digest, deadline)
    node, node_version, node_major, node_codes = _probe_node(deadline)
    codes.update(_reprefixed(profile, node_codes))
    codes.update(_node_floor_codes(profile, node_version, node_major))
    return _built_identity(
        profile,
        (node, node_version, node_major),
        (server, server_digest[0] or None),
        configuration_sha256,
        tuple(sorted(codes)),
    )


def _require_type(value: object, expected: type, label: str) -> None:
    if not isinstance(value, expected):
        raise TypeError(f"{label} must be a {expected.__name__}")


def _require_discovery_arguments(
    profile: LanguageServerProfile, repository: RepositoryScope, state_root: Path
) -> None:
    _require_type(profile, LanguageServerProfile, "profile")
    _require_type(repository, RepositoryScope, "repository")
    _require_type(state_root, Path, "state_root")


def discover_managed_server(
    profile: LanguageServerProfile,
    repository: RepositoryScope,
    *,
    state_root: Path,
    deadline: float | None = None,
) -> PyrightIdentity:
    """The identity of this profile's managed install, mutating nothing."""
    _require_discovery_arguments(profile, repository, state_root)
    deadline = _validated_deadline(deadline)
    _check_deadline(deadline)
    configuration_sha256 = repository_configuration_digest(profile, repository, deadline)
    server = profile.server_path(state_root)
    server_digest = _digest_of(server, MAX_SERVER_BYTES, deadline)
    if server_digest[1] == "missing":
        return _missing_identity(profile, configuration_sha256, "missing")
    return _inspected_identity(
        profile, state_root, server, server_digest, configuration_sha256, deadline
    )
