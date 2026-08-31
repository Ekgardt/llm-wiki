"""The language-neutral seam, and that Pyright through it is the same Pyright.

The point of these tests is not that the seam works -- it is that expressing the
existing Pyright path through the seam changes nothing. Every assertion about
Pyright below is written against the constants and construction that governed it
before this module existed, so the equivalence cannot be satisfied by editing
the seam to agree with itself.
"""

from __future__ import annotations

import json
from pathlib import Path

import lsp_profiles
import lsp_protocol
import pyright_profile
import pyright_session
import pytest
from lsp_server_profile import (
    READINESS_INITIALIZED,
    READINESS_WORK_DONE_PROGRESS,
    IdentityNotification,
    LanguageServerProfile,
    ProfileError,
    ProfileRegistry,
    RuntimeOption,
    freeze_profile_value,
    thaw_profile_value,
)

# `Path("/srv/vault").is_absolute()` is False on Windows — a path needs a drive
# there — so every root handed to a profile has to be anchored to one. Eight
# Windows jobs failed on 2026-08-30 with "node must be an absolute path" and a
# comparison of "D:\\srv\\vault…" against "\\srv\\vault…".
ANCHOR = Path(Path.cwd().anchor)


def _abs(*parts: str) -> Path:
    """An absolute path on every platform these tests run on."""
    return ANCHOR.joinpath(*parts)


STATE_ROOT = _abs("srv", "vault")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


# --------------------------------------------------------------------------
# Pyright through the seam is byte-for-byte the Pyright that already shipped.
# --------------------------------------------------------------------------


def test_the_pyright_launch_command_matches_the_one_the_session_already_builds():
    """`_start_configured_process` builds this argv; the profile must reproduce it."""
    node = _abs("usr", "bin", "node")
    server = STATE_ROOT / "cache/code-tools/pyright/1.1.411/package/langserver.index.js"
    owner = STATE_ROOT / "run/lsp" / ("a" * 32)

    # The literal construction from scripts/pyright_session.py, kept verbatim so
    # that a change on either side shows up here as a difference.
    expected = (
        str(node),
        str(server),
        "--stdio",
        f"--cancellationReceive=file:{owner / 'cancellation'}",
    )

    assert lsp_profiles.PYRIGHT_PROFILE.launch_command(node, server, owner) == expected


def test_the_pyright_configuration_sent_over_the_wire_is_unchanged():
    profile = lsp_profiles.PYRIGHT_PROFILE
    already_shipped = pyright_profile.thaw_pyright_profile_value(
        pyright_profile.PYRIGHT_CONFIGURATION
    )
    assert _canonical(profile.wire_configuration()) == _canonical(already_shipped)


def test_the_pyright_initialization_options_are_unchanged_and_take_no_runtime_path():
    profile = lsp_profiles.PYRIGHT_PROFILE
    already_shipped = pyright_profile.thaw_pyright_profile_value(
        pyright_profile.PYRIGHT_INITIALIZATION_OPTIONS
    )
    assert profile.runtime_option is None
    assert _canonical(profile.wire_initialization_options(STATE_ROOT)) == _canonical(
        already_shipped
    )


def test_the_pyright_profile_pins_the_artifact_the_installer_already_pins():
    profile = lsp_profiles.PYRIGHT_PROFILE
    assert profile.package_url == pyright_profile.PYRIGHT_PACKAGE_URL
    assert profile.package_integrity == pyright_profile.PYRIGHT_PACKAGE_INTEGRITY
    assert profile.server_relative == pyright_profile.PYRIGHT_SERVER_RELATIVE
    assert profile.node_major == pyright_profile.QUALIFIED_NODE_MAJOR


def test_the_pyright_managed_root_matches_lsp_paths():
    import lsp_paths

    assert lsp_profiles.PYRIGHT_PROFILE.managed_root(STATE_ROOT) == (
        lsp_paths.managed_pyright_root(STATE_ROOT)
    )


def test_the_pyright_vendor_notifications_are_the_ones_the_protocol_allows():
    """These three names are hardcoded in lsp_protocol today; the profile owns them."""
    hardcoded = {
        name
        for name in lsp_protocol.SERVER_NOTIFICATIONS
        if name.startswith("pyright/")
    }
    assert lsp_profiles.PYRIGHT_PROFILE.server_notifications == hardcoded


def test_pyright_degradation_codes_keep_their_existing_names():
    profile = lsp_profiles.PYRIGHT_PROFILE
    assert profile.degradation_code("startup_timeout") == "pyright_startup_timeout"
    assert profile.degradation_code("startup_failed") == "pyright_startup_failed"
    assert profile.degradation_code("executable_digest_mismatch") == (
        "pyright_executable_digest_mismatch"
    )


def test_pyright_readiness_does_not_wait_on_progress():
    """Pyright is ready when initialize says so; nothing about that changes."""
    assert lsp_profiles.PYRIGHT_PROFILE.readiness == READINESS_INITIALIZED
    assert lsp_profiles.PYRIGHT_PROFILE.gates_on_progress() is False


def test_the_existing_client_already_advertises_work_done_progress():
    """The readiness gate a second server needs is reachable with today's client.

    If this ever goes false, the TypeScript profile's readiness policy becomes
    unreachable and it will answer before its project loads.
    """
    assert pyright_session._CLIENT_CAPABILITIES["window"]["workDoneProgress"] is True


def test_the_protocol_already_accepts_the_progress_create_request():
    assert "window/workDoneProgress/create" in lsp_protocol.SERVER_REQUESTS
    assert "$/progress" in lsp_protocol.SERVER_NOTIFICATIONS


# --------------------------------------------------------------------------
# TypeScript: the three fields that exist because a second language forced them.
# --------------------------------------------------------------------------


def test_typescript_is_handed_the_tsserver_we_pinned_not_one_it_finds():
    profile = lsp_profiles.TYPESCRIPT_PROFILE
    options = profile.wire_initialization_options(STATE_ROOT)
    expected = str(
        STATE_ROOT
        / "cache/code-tools/typescript-language-server/6.0.0"
        / lsp_profiles.TSSERVER_RELATIVE
    )
    assert options["tsserver"]["path"] == expected
    assert Path(options["tsserver"]["path"]).is_absolute()


def test_the_pinned_tsserver_is_a_typescript_that_still_ships_one():
    """TypeScript 7 has no lib/tsserver.js; pinning `latest` would not start."""
    assert lsp_profiles.TSSERVER_VERSION.startswith("5.")
    assert lsp_profiles.TSSERVER_RELATIVE == Path("typescript/lib/tsserver.js")


def test_typescript_waits_for_the_project_before_answering():
    profile = lsp_profiles.TYPESCRIPT_PROFILE
    assert profile.readiness == READINESS_WORK_DONE_PROGRESS
    assert profile.gates_on_progress() is True


def test_typescript_confirms_after_initialize_that_it_loaded_our_engine():
    notification = lsp_profiles.TYPESCRIPT_PROFILE.identity_notification
    assert notification.confirmed({"version": "5.9.3", "source": "user-setting"}) == (
        "5.9.3",
        True,
    )


@pytest.mark.parametrize("source", ["workspace", "bundled", "user-setting-but-not"])
def test_a_typescript_the_server_found_for_itself_is_not_confirmed(source):
    """Anything but `user-setting` means it is not the engine we pinned."""
    notification = lsp_profiles.TYPESCRIPT_PROFILE.identity_notification
    _version, confirmed = notification.confirmed({"version": "5.9.3", "source": source})
    assert confirmed is False


@pytest.mark.parametrize("params", [None, {"version": 5}, {"source": "user-setting"}])
def test_an_unreadable_identity_notification_confirms_nothing(params):
    notification = lsp_profiles.TYPESCRIPT_PROFILE.identity_notification
    assert notification.confirmed(params) == (None, False)


def test_typescript_carries_a_node_minor_floor_that_pyright_never_needed():
    assert lsp_profiles.TYPESCRIPT_PROFILE.node_minor_floor == 22
    assert lsp_profiles.PYRIGHT_PROFILE.node_minor_floor == 0


def test_typescript_takes_no_owner_argument():
    node = _abs("usr", "bin", "node")
    server = STATE_ROOT / "cache/x/package/lib/cli.mjs"
    owner = STATE_ROOT / "run/lsp" / ("b" * 32)
    assert lsp_profiles.TYPESCRIPT_PROFILE.launch_command(node, server, owner) == (
        str(node),
        str(server),
        "--stdio",
    )


def test_typescript_declares_only_its_own_vendor_notification():
    assert lsp_profiles.TYPESCRIPT_PROFILE.server_notifications == frozenset(
        {"$/typescriptVersion"}
    )


# --------------------------------------------------------------------------
# The registry routes by suffix, and refuses to guess.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected",
    [
        (Path("scripts/doctor.py"), "pyright"),
        (Path("a/b/c.pyi"), "pyright"),
        (Path("src/main.ts"), "typescript"),
        (Path("src/App.TSX"), "typescript"),
        (Path("bundle.mjs"), "typescript"),
    ],
)
def test_the_registry_routes_a_file_to_its_server(path, expected):
    profile = lsp_profiles.profile_for_path(path)
    assert profile is not None
    assert profile.name == expected


@pytest.mark.parametrize(
    "path", [Path("main.go"), Path("lib.rs"), Path("README.md"), Path("Makefile")]
)
def test_an_unmanaged_language_falls_back_rather_than_failing(path):
    """None is the structural-evidence path, not an error."""
    assert lsp_profiles.profile_for_path(path) is None


def test_asking_for_a_server_we_do_not_manage_is_an_error_not_a_guess():
    with pytest.raises(ProfileError):
        lsp_profiles.profile_named("gopls")


def test_the_registry_names_what_it_manages():
    assert lsp_profiles.REGISTRY.names() == ("pyright", "typescript")


def test_two_profiles_may_not_claim_the_same_suffix():
    first = lsp_profiles.PYRIGHT_PROFILE
    clash = LanguageServerProfile(
        name="other",
        language_ids=("python",),
        file_suffixes=(".py",),
        version="1",
        package_url="https://example.invalid/a.tgz",
        package_integrity="sha512-AAAA",
        server_relative=Path("package/main.js"),
        managed_relative_root=Path("cache/code-tools/other/1"),
        install_manifest_schema="other/v1",
        node_major=22,
        launch_flags=("--stdio",),
        server_notifications=frozenset(),
        configuration=freeze_profile_value({}),
        initialization_options=freeze_profile_value({}),
        readiness=READINESS_INITIALIZED,
        degradation_prefix="other",
    )
    with pytest.raises(ProfileError):
        ProfileRegistry((first, clash))


# --------------------------------------------------------------------------
# The seam refuses what it cannot honour.
# --------------------------------------------------------------------------


def _profile(**overrides) -> LanguageServerProfile:
    fields = {
        "name": "x",
        "language_ids": ("x",),
        "file_suffixes": (".x",),
        "version": "1",
        "package_url": "https://example.invalid/a.tgz",
        "package_integrity": "sha512-AAAA",
        "server_relative": Path("package/main.js"),
        "managed_relative_root": Path("cache/code-tools/x/1"),
        "install_manifest_schema": "x/v1",
        "node_major": 22,
        "launch_flags": ("--stdio",),
        "server_notifications": frozenset(),
        "configuration": freeze_profile_value({}),
        "initialization_options": freeze_profile_value({}),
        "readiness": READINESS_INITIALIZED,
        "degradation_prefix": "x",
    }
    fields.update(overrides)
    return LanguageServerProfile(**fields)


@pytest.mark.parametrize(
    "overrides",
    [
        {"package_url": "http://example.invalid/a.tgz"},
        {"package_integrity": "sha256-AAAA"},
        {"server_relative": _abs("absolute", "main.js")},
        {"server_relative": Path("../escape/main.js")},
        {"managed_relative_root": Path("../../etc")},
        {"readiness": "whenever"},
        {"node_major": 0},
        {"node_minor_floor": -1},
        {"file_suffixes": ()},
        {"language_ids": ()},
    ],
)
def test_an_unhonourable_profile_is_refused_at_construction(overrides):
    with pytest.raises(ProfileError):
        _profile(**overrides)


def test_a_relative_launch_path_is_refused():
    with pytest.raises(ProfileError):
        _profile().launch_command(Path("node"), _abs("a", "b.js"), _abs("c"))


def test_a_runtime_option_will_not_overwrite_a_non_object():
    option = RuntimeOption(key_path=("a", "b"), sibling_relative=Path("t.js"))
    with pytest.raises(ProfileError):
        option.applied({"a": "already a string"}, STATE_ROOT / "managed")


def test_a_runtime_option_creates_the_branch_it_needs():
    option = RuntimeOption(key_path=("a", "b", "c"), sibling_relative=Path("t.js"))
    managed = _abs("srv", "managed")
    result = option.applied({}, managed)
    assert result == {"a": {"b": {"c": str(managed / "t.js")}}}


def test_a_runtime_option_needs_an_absolute_managed_root():
    option = RuntimeOption(key_path=("a",), sibling_relative=Path("t.js"))
    with pytest.raises(ProfileError):
        option.applied({}, Path("relative/managed"))


def test_an_identity_notification_needs_every_field_named():
    with pytest.raises(ProfileError):
        IdentityNotification(
            method="", version_key="v", source_key="s", required_source="u"
        )


# --------------------------------------------------------------------------
# Freezing is what stops a caller mutating another session's profile.
# --------------------------------------------------------------------------


def test_a_frozen_profile_value_cannot_be_mutated():
    frozen = freeze_profile_value({"a": {"b": [1, 2]}})
    with pytest.raises(TypeError):
        frozen["a"] = 1
    assert frozen["a"]["b"] == (1, 2)


def test_thawing_returns_a_plain_mutable_copy():
    frozen = freeze_profile_value({"a": {"b": [1, 2]}})
    thawed = thaw_profile_value(frozen)
    thawed["a"]["b"].append(3)
    assert frozen["a"]["b"] == (1, 2)


def test_an_unsupported_profile_value_is_refused():
    with pytest.raises(ProfileError):
        freeze_profile_value({"a": object()})


def test_the_wire_options_are_a_fresh_copy_each_time():
    """A caller must not be able to poison the next session's options."""
    profile = lsp_profiles.TYPESCRIPT_PROFILE
    first = profile.wire_initialization_options(STATE_ROOT)
    first["tsserver"]["path"] = "/tmp/attacker/tsserver.js"
    second = profile.wire_initialization_options(STATE_ROOT)
    assert second["tsserver"]["path"] != "/tmp/attacker/tsserver.js"
