"""The classification measurement stand must fail when classification fails."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BENCHMARK = ROOT / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

import build_flush_corpus  # noqa: E402
import run_flush_classification as stand  # noqa: E402


def _corpus() -> dict:
    return stand.load_corpus(stand.CORPUS)


def _values(corpus: dict, field: str) -> list[str]:
    return [case[field] for case in corpus["cases"]]


def _answer_every(corpus: dict, tier: str, response: str) -> dict:
    for case in corpus["cases"]:
        if case["expected_tier"] == tier:
            case["canned_response"] = response
    return corpus


def test_the_shipped_corpus_is_valid_and_covers_both_outcomes():
    corpus = _corpus()

    assert set(_values(corpus, "expected_tier")) == {"major", "minor", "ok"}
    assert {"EN", "RU"} <= set(_values(corpus, "language"))
    assert len(set(_values(corpus, "case_id"))) == len(corpus["cases"])


def test_the_canned_run_reports_a_clean_sheet_and_passes_its_gates():
    report = stand.run(_corpus(), stand.ADAPTERS["canned"])

    metrics = report["metrics"]

    assert (metrics["tier_accuracy"], metrics["durable_content_recall"]) == (1.0, 1.0)
    assert metrics["false_promotion_rate"] == 0.0
    assert (report["misses"], report["gates"]["passed"]) == ([], True)


def test_a_dropped_decision_is_counted_as_a_miss_and_fails_the_gate():
    """The number the audit asked for: a decision that never reached memory."""
    corpus = _answer_every(_corpus(), "major", "FLUSH_OK\n")

    report = stand.run(corpus, stand.ADAPTERS["canned"])
    gates = report["gates"]

    assert report["metrics"]["durable_content_recall"] < 1.0
    assert (gates["metric_results"]["durable_content_recall"], gates["passed"]) == (False, False)
    assert any(item["observed_tier"] == "ok" for item in report["misses"])


def test_promoting_pure_status_chatter_fails_the_gate():
    corpus = _answer_every(
        _corpus(), "ok", "FLUSH_MAJOR\n\n- **Decisions made** — none really.\n"
    )

    report = stand.run(corpus, stand.ADAPTERS["canned"])
    gates = report["gates"]

    assert report["metrics"]["false_promotion_rate"] == 1.0
    assert (gates["metric_results"]["false_promotion_rate"], gates["passed"]) == (False, False)


def test_the_stand_scores_the_prompt_the_product_sends():
    """A copied prompt would measure something the product does not use."""
    import flush_memory

    case = _corpus()["cases"][0]
    prompt = flush_memory.build_classification_prompt(case["transcript"], case["event"])

    assert case["transcript"] in prompt
    assert "FLUSH_MAJOR" in prompt
    assert "FLUSH_MAJOR" in flush_memory.CLASSIFICATION_SYSTEM_PROMPT


def test_the_cli_returns_nonzero_when_a_gate_fails(tmp_path: Path, capsys):
    corpus = _corpus()
    for case in corpus["cases"]:
        case["canned_response"] = "FLUSH_OK\n"
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps(corpus, ensure_ascii=False), encoding="utf-8")

    assert stand.main(["--corpus", str(path), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["gates"]["passed"] is False


def test_an_invalid_corpus_is_rejected(tmp_path: Path):
    from reliable_memory import SchemaValidationError

    path = tmp_path / "corpus.json"
    corpus = _corpus()
    corpus["cases"][0]["expected_tier"] = "enormous"
    path.write_text(json.dumps(corpus, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(SchemaValidationError):
        stand.load_corpus(path)


def _live_case(case_id: str, status: str, quote_tier: str | None) -> dict:
    return {
        "case_id": case_id,
        "language": "EN",
        "event": "session-end",
        "content_classes": ["decision"],
        "transcript": "user: pick one\nassistant: rollback journal, WAL breaks",
        "expected_tier": "major",
        "required_markers": ["rollback journal"],
        "label_provenance": "readings",
        "label_status": status,
        "readings": {"rubric": "major", "quote": quote_tier, "passage": ""},
    }


def _live_corpus(cases: list[dict]) -> dict:
    return {
        "corpus_id": "live-test",
        "schema_version": "flush-classification/v3",
        "thresholds": {
            "tier_accuracy": 0.8,
            "durable_content_recall": 0.8,
            "false_promotion_rate": 0.2,
        },
        "cases": cases,
    }


def test_a_contested_case_counts_in_no_metric(tmp_path):
    """The two readings disagreed; the case stays, and the numbers leave it out."""
    corpus = _live_corpus(
        [
            _live_case("live-001-agreed", "confirmed", "major"),
            _live_case("live-002-contested", "contested", None),
        ]
    )
    path = tmp_path / "live.json"
    path.write_text(json.dumps(corpus), encoding="utf-8")
    seen: list[str] = []

    def adapter(case: dict) -> str:
        seen.append(case["case_id"])
        return "FLUSH_OK\n"

    report = stand.run(stand.load_corpus(path), adapter)

    assert seen == ["live-001-agreed"]
    assert report["labels"] == {"case_count": 2, "confirmed_count": 1, "contested_count": 1}
    assert report["metrics"]["case_count"] == 1
    assert report["metrics"]["tier_accuracy"] == 0.0


def test_a_corpus_with_no_confirmed_label_passes_no_gate():
    report = stand.run(
        _live_corpus([_live_case("live-001-contested", "contested", "minor")]),
        lambda case: "FLUSH_MAJOR\n\n- rollback journal\n",
    )

    assert report["gates"]["metric_results"]["labelled"] is False
    assert report["gates"]["passed"] is False


def test_the_shipped_corpus_is_hand_written_and_counts_as_confirmed():
    corpus = stand.load_corpus(stand.CORPUS)

    assert stand.label_state(corpus)["contested_count"] == 0


def test_a_corpus_that_says_a_human_reviewed_it_is_refused(tmp_path):
    """Schema v2 carried `label_reviewed`; nobody reviews labels by hand now."""
    case = _live_case("live-001-reviewed", "confirmed", "major")
    case["label_reviewed"] = True
    path = tmp_path / "live.json"
    path.write_text(json.dumps(_live_corpus([case])), encoding="utf-8")

    from reliable_memory import SchemaValidationError

    with pytest.raises(SchemaValidationError):
        stand.load_corpus(path)


def test_an_unknown_schema_version_is_refused(tmp_path):
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps({"schema_version": "flush-classification/v9"}), encoding="utf-8")

    with pytest.raises(ValueError, match="unknown corpus schema"):
        stand.load_corpus(path)


def test_the_corpus_builder_skips_the_memory_s_own_provider_calls(tmp_path: Path) -> None:
    """39 of the 40 live cases were `claude -p` calls of the classifier itself (2026-09-23)."""
    import json as _json

    own = tmp_path / "a" / "own.jsonl"
    held = tmp_path / "a" / "held.jsonl"
    own.parent.mkdir()
    own.write_text(_json.dumps({"type": "user", "entrypoint": "sdk-cli", "message": {"role": "user", "content": "x"}}) + "\n", encoding="utf-8")
    held.write_text(_json.dumps({"type": "user", "entrypoint": "cli", "message": {"role": "user", "content": "y"}}) + "\n", encoding="utf-8")

    assert build_flush_corpus._transcripts(tmp_path, 10) == [held]










def test_the_case_carries_the_text_the_product_classifies(tmp_path: Path):
    """Raw host JSONL is not what the classifier reads since 2026-09-06."""
    import flush_memory

    builder = build_flush_corpus
    noise = {"type": "file-history-snapshot", "snapshot": {"x": "y" * 300}}
    said = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "We chose lzma."}]}}
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in [noise, said, noise]), encoding="utf-8")

    excerpt = builder._excerpt(path)

    assert excerpt == flush_memory.readable_evidence(path.read_text(encoding="utf-8")).strip()
    assert ("We chose lzma." in excerpt, "file-history-snapshot" in excerpt) == (True, False)






def test_builder_keeps_label_aliases_and_actual_text_orchestration(tmp_path, monkeypatch):
    import flush_corpus_labels as labels

    builder = build_flush_corpus
    assert builder.Verdict is labels.Verdict
    assert builder.QuoteReading is labels.QuoteReading
    assert builder.parse_verdict is labels.parse_verdict
    assert builder.parse_quote_reading is labels.parse_quote_reading
    assert builder.build_case is labels.build_case
    assert builder.agreement is labels.agreement
    path = tmp_path / 'held.jsonl'
    message = 'We chose rollback journal for durable writes.'
    path.write_text(json.dumps({'type': 'user', 'message': {'role': 'user', 'content': message}}) + '\n', encoding='utf-8')
    monkeypatch.setattr(builder, '_judge', lambda text: '{"keeps":"much","kinds":["decision"],"phrases":["rollback journal"]}')
    monkeypatch.setattr(builder, '_quote_reader', lambda text: json.dumps({'keeps': 'much', 'quote': message}))
    case = builder._labelled_case(path, 1)
    assert case['label_status'] == 'confirmed'
    assert case['required_markers'] == ['rollback journal']
    assert message in case['transcript']


def test_builder_label_imports_work_from_a_neutral_directory(tmp_path):
    import subprocess

    result = subprocess.run(
        [sys.executable, str(BENCHMARK / 'build_flush_corpus.py'), '--help'],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert '--transcripts' in result.stdout


def test_builder_namespace_import_shares_the_canonical_label_types(tmp_path):
    import subprocess

    program = (
        'import sys; sys.path.insert(0, sys.argv[1]); '
        'import benchmark.build_flush_corpus as builder; import flush_corpus_labels as labels; '
        'assert builder.Verdict is labels.Verdict; assert builder.QuoteReading is labels.QuoteReading'
    )
    result = subprocess.run(
        [sys.executable, '-c', program, str(ROOT)], cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_runner_keeps_scoring_aliases():
    import flush_classification_metrics as metrics

    assert stand.CaseOutcome is metrics.CaseOutcome
    assert stand.score_case is metrics.score_case
    assert stand.confirmed_cases is metrics.confirmed_cases
    assert stand.label_state is metrics.label_state
    assert stand.measure is metrics.measure
    assert stand.evaluate is metrics.evaluate


def test_runner_executes_from_neutral_directory(tmp_path):
    import subprocess

    result = subprocess.run(
        [sys.executable, str(BENCHMARK / 'run_flush_classification.py'), '--json'],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['gates']['passed'] is True


def test_runner_namespace_import_shares_scoring_type(tmp_path):
    import subprocess

    program = (
        'import sys; sys.path.insert(0, sys.argv[1]); '
        'import benchmark.run_flush_classification as runner; '
        'import flush_classification_metrics as metrics; '
        'assert runner.CaseOutcome is metrics.CaseOutcome; '
        'assert runner.run(runner.load_corpus(runner.CORPUS), runner.ADAPTERS["canned"])["gates"]["passed"]'
    )
    result = subprocess.run(
        [sys.executable, '-c', program, str(ROOT)], cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
