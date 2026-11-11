"""Original corpus-label assertions, now independent of provider orchestration."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'benchmark'))
from tests.modules.flush_corpus_labels.contract_helpers import _builder  # noqa: E402

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
