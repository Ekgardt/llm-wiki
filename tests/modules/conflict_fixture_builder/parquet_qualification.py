"""Explicit optional suite; real Arrow files and real CLI, without absent-package skips."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from benchmark import build_conflict_fixture as builder

ROOT = Path(__file__).resolve().parents[3]
ROW = {
    'metadata': {'qa_pair_ids': ['factconsolidation_sh_6k_0']},
    'context': '0. The capital of Test is Old.\n9. The capital of Test is Новый.\n3. Unknown frame.',
    'answers': [['Новый']],
}


def _write(path, rows):
    pq.write_table(pa.Table.from_pylist(rows), path)
    return path


def _args(source, output):
    return ['--parquet', str(source), '--out', str(output)]


def test_real_cli_binds_source_bytes_and_preserves_full_result(tmp_path):
    source = _write(tmp_path / 'данные.parquet', [ROW])
    output = tmp_path / 'результат.json'
    command = [sys.executable, str(ROOT / 'benchmark/build_conflict_fixture.py'), *_args(source, output)]
    result = subprocess.run(command, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert not result.stderr
    document = json.loads(output.read_text(encoding='utf-8'))
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    expected = {
        'schema_version': 'conflict-resolution/v1', 'row_id': 'factconsolidation_sh_6k',
        'source_url': 'https://huggingface.co/datasets/ai-hyz/MemoryAgentBench/resolve/main/data/Conflict_Resolution-00000-of-00001.parquet',
        'source_sha256': digest, 'numbered_lines': 3, 'parsed_facts': 2,
        'unparsed_lines': ['3. Unknown frame.'],
        'conflicts': [{'subject': 'Test', 'property': 'capital',
                       'observations': [{'serial': 0, 'value': 'Old'}, {'serial': 9, 'value': 'Новый'}],
                       'truth': 'Новый', 'gold_confirmed': True}],
    }
    assert document == expected
    assert output.read_text(encoding='utf-8') == json.dumps(expected, indent=2, ensure_ascii=False, sort_keys=True) + '\n'
    assert result.stdout == (
        f'row              factconsolidation_sh_6k\nsource sha256    {digest}\n'
        'numbered lines   3\nparsed facts     2\nunparsed lines   1\n'
        'conflicts        1\ngold-confirmed   1\n'
    )


def test_reader_selects_first_matching_row(tmp_path):
    other = {**ROW, 'metadata': {'qa_pair_ids': ['other']}}
    later = {**ROW, 'context': 'later matching row'}
    source = _write(tmp_path / 'rows.parquet', [other, ROW, later])
    assert builder.read_row(source, 'factconsolidation_sh_6k') == ROW


def test_absent_row_preserves_existing_output(tmp_path):
    source = _write(tmp_path / 'rows.parquet', [ROW])
    output = tmp_path / 'out.json'
    output.write_bytes(b'existing fixture')
    with pytest.raises(SystemExit, match='no row whose qa_pair_ids'):
        builder.main([*_args(source, output), '--row', 'missing'])
    assert output.read_bytes() == b'existing fixture'


def test_corrupt_parquet_preserves_existing_output(tmp_path):
    source = tmp_path / 'bad.parquet'
    source.write_bytes(b'not parquet')
    output = tmp_path / 'out.json'
    output.write_bytes(b'existing fixture')
    with pytest.raises(pa.ArrowInvalid):
        builder.main(_args(source, output))
    assert output.read_bytes() == b'existing fixture'


def test_missing_parquet_does_not_create_output(tmp_path):
    output = tmp_path / 'out.json'
    with pytest.raises(FileNotFoundError):
        builder.main(_args(tmp_path / 'missing.parquet', output))
    assert not output.exists()
