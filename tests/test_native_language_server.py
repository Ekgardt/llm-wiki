"""A managed server that is a native executable, built rather than unpacked.

gopls is published only as a Go module, so its install unpacks a pinned Go
toolchain and compiles one pinned module with it, and its launch executes the
verified copy itself instead of handing it to Node. Research:
`docs/research/2026-09-12-installing-go-and-building-gopls.md`.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import lsp_profiles  # noqa: E402
from lsp_process import GenerationLaunch, _require_same_program  # noqa: E402
from lsp_server_profile import ProfileError, SourceBuild, normalized_platform  # noqa: E402

FAKE_TOOLCHAIN = """#!/bin/sh
if [ "$1" != "install" ]; then
  echo "unexpected argument: $1" >&2
  exit 2
fi
[ -n "$GOBIN" ] || exit 3
[ "$GOTOOLCHAIN" = "local" ] || exit 4
mkdir -p "$GOBIN"
printf 'built %s\\n' "$2" > "$GOBIN/gopls"
chmod 0755 "$GOBIN/gopls"
"""


def _build(**overrides) -> SourceBuild:
    fields = {
        "module": "golang.org/x/tools/gopls",
        "version": "v0.23.0",
        "toolchain_relative": Path("go/bin/go"),
        "binary_relative": Path("bin/gopls"),
    }
    fields.update(overrides)
    return SourceBuild(**fields)


def _staged_toolchain(tmp_path: Path, body: str = FAKE_TOOLCHAIN) -> Path:
    toolchain = tmp_path / "go/bin/go"
    toolchain.parent.mkdir(parents=True)
    toolchain.write_text(body, encoding="utf-8")
    toolchain.chmod(toolchain.stat().st_mode | stat.S_IXUSR)
    return toolchain


@pytest.mark.skipif(os.name == "nt", reason="the fake toolchain is a POSIX shell script")
def test_the_build_runs_the_pinned_toolchain_inside_the_staging_directory(tmp_path):
    from go_source_build import build_source_server

    _staged_toolchain(tmp_path)

    binary = build_source_server(tmp_path, _build())

    assert binary == tmp_path / "bin/gopls"
    assert binary.read_text(encoding="utf-8") == "built golang.org/x/tools/gopls@v0.23.0\n"


PATH_VARIABLES = ("GOPATH", "GOMODCACHE", "GOCACHE", "GOBIN", "HOME")


def test_the_build_environment_reads_and_writes_only_the_staging_directory(tmp_path):
    from go_source_build import build_environment

    environment = build_environment(tmp_path)
    root = str(tmp_path.resolve())
    inside = [environment[name].startswith(root) for name in PATH_VARIABLES]

    assert (environment["GOTOOLCHAIN"], inside) == ("local", [True] * len(PATH_VARIABLES))
    assert "GOPROXY" not in environment


@pytest.mark.skipif(os.name == "nt", reason="the fake toolchain is a POSIX shell script")
def test_a_failed_build_names_the_module_it_could_not_build(tmp_path):
    from go_source_build import SourceBuildError, build_source_server

    _staged_toolchain(tmp_path, "#!/bin/sh\necho 'no network' >&2\nexit 1\n")

    with pytest.raises(SourceBuildError, match="golang.org/x/tools/gopls@v0.23.0"):
        build_source_server(tmp_path, _build())


def test_a_missing_toolchain_is_refused_before_anything_runs(tmp_path):
    from go_source_build import SourceBuildError, build_source_server

    with pytest.raises(SourceBuildError, match="go/bin/go"):
        build_source_server(tmp_path, _build())


@pytest.mark.skipif(os.name == "nt", reason="the fake toolchain is a POSIX shell script")
def test_a_build_that_writes_nothing_is_a_failure_not_an_install(tmp_path):
    from go_source_build import SourceBuildError, build_source_server

    _staged_toolchain(tmp_path, "#!/bin/sh\nexit 0\n")

    with pytest.raises(SourceBuildError, match="bin/gopls"):
        build_source_server(tmp_path, _build())


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
    profile = lsp_profiles.profile_named("rust-analyzer")
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
    profile = lsp_profiles.profile_named("rust-analyzer")
    environment = profile.launch_environment(Path("/state"))

    assert environment["LD_LIBRARY_PATH"].endswith("/toolchain/lib")
    assert environment["PATH"].endswith("/toolchain/bin")


def test_one_spelling_of_a_platform():
    assert normalized_platform("Linux", "x86_64") == normalized_platform("linux", "AMD64")
    assert normalized_platform("Darwin", "arm64") == normalized_platform("darwin", "aarch64")
