"""Exercise real judged JSONL, staging JSON and output without a model."""
import json
import sys
from pathlib import Path

import pytest

BENCHMARK = Path(__file__).resolve().parents[3] / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

import ledger_offline as offline  # noqa: E402

from tests.the_offline_ledger_reads_the_question_not_the_label_helpers import QUESTION  # noqa: E402


def _judged(tmp_path):
    row = {
        "question_id": "вопрос-自行车", "question": QUESTION["question"],
        "question_type": "unrelated-label", "gold": "2", "hypothesis": "1",
        "judge_correct": False,
    }
    rows = [row, dict(row, question_id="missing-but-abstained", is_abstention=True),
            dict(row, question_id="missing-but-lookup", question="Which bike is mine?")]
    path = tmp_path / "judged.jsonl"
    path.write_text("\n" + "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    return path, row["question_id"]


def test_cli_reads_unicode_question_and_writes_real_counts(tmp_path, capsys):
    judged, question_id = _judged(tmp_path)
    (tmp_path / f"{question_id}.question.json").write_text(json.dumps(QUESTION), encoding="utf-8")
    output = tmp_path / "result.json"
    assert offline.main(['--judged', str(judged), '--staging', str(tmp_path), '--out', str(output)]) == 0
    results = json.loads(output.read_text(encoding="utf-8"))
    summary = json.loads(capsys.readouterr().out)
    assert len(results) == 1
    assert (results[0]['question_id'], results[0]['records'], results[0]['figure'],
            results[0]['verdict'], results[0]['reconciled']) == (question_id, 3, 2.0, 'fixed', False)
    assert summary['verdicts']['all']['fixed'] == 1
    assert summary['reconcile']['wrong_flagged'] == 1


def test_missing_staged_question_remains_an_error(tmp_path):
    judged, _ = _judged(tmp_path)
    with pytest.raises(FileNotFoundError):
        offline.evaluate(judged, tmp_path)


def test_malformed_judged_json_remains_an_error(tmp_path):
    path = tmp_path / 'judged.jsonl'
    path.write_text('{broken', encoding='utf-8')
    with pytest.raises(json.JSONDecodeError):
        offline.counting_rows(path)
