"""Real source trees and the public CLI, without executing discovered writers."""
import subprocess
import sys
from pathlib import Path

import check_knowledge_writers as scanner
import pytest

from tests.slow_machine import LONG_TIMEOUT

ROOT = Path(__file__).resolve().parents[3]
DIRECT = 'Path("knowledge/notes/page.md").write_text("must never run")\n'
APPROVED = (
    'from markdown_transaction import mutate_knowledge\n'
    'def publish():\n'
    '    mutate_knowledge("event", {Path("knowledge/notes/page.md"): b"body"})\n'
)


def _write(root, name, source):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding='utf-8')
    return path


def _cli(root):
    return subprocess.run(
        [sys.executable, str(ROOT / 'scripts/check_knowledge_writers.py'), str(root)],
        cwd=root, capture_output=True, text=True, timeout=LONG_TIMEOUT,
    )


def test_discovery_covers_existing_roots_and_installers_without_executing_source(tmp_path):
    names = ['scripts/writer.py', 'integrations/nested/adapter.py', 'install_custom.py']
    for name in names:
        _write(tmp_path, name, DIRECT)
    _write(tmp_path, 'unrelated.py', DIRECT)
    _write(tmp_path, 'tests/test_example.py', DIRECT)
    findings = scanner.discover_repository_writers(tmp_path)
    assert {finding.path.as_posix() for finding in findings} == set(names)
    assert all(not finding.approved for finding in findings)
    assert not (tmp_path / 'knowledge').exists()


def test_cli_reports_unapproved_write_and_nonzero_status(tmp_path):
    _write(tmp_path, 'scripts/writer.py', DIRECT)
    result = _cli(tmp_path)
    assert result.returncode == 1, result.stderr
    assert result.stdout.strip() == 'scripts/writer.py:1: DIRECT COVERED WRITE: write_text'
    assert not (tmp_path / 'knowledge').exists()


def test_canonical_binding_has_the_existing_success_status_and_entrypoint(tmp_path):
    _write(tmp_path, 'scripts/writer.py', APPROVED)
    result = _cli(tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'scripts/writer.py:3: approved: mutate_knowledge'
    assert scanner.discover_repository_entrypoints(tmp_path, files={'writer.py'}) == {
        'scripts/writer.py:publish',
    }
    assert scanner.discover_repository_entrypoints(tmp_path, files={'other.py'}) == set()
    assert not (tmp_path / 'knowledge').exists()


def test_invalid_python_is_not_reported_as_an_empty_success(tmp_path):
    path = _write(tmp_path, 'scripts/broken.py', 'def broken(:\n')
    with pytest.raises(SyntaxError):
        scanner.scan_source(path, path.read_text(encoding='utf-8'))
    with pytest.raises(SyntaxError):
        scanner.discover_repository_writers(tmp_path)
