"""Deterministic language-server install receipt values; no discovery or IO."""
from __future__ import annotations

from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes
from lsp_server_profile import LanguageServerProfile, is_sha256, thaw_profile_value

INSTALL_MANIFEST_NAME = "install-manifest.json"


MANIFEST_KEYS = frozenset(
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


def runtime_relative_text(profile: LanguageServerProfile) -> str:
    runtime = profile.runtime_option
    if runtime is None:
        return ""
    return runtime.sibling_relative.as_posix()


def runtime_field(profile: LanguageServerProfile, attribute: str) -> str:
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
        "runtime_integrity": runtime_field(profile, "package_integrity"),
        "runtime_relative_path": runtime_relative_text(profile),
        "runtime_sha256": runtime_sha256,
        "runtime_url": runtime_field(profile, "package_url"),
        "schema_version": profile.install_manifest_schema,
        "server_relative_path": profile.server_relative.as_posix(),
        "server_sha256": server_sha256,
        "version": profile.version,
    }


def manifest_shape_ok(value: object) -> bool:
    """A closed set of string keys, one of which is a real digest."""
    if not isinstance(value, dict) or set(value) != MANIFEST_KEYS:
        return False
    if any(not isinstance(item, str) for item in value.values()):
        return False
    return is_sha256(value["server_sha256"])


def require_manifest_shape(value: object) -> None:
    if not manifest_shape_ok(value):
        raise ManifestError("manifest_malformed")


MANIFEST_FIELD_CODES = (
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


def require_manifest_field(
    profile: LanguageServerProfile,
    value: dict,
    expected: dict,
    field: str,
    reason: str,
) -> None:
    if value[field] != expected[field]:
        raise ManifestError(profile.degradation_code(reason))


def require_manifest_fields(
    profile: LanguageServerProfile, value: dict, expected: dict
) -> None:
    for field, reason in MANIFEST_FIELD_CODES:
        require_manifest_field(profile, value, expected, field, reason)


def validate_install_manifest(
    profile: LanguageServerProfile, value: object
) -> dict[str, str]:
    """Check the receipt against the pins this build carries."""
    require_manifest_shape(value)
    assert isinstance(value, dict)
    expected = build_install_manifest(
        profile,
        server_sha256=value["server_sha256"],
        runtime_sha256=value["runtime_sha256"],
    )
    require_manifest_fields(profile, value, expected)
    return {field: value[field] for field in sorted(MANIFEST_KEYS)}


# Preserve the existing public Python serialization paths.
ManifestError.__module__ = "lsp_identity"
profile_initialization_options_sha256.__module__ = "lsp_identity"
profile_configuration_sha256.__module__ = "lsp_identity"
build_install_manifest.__module__ = "lsp_identity"
validate_install_manifest.__module__ = "lsp_identity"
