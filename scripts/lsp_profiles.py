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

import platform
from pathlib import Path

from lsp_server_profile import (
    READINESS_INITIALIZED,
    READINESS_WORK_DONE_PROGRESS,
    IdentityNotification,
    LanguageServerProfile,
    PackageLaunch,
    PlatformArtifact,
    ProfileRegistry,
    RuntimeOption,
    SourceBuild,
    freeze_profile_value,
    normalized_platform,
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
    owner_argument_template="--cancellationReceive=file:{owner}",
    owner_argument_relative=Path("cancellation"),
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

# `cli.mjs` reads exactly one thing relative to itself -- `../package.json`, for
# `{version}`, which it hands to commander's `.version()`. Measured on
# 2026-08-28 by reading the bundle and on 2026-08-29 by driving an `initialize`
# handshake from a launch root holding this three-field manifest instead of the
# shipped one: identical traffic, identical `user-setting` identity. Authored
# rather than copied so that no unverified byte of the operator-writable install
# root is read at exec time. See
# `docs/research/2026-08-29-launching-a-verified-server-without-a-toctou-window.md`.
TYPESCRIPT_LAUNCH_MANIFEST = freeze_profile_value(
    {
        "name": "typescript-language-server",
        "version": TYPESCRIPT_VERSION,
        "type": "module",
    }
)

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
        package_url=TSSERVER_PACKAGE_URL,
        package_integrity=TSSERVER_PACKAGE_INTEGRITY,
    ),
    degradation_prefix="typescript",
    # The two root-level files that change what this server answers. Listed so a
    # `tsconfig.json` edit invalidates the freshness record the same way a
    # `pyproject.toml` edit does for Python.
    configuration_names=("tsconfig.json", "jsconfig.json"),
    package_launch=PackageLaunch(
        entry_relative=Path("lib/cli.mjs"),
        manifest=TYPESCRIPT_LAUNCH_MANIFEST,
    ),
)

# ---------------------------------------------------------------------------
# gopls: the one profile that is compiled rather than unpacked
# ---------------------------------------------------------------------------
#
# The Go team publishes gopls only as a Go module, so this profile pins a Go
# toolchain per platform and builds one pinned module version with it. The
# binary a build produces is not bit-reproducible, so what is pinned here is
# every input; the digest that build produced is recorded in the install
# manifest and checked before each launch, exactly as Pyright's entry file is.
# Research: `docs/research/2026-09-12-installing-go-and-building-gopls.md`.

GO_VERSION = "1.27.1"
GOPLS_VERSION = "v0.23.0"

GO_ARTIFACTS = (
    PlatformArtifact(
        system="linux",
        machine="x86_64",
        url="https://dl.google.com/go/go1.27.1.linux-amd64.tar.gz",
        integrity="sha256-Y9M58NpatTY1pW8kkKeYTf4S38/yKtdJ9j7a9ZAWhEU=",
        size=70553950,
    ),
    PlatformArtifact(
        system="linux",
        machine="arm64",
        url="https://dl.google.com/go/go1.27.1.linux-arm64.tar.gz",
        integrity="sha256-NFC0Wj+e6FaHknNqXF5wofLps2w1qPdJWMA+UdfZK+w=",
        size=67009954,
    ),
    PlatformArtifact(
        system="darwin",
        machine="x86_64",
        url="https://dl.google.com/go/go1.27.1.darwin-amd64.tar.gz",
        integrity="sha256-j49SxmSVQs8Ce7ybnGjR7AQvnzSAikBBPwuLP2bzyqQ=",
        size=71621873,
    ),
    PlatformArtifact(
        system="darwin",
        machine="arm64",
        url="https://dl.google.com/go/go1.27.1.darwin-arm64.tar.gz",
        integrity="sha256-7iFdV+DsJpxgzJzspo5r2jIbqe5a/iT0sJiHA8LYfRI=",
        size=68100347,
    ),
    PlatformArtifact(
        system="windows",
        machine="x86_64",
        url="https://dl.google.com/go/go1.27.1.windows-amd64.zip",
        integrity="sha256-o5EbXg4bEFPyXtBnX0wcaq0eK/zyU98rm+TKq9Lt2V0=",
        size=78931360,
    ),
)

# The unpacked toolchain and the built server, both inside the managed root.
GO_TOOLCHAIN_RELATIVE = Path("go/bin/go")
GOPLS_BINARY_RELATIVE = Path("bin/gopls")
GO_TOOLCHAIN_RELATIVE_WINDOWS = Path("go/bin/go.exe")
GOPLS_BINARY_RELATIVE_WINDOWS = Path("bin/gopls.exe")

# Measured on `go1.27.1.linux-amd64.tar.gz` (2026-09-12): 17 353 members,
# 243 910 739 bytes unpacked, largest member 28 238 898 bytes. The npm pins keep
# the module-wide bounds they have today; these are this profile's own.
GOPLS_MAX_COMPRESSED_BYTES = 96 * 1024 * 1024
GOPLS_MAX_DECOMPRESSED_BYTES = 512 * 1024 * 1024
GOPLS_MAX_MEMBERS = 32768

# gopls reports package loading as work-done progress, and answers before it
# finishes if asked -- the same trap tsserver sets, and the same gate.
GOPLS_NOTIFICATIONS = frozenset({"window/showMessage", "window/logMessage"})

# Local, read-only defaults: no build on save, no module downloads triggered by
# a navigation query, and the standard library kept out of workspace symbols.
GOPLS_CONFIGURATION = freeze_profile_value(
    {
        "gopls": {
            "build.allowModfileModifications": False,
            "build.allowImplicitNetworkAccess": False,
            "ui.diagnostic.analyses": {},
            "ui.navigation.importShortcut": "Definition",
        }
    }
)


# gopls runs `go list` while it answers, so the pinned toolchain has to be the
# one on its PATH -- only ours, so a system Go cannot answer instead -- and its
# caches have to live inside the managed root. `GOTOOLCHAIN=local` keeps the pin
# a pin at query time for the same reason it does at build time.
GOPLS_ENVIRONMENT_TEMPLATE = (
    ("PATH", "{root}/go/bin"),
    ("GOROOT", "{root}/go"),
    ("GOPATH", "{root}/gopath"),
    ("GOMODCACHE", "{root}/gopath/pkg/mod"),
    ("GOCACHE", "{root}/gocache"),
    ("GOTOOLCHAIN", "local"),
)


def _gopls_artifact() -> PlatformArtifact:
    """This machine's toolchain archive, falling back to the 64-bit Linux pin.

    A platform with no pin cannot install, and the profile still has to exist:
    `doctor` and the registry are read on every platform, and an import that
    raised would take the whole navigation path down instead of one language.
    """
    found = None
    for artifact in GO_ARTIFACTS:
        if normalized_platform(artifact.system, artifact.machine) == _this_platform():
            found = artifact
    return found if found is not None else GO_ARTIFACTS[0]


def _this_platform() -> tuple[str, str]:
    return normalized_platform(platform.system(), platform.machine())


def _windows() -> bool:
    return _this_platform()[0] == "windows"


def _gopls_relative(posix: Path, windows: Path) -> Path:
    return windows if _windows() else posix


GOPLS_ARTIFACT = _gopls_artifact()

GOPLS_PROFILE = LanguageServerProfile(
    name="gopls",
    language_ids=("go",),
    file_suffixes=(".go",),
    version=GOPLS_VERSION,
    package_url=GOPLS_ARTIFACT.url,
    package_integrity=GOPLS_ARTIFACT.integrity,
    server_relative=_gopls_relative(GOPLS_BINARY_RELATIVE, GOPLS_BINARY_RELATIVE_WINDOWS),
    managed_relative_root=Path("cache/code-tools/gopls") / GOPLS_VERSION,
    install_manifest_schema="gopls-install/v1",
    node_major=None,
    native=True,
    # `gopls` with no argument is `gopls serve` over stdio, which is what every
    # editor launches. A `-mode=` flag belongs to the subcommand and is not
    # passed here, so the argv stays the one the upstream default documents.
    launch_flags=(),
    owner_argument_template=None,
    server_notifications=GOPLS_NOTIFICATIONS,
    configuration=GOPLS_CONFIGURATION,
    initialization_options=freeze_profile_value({}),
    readiness=READINESS_WORK_DONE_PROGRESS,
    identity_notification=None,
    runtime_option=None,
    degradation_prefix="gopls",
    configuration_names=("go.mod", "go.work", "go.sum"),
    platform_artifacts=GO_ARTIFACTS,
    source_build=SourceBuild(
        module="golang.org/x/tools/gopls",
        version=GOPLS_VERSION,
        toolchain_relative=_gopls_relative(
            GO_TOOLCHAIN_RELATIVE, GO_TOOLCHAIN_RELATIVE_WINDOWS
        ),
        binary_relative=_gopls_relative(
            GOPLS_BINARY_RELATIVE, GOPLS_BINARY_RELATIVE_WINDOWS
        ),
    ),
    max_compressed_bytes=GOPLS_MAX_COMPRESSED_BYTES,
    max_decompressed_bytes=GOPLS_MAX_DECOMPRESSED_BYTES,
    max_members=GOPLS_MAX_MEMBERS,
    environment_template=GOPLS_ENVIRONMENT_TEMPLATE,
)

REGISTRY = ProfileRegistry((PYRIGHT_PROFILE, TYPESCRIPT_PROFILE, GOPLS_PROFILE))

# The two notification methods that are not any one vendor's: `$/progress` is
# the specification's own, and published diagnostics are asked for by every
# profile's client capabilities.
NEUTRAL_SERVER_NOTIFICATIONS = frozenset(
    {"$/progress", "textDocument/publishDiagnostics"}
)


def server_notification_union() -> frozenset[str]:
    """Every notification any managed profile is allowed to send.

    `lsp_protocol` needs one allowlist because the transport is shared and has
    no profile: threading a per-session set through `lsp_process` would put
    language into the one layer measurement found free of it. Deriving the
    allowlist here keeps a single source of truth -- add a profile and the
    transport learns its notifications with it.
    """
    names: set[str] = set(NEUTRAL_SERVER_NOTIFICATIONS)
    for name in REGISTRY.names():
        names.update(REGISTRY.get(name).server_notifications)
    return frozenset(names)


def navigable_suffixes() -> frozenset[str]:
    """Every file suffix some managed profile can answer questions about.

    `workspace_revision` needs this to decide which files belong in a checkout's
    freshness record. Hardcoding `.py`/`.pyi` there is what made
    `compute_workspace_revision` return no entries at all on a TypeScript
    repository, so the document could not be validated and the answer failed
    before the server was reached. Derived here for the same reason
    `server_notification_union` is: add a profile and the freshness contract
    learns its files with it.
    """
    suffixes: set[str] = set()
    for name in REGISTRY.names():
        suffixes.update(REGISTRY.get(name).file_suffixes)
    return frozenset(suffixes)


def profile_configuration_names() -> frozenset[str]:
    """Root-level configuration files a managed profile declares as its own.

    Python's set is older than the profile seam and still lives in
    `workspace_revision.PYTHON_CONFIG_NAMES`, including its `requirements*.txt`
    prefix rule; this is the additive channel a second language uses instead of
    editing that rule.
    """
    names: set[str] = set()
    for name in REGISTRY.names():
        names.update(REGISTRY.get(name).configuration_names)
    return frozenset(names)


def profile_for_path(path: Path) -> LanguageServerProfile | None:
    """The managed profile that owns a file, or None for the structural path."""
    return REGISTRY.for_path(path)


def profile_named(name: str) -> LanguageServerProfile:
    return REGISTRY.get(name)
