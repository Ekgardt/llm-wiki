"""Real source trees and the public CLI, without executing discovered writers."""
import ast
import os
import shutil
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


def _defined_function_names():
    trees = (ast.parse(path.read_text(encoding='utf-8')) for path in (ROOT / 'scripts').rglob('*.py'))
    return {node.name for tree in trees for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}


def _standard_operation(name):
    return any(hasattr(owner, name) for owner in (Path, os, shutil))


def test_every_watched_writer_name_is_a_function_the_product_defines():
    """A renamed writer must not leave the scanner watching a name nothing calls."""
    watched = (scanner.DAILY_APPENDERS | scanner.APPLY_FUNCTIONS | scanner.APPLY_WRITES
               | scanner.ARCHIVE_PUBLISHERS)
    defined = _defined_function_names()
    assert {name for name in watched if not _standard_operation(name)} - defined == set()


def test_invalid_python_is_not_reported_as_an_empty_success(tmp_path):
    path = _write(tmp_path, 'scripts/broken.py', 'def broken(:\n')
    with pytest.raises(SyntaxError):
        scanner.scan_source(path, path.read_text(encoding='utf-8'))
    with pytest.raises(SyntaxError):
        scanner.discover_repository_writers(tmp_path)


def test_the_inventory_lists_the_transaction_apply_writes_where_they_are_defined():
    apply_file = ROOT / 'scripts' / 'llm_wiki' / scanner.TRANSACTION_APPLY_FILE
    tree = ast.parse(apply_file.read_text(encoding='utf-8'))
    defined = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    findings = scanner.scan_source(apply_file.relative_to(ROOT), apply_file.read_text(encoding='utf-8'))

    assert scanner.APPLY_FUNCTIONS <= defined
    assert {item.api for item in findings if item.approved} >= {'unlink', 'replace', 'delete_windows_handle'}
