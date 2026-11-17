"""Immutable Pyright package pins and wire defaults, independent of discovery."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes

PYRIGHT_PACKAGE_URL = "https://registry.npmjs.org/pyright/-/pyright-1.1.411.tgz"


PYRIGHT_PACKAGE_SHA256 = (
    "bd5c488fc20fa237a944279bf32cae2f986cf10d5d5d9e8705819859daeb2f4a"
)


PYRIGHT_PACKAGE_INTEGRITY = (
    "sha512-03S/vmS5lF1S/tVbKc2WNXCMq8JWCwta/qIYjj1jvqbQhoy+N3NgBzHTSmUlbYD6DJwqQ5XHf108QujoqeURvw=="
)


QUALIFIED_NODE_MAJOR = 22


PYRIGHT_SERVER_RELATIVE = Path("package/langserver.index.js")


def freeze_pyright_profile_value(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: freeze_pyright_profile_value(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(freeze_pyright_profile_value(item) for item in value)
    return value


def _thaw_mapping(value: Mapping) -> dict:
    return {key: thaw_pyright_profile_value(item) for key, item in value.items()}


def _thaw_tuple(value: tuple) -> list:
    return [thaw_pyright_profile_value(item) for item in value]


def thaw_pyright_profile_value(value: object) -> object:
    """Return a mutable JSON-domain copy of an immutable profile value."""
    if isinstance(value, Mapping):
        return _thaw_mapping(value)
    return _thaw_non_mapping(value)


def _thaw_non_mapping(value: object) -> object:
    if isinstance(value, tuple):
        return _thaw_tuple(value)
    if value is None or isinstance(value, (bool, int, str)):
        return value
    raise TypeError(f"unsupported Pyright profile value: {type(value).__name__}")


PYRIGHT_CONFIGURATION = freeze_pyright_profile_value({
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


PYRIGHT_INITIALIZATION_OPTIONS = freeze_pyright_profile_value(
    {"files": {"exclude": []}}
)


PYRIGHT_INSTALL_MANIFEST_SCHEMA = "pyright-install/v2"


PYRIGHT_CONFIGURATION_SHA256 = sha256_bytes(
    canonical_json_bytes(thaw_pyright_profile_value(PYRIGHT_CONFIGURATION))
)


PYRIGHT_INITIALIZATION_OPTIONS_SHA256 = sha256_bytes(
    canonical_json_bytes(thaw_pyright_profile_value(PYRIGHT_INITIALIZATION_OPTIONS))
)


# Existing public pickle/import identity is retained by the discovery facade.
thaw_pyright_profile_value.__module__ = "pyright_profile"
