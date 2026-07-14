from __future__ import annotations

import json
import sys
from pathlib import Path
from types import MappingProxyType

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
    qualifiers: list[dict[str, object]] | None = None,
) -> NormalizedClaim:
    typed = {"type": "string", "value": value}
    semantic = {
        "subject": subject,
        "relation": relation,
        "value": typed,
        "qualifiers": qualifiers or [],
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


def test_qualifier_scope_must_match_before_deterministic_supersession():
    from contradiction_pipeline import ContradictionPipeline

    broad = [{"key": "region", "value": {"type": "string", "value": "global"}}]
    narrow = [{"key": "region", "value": {"type": "string", "value": "eu"}}]
    result = ContradictionPipeline(evaluators=()).assess(
        claim("red", qualifiers=narrow),
        candidates=[indexed("blue", authority="web", qualifiers=broad)],
    )

    assert result.recommendation == "quarantine"
    assert result.lifecycle_mutations == ()


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


def test_provider_semantic_evaluation_falls_through_and_records_bounded_failure_lineage(
    monkeypatch,
):
    import contradiction_pipeline
    from llm_client import LLMResult, ProviderDescriptor

    def descriptor(name, index):
        return ProviderDescriptor(
            provider=name,
            model=f"{name}-model",
            capabilities=MappingProxyType(
                {"structured_output": "native", "max_tokens_enforced": True}
            ),
            inference_settings=MappingProxyType({"max_tokens": 800}),
            candidate_index=index,
            fallback_from=(),
        )

    unavailable, oversized, working = (
        descriptor("one", 0),
        descriptor("two", 1),
        descriptor("three", 2),
    )
    monkeypatch.setattr(
        contradiction_pipeline,
        "provider_candidates",
        lambda forced, max_tokens: [unavailable, oversized, working],
    )
    monkeypatch.setattr(
        contradiction_pipeline,
        "probe_candidate",
        lambda item: item is not unavailable,
    )

    def call(item, *args, **kwargs):
        text = (
            "x" * (contradiction_pipeline.MAX_SEMANTIC_OUTPUT_BYTES + 1)
            if item is oversized
            else '{"label":"compatible","confidence":"high","supported":true}'
        )
        return LLMResult(item, text, True, None, "native")

    monkeypatch.setattr(contradiction_pipeline, "call_candidate", call)
    result = contradiction_pipeline.ContradictionPipeline().assess(
        claim(relation="uses"),
        candidates=[indexed(relation="depends-on")],
        benchmark_gate=True,
    )

    assert len(result.evaluations) == 2, result.canonical()
    assert result.recommendation == "keep-both"
    failures = [item for item in result.evaluation_lineage if item["failure"]]
    assert any(item["stage"] == "primary.probe" for item in failures)
    assert any(item["failure"] == "output_too_large" for item in failures)
    assert all("provider" in item["descriptor"] for item in result.evaluation_lineage)


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
    (vault / "knowledge").mkdir(parents=True)
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

    assert result.candidate_path.startswith("knowledge/inbox/claims/")
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


def test_candidate_identity_does_not_collide_on_equal_fingerprints(tmp_path):
    from contradiction_pipeline import ContradictionPipeline
    from markdown_transaction import MarkdownCoordinator

    vault = tmp_path / "vault"
    state = tmp_path / "state"
    (vault / "knowledge").mkdir(parents=True)
    state.mkdir()
    pipeline = ContradictionPipeline(
        vault=vault,
        coordinator=MarkdownCoordinator(vault, state),
        source_page="knowledge/notes/source.md",
    )

    first = pipeline.assess(
        claim("red", claim_id="one", authority="inferred"),
        candidates=[indexed("blue", authority="user")],
    )
    second = pipeline.assess(
        claim("red", claim_id="two", authority="inferred"),
        candidates=[indexed("blue", authority="user")],
    )

    assert first.candidate_path != second.candidate_path
    assert len(list((vault / "knowledge/inbox/claims").glob("*.md"))) == 2


def test_candidate_commit_is_idempotent_and_rejects_non_directory_parent(tmp_path):
    from contradiction_pipeline import ContradictionPipeline
    from markdown_transaction import MarkdownCoordinator

    vault = tmp_path / "vault"
    state = tmp_path / "state"
    (vault / "knowledge").mkdir(parents=True)
    state.mkdir()
    pipeline = ContradictionPipeline(
        vault=vault,
        coordinator=MarkdownCoordinator(vault, state),
        source_page="knowledge/notes/source.md",
    )
    new = claim("red", authority="inferred")
    old = indexed("blue", authority="user")

    first = pipeline.assess(new, candidates=[old])
    second = pipeline.assess(new, candidates=[old])

    assert first.candidate_path == second.candidate_path
    assert len(list((vault / "knowledge/inbox/claims").glob("*.md"))) == 1

    blocked_vault = tmp_path / "blocked"
    blocked_state = tmp_path / "blocked-state"
    (blocked_vault / "knowledge").mkdir(parents=True)
    (blocked_vault / "knowledge/inbox").write_text("not a directory", encoding="utf-8")
    blocked_state.mkdir()
    blocked = ContradictionPipeline(
        vault=blocked_vault,
        coordinator=MarkdownCoordinator(blocked_vault, blocked_state),
        source_page="knowledge/notes/source.md",
    )
    with pytest.raises((PermissionError, NotADirectoryError)):
        blocked.assess(new, candidates=[old])


def test_all_compatible_supersession_mutations_are_sorted_and_conflicts_quarantine():
    from contradiction_pipeline import ContradictionPipeline

    candidates = [
        IndexedClaim("knowledge/notes/z.md", claim("blue", claim_id="z", authority="web")),
        IndexedClaim("knowledge/notes/a.md", claim("green", claim_id="a", authority="web")),
    ]
    result = ContradictionPipeline(evaluators=()).assess(
        claim("red"), candidates=candidates
    )
    assert result.lifecycle_mutations == (
        ("knowledge/notes/a.md", "a"),
        ("knowledge/notes/z.md", "z"),
    )

    refinement = IndexedClaim(
        "knowledge/notes/r.md",
        claim("red", claim_id="r", start=None),
    )
    conflicted = ContradictionPipeline(evaluators=()).assess(
        claim("red"), candidates=[candidates[0], refinement]
    )
    assert conflicted.recommendation == "quarantine"
    assert conflicted.lifecycle_mutations == ()


def test_secondary_search_context_is_retrieval_only_and_cannot_mutate():
    from contradiction_pipeline import ContradictionPipeline

    pipeline = ContradictionPipeline(
        evaluators=(),
        secondary_search=lambda query, limit: [
            {"path": "knowledge/notes/legacy.md", "title": "Legacy", "snippet": "old value"}
        ],
    )
    result = pipeline.assess(claim("red"))

    assert result.recommendation == "quarantine"
    assert result.lifecycle_mutations == ()
    assert result.evidence[0]["retrieval_only"] is True


def test_deterministic_lifecycle_edit_is_committed_without_candidate(tmp_path):
    from contradiction_pipeline import ContradictionPipeline
    from markdown_transaction import MarkdownCoordinator

    vault = tmp_path / "vault"
    state = tmp_path / "state"
    (vault / "knowledge/inbox/claims").mkdir(parents=True)
    (vault / "knowledge/notes").mkdir()
    (vault / "knowledge/projects").mkdir()
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


def test_superseding_last_substantive_claim_updates_page_and_rebuilds_index(tmp_path):
    from claims import ClaimIndex
    from contradiction_pipeline import ContradictionPipeline
    from markdown_transaction import MarkdownCoordinator

    vault = tmp_path / "vault"
    state = tmp_path / "state"
    (vault / "knowledge/inbox/claims").mkdir(parents=True)
    (vault / "knowledge/notes").mkdir()
    (vault / "knowledge/projects").mkdir()
    state.mkdir()
    old = indexed("blue", authority="web")
    ledger = {"schema_version": "claim-ledger/v1", "claims": [old.claim.record]}
    page = vault / old.page
    page.write_bytes(
        b"---\ntype: concept\nstatus: active\n---\n# Existing\n\n## Claims\n```json\n"
        + canonical_json_bytes(ledger)
        + b"\n```\n"
    )
    index = ClaimIndex(state, vault=vault)
    index.rebuild()
    pipeline = ContradictionPipeline(
        vault=vault,
        coordinator=MarkdownCoordinator(vault, state),
        claim_index=index,
        source_page="knowledge/notes/replacement.md",
        evaluators=(),
    )

    pipeline.assess(claim("red"), candidates=[old])

    content = page.read_text(encoding="utf-8")
    assert "status: superseded" in content
    assert "superseded_by: [[replacement]]" in content
    assert index.candidates(claim("red")) == []


def test_lifecycle_write_uses_bounded_cas_and_rejects_concurrent_edit(tmp_path, monkeypatch):
    from contradiction_pipeline import ContradictionPipeline
    from markdown_transaction import MarkdownCoordinator, TransactionFailure

    vault = tmp_path / "vault"
    state = tmp_path / "state"
    (vault / "knowledge/inbox/claims").mkdir(parents=True)
    (vault / "knowledge/notes").mkdir()
    state.mkdir()
    old = indexed("blue", authority="web")
    page = vault / old.page
    page.write_bytes(
        b"---\ntype: concept\n---\n# Existing\n\n## Claims\n```json\n"
        + canonical_json_bytes({"schema_version": "claim-ledger/v1", "claims": [old.claim.record]})
        + b"\n```\n"
    )
    coordinator = MarkdownCoordinator(vault, state)
    original_prepare = coordinator.prepare

    def race(changes, **kwargs):
        page.write_bytes(page.read_bytes() + b"concurrent\n")
        return original_prepare(changes, **kwargs)

    monkeypatch.setattr(coordinator, "prepare", race)
    pipeline = ContradictionPipeline(
        vault=vault, coordinator=coordinator, source_page="knowledge/notes/new.md"
    )
    with pytest.raises((TransactionFailure, ValueError), match="precondition|before"):
        pipeline.assess(claim("red"), candidates=[old])

    assert b"concurrent" in page.read_bytes()
    assert b'"lifecycle":"active"' in page.read_bytes()


def test_lifecycle_read_rejects_oversized_page(tmp_path):
    from claims import MAX_CLAIM_PAGE_BYTES
    from contradiction_pipeline import ContradictionPipeline
    from markdown_transaction import MarkdownCoordinator

    vault = tmp_path / "vault"
    state = tmp_path / "state"
    (vault / "knowledge/inbox/claims").mkdir(parents=True)
    (vault / "knowledge/notes").mkdir()
    state.mkdir()
    old = indexed("blue", authority="web")
    page = vault / old.page
    page.write_bytes(b"x" * (MAX_CLAIM_PAGE_BYTES + 1))
    pipeline = ContradictionPipeline(
        vault=vault,
        coordinator=MarkdownCoordinator(vault, state),
        source_page="knowledge/notes/new.md",
    )

    with pytest.raises(ValueError, match="exceeds"):
        pipeline.assess(claim("red"), candidates=[old])


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
