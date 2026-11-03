"""The unchanged registry data, readiness and route contracts."""
from __future__ import annotations

from pathlib import Path

import lsp_profiles
import pytest
from lsp_server_profile import (
    READINESS_INITIALIZED,
    READINESS_WORK_DONE_PROGRESS,
    LanguageServerProfile,
    ProfileError,
    ProfileRegistry,
    freeze_profile_value,
)

from tests.modules.lsp_server_profile.paths import STATE_ROOT, _abs


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


def test_the_pyright_managed_root_matches_lsp_paths():
    import lsp_paths

    assert lsp_profiles.PYRIGHT_PROFILE.managed_root(STATE_ROOT) == (
        lsp_paths.managed_pyright_root(STATE_ROOT)
    )


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
    "path", [Path("lib.rb"), Path("README.md"), Path("Makefile")]
)
def test_an_unmanaged_language_falls_back_rather_than_failing(path):
    """None is the structural-evidence path, not an error."""
    assert lsp_profiles.profile_for_path(path) is None


def test_asking_for_a_server_we_do_not_manage_is_an_error_not_a_guess():
    with pytest.raises(ProfileError):
        lsp_profiles.REGISTRY.get("clangd")


def test_the_registry_names_what_it_manages():
    assert lsp_profiles.REGISTRY.names() == (
        "gopls",
        "pyright",
        "rust-analyzer",
        "typescript",
    )


def test_a_native_server_is_its_own_interpreter():
    """gopls is an executable, not a Node program (#24, B).

    Research: docs/research/2026-09-12-installing-go-and-building-gopls.md.
    """
    profile = lsp_profiles.REGISTRY.get("gopls")
    command = profile.launch_command(
        _abs("unused", "node"), _abs("managed", "gopls"), _abs("owner")
    )

    assert (command, profile.node_major) == ((str(_abs("managed", "gopls")),), None)
    assert lsp_profiles.profile_for_path(Path("main.go")).name == "gopls"


def test_every_pinned_platform_archive_is_named_by_digest_and_size():
    artifacts = lsp_profiles.GOPLS_PROFILE.platform_artifacts
    systems = sorted({artifact.system for artifact in artifacts})

    assert (systems, len(artifacts)) == (["darwin", "linux", "windows"], 5)
    assert all(
        artifact.integrity.startswith("sha256-") and artifact.size > 0
        for artifact in artifacts
    )


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


def test_the_wire_options_are_a_fresh_copy_each_time():
    """A caller must not be able to poison the next session's options."""
    profile = lsp_profiles.TYPESCRIPT_PROFILE
    first = profile.wire_initialization_options(STATE_ROOT)
    first["tsserver"]["path"] = "/tmp/attacker/tsserver.js"
    second = profile.wire_initialization_options(STATE_ROOT)
    assert second["tsserver"]["path"] != "/tmp/attacker/tsserver.js"
