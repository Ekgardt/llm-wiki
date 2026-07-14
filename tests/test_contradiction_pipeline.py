from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from claims import IndexedClaim, NormalizedClaim
from reliable_memory import canonical_json_bytes, sha256_bytes


def claim(
    value: object = "blue",
    *,
    claim_id: str = "new",
    subject: str = "project",
    relation: str = "has-state",
    start: str | None = "2026-01-01",
    end: str | None = None,
    authority: str = "user",
    confidence: str = "high",
    lifecycle: str = "active",
) -> NormalizedClaim:
    typed = {"type": "string", "value": value}
    semantic = {
        "subject": subject,
        "relation": relation,
        "value": typed,
        "qualifiers": [],
        "validity": {"from": start, "to": end},
    }
    text = f"{subject} {relation} {value}"
    evidence_hash = sha256_bytes(text.encode())
    record = {
        "schema_version": "claim/v1",
        "id": claim_id,
        "fingerprint": sha256_bytes(canonical_json_bytes(semantic)),
        "text": text,
        **semantic,
        "observed_at": "2026-01-01T12:00:00Z",
        "lifecycle": lifecycle,
        "confidence": confidence,
        "authority": authority,
        "evidence": {
            "reference": (
                "daily:2026-01-01 sha256:" + "a" * 64
                + " block:12:00:00 bytes:0-1"
            ),
            "sha256": evidence_hash,
            "text": text,
        },
        "links": [],
        "extractor_version": "test/v1",
    }
    return NormalizedClaim(record)


def indexed(value: object = "red", **kwargs: object) -> IndexedClaim:
    return IndexedClaim(
        "knowledge/notes/existing.md",
        claim(value, claim_id="existing", **kwargs),
    )


def test_equal_claim_is_deterministically_equivalent():
    from contradiction_pipeline import ContradictionPipeline

    new = claim()
    result = ContradictionPipeline().assess(
        new, candidates=[IndexedClaim("knowledge/notes/existing.md", new)]
    )

    assert result.contradiction_class == "equivalent"
    assert result.recommendation == "keep-both"
    assert result.candidate_path is None
    assert result.canonical()["recommendation"] == "keep-both"


def test_disjoint_intervals_are_not_a_contradiction():
    from contradiction_pipeline import ContradictionPipeline

    result = ContradictionPipeline().assess(
        claim("red", start="2026-02-01"),
        candidates=[indexed("blue", start="2026-01-01", end="2026-02-01")],
    )

    assert result.contradiction_class == "temporal-distinct"
    assert result.recommendation == "keep-both"


def test_functional_conflict_supersedes_only_with_overlap_authority_and_ledger():
    from contradiction_pipeline import ContradictionPipeline

    result = ContradictionPipeline().assess(
        claim("red", authority="user"),
        candidates=[indexed("blue", authority="web")],
    )
    assert result.contradiction_class == "contradiction"
    assert result.recommendation == "supersede"
    assert result.lifecycle_mutations == (("knowledge/notes/existing.md", "existing"),)

    ledgerless = IndexedClaim(
        "knowledge/notes/existing.md", indexed("blue").claim, ledger_backed=False
    )
    blocked = ContradictionPipeline().assess(claim("red"), candidates=[ledgerless])
    assert blocked.recommendation == "quarantine"
    assert blocked.lifecycle_mutations == ()


def test_lower_authority_conflict_is_quarantined():
    from contradiction_pipeline import ContradictionPipeline

    result = ContradictionPipeline().assess(
        claim("red", authority="inferred"),
        candidates=[indexed("blue", authority="user")],
    )
    assert result.recommendation == "quarantine"


def test_inactive_existing_claim_is_never_superseded():
    from contradiction_pipeline import ContradictionPipeline

    result = ContradictionPipeline(evaluators=()).assess(
        claim("red"), candidates=[indexed("blue", lifecycle="superseded")]
    )
    assert result.recommendation == "keep-both"
    assert result.lifecycle_mutations == ()


def test_semantic_conflict_is_always_quarantined_even_after_gate():
    from contradiction_pipeline import ContradictionPipeline, Evaluation

    calls = []

    def evaluator(new, old, *, critique, prior_label=None):
        calls.append((critique, prior_label))
        return Evaluation("contradiction", "high", True, "semantic-a")

    result = ContradictionPipeline(evaluators=[evaluator, evaluator]).assess(
        claim(relation="uses"),
        candidates=[indexed("red", relation="depends-on")],
        benchmark_gate=True,
    )

    assert result.recommendation == "quarantine"
    assert result.lifecycle_mutations == ()
    assert calls == [(False, None), (True, "contradiction")]


def test_semantic_compatible_publishes_only_after_calibration_gate():
    from contradiction_pipeline import ContradictionPipeline, Evaluation

    def evaluator(new, old, *, critique, prior_label=None):
        return Evaluation("compatible", "high", True, "semantic")

    pipeline = ContradictionPipeline(evaluators=[evaluator])
    before = pipeline.assess(
        claim(relation="uses"), candidates=[indexed(relation="depends-on")]
    )
    after = pipeline.assess(
        claim(relation="uses"),
        candidates=[indexed(relation="depends-on")],
        benchmark_gate=True,
    )

    assert before.recommendation == "quarantine"
    assert after.recommendation == "keep-both"


@pytest.mark.parametrize(
    "first,second",
    [
        (("contradiction", "high", True), ("compatible", "high", True)),
        (("contradiction", "low", True), ("contradiction", "low", True)),
        (("contradiction", "high", False), ("contradiction", "high", False)),
        (None, ("contradiction", "high", True)),
    ],
)
def test_semantic_disagreement_malformed_unsupported_or_low_confidence_quarantines(
    first, second
):
    from contradiction_pipeline import ContradictionPipeline, Evaluation

    outcomes = iter((first, second))

    def evaluator(new, old, *, critique, prior_label=None):
        item = next(outcomes)
        if item is None:
            return {"bad": "shape"}
        return Evaluation(*item, evaluator="semantic")

    result = ContradictionPipeline(evaluators=[evaluator]).assess(
        claim(relation="uses"), candidates=[indexed(relation="depends-on")]
    )
    assert result.recommendation == "quarantine"


def test_page_superseded_only_when_all_substantive_claims_are_superseded():
    from contradiction_pipeline import page_is_superseded

    active = claim().record
    superseded = {**active, "id": "old", "lifecycle": "superseded"}
    assert not page_is_superseded([superseded, active])
    assert page_is_superseded([superseded, superseded])
    assert not page_is_superseded([])


def test_candidate_markdown_is_schema_valid_and_written_by_transaction(tmp_path):
    from contradiction_pipeline import ContradictionPipeline
    from markdown_transaction import MarkdownCoordinator
    from reliable_memory import validate_schema

    vault = tmp_path / "vault"
    state = tmp_path / "state"
    (vault / "knowledge/inbox/claims").mkdir(parents=True)
    state.mkdir()
    pipeline = ContradictionPipeline(
        vault=vault,
        coordinator=MarkdownCoordinator(vault, state),
        source_page="knowledge/notes/source.md",
    )

    result = pipeline.assess(
        claim("red", authority="inferred"),
        candidates=[indexed("blue", authority="user")],
    )

    assert result.candidate_path == f"knowledge/inbox/claims/{claim('red', authority='inferred').record['fingerprint']}.md"
    content = (vault / result.candidate_path).read_text(encoding="utf-8")
    assert content.startswith("---\ntype: claim-candidate\nstatus: quarantined\n---\n")
    encoded = content.split("```json\n", 1)[1].split("\n```", 1)[0]
    record = json.loads(encoded)
    assert canonical_json_bytes(record).decode() == encoded
    validate_schema(
        record,
        Path(__file__).resolve().parent.parent
        / "scripts/schemas/claim-candidate-v1.json",
    )
    assert record["claim"]["lifecycle"] == "quarantined"


def test_deterministic_lifecycle_edit_is_committed_without_candidate(tmp_path):
    from contradiction_pipeline import ContradictionPipeline
    from markdown_transaction import MarkdownCoordinator

    vault = tmp_path / "vault"
    state = tmp_path / "state"
    (vault / "knowledge/inbox/claims").mkdir(parents=True)
    (vault / "knowledge/notes").mkdir()
    state.mkdir()
    old = indexed("blue", authority="web")
    ledger = {"schema_version": "claim-ledger/v1", "claims": [old.claim.record]}
    page = vault / old.page
    page.write_bytes(
        b"---\ntype: concept\n---\n# Existing\n\n## Claims\n```json\n"
        + canonical_json_bytes(ledger)
        + b"\n```\n"
    )
    pipeline = ContradictionPipeline(
        vault=vault,
        coordinator=MarkdownCoordinator(vault, state),
        source_page="knowledge/notes/source.md",
    )

    result = pipeline.assess(claim("red", authority="user"), candidates=[old])

    assert result.recommendation == "supersede"
    assert result.candidate_path is None
    updated = page.read_text(encoding="utf-8")
    assert '"lifecycle":"superseded"' in updated
    assert not any((vault / "knowledge/inbox/claims").iterdir())


def test_assess_raw_runs_split_extract_verify_normalize_in_order():
    from contradiction_pipeline import ContradictionPipeline

    class Claims:
        calls = []

        def split_blocks(self, source):
            self.calls.append("split")
            return ("block",)

        def extract(self, block, extraction):
            self.calls.append("extract")
            return ("claim",)

        def verify_literal(self, raw):
            self.calls.append("verify")
            return "verified"

        def normalize(self, verified):
            self.calls.append("normalize")
            return claim()

    pipeline = ContradictionPipeline(claim_pipeline=Claims())
    results = pipeline.assess_raw(b"source", {"schema_version": "x"})

    assert len(results) == 1
    assert pipeline.claim_pipeline.calls == ["split", "extract", "verify", "normalize"]
