"""Real local IO preserves the adapter's existing interchange contract."""
import json

import pytest

from tests import test_refusalbench_becomes_a_question_this_stand_can_read as original

adapter = original.refusalbench_data


def test_local_conversion_writes_unicode_and_preserves_abstention(tmp_path):
    row = dict(original.SINGLE, perturbed_query="Кто ответил?")
    source = tmp_path / "source.jsonl"
    rows = [row, original.REFUSABLE]
    source.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    target = tmp_path / "nested" / "converted.json"

    assert adapter.convert(source, target) == target
    rows = json.loads(target.read_text(encoding="utf-8"))
    assert len(rows) == 2
    assert rows[0]["question_id"] == 'RB-NQ_1'
    assert rows[0]["question"] == 'Кто ответил?'
    assert rows[0]["answer"] == 'Ann'
    assert rows[1]["question_id"].endswith("_abs")
    assert rows[1]["answer"] == ""


@pytest.mark.parametrize("content,error", [
    ("{", json.JSONDecodeError),
    ('  \n', adapter.DatasetUnavailable),
])
def test_invalid_or_empty_source_is_not_replaced(tmp_path, content, error):
    source = tmp_path / "source"
    source.write_text(content, encoding="utf-8")
    with pytest.raises(error):
        adapter.load_rows(source)
