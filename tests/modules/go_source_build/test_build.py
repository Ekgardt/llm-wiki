"""Original staged-build tests and platform qualifiers, unchanged."""
import os
import stat
from pathlib import Path

import pytest

from tests.modules.go_source_build.fixtures import _build

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


PATH_VARIABLES = ("GOPATH", "GOMODCACHE", "GOCACHE", "GOBIN", "HOME")


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
