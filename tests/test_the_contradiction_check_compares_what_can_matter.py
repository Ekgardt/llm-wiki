"""The contradiction check compares only what can change its answer.

Relation-only matches filled the candidate list and a silent cut at 50 could hide
the claim that mattered; the compile asked model evaluators where their answer
could not decide; instants were compared as text. See
docs/research/2026-09-25-the-contradiction-check-compares-what-can-matter.md.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import compile_memory
from claims import ClaimIndex, NormalizedClaim
from contradiction_pipeline import intervals_overlap
from reliable_memory import canonical_json_bytes, sha256_bytes

from tests.test_claims import ledger_page
from tests.test_compile_transactions import (
    _claim_record,
    _daily,
    vault,  # noqa: F401 - the fixture is used by name
)
from tests.test_contradiction_pipeline import claim, indexed

SEMANTIC_KEYS = ("subject", "relation", "value", "qualifiers", "validity")


def _record(root: Path, number: int, subject: str) -> dict[str, object]:
    record = _claim_record(
        root, claim_id=f"{subject}-{number}", value=f"v{number}",
        text="A durable exact-byte observation.", authority="user",
    )
    record["subject"] = subject
    record["fingerprint"] = sha256_bytes(canonical_json_bytes({key: record[key] for key in SEMANTIC_KEYS}))
    return record


def _index(root: Path, state_root: Path, records) -> ClaimIndex:
    for number, record in enumerate(records):
        (root / f"knowledge/notes/page-{number:03d}.md").write_bytes(ledger_page(record))
    index = ClaimIndex(state_root, vault=root)
    index.rebuild()
    return index


def test_every_claim_about_the_subject_is_a_candidate_and_no_other(vault) -> None:  # noqa: F811 - the fixture
    root, state_root = vault
    _daily(root)
    same = [_record(root, number, "project") for number in range(60)]
    other = [_record(root, number, "elsewhere") for number in range(5)]
    index = _index(root, state_root, same + other)

    found = index.candidates(NormalizedClaim(_record(root, 99, "project")))

    assert ({item.claim.record["subject"] for item in found}, len(found)) == ({"project"}, 60)


def test_the_compile_asks_no_model_for_a_verdict_it_cannot_use(monkeypatch) -> None:
    import llm_client

    asked = []
    monkeypatch.setattr(llm_client, "call_candidate", lambda *args, **kwargs: asked.append(args))
    plan = SimpleNamespace(claim_index=None, coordinator=None)
    pipeline = compile_memory._ApplyPlan._pipeline(plan, "knowledge/notes/page.md")

    result = pipeline.assess(claim("red"), candidates=[indexed("blue", relation="owned-by")], commit=False)

    assert (pipeline.evaluators, result.recommendation, asked) == ((), "quarantine", [])


def test_an_interval_that_starts_half_a_second_after_another_ends_does_not_overlap() -> None:
    ended = {"from": "2026-08-01", "to": "2026-09-01T00:00:00Z"}
    started = {"from": "2026-09-01T00:00:00.500000Z", "to": None}

    assert intervals_overlap(ended, started) is False
    assert intervals_overlap(ended, {"from": "2026-08-31T23:59:59.500000Z", "to": None}) is True
