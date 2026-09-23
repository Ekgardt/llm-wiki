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
    import sys as _sys

    benchmark = Path(__file__).resolve().parent.parent / "benchmark"
    if str(benchmark) not in _sys.path:
        _sys.path.insert(0, str(benchmark))
    import build_flush_corpus

    own = tmp_path / "a" / "own.jsonl"
    held = tmp_path / "a" / "held.jsonl"
    own.parent.mkdir()
    own.write_text(_json.dumps({"type": "user", "entrypoint": "sdk-cli", "message": {"role": "user", "content": "x"}}) + "\n", encoding="utf-8")
    held.write_text(_json.dumps({"type": "user", "entrypoint": "cli", "message": {"role": "user", "content": "y"}}) + "\n", encoding="utf-8")

    assert build_flush_corpus._transcripts(tmp_path, 10) == [held]


def _builder():
    benchmark = Path(__file__).resolve().parent.parent / "benchmark"
    if str(benchmark) not in sys.path:
        sys.path.insert(0, str(benchmark))
    import build_flush_corpus

    return build_flush_corpus


SESSION = "user: WAL or rollback?\n\nassistant: Rollback journal with synchronous FULL, because WAL breaks on network disks."


def test_a_quote_the_transcript_does_not_carry_voids_the_reading():
    """An invented passage is not a wrong label; it is no label at all."""
    builder = _builder()
    invented = '{"quote": "We chose WAL for its speed.", "keeps": "much"}'
    verbatim = '{"quote": "Rollback journal with   synchronous FULL, because WAL", "keeps": "much"}'

    assert builder.parse_quote_reading(invented, SESSION).tier is None
    assert builder.parse_quote_reading(verbatim, SESSION).tier == "major"
    assert builder.parse_quote_reading('{"quote": "", "keeps": "nothing"}', SESSION).tier == "ok"


def test_a_stitched_quote_with_one_real_sentence_is_grounded():
    """Four of seven voided quotes on 2026-09-23 joined two real, non-adjacent sentences."""
    builder = _builder()
    stitched = "We chose WAL for its speed. Rollback journal with synchronous FULL, because WAL breaks on network disks."

    assert builder.quote_is_verbatim(stitched, SESSION) is True
    assert builder.quote_is_verbatim("synchronous FULL", SESSION) is False


def test_commentary_after_the_json_does_not_lose_the_reading():
    """One reading answered its JSON and then argued with itself in prose with braces."""
    builder = _builder()
    answer = '{"quote": "WAL breaks on network disks.", "keeps": "much"}\n\nWait — {the text} says "rollback".'

    reading = builder.parse_quote_reading(answer, SESSION)

    assert (reading.tier, reading.passage) == ("major", "WAL breaks on network disks.")


def test_the_case_carries_the_text_the_product_classifies(tmp_path: Path):
    """Raw host JSONL is not what the classifier reads since 2026-09-06."""
    import flush_memory

    builder = _builder()
    noise = {"type": "file-history-snapshot", "snapshot": {"x": "y" * 300}}
    said = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "We chose lzma."}]}}
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in [noise, said, noise]), encoding="utf-8")

    excerpt = builder._excerpt(path)

    assert excerpt == flush_memory._readable_evidence(path.read_text(encoding="utf-8")).strip()
    assert ("We chose lzma." in excerpt, "file-history-snapshot" in excerpt) == (True, False)


def test_two_readings_that_disagree_make_a_contested_case(tmp_path: Path):
    builder = _builder()
    verdict = builder.Verdict(tier="major", kinds=("decision",), phrases=("rollback journal",))

    agreed = builder.build_case(
        tmp_path / "a.jsonl", 1, SESSION, verdict, builder.QuoteReading("major", "WAL breaks")
    )
    contested = builder.build_case(
        tmp_path / "b.jsonl", 2, SESSION, verdict, builder.QuoteReading(None, "We chose WAL")
    )
    summary = builder.agreement([agreed, contested])

    assert (agreed["label_status"], contested["label_status"]) == ("confirmed", "contested")
    assert contested["readings"] == {"rubric": "major", "quote": None, "passage": "We chose WAL"}
    assert summary == {"confirmed": 1, "contested": 1, "kappa": None}


def test_the_readings_kappa_is_reported_from_thirty_cases():
    builder = _builder()
    pairs = [("major", "major"), ("ok", "ok"), ("minor", "major")] * 10

    assert builder.cohens_kappa(pairs[:29]) is None
    assert builder.cohens_kappa(pairs) == 0.5
