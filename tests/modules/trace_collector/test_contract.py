"""Convert a newly recorded trusted profile through the real CLI and reader."""
import cProfile
import json
import subprocess
import sys
from pathlib import Path

import pytest
import trace_collect

from tests.slow_machine import LONG_TIMEOUT

ROOT = Path(__file__).resolve().parents[3]
SOURCE = '''def identity(fn):
    return fn

@identity
def leaf():
    return 1

def caller():
    return leaf() + leaf() + outside()

caller()
'''


def _profile(tmp_path):
    root = tmp_path / 'repository'
    root.mkdir()
    source = root / 'app.py'
    source.write_text(SOURCE, encoding='utf-8')
    outside = tmp_path / 'outside.py'
    outside.write_text('def outside():\n    return 2\n', encoding='utf-8')
    external = {}
    exec(compile(outside.read_text(), str(outside), 'exec'), external, external)
    namespace = {'outside': external['outside']}
    profiler = cProfile.Profile()
    profiler.runctx(compile(SOURCE, str(source), 'exec'), namespace, namespace)
    profile = tmp_path / 'own.prof'
    profiler.dump_stats(str(profile))
    return root, profile


def _assert_edges(rows):
    assert {(r['caller']['name'], r['callee']['name'], r['count']) for r in rows} == {
        ('<module>', 'identity', 1), ('<module>', 'caller', 1), ('caller', 'leaf', 2),
    }
    assert {r[side]['path'] for r in rows for side in ('caller', 'callee')} == {'app.py'}
    _assert_decorated_identity(rows)


def _assert_decorated_identity(rows):
    leaf_edge = next(r for r in rows if r['callee']['name'] == 'leaf')
    assert leaf_edge['callee']['line'] == 4  # First decorator, not def on line 5.
    assert leaf_edge['caller']['line'] == 8


def test_real_profile_preserves_count_and_frame_identity_and_excludes_external_code(tmp_path):
    root, profile = _profile(tmp_path)
    _assert_edges(trace_collect.edges_from_profile(profile, root))


def test_cli_writes_the_existing_header_and_complete_edge_records(tmp_path):
    root, profile = _profile(tmp_path)
    destination = tmp_path / 'trace.jsonl'
    result = subprocess.run(
        [sys.executable, str(ROOT / 'scripts/trace_collect.py'), str(profile),
         str(destination), '--root', str(root)],
        capture_output=True, text=True, timeout=LONG_TIMEOUT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    data = destination.read_bytes()
    records = [json.loads(line) for line in data.splitlines()]
    assert records[0] == {
        'schema': 'execution-trace/v1', 'collector': 'trace_collect/cProfile',
        'profile_name': 'own.prof', 'repository_root': str(root.resolve()), 'edge_count': 3,
    }
    _assert_edges(records[1:])
    assert data.endswith(b'\n')
    assert result.stdout.strip() == f'wrote 3 edge(s) to {destination}'


def test_missing_trusted_profile_is_not_a_successful_empty_trace(tmp_path):
    destination = tmp_path / 'trace.jsonl'
    with pytest.raises(FileNotFoundError):
        trace_collect.main([str(tmp_path / 'absent.prof'), str(destination), '--root', str(tmp_path)])
    assert not destination.exists()
