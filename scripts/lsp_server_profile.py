"""Language-neutral description of one pinned, managed language server.

The read-only LSP navigation engine (see
`knowledge/notes/read-only-lsp-navigation-engine-decision.md`) was built as one
production slice: Python through a pinned Pyright. Measurement on 2026-08-28
(`docs/research/2026-08-28-precise-navigation-beyond-python.md`) found that the
expensive half of that slice is already language-neutral -- process containment,
owner and lease evidence, framing, cancellation and position encoding carry five
incidental mentions of Pyright across 11,591 lines, four of them prose.

What is actually language-shaped is small and enumerable, and this module is
that enumeration. A profile is data: the pinned artifact, how to launch it, what
it must be told at initialize, which vendor notifications it is allowed to send,
how to know it is ready, and how to confirm afterwards that it loaded what we
pinned. Nothing here starts a process or speaks the protocol; those stay in
`lsp_process` and `lsp_protocol`, which need no profile to do their work.

Three fields exist only because a second language forced them into view, and a
design that omitted them would install and start and then answer wrongly:

* `runtime_option_path` -- Pyright's initialization options are frozen constants
  hashed into its identity. typescript-language-server must be handed the
  absolute path of the `tsserver.js` we pinned, because left alone it searches
  the repository's `node_modules` and then its own, and a managed install has
  neither.
* `readiness` -- Pyright is ready when it says it is initialized.
  typescript-language-server answers before its project is loaded, and the
  answers are well-formed and wrong: measured 0/12 correct ungated against 12/12
  gated on the work-done-progress `end` notification.
* `identity_notification` -- Pyright's version identity is settled by the
  executable digest before launch. TypeScript's can only be confirmed after
  initialize, from the `source` field the server reports back.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

# How a server tells us it is ready to answer a semantic query.
#
# "initialized" -- readiness follows the initialize handshake and the profile's
# own document priming. This is what the Pyright path has always done.
#
# "work-done-progress" -- the server answers before its project graph exists, so
# readiness is the `$/progress` `end` that closes the work-done token the server
# opened. The client must advertise `window.workDoneProgress` and must reply to
# `window/workDoneProgress/create`, or the server never sends `$/progress` at
# all and every query silently races the project load.
READINESS_INITIALIZED = "initialized"
READINESS_WORK_DONE_PROGRESS = "work-done-progress"

_READINESS_POLICIES = frozenset({READINESS_INITIALIZED, READINESS_WORK_DONE_PROGRESS})

# A pinned artifact is identified by an npm-style Subresource Integrity string.
_INTEGRITY_PREFIX = "sha512-"
_SHA256_LENGTH = 64
_HEX_DIGITS = frozenset("0123456789abcdef")


class ProfileError(ValueError):
    """A profile is internally inconsistent, or names something it cannot have."""


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ProfileError(f"{label} must be a string")
    if not value:
        raise ProfileError(f"{label} must not be empty")
    return value


def _require_contained(path: Path, label: str) -> None:
    if path.is_absolute() or ".." in path.parts:
        raise ProfileError(f"{label} must be a relative path that does not escape")


def _require_relative(path: object, label: str) -> Path:
    if not isinstance(path, Path):
        raise ProfileError(f"{label} must be a Path")
    _require_contained(path, label)
    return path


def _require_tuple_of_text(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise ProfileError(f"{label} must be a tuple")
    for item in value:
        _require_text(item, f"{label} entry")
    return value


def _require_integrity(value: object) -> str:
    text = _require_text(value, "package_integrity")
    if not text.startswith(_INTEGRITY_PREFIX):
        raise ProfileError("package_integrity must be a sha512 SRI string")
    return text


def _require_readiness(value: object) -> str:
    text = _require_text(value, "readiness")
    if text not in _READINESS_POLICIES:
        raise ProfileError(f"unsupported readiness policy: {text}")
    return text


def _require_node_major(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProfileError("node_major must be an integer")
    if value < 1:
        raise ProfileError("node_major must be positive")
    return value


def _require_node_minor(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProfileError("node_minor_floor must be an integer")
    if value < 0:
        raise ProfileError("node_minor_floor must not be negative")
    return value


_SCALAR_TYPES = (bool, int, str)


def _is_scalar(value: object) -> bool:
    return value is None or isinstance(value, _SCALAR_TYPES)


def _frozen_scalar(value: object) -> object:
    if _is_scalar(value):
        return value
    raise ProfileError(f"unsupported profile value: {type(value).__name__}")


def _frozen_mapping(value: Mapping) -> MappingProxyType:
    return MappingProxyType(
        {key: freeze_profile_value(item) for key, item in value.items()}
    )


def _frozen_sequence(value: list | tuple) -> tuple:
    return tuple(freeze_profile_value(item) for item in value)


def freeze_profile_value(value: object) -> object:
    """Deep-freeze a JSON-shaped profile value into hashable, immutable form.

    Mirrors `pyright_profile._freeze_pyright_profile_value` so a profile's
    static option trees cannot be mutated by a caller that received them.
    """
    if isinstance(value, Mapping):
        return _frozen_mapping(value)
    if isinstance(value, (list, tuple)):
        return _frozen_sequence(value)
    return _frozen_scalar(value)


def thaw_profile_value(value: object) -> object:
    """Return a plain mutable copy of a frozen profile value, for the wire."""
    if isinstance(value, Mapping):
        return {key: thaw_profile_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_profile_value(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class IdentityNotification:
    """A post-initialize assertion that the server loaded what we pinned.

    Pyright needs none: its identity is the digest of the file we launched.
    typescript-language-server resolves its own engine at runtime and reports
    the outcome, so the only way to know it did not silently pick up some other
    TypeScript is to read the notification it sends and insist on the source we
    asked for.
    """

    method: str
    version_key: str
    source_key: str
    required_source: str

    def __post_init__(self) -> None:
        _require_text(self.method, "identity_notification.method")
        _require_text(self.version_key, "identity_notification.version_key")
        _require_text(self.source_key, "identity_notification.source_key")
        _require_text(self.required_source, "identity_notification.required_source")

    def confirmed(self, params: object) -> tuple[str | None, bool]:
        """Read (version, whether the source is the one we pinned) from params."""
        if not isinstance(params, Mapping):
            return None, False
        version = params.get(self.version_key)
        source = params.get(self.source_key)
        if not isinstance(version, str) or not isinstance(source, str):
            return None, False
        return version, source == self.required_source


@dataclass(frozen=True, slots=True)
class PackageLaunch:
    """A server that must be executed from inside a package root, not a descriptor.

    Declared only by profiles whose entry point reads a file relative to its own
    location. `typescript-language-server@6.0.0` does: `cli.mjs` evaluates
    `new URL('../package.json', import.meta.url)` to read one field, `version`.
    `import.meta.url` is not settable and has no descriptor form, so the
    descriptor launch that closes the verify-then-execute race on Pyright cannot
    execute such a server at all -- measured, see
    `docs/research/2026-08-29-launching-a-verified-server-without-a-toctou-window.md`.

    `entry_relative` is where the verified entry sits inside the launch root.
    `manifest` is the `package.json` this build **authors** rather than copies:
    copying the installed one would carry unverified bytes out of the
    operator-writable install root and into the exec path, which is the thing the
    copy-aside exists to prevent. A profile that left this None keeps the
    descriptor launch untouched.
    """

    entry_relative: Path
    manifest: object

    def __post_init__(self) -> None:
        _require_relative(self.entry_relative, "package_launch.entry_relative")
        if not isinstance(self.manifest, Mapping):
            raise ProfileError("package_launch.manifest must be a mapping")
        if not self.manifest:
            raise ProfileError("package_launch.manifest must not be empty")


@dataclass(frozen=True, slots=True)
class RuntimeOption:
    """An initialization option whose value is a path only known at install time.

    `key_path` walks into the initialization options; `sibling_relative` is the
    file inside the managed root that the value must point at. Kept declarative
    on purpose: a callable here would be unhashable, untestable as data, and
    would put arbitrary code inside a frozen profile.

    `package_url` and `package_integrity` pin the second tarball that has to be
    unpacked for `sibling_relative` to exist. They are on the runtime option
    rather than on the profile because they only make sense together with it: a
    profile with no runtime path needs no second artifact, and a runtime path
    with no artifact could never be satisfied by an install.
    """

    key_path: tuple[str, ...]
    sibling_relative: Path
    package_url: str | None = None
    package_integrity: str | None = None

    def __post_init__(self) -> None:
        _require_tuple_of_text(self.key_path, "runtime_option.key_path")
        if not self.key_path:
            raise ProfileError("runtime_option.key_path must not be empty")
        _require_relative(self.sibling_relative, "runtime_option.sibling_relative")
        self._check_artifact()

    def _check_artifact(self) -> None:
        if self.package_url is None and self.package_integrity is None:
            return
        url = _require_text(self.package_url, "runtime_option.package_url")
        if not url.startswith("https://"):
            raise ProfileError("runtime_option.package_url must be https")
        _require_integrity(self.package_integrity)

    @property
    def install_subdirectory(self) -> str:
        """The directory under the managed root this artifact unpacks into."""
        return self.sibling_relative.parts[0]

    def resolved_path(self, managed_root: Path) -> Path:
        if not isinstance(managed_root, Path):
            raise ProfileError("managed_root must be a Path")
        if not managed_root.is_absolute():
            raise ProfileError("managed_root must be absolute")
        return managed_root / self.sibling_relative

    def applied(self, options: dict, managed_root: Path) -> dict:
        """Return options with the runtime path written at `key_path`."""
        if not isinstance(options, dict):
            raise ProfileError("initialization options must be a dict")
        cursor = options
        for key in self.key_path[:-1]:
            cursor = _descended(cursor, key)
        cursor[self.key_path[-1]] = str(self.resolved_path(managed_root))
        return options


def _descended(cursor: dict, key: str) -> dict:
    child = cursor.get(key)
    if child is None:
        child = {}
        cursor[key] = child
    if not isinstance(child, dict):
        raise ProfileError(f"runtime option path collides at {key!r}")
    return child


@dataclass(frozen=True, slots=True)
class LanguageServerProfile:
    """Everything about a pinned managed language server that is language-shaped.

    Every field here was found by measurement to differ between Pyright and
    typescript-language-server, or to be the reason a shared module currently
    names Pyright. Nothing that both servers agree on belongs here.
    """

    name: str
    language_ids: tuple[str, ...]
    file_suffixes: tuple[str, ...]
    version: str
    package_url: str
    package_integrity: str
    server_relative: Path
    managed_relative_root: Path
    install_manifest_schema: str
    node_major: int
    launch_flags: tuple[str, ...]
    server_notifications: frozenset[str]
    configuration: object
    initialization_options: object
    readiness: str
    degradation_prefix: str
    node_minor_floor: int = 0
    owner_argument_template: str | None = None
    owner_argument_relative: Path | None = None
    runtime_option: RuntimeOption | None = None
    identity_notification: IdentityNotification | None = None
    package_launch: PackageLaunch | None = None
    configuration_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self._check_names()
        self._check_artifact()
        self._check_runtime()
        self._check_launch()

    def _check_launch(self) -> None:
        """A package launch has to name the same entry the artifact pin names."""
        launch = self.package_launch
        if launch is None:
            return
        self._check_launch_entry(launch)

    def _check_launch_entry(self, launch: object) -> None:
        if not isinstance(launch, PackageLaunch):
            raise ProfileError("package_launch must be a PackageLaunch")
        wanted = launch.entry_relative.parts
        if self.server_relative.parts[-len(wanted) :] != wanted:
            raise ProfileError("package_launch.entry_relative must end server_relative")

    def _check_names(self) -> None:
        _require_text(self.name, "name")
        _require_text(self.degradation_prefix, "degradation_prefix")
        _require_text(self.install_manifest_schema, "install_manifest_schema")
        _require_tuple_of_text(self.language_ids, "language_ids")
        _require_tuple_of_text(self.file_suffixes, "file_suffixes")
        _require_tuple_of_text(self.launch_flags, "launch_flags")
        _require_tuple_of_text(self.configuration_names, "configuration_names")
        if not self.language_ids:
            raise ProfileError("language_ids must not be empty")
        if not self.file_suffixes:
            raise ProfileError("file_suffixes must not be empty")

    def _check_artifact(self) -> None:
        _require_text(self.version, "version")
        _require_text(self.package_url, "package_url")
        _require_integrity(self.package_integrity)
        _require_relative(self.server_relative, "server_relative")
        if self.owner_argument_relative is not None:
            _require_relative(self.owner_argument_relative, "owner_argument_relative")
        _require_relative(self.managed_relative_root, "managed_relative_root")
        if not self.package_url.startswith("https://"):
            raise ProfileError("package_url must be https")

    def _check_runtime(self) -> None:
        _require_node_major(self.node_major)
        _require_node_minor(self.node_minor_floor)
        _require_readiness(self.readiness)
        if not isinstance(self.server_notifications, frozenset):
            raise ProfileError("server_notifications must be a frozenset")
        _require_tuple_of_text(tuple(sorted(self.server_notifications)), "notification")

    def managed_root(self, state_root: Path) -> Path:
        """Derive this profile's managed artifact root without creating it."""
        if not isinstance(state_root, Path):
            raise ProfileError("state_root must be a Path")
        return state_root.resolve() / self.managed_relative_root

    def server_path(self, state_root: Path) -> Path:
        return self.managed_root(state_root) / self.server_relative

    def handles_suffix(self, suffix: str) -> bool:
        return _require_text(suffix, "suffix").casefold() in self.file_suffixes

    def degradation_code(self, reason: str) -> str:
        """Name a startup or capability failure in this profile's namespace."""
        return f"{self.degradation_prefix}_{_require_text(reason, 'reason')}"

    def launch_command(self, node: Path, server: Path, owner: Path) -> tuple[str, ...]:
        """Build the exact argv for this server under its owner scratch root."""
        _require_absolute_paths(node, server, owner)
        tail = self._owner_arguments(owner)
        return (str(node), str(server), *self.launch_flags, *tail)

    def _owner_arguments(self, owner: Path) -> tuple[str, ...]:
        """The owner-scoped argument, joined as a path rather than as text.

        The template used to carry its own `/cancellation` suffix, which on
        Windows produced `D:\\a\\...\\owner/cancellation` — a separator the
        session it replaced never emitted. Measured 2026-08-30: the
        `pyright-windows` job failed on exactly that argument while every POSIX
        job passed, because there the two spellings are the same string.
        """
        template = self.owner_argument_template
        if template is None:
            return ()
        target = owner if self.owner_argument_relative is None else (
            owner / self.owner_argument_relative
        )
        return (template.format(owner=target),)

    def wire_initialization_options(self, state_root: Path) -> dict:
        """Initialization options as sent, with any runtime path filled in."""
        options = thaw_profile_value(self.initialization_options)
        if not isinstance(options, dict):
            raise ProfileError("initialization_options must be an object")
        if self.runtime_option is None:
            return options
        return self.runtime_option.applied(options, self.managed_root(state_root))

    def wire_configuration(self) -> object:
        """Configuration as answered to `workspace/configuration`."""
        return thaw_profile_value(self.configuration)

    def gates_on_progress(self) -> bool:
        """Whether a query must wait for a work-done-progress `end`."""
        return self.readiness == READINESS_WORK_DONE_PROGRESS


def _require_absolute_paths(node: Path, server: Path, owner: Path) -> None:
    for label, value in (("node", node), ("server", server), ("owner", owner)):
        if not isinstance(value, Path):
            raise ProfileError(f"{label} must be a Path")
        if not value.is_absolute():
            raise ProfileError(f"{label} must be an absolute path")


class ProfileRegistry:
    """The set of language servers this build knows how to manage."""

    def __init__(self, profiles: tuple[LanguageServerProfile, ...]) -> None:
        self._by_name = {profile.name: profile for profile in profiles}
        if len(self._by_name) != len(profiles):
            raise ProfileError("profile names must be unique")
        self._by_suffix = _suffix_index(profiles)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_name))

    def get(self, name: str) -> LanguageServerProfile:
        profile = self._by_name.get(_require_text(name, "name"))
        if profile is None:
            raise ProfileError(f"no managed language server profile named {name!r}")
        return profile

    def for_suffix(self, suffix: str) -> LanguageServerProfile | None:
        """The profile that owns this file suffix, or None to fall back.

        None is the structural-evidence path, not an error: a repository full of
        languages we do not have a precise tier for is the normal case.
        """
        return self._by_suffix.get(_require_text(suffix, "suffix").casefold())

    def for_path(self, path: Path) -> LanguageServerProfile | None:
        if not isinstance(path, Path):
            raise ProfileError("path must be a Path")
        if not path.suffix:
            return None
        return self.for_suffix(path.suffix)


def _suffix_index(
    profiles: tuple[LanguageServerProfile, ...],
) -> dict[str, LanguageServerProfile]:
    index: dict[str, LanguageServerProfile] = {}
    for profile in profiles:
        _index_one(index, profile)
    return index


def _index_one(
    index: dict[str, LanguageServerProfile], profile: LanguageServerProfile
) -> None:
    for suffix in profile.file_suffixes:
        _claim_suffix(index, suffix, profile)


def _claim_suffix(
    index: dict[str, LanguageServerProfile],
    suffix: str,
    profile: LanguageServerProfile,
) -> None:
    folded = suffix.casefold()
    owner = index.get(folded)
    if owner is not None:
        raise ProfileError(
            f"suffix {folded!r} is claimed by both {owner.name!r} and {profile.name!r}"
        )
    index[folded] = profile


def is_sha256(value: object) -> bool:
    """Whether a value is a lowercase hexadecimal SHA-256 digest."""
    if not isinstance(value, str) or len(value) != _SHA256_LENGTH:
        return False
    return set(value).issubset(_HEX_DIGITS)
