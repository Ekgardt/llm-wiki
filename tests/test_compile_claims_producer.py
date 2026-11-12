"""The producer end of the claims subsystem.

Every claim record downstream of the compile plan was proved years ago: the
ledger writer, `ClaimIndex`, the contradiction policy, all of it. What was never
proved is that anything can *make* one. It could not: the draft prompt never
mentioned claims, and the schema it did carry asked a language model for a
sha256 fingerprint, a literal hash and a byte span into a file it only ever sees
as text. Measured against the real `claude` provider on this vault's own
2026-08-20 daily, the model volunteered a claim anyway and fabricated all three
(`"fingerprint": "a1b2c3d4e5f6a1b2..."`), and the whole compile plan died with
`claim semantic fields are not canonical`.

These tests hold the producer to the split the fix draws: the model supplies the
sentence's meaning, this process supplies every fact about bytes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from reliable_memory import canonical_json_bytes, sha256_bytes

from tests.compile_claims_producer_helpers import (
    DATE,
    QUOTE,
    SECOND_QUOTE,
    _candidate,
    _daily,
    _draft,
    _evidence,
    _operation,
    vault,  # noqa: F401
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


# --- 1. the prompt --------------------------------------------------------


def test_the_draft_prompt_names_claims_and_what_the_model_must_supply(vault):  # noqa: F811
    """A field the prompt never mentions is a field nobody asked for.

    `claims` existed only as one optional key inside 6 KB of schema JSON pasted
    into the system prompt. Measured on this vault's 2026-08-20 daily, the word
    "claim" appeared zero times in the whole draft prompt.
    """
    root, _state_root = vault
    from llm_wiki import (
        compile_pipeline_prompts,
        compile_pipeline_sources,
    )

    daily = _daily(root)
    inputs = compile_pipeline_sources.snapshot_compile_inputs([daily])
    preamble = compile_pipeline_prompts.draft_prompt(inputs).split("IMMUTABLE SOURCES")[0]

    assert "claims" in preamble
    assert "evidence_index" in preamble


def test_the_draft_never_asks_the_model_for_a_fact_about_bytes(vault):  # noqa: F811
    """Identity and provenance are computed here, so they are not requested."""
    from llm_wiki import (
        compile_pipeline_base,
    )

    candidate = compile_pipeline_base.CLAIM_CANDIDATE_SCHEMA["properties"]
    assert set(candidate) == {
        "evidence_index",
        "subject",
        "relation",
        "value",
        "qualifiers",
    }
    for derived in ("fingerprint", "evidence", "observed_at", "id", "authority"):
        assert derived not in candidate


# --- 2. derivation --------------------------------------------------------


_SEMANTIC_KEYS = ("subject", "relation", "value", "qualifiers", "validity")


def _derived_facts(record: dict, daily: Path) -> dict[str, object]:
    """Every field the compiler owns, read back off the record it wrote."""
    semantic = {key: record[key] for key in _SEMANTIC_KEYS}
    reference = str(record["evidence"]["reference"])
    return {
        "fingerprint": record["fingerprint"] == sha256_bytes(canonical_json_bytes(semantic)),
        "text": record["text"],
        "evidence_text": record["evidence"]["text"],
        "evidence_sha256": record["evidence"]["sha256"],
        "observed_at": record["observed_at"],
        "lifecycle": record["lifecycle"],
        "authority": record["authority"],
        "reference_prefix": reference.split(" bytes:")[0],
    }


def test_a_drafted_candidate_becomes_a_record_bound_to_the_source_bytes(vault):  # noqa: F811
    root, _state_root = vault
    from claims import validate_claim_record
    from llm_wiki import (
        compile_pipeline_operations,
        compile_pipeline_prompts,
        compile_pipeline_sources,
    )

    daily = _daily(root)
    inputs = compile_pipeline_sources.snapshot_compile_inputs([daily])
    operations = compile_pipeline_prompts.draft_operations(_draft([_operation([_candidate()])]))

    derived = compile_pipeline_operations.with_derived_claims(operations, inputs)

    records = derived[0]["claims"]
    validate_claim_record(records[0])
    assert _derived_facts(records[0], daily) == {
        "fingerprint": True,
        "text": QUOTE,
        "evidence_text": QUOTE,
        "evidence_sha256": sha256_bytes(QUOTE.encode("utf-8")),
        "observed_at": f"{DATE}T10:00:00Z",
        "lifecycle": "active",
        "authority": "ai-derived",
        "reference_prefix": (
            f"daily:{DATE} sha256:{sha256_bytes(daily.read_bytes())} block:10:00:00"
        ),
    }


def test_the_derived_record_survives_the_plan_validator(vault):  # noqa: F811
    root, _state_root = vault
    from llm_wiki import (
        compile_pipeline_operations,
        compile_pipeline_prompts,
        compile_pipeline_sources,
    )

    daily = _daily(root)
    inputs = compile_pipeline_sources.snapshot_compile_inputs([daily])
    operations = compile_pipeline_operations.with_derived_claims(
        compile_pipeline_prompts.draft_operations(_draft([_operation([_candidate()])])), inputs
    )

    plan = compile_pipeline_prompts.normalize_plan(operations, inputs)

    content = json.loads(str(plan["operations"][0]["content"]))
    assert len(content["claims"]) == 1


def test_the_reviewer_is_not_billed_for_records_it_cannot_change(vault):  # noqa: F811
    """A derived claim has nothing in it for a critic to improve.

    The reviewer decides whether an operation is specific, durable and exactly
    evidenced. Its claims come from bytes this process already verified, and a
    full `claim/v1` record is about 700 characters — on a long day that shrinks
    the review batches and buys extra provider calls to re-read what cannot
    change.
    """
    root, _state_root = vault
    from llm_wiki import (
        compile_pipeline_operations,
        compile_pipeline_prompts,
        compile_pipeline_sources,
    )

    daily = _daily(root)
    inputs = compile_pipeline_sources.snapshot_compile_inputs([daily])
    operations = compile_pipeline_operations.with_derived_claims(
        compile_pipeline_prompts.draft_operations(_draft([_operation([_candidate()])])), inputs
    )

    prompt = compile_pipeline_prompts.critique_prompt(inputs, operations)

    assert operations[0]["claims"], "the plan still carries the derived record"
    assert "fingerprint" not in prompt


# --- 3. a bad claim costs the claim, not the page -------------------------


_FABRICATED = {
    "schema_version": "claim/v1",
    "id": "claim-bounded-lease-expiry",
    "fingerprint": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
    "text": QUOTE,
    "subject": "maintenance lease",
    "relation": "ends-at",
    "value": {"type": "string", "value": "30 seconds"},
    "qualifiers": [],
    "validity": {"from": DATE, "to": None},
    "observed_at": f"{DATE}T10:00:00Z",
    "lifecycle": "active",
    "confidence": "high",
    "authority": "user",
    "evidence": {
        "reference": f"daily:{DATE} sha256:{'0' * 64} block:deadbeef bytes:0-46",
        "sha256": "0" * 64,
        "text": QUOTE,
    },
    "links": [],
    "extractor_version": "compile-draft/v3",
}


def test_a_fabricated_claim_costs_the_claim_and_never_the_page(vault):  # noqa: F811
    """The exact shape the real `claude` provider returned, unasked.

    Before the fix this reached `normalize_plan` and killed the whole plan with
    `claim semantic fields are not canonical` — two good pages lost to one
    optional field the model was never asked for.
    """
    root, _state_root = vault
    from llm_wiki import (
        compile_pipeline_operations,
        compile_pipeline_prompts,
        compile_pipeline_sources,
    )

    daily = _daily(root)
    inputs = compile_pipeline_sources.snapshot_compile_inputs([daily])

    operations = compile_pipeline_prompts.draft_operations(_draft([_operation([dict(_FABRICATED)])]))
    operations = compile_pipeline_operations.with_derived_claims(operations, inputs)
    plan = compile_pipeline_prompts.normalize_plan(operations, inputs)

    assert len(plan["operations"]) == 1
    assert "claims" not in json.loads(str(plan["operations"][0]["content"]))


def test_claims_of_the_wrong_shape_entirely_still_cost_only_the_claims(vault):  # noqa: F811
    root, _state_root = vault
    from llm_wiki import (
        compile_pipeline_operations,
        compile_pipeline_prompts,
        compile_pipeline_sources,
    )

    daily = _daily(root)
    inputs = compile_pipeline_sources.snapshot_compile_inputs([daily])
    operation = _operation()
    operation["claims"] = {"not": "an array"}

    operations = compile_pipeline_prompts.draft_operations(_draft([operation]))
    plan = compile_pipeline_prompts.normalize_plan(
        compile_pipeline_operations.with_derived_claims(operations, inputs), inputs
    )

    assert "claims" not in json.loads(str(plan["operations"][0]["content"]))


def test_a_candidate_pointing_past_the_evidence_costs_only_itself(vault):  # noqa: F811
    root, _state_root = vault
    from llm_wiki import (
        compile_pipeline_operations,
        compile_pipeline_prompts,
        compile_pipeline_sources,
    )

    daily = _daily(root)
    inputs = compile_pipeline_sources.snapshot_compile_inputs([daily])
    operations = compile_pipeline_prompts.draft_operations(
        _draft([_operation([_candidate(evidence_index=7), _candidate()])])
    )

    derived = compile_pipeline_operations.with_derived_claims(operations, inputs)

    assert len(derived[0]["claims"]) == 1
    assert derived[0]["claims"][0]["subject"] == "maintenance lease"


def test_two_candidates_with_the_same_meaning_yield_one_record(vault):  # noqa: F811
    root, _state_root = vault
    from llm_wiki import (
        compile_pipeline_operations,
        compile_pipeline_prompts,
        compile_pipeline_sources,
    )

    daily = _daily(root)
    inputs = compile_pipeline_sources.snapshot_compile_inputs([daily])
    operations = compile_pipeline_prompts.draft_operations(
        _draft([_operation([_candidate(), _candidate()])])
    )

    derived = compile_pipeline_operations.with_derived_claims(operations, inputs)

    assert len(derived[0]["claims"]) == 1


# --- 4. a long day is compiled in parts ------------------------------------


def _oversized_daily(root: Path) -> Path:
    """One day long enough that the compiler takes it in parts, as real days are."""
    from evidence_resolver import MAX_DAILY_PART_BYTES

    filler = "\n".join(
        f"<!-- llm-wiki-operation:{minute:064d} -->\n"
        f"## [09:{minute:02d}:00] session-end | manual\nRoutine heartbeat {minute}."
        for minute in range(60)
    )
    body = (filler + "\n") * (MAX_DAILY_PART_BYTES // len(filler) + 2)
    path = root / "knowledge/daily" / f"{DATE}.md"
    path.write_text(
        f"# Daily Session Memory — {DATE}\n\n"
        f"{body}"
        "## [11:00:00] session-end | manual\n"
        f"{SECOND_QUOTE}\n",
        encoding="utf-8",
    )
    return path


def test_a_claim_binds_to_the_part_of_a_split_day_that_holds_its_quote(vault):  # noqa: F811
    """The same defect fixed for quoted evidence on 2026-08-24, still open here.

    `_daily_for_evidence` asked for the *sole* snapshot of a date, and a long day
    carries one snapshot per part under one logical path. Every real daily of
    this vault is far past the 16 KiB part bound, so no claim of a real day could
    ever have bound — the reference resolved against no snapshot at all.
    """
    root, _state_root = vault
    from llm_wiki import (
        compile_pipeline_operations,
        compile_pipeline_prompts,
        compile_pipeline_sources,
    )

    daily = _oversized_daily(root)
    inputs = compile_pipeline_sources.snapshot_compile_inputs([daily])
    operation = _operation([_candidate(evidence_index=0)])
    operation["evidence"] = [_evidence(SECOND_QUOTE, "11:00:00")]
    operation["slug"] = "nightly-pass-hour"

    derived = compile_pipeline_operations.with_derived_claims(
        compile_pipeline_prompts.draft_operations(_draft([operation])), inputs
    )
    compile_pipeline_prompts.normalize_plan(derived, inputs)

    assert len(inputs.dailies) > 1
    assert [item["evidence"]["text"] for item in derived[0]["claims"]] == [SECOND_QUOTE]


# --- 5. end to end: the ledger the index reads back ------------------------


def test_a_drafted_claim_reaches_the_page_ledger_and_the_claim_index(vault):  # noqa: F811
    root, state_root = vault
    from claims import ClaimIndex
    from llm_wiki import (
        compile_pipeline_apply,
        compile_pipeline_operations,
        compile_pipeline_prompts,
        compile_pipeline_sources,
    )
    from markdown_transaction import MarkdownCoordinator

    daily = _daily(root)
    inputs = compile_pipeline_sources.snapshot_compile_inputs([daily])
    operations = compile_pipeline_operations.with_derived_claims(
        compile_pipeline_prompts.draft_operations(_draft([_operation([_candidate()])])), inputs
    )
    plan = compile_pipeline_prompts.normalize_plan(operations, inputs)

    compile_pipeline_apply.apply_compile_plan(
        inputs,
        plan,
        action_key="c" * 64,
        trigger="manual",
        coordinator=MarkdownCoordinator(root, state_root),
        completed_at="2026-08-28T12:00:00Z",
    )

    page = root / "knowledge/notes/bounded-lease-expiry.md"
    assert "## Claims" in page.read_text(encoding="utf-8")

    index = ClaimIndex(state_root, vault=root)
    index.rebuild()
    active = index.active_records(subject="maintenance lease")
    assert len(active) == 1
    assert active[0].claim.record["evidence"]["text"] == QUOTE
