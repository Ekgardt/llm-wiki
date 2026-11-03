"""Actual process transport and temporary build-cache retirement."""
import json
import subprocess
import sys

import go_source_build as build
import pytest

from tests.slow_machine import LONG_TIMEOUT


def test_child_receives_staging_cwd_and_explicit_toolchain_policy(tmp_path):
    environment = build.build_environment(tmp_path)
    program = 'import json,os; print(json.dumps([os.getcwd(),os.environ["GOTOOLCHAIN"],os.environ["GOBIN"]]))'
    result = build._run([sys.executable, '-c', program], environment, tmp_path, LONG_TIMEOUT)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [str(tmp_path), 'local', str(tmp_path / 'bin')]


def test_a_missing_executable_is_a_named_build_failure(tmp_path):
    with pytest.raises(build.SourceBuildError, match='pinned toolchain could not run'):
        build._run([str(tmp_path / 'missing-toolchain')], build.build_environment(tmp_path),
                   tmp_path, LONG_TIMEOUT)


def test_timeout_conversion_retains_the_original_cause(tmp_path, monkeypatch):
    failure = subprocess.TimeoutExpired(['toolchain'], 1)

    def expired(*args, **kwargs):
        raise failure

    monkeypatch.setattr(build.subprocess, 'run', expired)
    with pytest.raises(build.SourceBuildError, match='exceeded its deadline') as caught:
        build._run(['toolchain'], {}, tmp_path, 1)
    assert caught.value.__cause__ is failure


def _read_only_cache(root):
    folder = root / 'nested'
    folder.mkdir(parents=True)
    item = folder / 'artifact'
    item.write_bytes(b'build-only')
    item.chmod(0o400)
    folder.chmod(0o500)
    root.chmod(0o500)


def test_cleanup_removes_read_only_build_caches_and_retains_the_binary(tmp_path):
    for name in build.BUILD_ONLY_DIRECTORIES:
        _read_only_cache(tmp_path / name)
    binary = tmp_path / 'bin' / 'gopls'
    binary.parent.mkdir()
    binary.write_bytes(b'keep-the-built-binary')
    build.prune_build_caches(tmp_path)
    assert not any((tmp_path / name).exists() for name in build.BUILD_ONLY_DIRECTORIES)
    assert binary.read_bytes() == b'keep-the-built-binary'
