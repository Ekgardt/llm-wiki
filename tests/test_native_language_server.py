"""A managed server that is a native executable, built rather than unpacked.

gopls is published only as a Go module, so its install unpacks a pinned Go
toolchain and compiles one pinned module with it, and its launch executes the
verified copy itself instead of handing it to Node. Research:
`docs/research/2026-09-12-installing-go-and-building-gopls.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.modules.go_source_build.fixtures import _build

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import lsp_profiles  # noqa: E402
from lsp_process import GenerationLaunch, _require_same_program  # noqa: E402
from lsp_server_profile import ProfileError, normalized_platform  # noqa: E402


def test_a_source_build_refuses_an_unusable_declaration():
    """`Path("/x")` is absolute on POSIX and drive-relative on Windows, so the
    escaping path is written with `..`, which is refused on both."""
    with pytest.raises(ProfileError):
        _build(timeout_seconds=0)
    with pytest.raises(ProfileError):
        _build(binary_relative=Path("../escape/gopls"))


def test_the_launch_may_verify_the_program_but_never_substitute_one(tmp_path):
    """The owner root holds the verified copy; anything else is a substitution."""
    owner = tmp_path / "owner"
    owner.mkdir()
    verified = owner / "gopls-copy"
    verified.write_bytes(b"")
    configured = ("/managed/bin/gopls",)

    _require_same_program((str(verified),), configured, GenerationLaunch((str(verified),)), owner)
    with pytest.raises(ValueError, match="cannot replace the configured executable"):
        _require_same_program(
            ("/usr/bin/gopls",), configured, GenerationLaunch(("/usr/bin/gopls",)), owner
        )


def test_an_inherited_descriptor_is_also_the_same_program(tmp_path):
    configured = ("/managed/bin/pyright",)
    launch = GenerationLaunch(("/proc/self/fd/7",), (7,))

    _require_same_program(("/proc/self/fd/7",), configured, launch, tmp_path)
    with pytest.raises(ValueError):
        _require_same_program(
            ("/proc/self/fd/9",), configured, GenerationLaunch(("/proc/self/fd/9",)), tmp_path
        )


def test_a_rust_installer_bundle_lands_under_one_prefix():
    """`<archive>/<component>/bin/x` becomes `toolchain/bin/x` (#24, Rust).

    Research: docs/research/2026-09-12-installing-rust-for-precise-navigation.md.
    """
    from install_language_server import _Placement

    placement = _Placement(strip=2, prefix=Path("toolchain"))
    member = Path("rust-analyzer-1.98.1-x86_64-unknown-linux-gnu/rust-analyzer-preview/bin/ra")

    assert placement.target(member) == Path("toolchain/bin/ra")
    assert placement.target(Path("root/component")) is None


def test_the_npm_shape_still_reroots_its_package_directory():
    from install_language_server import _Placement

    placement = _Placement(subdirectory="typescript")

    assert placement.target(Path("package/lib/tsserver.js")) == Path(
        "typescript/lib/tsserver.js"
    )
    assert placement.target(Path("elsewhere/lib.js")) is None


def test_the_archive_mode_follows_the_pinned_url():
    from install_language_server import _archive_mode

    assert _archive_mode("https://example.invalid/a.tar.xz") == "r:xz"
    assert _archive_mode("https://example.invalid/a.tgz") == "r:gz"


def _component_platforms(components) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for component in components:
        pairs.update((item.system, item.machine) for item in component.artifacts)
    return pairs


def test_every_rust_component_is_pinned_for_every_platform_we_support():
    profile = lsp_profiles.REGISTRY.get("rust-analyzer")
    names = [component.name for component in profile.components]

    assert names == ["rustc", "rust-std", "cargo", "rust-src"]
    assert len(_component_platforms(profile.components)) == 5


def test_a_component_refuses_an_empty_or_foreign_artifact_table():
    from lsp_server_profile import ServerComponent

    with pytest.raises(ProfileError):
        ServerComponent(name="rustc", prefix=Path("toolchain"), artifacts=())
    with pytest.raises(ProfileError):
        ServerComponent(
            name="rustc", prefix=Path("toolchain"), artifacts=("not an artifact",)
        )


def test_the_rust_profile_names_the_library_path_its_copy_needs():
    """The verified copy runs outside the toolchain, so the loader needs telling."""
    profile = lsp_profiles.REGISTRY.get("rust-analyzer")
    environment = profile.launch_environment(Path("/state"))

    assert environment["LD_LIBRARY_PATH"].endswith("/toolchain/lib")
    assert environment["PATH"].endswith("/toolchain/bin")


def test_one_spelling_of_a_platform():
    assert normalized_platform("Linux", "x86_64") == normalized_platform("linux", "AMD64")
    assert normalized_platform("Darwin", "arm64") == normalized_platform("darwin", "aarch64")
