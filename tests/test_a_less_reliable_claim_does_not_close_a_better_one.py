"""A later claim of lesser standing leaves the earlier one open.

Newest-wins was the rule: any later claim with a different value for the same
(subject, relation, qualifiers) key ended the earlier one. The field measured
on 2026-09-07 says recency alone ties the model and loses off freshness
questions (arXiv:2606.01435), that explicit supersession beats passive recency
(arXiv:2605.20926), and that a reliability-weighted update scores 100 against
67 for last-writer-wins (arXiv:2606.22030).

So a claim inferred by a model does not close one the user stated. Both stay
open and the reader sees the conflict. A later claim of equal or higher
standing closes as before — the write-time closing that took stale-fact
errors to ~0% in arXiv:2606.26511 is kept.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from bitemporal_claims import as_of, history, reliability  # noqa: E402


def _claim(
    claim_id: str,
    value: str,
    observed_at: str,
    *,
    authority: str = "user",
    confidence: str = "high",
) -> dict:
    return {
        "id": claim_id,
        "subject": "session 5696e5d8",
        "relation": "has-state",
        "value": {"type": "entity", "value": value},
        "qualifiers": [],
        "observed_at": observed_at,
        "validity": {"from": None, "to": None},
        "lifecycle": "active",
        "authority": authority,
        "confidence": confidence,
    }


EARLY = "2026-01-02T03:00:00Z"
LATE = "2026-01-02T11:00:00Z"
LATER = "2026-01-02T15:00:00Z"


def _values(beliefs) -> list[str]:
    return sorted(item.record["value"]["value"] for item in beliefs)


def test_an_inferred_claim_does_not_close_what_the_user_said():
    records = [
        _claim("c1", "fix-pip", EARLY),
        _claim("c2", "agenticos", LATE, authority="inferred", confidence="low"),
    ]

    first, second = history(records)

    assert first.expired_at is None
    assert first.invalid_at is None
    assert _values(as_of(records, valid_at=LATER)) == ["agenticos", "fix-pip"]


def test_a_user_claim_closes_an_earlier_model_claim():
    records = [
        _claim("c1", "fix-pip", EARLY, authority="ai-derived", confidence="high"),
        _claim("c2", "agenticos", LATE),
    ]

    first, second = history(records)

    assert first.expired_at == LATE
    assert _values(as_of(records, valid_at=LATER)) == ["agenticos"]


def test_equal_standing_still_means_newest_wins():
    records = [_claim("c1", "fix-pip", EARLY), _claim("c2", "agenticos", LATE)]

    first, second = history(records)

    assert first.expired_at == LATE
    assert second.expired_at is None


def test_a_reliable_third_claim_closes_both_open_ones():
    records = [
        _claim("c1", "fix-pip", EARLY),
        _claim("c2", "agenticos", LATE, authority="inferred"),
        _claim("c3", "llm-wiki", LATER),
    ]

    first, second, third = history(records)

    assert first.expired_at == LATER
    assert second.expired_at == LATER
    assert third.expired_at is None
    assert _values(as_of(records, valid_at="2026-01-02T16:00:00Z")) == ["llm-wiki"]


def test_confidence_breaks_a_tie_on_authority():
    records = [
        _claim("c1", "fix-pip", EARLY, confidence="high"),
        _claim("c2", "agenticos", LATE, confidence="low"),
    ]

    first, _second = history(records)

    assert first.expired_at is None


def test_reliability_reads_authority_before_confidence():
    assert reliability(_claim("x", "v", EARLY, authority="web", confidence="low")) > reliability(
        _claim("y", "v", EARLY, authority="ai-derived", confidence="high")
    )
    assert reliability({"authority": "nonsense", "confidence": "nonsense"}) == (0, 0)
