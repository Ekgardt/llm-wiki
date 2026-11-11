"""Installation, project metadata and health agree on the supported interpreter."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

from tests.shell_function_source import _shell_function
from tests.slow_machine import LONG_TIMEOUT

ROOT = Path(__file__).resolve().parents[1]


def test_project_pin_and_native_matrix_use_the_approved_series():
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
    workflow = yaml.safe_load((ROOT / '.github/workflows/tests.yml').read_text(encoding='utf-8'))
    assert project['project']['requires-python'] == '>=3.14,<3.15'
    assert (ROOT / '.python-version').read_text(encoding='utf-8').strip() == '3.14'
    matrix = workflow['jobs']['pytest-full']['strategy']['matrix']
    assert {entry['python'] for entry in matrix['include']} == {'3.14'}
    assert {entry['os'] for entry in matrix['include']} == {'ubuntu-24.04', 'windows-2025', 'macos-15'}
    assert matrix['shard'] == [1, 2, 3, 4, 5]


@pytest.mark.parametrize(('version', 'expected'), [
    ((3, 10, 22), 'error'), ((3, 13, 15), 'error'),
    ((3, 14, 0), 'ok'), ((3, 14, 7), 'ok'), ((3, 15, 0), 'error'),
])
def test_health_refuses_interpreters_outside_the_supported_series(tmp_path, monkeypatch, version, expected):
    from llm_wiki import doctor_base

    monkeypatch.setattr(sys, 'version_info', version)
    result = doctor_base.environment_check(tmp_path, tmp_path)
    assert result['details']['python']['status'] == expected


def _selection_block():
    source = (ROOT / 'install.sh').read_text(encoding='utf-8')
    return _shell_function(source, 'select_bootstrap_python') + '\nselect_bootstrap_python\n'


@pytest.mark.skipif(os.name == 'nt' or shutil.which('bash') is None, reason='Native POSIX shell')
@pytest.mark.parametrize('available', [True, False])
def test_bootstrap_uses_selected_python_or_explains_installation(tmp_path, available):
    uv = tmp_path / 'uv'
    uv.write_text('#!/bin/sh\n[ "$*" = "python find 3.14" ] || exit 9\n'
                  'test "$TEST_PYTHON_AVAILABLE" = yes || exit 1\nprintf "%s\\n" "$TEST_PYTHON"\n', encoding='utf-8')
    uv.chmod(0o700)
    environment = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ['PATH'],
                       TEST_PYTHON=sys.executable, TEST_PYTHON_AVAILABLE={True: 'yes', False: 'no'}[available])
    prelude = 'fail() { printf "%s\\n" "$1"; exit 1; }; ok() { printf "%s\\n" "$1"; };\n'
    result = subprocess.run(['bash', '-c', prelude + _selection_block()], env=environment,
                            capture_output=True, text=True, timeout=LONG_TIMEOUT)
    assert result.returncode == {True: 0, False: 1}[available]
    expected = {True: 'Python 3.14', False: 'uv python install 3.14'}[available]
    assert expected in result.stdout


_PRODUCT_DIRECTORIES = ('scripts', 'benchmark')
_RETIRED_FOR_THE_ONE_SERIES = {
    'a shim for fromisoformat before 3.11': r"""\.replace\(\s*["']Z["']\s*,\s*["']\+00:00["']""",
    'a claim that 3.10 is still supported': r'3\.10\b[^\n]{0,60}\b(?:supported|lowest|minimum)\b',
    'contention read from the message text': r"""["'](?:database is )?(?:locked|busy)["']\s+(?:not\s+)?in\s+(?:str\(|\w*(?:error|exc|message|text))""",
}


def _product_sources():
    for directory in _PRODUCT_DIRECTORIES:
        yield from (ROOT / directory).rglob('*.py')


@pytest.mark.parametrize('what', sorted(_RETIRED_FOR_THE_ONE_SERIES))
def test_the_product_carries_nothing_retired_with_the_older_pythons(what):
    pattern = re.compile(_RETIRED_FOR_THE_ONE_SERIES[what])
    found = [str(path.relative_to(ROOT)) for path in _product_sources() if pattern.search(path.read_text(encoding='utf-8'))]
    assert found == []
