"""The measured serialization includes selected exact source records."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from llm_wiki.context_bundle import unpack_files

from tests.module_context import _manifest_bundle, measure_context, relative_context_names
from tests.module_record_context import context_record_files
from tests.module_record_context_helpers import workspace as workspace
from tests.module_records_helpers import PARTS
from tests.module_records_helpers import catalog as catalog


def test_compact_packet_contains_selected_original_bytes(workspace):
    root, manifest = workspace
    manifest['serialization'] = 'exact-duplicates/v1'
    files = {root / 'docs/records.json'}
    entries = unpack_files(_manifest_bundle(root, files, manifest))
    assert entries == {
        'docs/records.json': (root / 'docs/records.json').read_bytes(),
        '@record/docs/history.md#second': PARTS[1],
    }


def test_raw_packet_contains_selected_original_bytes(workspace):
    root, manifest = workspace
    expected = f'FILE @record/docs/history.md#second {len(PARTS[1])} bytes\n'
    assert _manifest_bundle(root, set(), manifest) == expected + PARTS[1].decode() + '\n'


def _packet(root, manifest):
    needed = context_record_files(root, manifest) | {root / name for name in (
        'context.json', 'tests/module_context.py',
        'AGENTS.md', 'CLAUDE.md', 'rules/development-laws.md',
        'docs/AGENTS.md', 'docs/STRUCTURE.md',
        'scripts/llm_wiki/context_bundle.py', 'docs/modules/context-tooling.md',
    )}
    for path in needed:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text('')
    manifest.update(encoding='o200k_base', max_tokens=100000,
                    files=relative_context_names(root, needed))
    path = root / 'context.json'
    path.write_text(json.dumps(manifest))
    return path


@pytest.mark.parametrize('missing', [
    'docs/records.json', 'tests/module_records.py', 'tests/module_record_context.py',
    'tests/module_context_paths.py', 'docs/modules/context-records.md',
])
def test_catalog_and_validator_cannot_disappear_from_measured_context(workspace, missing):
    root, manifest = workspace
    path = _packet(root, manifest)
    manifest['files'].remove(missing)
    path.write_text(json.dumps(manifest))
    with pytest.raises(AssertionError, match='Missing required context'):
        measure_context(root, path)


def test_measurement_rejects_changed_unselected_source(workspace):
    root, manifest = workspace
    path = _packet(root, manifest)
    (root / 'docs/history.md').write_text('changed')
    with pytest.raises(ValueError, match='length changed'):
        measure_context(root, path)


def test_measurement_counts_the_actual_serialized_records(workspace):
    from tests.module_context import context_files, count_tokens

    root, manifest = workspace
    manifest['serialization'] = 'exact-duplicates/v1'
    path = _packet(root, manifest)
    expected = _manifest_bundle(root, context_files(root, manifest), manifest)
    count, files = measure_context(root, path)
    assert count == count_tokens(expected)
    assert files == len(manifest['files'])
    assert unpack_files(expected)['@record/docs/history.md#second'] == PARTS[1]


def test_whole_file_cannot_impersonate_a_record_entry(workspace):
    root, manifest = workspace
    path = root / '@record/docs/history.md#second'
    path.parent.mkdir(parents=True)
    path.write_bytes(PARTS[1])
    with pytest.raises(AssertionError, match='Reserved entry path'):
        _manifest_bundle(root, {path}, manifest)


_OFFLINE_COUNT = "from tests.module_context import count_tokens; print(count_tokens('hello world'))"
_DEAD_PROXY = 'http://127.0.0.1:9'


def _offline_environment(empty_cache):
    names = ('HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY', 'https_proxy', 'http_proxy', 'all_proxy')
    proxies = dict.fromkeys(names, _DEAD_PROXY)
    return {**os.environ, **proxies, 'TIKTOKEN_CACHE_DIR': str(empty_cache), 'PYTHONPATH': 'scripts'}


def test_token_counting_reads_the_vendored_vocabulary_without_a_network(tmp_path):
    checkout = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, '-c', _OFFLINE_COUNT], cwd=checkout, env=_offline_environment(tmp_path),
        capture_output=True, text=True, timeout=120,
    )
    assert (result.returncode, result.stdout.strip()) == (0, '2'), result.stderr[-2000:]


def test_token_counting_leaves_the_cache_setting_as_it_found_it(monkeypatch):
    from tests.module_context import _reading_vendored_encodings
    monkeypatch.setenv('TIKTOKEN_CACHE_DIR', 'chosen-by-the-operator')
    with _reading_vendored_encodings():
        assert os.environ['TIKTOKEN_CACHE_DIR'].endswith('tiktoken')
    assert os.environ['TIKTOKEN_CACHE_DIR'] == 'chosen-by-the-operator'
