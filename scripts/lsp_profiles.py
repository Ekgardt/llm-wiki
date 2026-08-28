"""The managed language servers this build knows how to pin, install and drive.

Pyright is defined here from the constants that have always governed it in
`scripts/pyright_profile.py` -- imported, never retyped -- so that expressing it
through the language-neutral seam cannot drift from the behaviour the existing
Pyright tests pin. `tests/test_lsp_server_profile.py` asserts that equivalence
directly: same argv, same configuration bytes, same initialization-option bytes.

TypeScript is defined from measurement recorded in
`docs/research/2026-08-28-precise-navigation-beyond-python.md`. Three things in
it look arbitrary and are not:

* The pin is `typescript@5.9.3`, not `latest`. `typescript@7.0.2` is the
  Go-native port and ships no `lib/tsserver.js` at all -- its engine is a
  per-platform native binary in one of twenty optional dependencies. Verified by
  listing both tarballs on 2026-08-28.
* `tsserver.path` is passed explicitly. Left alone the server looks for
  `node_modules/typescript` in the repository and then beside its own bundle; a
  managed install has neither, and this vault cannot require a repository to
  have run `npm install`.
* Readiness gates on work-done progress. Ungated, the server answers before its
  project graph exists, and the wrong answer is well-formed: go-to-definition
  returns the import binding instead of the declaration. Measured 0/12 correct
  ungated, 12/12 gated.
"""

from __future__ import annotations

from pathlib import Path

from lsp_server_profile import (
    READINESS_INITIALIZED,
    READINESS_WORK_DONE_PROGRESS,
    IdentityNotification,
    LanguageServerProfile,
    ProfileRegistry,
    RuntimeOption,
    freeze_profile_value,
)
from pyright_profile import (
    PYRIGHT_CONFIGURATION,
    PYRIGHT_INITIALIZATION_OPTIONS,
    PYRIGHT_INSTALL_MANIFEST_SCHEMA,
    PYRIGHT_PACKAGE_INTEGRITY,
    PYRIGHT_PACKAGE_URL,
    PYRIGHT_SERVER_RELATIVE,
    QUALIFIED_NODE_MAJOR,
)

PYRIGHT_VERSION = "1.1.411"

# Pyright's vendor progress notifications. These are the three names currently
# hardcoded in `lsp_protocol.SERVER_NOTIFICATIONS`; carrying them on the profile
# is what lets a second server declare its own without editing that module.
PYRIGHT_NOTIFICATIONS = frozenset(
    {
        "pyright/beginProgress",
        "pyright/endProgress",
        "pyright/reportProgress",
    }
)

PYRIGHT_PROFILE = LanguageServerProfile(
    name="pyright",
    language_ids=("python",),
    file_suffixes=(".py", ".pyi"),
    version=PYRIGHT_VERSION,
    package_url=PYRIGHT_PACKAGE_URL,
    package_integrity=PYRIGHT_PACKAGE_INTEGRITY,
    server_relative=PYRIGHT_SERVER_RELATIVE,
    managed_relative_root=Path("cache/code-tools/pyright") / PYRIGHT_VERSION,
    install_manifest_schema=PYRIGHT_INSTALL_MANIFEST_SCHEMA,
    node_major=QUALIFIED_NODE_MAJOR,
    launch_flags=("--stdio",),
    # Pyright takes a cancellation directory under the owner scratch root. The
    # template is the exact string `pyright_session._start_configured_process`
    # has always built.
    owner_argument_template="--cancellationReceive=file:{owner}/cancellation",
    server_notifications=PYRIGHT_NOTIFICATIONS,
    configuration=PYRIGHT_CONFIGURATION,
    initialization_options=PYRIGHT_INITIALIZATION_OPTIONS,
    # Pyright's identity is the digest of the file we launch, checked before the
    # process starts, so there is nothing left to confirm afterwards.
    readiness=READINESS_INITIALIZED,
    identity_notification=None,
    runtime_option=None,
    degradation_prefix="pyright",
)

TYPESCRIPT_VERSION = "6.0.0"
TYPESCRIPT_PACKAGE_URL = (
    "https://registry.npmjs.org/typescript-language-server/-/"
    "typescript-language-server-6.0.0.tgz"
)
TYPESCRIPT_PACKAGE_SHA256 = (
    "6e23b48efc76af4e70928cdfe62ea6e6cfef67ab4c1e7579c4e82dd284fbdfd2"
)
TYPESCRIPT_PACKAGE_INTEGRITY = (
    "sha512-LXtzY3UZGfghWA5eRU6/T5j1+YiGRgy14mR3GOKyTKlE1op1TYKQnLVxwBsmnXeDhGLuvzZyIHBAqvrekAITYQ=="
)
TYPESCRIPT_SERVER_RELATIVE = Path("package/lib/cli.mjs")

# The engine the server drives, pinned separately and installed as a sibling
# inside the same managed root. 5.9.3 is the last release carrying
# `lib/tsserver.js`; see the module docstring.
TSSERVER_VERSION = "5.9.3"
TSSERVER_PACKAGE_URL = "https://registry.npmjs.org/typescript/-/typescript-5.9.3.tgz"
TSSERVER_PACKAGE_SHA256 = (
    "10e108c9cf7d5f2879053dff18515fb405abf2ccef63eaaf017d9c571687a1d3"
)
TSSERVER_PACKAGE_INTEGRITY = (
    "sha512-jl1vZzPDinLr9eUt3J/t7V6FgNEw9QjvBPdysz9KfQDD41fQrC2Y4vKQdiaUpFT4bXlb1RHhLpp8wtm6M5TgSw=="
)
TSSERVER_RELATIVE = Path("typescript/lib/tsserver.js")

# `engines.node` on typescript-language-server@6.0.0 is ">=22.22.2". The Pyright
# profile has never needed a minor floor; this one does.
TYPESCRIPT_NODE_MAJOR = 22
TYPESCRIPT_NODE_MINOR_FLOOR = 22

TYPESCRIPT_NOTIFICATIONS = frozenset({"$/typescriptVersion"})

# `logVerbosity: off` keeps tsserver from writing a log file next to the
# repository; the managed path owns its scratch and writes nothing else.
TYPESCRIPT_INITIALIZATION_OPTIONS = freeze_profile_value(
    {
        "hostInfo": "llm-wiki",
        "tsserver": {"logVerbosity": "off", "path": ""},
        "preferences": {"includeCompletionsForModuleExports": False},
    }
)

TYPESCRIPT_CONFIGURATION = freeze_profile_value(
    {
        "typescript": {
            "tsserver": {"useSyntaxServer": "never"},
            "preferences": {"includePackageJsonAutoImports": "off"},
        },
        "javascript": {"preferences": {"includePackageJsonAutoImports": "off"}},
    }
)

TYPESCRIPT_PROFILE = LanguageServerProfile(
    name="typescript",
    language_ids=("typescript", "typescriptreact", "javascript", "javascriptreact"),
    file_suffixes=(".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"),
    version=TYPESCRIPT_VERSION,
    package_url=TYPESCRIPT_PACKAGE_URL,
    package_integrity=TYPESCRIPT_PACKAGE_INTEGRITY,
    server_relative=TYPESCRIPT_SERVER_RELATIVE,
    managed_relative_root=(
        Path("cache/code-tools/typescript-language-server") / TYPESCRIPT_VERSION
    ),
    install_manifest_schema="typescript-language-server-install/v1",
    node_major=TYPESCRIPT_NODE_MAJOR,
    node_minor_floor=TYPESCRIPT_NODE_MINOR_FLOOR,
    launch_flags=("--stdio",),
    owner_argument_template=None,
    server_notifications=TYPESCRIPT_NOTIFICATIONS,
    configuration=TYPESCRIPT_CONFIGURATION,
    initialization_options=TYPESCRIPT_INITIALIZATION_OPTIONS,
    readiness=READINESS_WORK_DONE_PROGRESS,
    identity_notification=IdentityNotification(
        method="$/typescriptVersion",
        version_key="version",
        source_key="source",
        # Anything but `user-setting` means the server found some other
        # TypeScript -- the repository's, or one beside its own bundle -- and
        # the answer would no longer be the pinned engine's answer.
        required_source="user-setting",
    ),
    runtime_option=RuntimeOption(
        key_path=("tsserver", "path"),
        sibling_relative=TSSERVER_RELATIVE,
    ),
    degradation_prefix="typescript",
)

REGISTRY = ProfileRegistry((PYRIGHT_PROFILE, TYPESCRIPT_PROFILE))


def profile_for_path(path: Path) -> LanguageServerProfile | None:
    """The managed profile that owns a file, or None for the structural path."""
    return REGISTRY.for_path(path)


def profile_named(name: str) -> LanguageServerProfile:
    return REGISTRY.get(name)
