"""Real local IO preserves the adapter's existing interchange contract."""
import json

import pytest

from tests import test_locomo_becomes_a_question_this_stand_can_read as original

adapter = original.locomo_data


def test_local_conversion_writes_unicode_and_preserves_abstention(tmp_path):
    conversation = dict(original.CONVERSATION, sample_id="разговор")
    source = tmp_path / "source.json"
    source.write_text(json.dumps([conversation], ensure_ascii=False), encoding="utf-8")
    target = tmp_path / "nested" / "converted.json"

    assert adapter.convert(source, target) == target
    rows = json.loads(target.read_text(encoding="utf-8"))
    assert len(rows) == 2
    assert rows[0]["question_id"] == 'разговор_0'
    assert rows[0]["question"] == 'What did Ann do?'
    assert rows[0]["answer"] == 'ran a race'
    assert rows[1]["question_id"].endswith("_abs")
    assert rows[1]["answer"] == ""


@pytest.mark.parametrize("content,error", [
    ("{", json.JSONDecodeError),
    ('[]', adapter.DatasetUnavailable),
])
def test_invalid_or_empty_source_is_not_replaced(tmp_path, content, error):
    source = tmp_path / "source"
    source.write_text(content, encoding="utf-8")
    with pytest.raises(error):
        adapter.load_source(source)
