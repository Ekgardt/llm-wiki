"""Gold-file IO preserves the existing independently pinned corpus identity."""
import hashlib
import json
from pathlib import Path

import pytest

from tests.module_context import context_reader

from .support import qualification_generator as generator

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/python-qualification-corpus-context.json')


def test_cli_writes_the_pinned_gold_and_reports_its_identity(tmp_path, capsys):
    expected = json.loads(read_source(ROOT / 'benchmark/code-navigation-python-v1.json'))
    gold = tmp_path / 'gold.json'
    assert generator.main([str(tmp_path / 'corpus'), '--gold', str(gold)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['line_count'] == 100000
    assert report['gold_queries'] == 400
    assert report['source_manifest_sha256'] == expected['expected_source_manifest_sha256']
    assert report['gold_sha256'] == expected['expected_gold_sha256']
    assert hashlib.sha256(gold.read_bytes().removesuffix(b'\n')).hexdigest() == expected['expected_gold_sha256']
    assert len(json.loads(gold.read_text(encoding='utf-8'))['queries']) == 400


def test_existing_destination_is_refused_without_overwriting(tmp_path):
    marker = tmp_path / 'keep.txt'
    marker.write_bytes(b'original data')
    with pytest.raises(FileExistsError):
        generator.generate_qualification_repository(tmp_path)
    assert marker.read_bytes() == b'original data'
    assert list(tmp_path.iterdir()) == [marker]
