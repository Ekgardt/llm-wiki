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

import run_flush_classification as stand  # noqa: E402


def _corpus() -> dict:
    return stand.load_corpus(stand.CORPUS)


def test_the_shipped_corpus_is_valid_and_covers_both_outcomes():
    corpus = _corpus()
    tiers = {case["expected_tier"] for case in corpus["cases"]}
    languages = {case["language"] for case in corpus["cases"]}

    assert tiers == {"major", "minor", "ok"}
    assert {"EN", "RU"} <= languages
    assert len({case["case_id"] for case in corpus["cases"]}) == len(corpus["cases"])


def test_the_canned_run_reports_a_clean_sheet_and_passes_its_gates():
    report = stand.run(_corpus(), stand.ADAPTERS["canned"])

    assert report["metrics"]["tier_accuracy"] == 1.0
    assert report["metrics"]["durable_content_recall"] == 1.0
    assert report["metrics"]["false_promotion_rate"] == 0.0
    assert report["misses"] == []
    assert report["gates"]["passed"] is True


def test_a_dropped_decision_is_counted_as_a_miss_and_fails_the_gate():
    """The number the audit asked for: a decision that never reached memory."""
    corpus = _corpus()
    for case in corpus["cases"]:
        if case["expected_tier"] == "major":
            case["canned_response"] = "FLUSH_OK\n"

    report = stand.run(corpus, stand.ADAPTERS["canned"])

    assert report["metrics"]["durable_content_recall"] < 1.0
    assert report["gates"]["metric_results"]["durable_content_recall"] is False
    assert report["gates"]["passed"] is False
    assert any(item["observed_tier"] == "ok" for item in report["misses"])


def test_promoting_pure_status_chatter_fails_the_gate():
    corpus = _corpus()
    for case in corpus["cases"]:
        if case["expected_tier"] == "ok":
            case["canned_response"] = "FLUSH_MAJOR\n\n- **Decisions made** — none really.\n"

    report = stand.run(corpus, stand.ADAPTERS["canned"])

    assert report["metrics"]["false_promotion_rate"] == 1.0
    assert report["gates"]["metric_results"]["false_promotion_rate"] is False
    assert report["gates"]["passed"] is False


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
