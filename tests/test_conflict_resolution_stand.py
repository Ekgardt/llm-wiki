"""The conflict-resolution stand must be trustworthy before its numbers are.

These integration tests cover the deterministic resolvers, SubEM scoring and
the reply parser, with the shipped fixture — plus the
one product behaviour the whole claim rests on: that this vault's rule refuses
an ambiguous order by name instead of guessing at it.

Pure frame, grouping and version tests live in tests/modules/factconsolidation_data.
Nothing here calls a provider or writes into the live vault.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "benchmark"))

import factconsolidation_data as data  # noqa: E402
import run_conflict_resolution as runner  # noqa: E402


def _conflict(first: str, second: str, serials: tuple[int, int] = (0, 300)):
    return data.Conflict(
        subject="Fatah",
        prop="chairperson",
        observations=((serials[0], first), (serials[1], second)),
        truth=second,
        gold_confirmed=True,
    )


def test_the_vault_rule_returns_the_later_value_when_the_order_is_total():
    outcome = runner.resolve_vault(_conflict("Mahmoud Abbas", "Moshe Kahlon"), 1)
    assert outcome.refusal is None
    assert outcome.answer == "moshe kahlon"


def test_the_vault_rule_refuses_by_name_when_two_values_share_one_observation():
    """The claims arrive from one daily block, so no order can be read."""
    item = _conflict("Mahmoud Abbas", "Moshe Kahlon", serials=(0, 1))
    outcome = runner.resolve_vault(item, 512)
    assert outcome.answer is None
    assert outcome.refusal == "bitemporal_ambiguous_observation"


def test_a_rule_that_cannot_abstain_answers_the_same_tie_with_the_stale_value():
    """Why the refusal is worth its abstention: the alternative is silent error."""
    item = _conflict("Mahmoud Abbas", "Moshe Kahlon", serials=(0, 1))
    outcome = runner.resolve_argmax_observed(item, 512)
    assert outcome.refusal is None
    assert outcome.answer == "Mahmoud Abbas"
    assert runner.verdict(outcome, item.truth) == runner.WRONG


def test_the_paper_resolver_reads_the_serial_and_never_abstains():
    outcome = runner.resolve_argmax(_conflict("Mahmoud Abbas", "Moshe Kahlon"))
    assert outcome.answer == "Moshe Kahlon"
    assert outcome.refusal is None


def test_observation_instants_collapse_exactly_at_the_block_boundary():
    assert runner.observed_at(0, 1) != runner.observed_at(1, 1)
    assert runner.observed_at(0, 32) == runner.observed_at(31, 32)
    assert runner.observed_at(31, 32) != runner.observed_at(32, 32)


def test_subem_scores_an_answer_that_contains_the_expected_value():
    assert runner.verdict(runner.Outcome("Moshe Kahlon"), "moshe kahlon") == runner.CORRECT
    assert runner.verdict(runner.Outcome("It is Moshe Kahlon"), "Moshe Kahlon") == runner.CORRECT
    assert runner.verdict(runner.Outcome("Mahmoud Abbas"), "Moshe Kahlon") == runner.WRONG
    assert runner.verdict(runner.Outcome(None), "Moshe Kahlon") == runner.ABSTAIN


def test_a_refusal_is_never_scored_as_a_correct_answer():
    result = runner.run_arm(
        "vault",
        [_conflict("Mahmoud Abbas", "Moshe Kahlon", serials=(0, 1))],
        lambda item: runner.resolve_vault(item, 512),
    )
    assert result.correct == 0
    assert result.abstained == 1
    assert result.as_json()["accuracy"] == 0.0


def test_the_provider_reply_parser_separates_abstention_from_an_answer():
    assert runner._parsed_reply("NO ANSWER", 0.0).refusal == "llm_no_answer"
    assert runner._parsed_reply("", 0.0).refusal == "provider_empty"
    assert runner._parsed_reply(None, 0.0).refusal == "provider_empty"
    assert runner._parsed_reply("Moshe Kahlon\nbecause", 0.0).answer == "Moshe Kahlon"


def test_the_shipped_fixture_loads_and_carries_the_bytes_it_was_derived_from():
    document = data.load_fixture(runner.DEFAULT_FIXTURE)
    assert document["row_id"] == "factconsolidation_sh_6k"
    assert len(document["source_sha256"]) == 64
    assert len(data.fixture_conflicts(document)) == len(document["conflicts"])


def test_the_vault_rule_is_never_wrong_on_the_shipped_fixture_at_any_granularity():
    """The invariant the note reports: coarser evidence costs answers, not truth."""
    items = data.fixture_conflicts(data.load_fixture(runner.DEFAULT_FIXTURE))
    for size in runner.SWEEP_BLOCK_SIZES:
        result = runner.run_arm(
            "vault", items, lambda item, k=size: runner.resolve_vault(item, k)
        )
        assert result.wrong == 0, f"block size {size} produced a wrong answer"


def test_resolving_the_whole_ledger_at_once_agrees_with_one_conflict_at_a_time():
    """The stand feeds one conflict at a time; a real ledger arrives whole."""
    from bitemporal_claims import as_of

    items = data.fixture_conflicts(data.load_fixture(runner.DEFAULT_FIXTURE))
    records = [record for item in items for record in runner.claim_records(item, 1)]
    surviving = as_of(records, valid_at=runner.FAR_FUTURE)
    assert len(surviving) == len(items)
    assert {_survivor_key(belief) for belief in surviving} == {
        (item.subject, item.prop, item.truth.strip().casefold()) for item in items
    }


def _survivor_key(belief):
    record = belief.record
    qualifier = record["qualifiers"][0]["value"]["value"]
    return (record["subject"], qualifier, record["value"]["value"])


def test_the_provider_is_never_called_from_inside_this_repository(monkeypatch):
    """The 175s-versus-12s lesson, pinned so it cannot regress silently."""
    monkeypatch.chdir(REPO)
    before = Path.cwd()
    runner.provider_call()
    after = Path.cwd()
    os.chdir(before)
    assert after != REPO
    assert REPO not in after.parents
