"""Stored fixtures and synthetic construction use the real local IO boundary."""
import json

from tests.module_context import context_reader
from tests.modules.factconsolidation_data.test_parsing import ROOT, data

read_source = context_reader(ROOT, ROOT / 'docs/modules/factconsolidation-data-context.json')


def test_entire_shipped_fixture_retains_serials_and_truth():
    path = ROOT / 'benchmark/conflict-resolution-v1.json'
    stored = json.loads(read_source(path))
    loaded = data.load_fixture(path)
    assert loaded == stored
    conflicts = data.fixture_conflicts(loaded)
    assert len(conflicts) == 161
    assert conflicts[0].observations == ((0, 'London'), (306, 'Leeds'))
    assert conflicts[-1].truth == 'John McVie'
    assert sum(item.gold_confirmed for item in conflicts) == 72


def test_synthetic_fixture_roundtrip_reports_unparsed_lines(tmp_path):
    row = {
        'context': '9. The capital of Test is Новый.\n0. The capital of Test is Old.\n'
                   '3. This numbered statement has no known frame.\nignored heading',
        'answers': [['Новый']],
    }
    fixture = data.build_fixture(row, row_id='synthetic', source_sha256='a' * 64)
    path = tmp_path / 'fixture.json'
    path.write_text(json.dumps(fixture, ensure_ascii=False), encoding='utf-8')
    loaded = data.load_fixture(path)
    assert loaded['numbered_lines'] == 3
    assert loaded['parsed_facts'] == 2
    assert loaded['unparsed_lines'] == ['3. This numbered statement has no known frame.']
    assert loaded['source_sha256'] == 'a' * 64
    assert data.fixture_conflicts(loaded) == (
        data.Conflict('Test', 'capital', ((0, 'Old'), (9, 'Новый')), 'Новый', True),
    )
