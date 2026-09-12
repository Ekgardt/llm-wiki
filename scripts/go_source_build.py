"""Build one pinned Go module with one pinned toolchain, inside the install root.

gopls is published only as a Go module, so its install unpacks a toolchain and
then compiles the server with it. Everything the build reads or writes lives
under the staging directory the installer owns: `GOPATH`, `GOMODCACHE`,
`GOCACHE` and `GOBIN` all point inside it, so no user-level Go state is read,
changed, or even discovered, and removing `cache/` removes every byte of this.

`GOTOOLCHAIN=local` is what keeps the pin a pin: without it Go downloads and
runs whatever toolchain a module's `go` directive asks for, which would make
the installed compiler a suggestion. Module authenticity stays the checksum
database's job, so every module in the build graph is verified against a
transparency log.

Research: `docs/research/2026-09-12-installing-go-and-building-gopls.md`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from lsp_server_profile import SourceBuild

INHERITED_VARIABLES = ("PATH", "SystemRoot", "TEMP", "TMP", "USERPROFILE", "windir")


class SourceBuildError(RuntimeError):
    """The pinned module could not be built as pinned."""


def build_environment(staging: Path) -> dict[str, str]:
    """A build environment that reads and writes only inside `staging`."""
    root = staging.resolve()
    environment = {
        name: os.environ[name] for name in INHERITED_VARIABLES if name in os.environ
    }
    environment.update({
        "GOPATH": str(root / "gopath"),
        "GOMODCACHE": str(root / "gopath" / "pkg" / "mod"),
        "GOCACHE": str(root / "gocache"),
        "GOBIN": str(root / "bin"),
        "GOTOOLCHAIN": "local",
        "GOFLAGS": "-mod=mod",
        "HOME": str(root / "home"),
    })
    return environment


def _module_argument(build: SourceBuild) -> str:
    return f"{build.module}@{build.version}"


def _run(command: list[str], environment: dict[str, str], cwd: Path, timeout: float):
    try:
        return subprocess.run(
            command,
            env=environment,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise SourceBuildError("the build exceeded its deadline") from error
    except OSError as error:
        raise SourceBuildError(f"the pinned toolchain could not run: {error}") from error


def _require_success(completed, build: SourceBuild) -> None:
    if completed.returncode == 0:
        return
    tail = (completed.stderr or completed.stdout or "").strip().splitlines()[-3:]
    raise SourceBuildError(
        f"building {_module_argument(build)} failed: " + " | ".join(tail)
    )


def _require_binary(staging: Path, build: SourceBuild) -> Path:
    binary = staging / build.binary_relative
    if not binary.is_file():
        raise SourceBuildError(f"the build wrote no {build.binary_relative.as_posix()}")
    return binary


def _require_toolchain(staging: Path, build: SourceBuild) -> Path:
    """The compiler the archive was supposed to contain.

    Relative paths are reported as POSIX text so one message reads the same on
    every platform, the way `install_manifest` records `server_relative_path`.
    """
    toolchain = staging / build.toolchain_relative
    if not toolchain.is_file():
        raise SourceBuildError(
            f"the unpacked archive holds no {build.toolchain_relative.as_posix()}"
        )
    return toolchain


BUILD_ONLY_DIRECTORIES = ("gocache", "gopath")


def _unlock_tree(root: Path) -> None:
    """Make a Go module cache removable.

    Go writes both the files and their directories read-only, and a read-only
    directory refuses the unlink of what is inside it, so the permissions have
    to be cleared from the top down before anything is removed.
    """
    for parent, directories, files in os.walk(root):
        for name in (*directories, *files):
            _clear_mode(Path(parent) / name)
    _clear_mode(root)


def _clear_mode(path: Path) -> None:
    try:
        path.chmod(0o700)
    except OSError:
        pass


def prune_build_caches(staging: Path) -> None:
    """Drop what only the build needed.

    Measured 2026-09-12 on this machine: the install is 863 MB right after the
    build, of which the toolchain is 282 MB and the server 42 MB; the module
    cache (159 MB) and the build cache (383 MB) hold gopls's own dependencies,
    which nothing reads again. Both paths are recreated, empty, by the first
    query that needs them.
    """
    for name in BUILD_ONLY_DIRECTORIES:
        _unlock_tree(staging / name)
        shutil.rmtree(staging / name, ignore_errors=True)


def build_source_server(staging: Path, build: SourceBuild) -> Path:
    """Compile `build.module` into `staging`, returning the server it wrote."""
    toolchain = _require_toolchain(staging, build)
    environment = build_environment(staging)
    Path(environment["HOME"]).mkdir(parents=True, exist_ok=True)
    completed = _run(
        [str(toolchain), "install", _module_argument(build)],
        environment,
        staging,
        build.timeout_seconds,
    )
    _require_success(completed, build)
    binary = _require_binary(staging, build)
    prune_build_caches(staging)
    return binary
