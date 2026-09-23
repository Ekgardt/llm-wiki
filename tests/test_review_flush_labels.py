"""The human review of classification labels must be safe to interrupt.

No real sessions and no terminal: the corpus is a fixture and the answers come
from an injected stream. What is checked is the four properties the review would
be worthless without — it resumes, it never re-asks an answered case, every
answer is on disk before the next one is read, and the machine's label is not
shown until after the answer.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BENCHMARK = ROOT / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

import review_flush_labels as review  # noqa: E402

TIERS = ("major", "minor", "ok")
_ANSWER_KEY = {"major": "m", "minor": "n", "ok": "o"}


def _case(index: int, tier: str = "major") -> dict:
    return {
        "case_id": f"live-{index:03d}-fixture",
        "language": "EN",
        "event": "session-end",
        "content_classes": ["decision"],
        "transcript": f"user: question {index}\nassistant: rollback journal, not WAL",
        "expected_tier": tier,
        "required_markers": ["rollback journal"],
        "label_provenance": "judge",
        "label_reviewed": False,
    }


def _corpus(
    tmp_path: Path,
    count: int = 3,
    tier: str = "major",
    tiers: list[str] | None = None,
) -> Path:
    chosen = tiers or [tier] * count
    corpus = {
        "corpus_id": "flush-classification-fixture",
        "schema_version": "flush-classification/v2",
        "thresholds": {
            "tier_accuracy": 0.8,
            "durable_content_recall": 0.8,
            "false_promotion_rate": 0.2,
        },
        "cases": [
            _case(index, chosen[index - 1]) for index in range(1, len(chosen) + 1)
        ],
    }
    path = tmp_path / "flush-classification-live.json"
    path.write_text(json.dumps(corpus, ensure_ascii=False), encoding="utf-8")
    return path


class _Stream:
    """Answers on demand, recording everything written before each one."""

    def __init__(self, answers: list[str], sink: list[str]) -> None:
        self._answers = list(answers)
        self._sink = sink
        self.seen_before_answer: list[str] = []

    def readline(self) -> str:
        self.seen_before_answer.append("".join(self._sink))
        if not self._answers:
            return ""
        return self._answers.pop(0)


def _run(corpus: Path, answers: list[str], *, extra: list[str] | None = None):
    sink: list[str] = []
    stream = _Stream(answers, sink)
    argv = ["--corpus", str(corpus), "--all", *(extra or [])]
    code = review.main(argv, stream=stream, write=sink.append)
    return code, "".join(sink), stream


def test_an_answered_case_is_never_asked_again(tmp_path: Path):
    corpus = _corpus(tmp_path, count=3)

    _run(corpus, ["m\n", "q\n"])
    _code, second, _stream = _run(corpus, ["q\n"])

    assert "live-001-fixture" not in second
    assert "live-002-fixture" in second
    assert "reviewed: 1 of 3" in second
    assert "remaining: 2" in second


def test_quitting_and_returning_keeps_the_earlier_answers(tmp_path: Path):
    corpus = _corpus(tmp_path, count=3)

    _run(corpus, ["m\n", "q\n"])
    _run(corpus, ["n\n", "q\n"])
    _code, third, _stream = _run(corpus, ["q\n"])

    verdicts = review.load_verdicts(review.default_verdicts_path(corpus))
    assert sorted(verdicts) == ["live-001-fixture", "live-002-fixture"]
    assert verdicts["live-001-fixture"]["human_tier"] == "major"
    assert verdicts["live-002-fixture"]["human_tier"] == "minor"
    assert "reviewed: 2 of 3" in third


def test_the_answer_is_on_disk_before_anything_else_can_fail(tmp_path: Path):
    """A closed terminal costs one case: the write happens before the reveal."""
    corpus = _corpus(tmp_path, count=2)
    sink: list[str] = []

    def exploding_write(text: str) -> None:
        sink.append(text)
        if "recorded." in text:
            raise RuntimeError("terminal closed")

    stream = _Stream(["m\n"], sink)
    with pytest.raises(RuntimeError):
        review.main(
            ["--corpus", str(corpus), "--all"], stream=stream, write=exploding_write
        )

    verdicts = review.load_verdicts(review.default_verdicts_path(corpus))
    assert list(verdicts) == ["live-001-fixture"]
    assert verdicts["live-001-fixture"]["machine_tier"] == "major"


def test_the_machine_label_is_not_shown_before_the_answer(tmp_path: Path):
    """`minor` appears nowhere in the fixture, the banner, or the prompt."""
    corpus = _corpus(tmp_path, count=1, tier="minor")

    _code, _output, stream = _run(corpus, ["m\n"])

    shown_before = stream.seen_before_answer[0]
    assert "live-001-fixture" in shown_before
    assert "minor" not in shown_before.casefold()
    assert "expected_tier" not in shown_before
    assert "required_markers" not in shown_before


def test_the_machine_label_is_revealed_after_the_answer(tmp_path: Path):
    corpus = _corpus(tmp_path, count=1, tier="ok")

    _code, output, _stream = _run(corpus, ["m\n"])

    assert "The machine said ok" in output


def test_a_skipped_case_comes_back_and_is_not_counted(tmp_path: Path):
    corpus = _corpus(tmp_path, count=2)

    _run(corpus, ["s\n", "q\n"])
    _code, second, _stream = _run(corpus, ["q\n"])

    assert "live-001-fixture" in second
    assert "reviewed: 0 of 2" in second


def test_an_unreadable_answer_is_refused_rather_than_recorded(tmp_path: Path):
    corpus = _corpus(tmp_path, count=1)

    _code, output, _stream = _run(corpus, ["yes\n", "o\n"])

    assert "answer with m, n, o, s or q" in output
    verdicts = review.load_verdicts(review.default_verdicts_path(corpus))
    assert verdicts["live-001-fixture"]["human_tier"] == "ok"


def test_too_few_reviewed_cases_report_no_kappa_at_all(tmp_path: Path):
    corpus = _corpus(tmp_path, count=2)

    _code, output, _stream = _run(corpus, ["m\n", "m\n"])

    assert "agreement: not reported" in output
    assert f"at least {review.KAPPA_MINIMUM_CASES}" in output


def test_kappa_is_reported_once_enough_cases_are_reviewed(tmp_path: Path):
    """All three tiers have to occur: kappa is undefined without variation."""
    tiers = [TIERS[index % len(TIERS)] for index in range(review.KAPPA_MINIMUM_CASES)]
    corpus = _corpus(tmp_path, tiers=tiers)
    answers = [_ANSWER_KEY[tier] + "\n" for tier in tiers]

    _code, output, _stream = _run(corpus, answers)

    assert "agreement: Cohen's kappa 1.000 (almost perfect)" in output
    assert "not reported" not in output


def test_cohens_kappa_is_one_when_the_two_labels_always_match():
    perfect = [("major", "major"), ("ok", "ok")] * 5

    assert review.cohens_kappa(perfect) == pytest.approx(1.0)


def test_cohens_kappa_is_zero_when_the_labels_agree_only_by_chance():
    """Two categories, balanced marginals, human and machine independent."""
    chance = [
        ("major", "major"),
        ("major", "ok"),
        ("ok", "major"),
        ("ok", "ok"),
    ]

    assert review.cohens_kappa(chance) == pytest.approx(0.0)


def test_cohens_kappa_is_undefined_without_two_categories():
    assert review.cohens_kappa([]) is None
    assert review.cohens_kappa([("ok", "ok")]) is None


def test_kappa_labels_follow_the_landis_and_koch_bands():
    assert review.kappa_label(0.75) == "substantial"
    assert review.kappa_label(0.0) == "no better than chance"
    assert review.kappa_label(0.9) == "almost perfect"


def test_the_seeded_subsample_is_stable_across_resumptions(tmp_path: Path):
    cases = [_case(index) for index in range(1, 21)]

    first = review.select_cases(cases, {}, sample=5, seed=7)
    answered = {str(first[0]["case_id"]): {"case_id": first[0]["case_id"]}}
    second = review.select_cases(cases, answered, sample=5, seed=7)

    assert len(first) == 5
    assert [case["case_id"] for case in second] == [
        case["case_id"] for case in first[1:]
    ]


def test_the_verdict_sidecar_never_holds_session_text(tmp_path: Path):
    corpus = _corpus(tmp_path, count=1)

    _run(corpus, ["m\n"])

    sidecar = review.default_verdicts_path(corpus)
    assert "rollback journal" not in sidecar.read_text(encoding="utf-8")
    assert sidecar.name == "flush-classification-live.review.jsonl"


def test_the_excerpt_shows_the_conversation_not_the_record_metadata():
    import json as _json

    import review_flush_labels

    lines = [
        _json.dumps({"type": "attachment", "parentUuid": "p1", "cwd": "/home/x", "attachment": {"style": "Kratko"}}),
        _json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "почини установщик"}]}}),
        _json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "uv run pytest -q"}},
            {"type": "text", "text": "Готово, тесты прошли."},
        ]}}),
    ]

    shown = review_flush_labels.excerpt("\n".join(lines))

    assert "почини установщик" in shown and "Готово, тесты прошли." in shown
    assert "parentUuid" not in shown and "Kratko" not in shown
    assert review_flush_labels.excerpt("plain text, not a transcript") == "plain text, not a transcript"


def test_the_prompt_is_flushed_before_the_answer_is_read(monkeypatch):
    import io

    import review_flush_labels

    class _Out(io.StringIO):
        flushes = 0

        def flush(self):
            self.flushes += 1
            super().flush()

    out = _Out()
    monkeypatch.setattr(review_flush_labels.sys, "stdout", out)

    review_flush_labels._flushing_write(review_flush_labels.PROMPT)

    assert (out.getvalue(), out.flushes) == (review_flush_labels.PROMPT, 1)
