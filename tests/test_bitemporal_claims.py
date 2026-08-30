"""A claim/v1 ledger read on two clocks: valid time and transaction time.

Every record here is built by the real `ClaimPipeline` — split, extract, verify
the literal against the exact evidence bytes, normalize — so these are real
claim/v1 records, not hand-made dictionaries that skip the checks.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

import pytest
from bitemporal_claims import (
    MULTI_VALUED_RELATIONS,
    SINGLE_VALUED_RELATIONS,
    BitemporalRefusal,
    as_of,
    history,
    unclassified_relations,
)
from claims import RELATIONS, ClaimPipeline, _validate_interval
from evidence_resolver import EvidenceResolver

DAILY = "2026-01-02"
OPEN = {"from": None, "to": None}


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _source(*entries: tuple[str, str]) -> bytes:
    body = f"# {DAILY}\n"
    for block_time, text in entries:
        body += f"## [{block_time}] session\n{text}\n"
    return body.encode()


def _extraction(
    source: bytes,
    block_time: str,
    text: str,
    *,
    claim_id: str,
    value: str,
    validity: dict,
    relation: str = "has-state",
) -> dict:
    literal = text.encode()
    start = source.index(literal)
    reference = (
        f"daily:{DAILY} sha256:{_sha(source)} block:{block_time} "
        f"bytes:{start}-{start + len(literal)}"
    )
    return {
        "schema_version": "claim-extraction/v1",
        "claims": [
            {
                "id": claim_id,
                "text": text,
                "subject": "session 5696e5d8",
                "relation": relation,
                "value": {"type": "entity", "value": value},
                "qualifiers": [],
                "validity": validity,
                "lifecycle": "active",
                "confidence": "high",
                "authority": "user",
                "evidence": {
                    "reference": reference,
                    "sha256": _sha(literal),
                    "text": text,
                },
                "links": [],
                "extractor_version": "bitemporal-test/1",
            }
        ],
    }


def _named_block(blocks, block_time: str):
    for block in blocks:
        if block.block_id == block_time:
            return block
    raise AssertionError(f"the source declares no block {block_time}")


def _vault(tmp_path: Path, source: bytes) -> Path:
    daily = tmp_path / "knowledge" / "daily"
    daily.mkdir(parents=True, exist_ok=True)
    (daily / f"{DAILY}.md").write_bytes(source)
    return tmp_path


def _record(vault: Path, source: bytes, block_time: str, extraction: dict) -> dict:
    pipeline = ClaimPipeline(EvidenceResolver(vault))
    block = _named_block(pipeline.split_blocks(source), block_time)
    claim = pipeline.extract(block, extraction)[0]
    return dict(pipeline.normalize(pipeline.verify_literal(claim)).record)


def _values(beliefs) -> list[str]:
    return [item.record["value"]["value"] for item in beliefs]


def _two_states(tmp_path: Path, *, first_validity=OPEN, second_validity=OPEN):
    """One subject told to be in one state, then in another, at two block times."""
    early = "Session 5696e5d8 is working on fix-pip."
    late = "Session 5696e5d8 is working on agenticos."
    source = _source(("03:04:05", early), ("11:00:00", late))
    vault = _vault(tmp_path, source)
    first = _record(
        vault,
        source,
        "03:04:05",
        _extraction(
            source,
            "03:04:05",
            early,
            claim_id="claim:first",
            value="fix-pip",
            validity=first_validity,
        ),
    )
    second = _record(
        vault,
        source,
        "11:00:00",
        _extraction(
            source,
            "11:00:00",
            late,
            claim_id="claim:second",
            value="agenticos",
            validity=second_validity,
        ),
    )
    return [first, second]


def test_as_of_returns_the_value_that_was_true_at_that_moment(tmp_path):
    """The question the ledger could not answer before: what was true as of DATE."""
    records = _two_states(tmp_path)
    assert _values(as_of(records, valid_at="2026-01-02T06:00:00Z")) == ["fix-pip"]
    assert _values(as_of(records, valid_at="2026-01-02T11:30:00Z")) == ["agenticos"]


def test_the_superseded_claim_carries_both_derived_ends(tmp_path):
    """Graphiti's rule, applied without an LLM: expired on one clock, invalid on the other."""
    ended, current = history(_two_states(tmp_path))
    assert ended.expired_at == "2026-01-02T11:00:00Z"
    assert ended.invalid_at == "2026-01-02T11:00:00Z"
    assert current.expired_at is None
    assert current.invalid_at is None


def test_an_earlier_known_at_still_answers_with_what_was_known_then(tmp_path):
    """The transaction clock: asking the vault what it believed before it learned."""
    records = _two_states(tmp_path)
    then = as_of(records, valid_at="2026-01-02T11:30:00Z", known_at="2026-01-02T09:00:00Z")
    now = as_of(records, valid_at="2026-01-02T11:30:00Z")
    assert _values(then) == ["fix-pip"]
    assert _values(now) == ["agenticos"]


def test_a_retroactive_correction_empties_the_belief_it_corrects(tmp_path):
    """A later claim valid from earlier is a correction, not a refusal."""
    records = _two_states(
        tmp_path,
        first_validity={"from": "2026-01-02T03:00:00Z", "to": None},
        second_validity={"from": "2026-01-02T03:00:00Z", "to": None},
    )
    assert _values(as_of(records, valid_at="2026-01-02T06:00:00Z")) == ["agenticos"]
    corrected = history(records)[0]
    assert corrected.invalid_at == "2026-01-02T03:00:00Z"


def test_a_multi_valued_relation_is_never_invalidated_by_a_sibling(tmp_path):
    """A module uses many modules; a later `uses` does not contradict an earlier one."""
    early = "Service A depends on Postgres."
    late = "Service A depends on Redis."
    source = _source(("03:04:05", early), ("11:00:00", late))
    vault = _vault(tmp_path, source)
    first = _record(
        vault,
        source,
        "03:04:05",
        _extraction(
            source,
            "03:04:05",
            early,
            claim_id="claim:pg",
            value="postgres",
            validity=OPEN,
            relation="depends-on",
        ),
    )
    second = _record(
        vault,
        source,
        "11:00:00",
        _extraction(
            source,
            "11:00:00",
            late,
            claim_id="claim:redis",
            value="redis",
            validity=OPEN,
            relation="depends-on",
        ),
    )
    believed = as_of([first, second], valid_at="2026-01-02T23:00:00Z")
    assert sorted(_values(believed)) == ["postgres", "redis"]


def test_conflicting_claims_observed_at_one_instant_refuse_by_name(tmp_path):
    """No order in the evidence means no winner, not a guessed winner."""
    early = "Session 5696e5d8 is working on fix-pip."
    late = "Session 5696e5d8 is working on agenticos."
    source = _source(("03:04:05", f"{early}\n{late}"))
    vault = _vault(tmp_path, source)
    first = _record(
        vault,
        source,
        "03:04:05",
        _extraction(
            source, "03:04:05", early, claim_id="claim:a", value="fix-pip", validity=OPEN
        ),
    )
    second = _record(
        vault,
        source,
        "03:04:05",
        _extraction(
            source,
            "03:04:05",
            late,
            claim_id="claim:b",
            value="agenticos",
            validity=OPEN,
        ),
    )
    assert first["observed_at"] == second["observed_at"] == "2026-01-02T03:04:05Z"
    with pytest.raises(BitemporalRefusal, match="bitemporal_ambiguous_observation"):
        as_of([first, second], valid_at="2026-01-02T23:00:00Z")


def test_a_claim_declaring_no_validity_is_valid_from_its_own_observation(tmp_path):
    """The backward-compatible reading: no new fields, no migration, still works."""
    records = _two_states(tmp_path)
    first = history([records[0]])[0]
    assert records[0]["validity"] == OPEN
    assert first.valid_from == "2026-01-02T03:04:05Z"
    assert as_of([records[0]], valid_at="2026-01-02T03:04:04Z") == ()
    assert len(as_of([records[0]], valid_at="2026-01-02T03:04:05Z")) == 1


def test_an_unreadable_time_refuses_by_name_instead_of_being_guessed(tmp_path):
    records = _two_states(tmp_path)
    with pytest.raises(BitemporalRefusal, match="bitemporal_time_invalid"):
        as_of(records, valid_at="yesterday")


def test_every_frozen_relation_is_classified_as_single_or_multi_valued():
    """A relation added later must trip this, not silently join the invalidating set."""
    assert unclassified_relations() == frozenset()
    assert SINGLE_VALUED_RELATIONS | MULTI_VALUED_RELATIONS == RELATIONS
    assert not SINGLE_VALUED_RELATIONS & MULTI_VALUED_RELATIONS


def test_every_supported_python_reads_a_one_digit_fraction():
    """`fromisoformat` took only three or six fractional digits before 3.11.

    This project supports 3.10, where `…:00.5Z` raises `Invalid isoformat
    string`. The test above passed locally on 3.12 and failed on every 3.10 job
    in CI on 2026-08-30. This one states the rule itself, so it fails on any
    interpreter where the padding is wrong rather than only on the old ones.
    """
    from claims import _six_digit_fraction

    padded = _six_digit_fraction("2026-08-19T00:00:00.5+00:00")

    assert padded == "2026-08-19T00:00:00.500000+00:00"
    assert datetime.fromisoformat(padded).microsecond == 500_000


def test_padding_changes_nothing_that_was_already_readable():
    from claims import _six_digit_fraction

    assert (
        _six_digit_fraction("2026-08-19T00:00:00.123456+00:00")
        == "2026-08-19T00:00:00.123456+00:00"
    )
    assert _six_digit_fraction("2026-08-19T00:00:00+00:00") == "2026-08-19T00:00:00+00:00"


def test_more_than_six_digits_is_left_to_be_refused():
    """Truncating would silently change the instant; refusing names the problem."""
    from claims import _six_digit_fraction

    text = "2026-08-19T00:00:00.1234567+00:00"

    assert _six_digit_fraction(text) == text


def test_a_sub_second_validity_interval_is_not_refused_as_inverted():
    """Canonical times keep fractional seconds, and `.` sorts before `Z`.

    Comparing them as text made `…:00.5Z` look earlier than `…:00Z`, so a real
    half-second interval was rejected as empty. Ordering is by instant now.
    """
    interval = _validate_interval(
        {"from": "2026-08-19T00:00:00Z", "to": "2026-08-19T00:00:00.5Z"}
    )
    assert interval["from"] == "2026-08-19T00:00:00Z"
    assert interval["to"] == "2026-08-19T00:00:00.500000Z"
