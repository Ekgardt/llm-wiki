"""Fail-closed contradiction assessment for evidence-verified atomic claims."""
from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

from bounded_io import read_stable_bytes
from claim_tree_manifest import snapshot_claim_tree
from claims import (
    CANDIDATE_SCHEMA,
    MAX_CLAIM_PAGE_BYTES,
    ClaimIndex,
    ClaimPipeline,
    IndexedClaim,
    NormalizedClaim,
    is_substantive,
    validate_claim_record,
)
from llm_client import (
    ProviderDescriptor,
    call_candidate,
    probe_candidate,
    provider_candidates,
)
from markdown_transaction import (
    MarkdownChange,
    MarkdownCoordinator,
    active_or_legacy_coordinator,
)
from reliable_memory import canonical_json_bytes, sha256_bytes, validate_schema

AUTHORITY = {"inferred": 0, "ai-derived": 1, "web": 2, "user": 3}
FUNCTIONAL_RELATIONS = frozenset(
    {"equals", "has-state", "has-value", "located-at", "starts-at", "ends-at"}
)
SEMANTIC_LABELS = frozenset({"contradiction", "compatible", "refinement"})
_CONFIDENCE_LEVELS = frozenset({"high", "medium", "low"})
RECOMMENDATIONS = frozenset({"refine", "supersede", "keep-both", "quarantine"})
MAX_SEMANTIC_OUTPUT_BYTES = 64 * 1024
_CLAIMS_RE = re.compile(
    rb"(?ms)(^## Claims[ \t]*\r?\n```json[ \t]*\r?\n)([^\r\n]+)(\r?\n```[ \t]*(?=\r?\n(?:## |\Z)|\Z))"
)
EVALUATION_SCHEMA = {
    "type": "object",
    "required": ["label", "confidence", "supported"],
    "properties": {
        "label": {"enum": sorted(SEMANTIC_LABELS)},
        "confidence": {"enum": ["high", "medium", "low"]},
        "supported": {"type": "boolean"},
    },
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Evaluation:
    label: str
    confidence: str
    supported: bool
    evaluator: str


class StaleLifecycleTarget(ValueError):
    """The ledger record no longer matches the claim that was assessed."""


@dataclass(frozen=True, order=True)
class LifecycleTarget:
    page: str
    claim_id: str
    fingerprint: str
    record_hash: str
    evidence_hash: str

    @classmethod
    def from_indexed(cls, existing: IndexedClaim) -> LifecycleTarget:
        record = existing.claim.record
        evidence = record["evidence"]
        assert isinstance(evidence, Mapping)
        return cls(
            existing.page,
            str(record["id"]),
            str(record["fingerprint"]),
            sha256_bytes(canonical_json_bytes(record)),
            str(evidence["sha256"]),
        )

    def canonical(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class LifecycleDecision:
    recommendation: str
    mutations: tuple[LifecycleTarget, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class ClaimAssessment:
    claim: NormalizedClaim
    contradiction_class: str
    recommendation: str
    evidence: tuple[dict[str, object], ...]
    validity: dict[str, object]
    lifecycle_mutations: tuple[LifecycleTarget, ...]
    candidate_path: str | None
    evaluations: tuple[Evaluation, ...] = ()
    evaluation_lineage: tuple[dict[str, object], ...] = ()

    def canonical(self) -> dict[str, object]:
        value = {
            "claim": self.claim.record,
            "contradiction_class": self.contradiction_class,
            "recommendation": self.recommendation,
            "evidence": list(self.evidence),
            "validity": self.validity,
            "lifecycle_mutations": [item.canonical() for item in self.lifecycle_mutations],
            "candidate_path": self.candidate_path,
            "evaluations": [asdict(item) for item in self.evaluations],
            "evaluation_lineage": list(self.evaluation_lineage),
        }
        return json.loads(canonical_json_bytes(value))


@dataclass(frozen=True)
class BenchmarkMetrics:
    extraction_f1: float
    candidate_recall: float
    class_macro_f1: float
    lifecycle_macro_f1: float
    provenance_correctness: float
    quarantine_risk: float
    false_supersession: float
    quarantine_coverage: float
    semantic_primary_calls: int
    semantic_critique_calls: int
    semantic_fallback_probes: int
    semantic_evaluators_independent: bool
    semantic_benchmark_gate: bool
    quarantine_candidates: int
    quarantine_notes_published: int

    def canonical(self) -> dict[str, object]:
        return asdict(self)


def _time_key(value: object, *, upper: bool) -> str:
    if value is None:
        return "9999-12-31T23:59:59Z" if upper else "0001-01-01T00:00:00Z"
    text = str(value)
    return f"{text}T00:00:00Z" if "T" not in text else text


def intervals_overlap(first: Mapping[str, object], second: Mapping[str, object]) -> bool:
    return max(
        _time_key(first.get("from"), upper=False),
        _time_key(second.get("from"), upper=False),
    ) < min(
        _time_key(first.get("to"), upper=True),
        _time_key(second.get("to"), upper=True),
    )


def _same_value(first: Mapping[str, object], second: Mapping[str, object]) -> bool:
    return canonical_json_bytes(first.get("value")) == canonical_json_bytes(second.get("value"))


def _same_qualifier_scope(
    first: Mapping[str, object], second: Mapping[str, object]
) -> bool:
    return canonical_json_bytes(first.get("qualifiers")) == canonical_json_bytes(
        second.get("qualifiers")
    )


_DETERMINISTIC_RULES: tuple[tuple[Callable[..., bool], str | None], ...] = (
    (lambda new, old: old.get("lifecycle") != "active", "compatible"),
    (lambda new, old: new["fingerprint"] == old["fingerprint"], "equivalent"),
    (lambda new, old: new["subject"] != old["subject"], "unrelated"),
    (lambda new, old: new["relation"] != old["relation"], None),
    (
        lambda new, old: not intervals_overlap(new["validity"], old["validity"]),
        "temporal-distinct",
    ),
    (lambda new, old: not _same_qualifier_scope(new, old), None),
    (lambda new, old: _same_value(new, old), "refinement"),
    (lambda new, old: new["relation"] in FUNCTIONAL_RELATIONS, "contradiction"),
)


def deterministic_class(
    new_claim: NormalizedClaim, existing: IndexedClaim
) -> str | None:
    """The first matching rule decides, in the declared order."""
    new = new_claim.record
    old = existing.claim.record
    for predicate, verdict in _DETERMINISTIC_RULES:
        if predicate(new, old):
            return verdict
    return "compatible"


def _ineligible_claim_decision(
    record: Mapping[str, object]
) -> LifecycleDecision | None:
    """Quarantine for a claim the policy refuses to act on at all, else None."""
    if record.get("lifecycle") != "active":
        return LifecycleDecision("quarantine", reason="new claim is not active")
    if record.get("confidence") == "low":
        return LifecycleDecision("quarantine", reason="low-confidence claim")
    return None


def _keep_both_decision(
    deterministic: str | None,
    record: Mapping[str, object],
    old: Mapping[str, object],
    existing: IndexedClaim,
) -> LifecycleDecision:
    return LifecycleDecision("keep-both", reason=deterministic)


def _refinement_decision(
    deterministic: str | None,
    record: Mapping[str, object],
    old: Mapping[str, object],
    existing: IndexedClaim,
) -> LifecycleDecision:
    return LifecycleDecision("refine", reason="deterministic refinement")


def _supersession_decision(
    deterministic: str | None,
    record: Mapping[str, object],
    old: Mapping[str, object],
    existing: IndexedClaim,
) -> LifecycleDecision:
    eligible = (
        existing.ledger_backed
        and intervals_overlap(record["validity"], old["validity"])
        and AUTHORITY[str(record["authority"])] >= AUTHORITY[str(old["authority"])]
    )
    if eligible:
        return LifecycleDecision(
            "supersede", (LifecycleTarget.from_indexed(existing),), "authoritative overlap"
        )
    return LifecycleDecision("quarantine", reason="supersession policy not satisfied")


_DETERMINISTIC_POLICY = MappingProxyType(
    {
        "equivalent": _keep_both_decision,
        "unrelated": _keep_both_decision,
        "temporal-distinct": _keep_both_decision,
        "compatible": _keep_both_decision,
        "refinement": _refinement_decision,
        "contradiction": _supersession_decision,
    }
)


def apply_policy(
    new_claim: NormalizedClaim,
    existing: IndexedClaim,
    evaluations: Sequence[Evaluation] = (),
    *,
    deterministic: str | None = None,
) -> LifecycleDecision:
    """Return a lifecycle decision; model evaluations can never mutate Markdown."""
    record = new_claim.record
    old = existing.claim.record
    refusal = _ineligible_claim_decision(record)
    if refusal is not None:
        return refusal
    policy = _DETERMINISTIC_POLICY.get(deterministic)
    if policy is None:
        # Semantic supersession is intentionally disabled, including after calibration.
        return LifecycleDecision("quarantine", reason="semantic result requires review")
    return policy(deterministic, record, old, existing)


def page_is_superseded(records: Sequence[Mapping[str, object]]) -> bool:
    substantive = []
    for record in records:
        active_view = {**record, "lifecycle": "active"}
        if is_substantive(active_view):
            substantive.append(record)
    return bool(substantive) and all(item.get("lifecycle") == "superseded" for item in substantive)


def _evaluation_fields_valid(value: Evaluation) -> bool:
    return (
        value.label in SEMANTIC_LABELS
        and value.confidence in _CONFIDENCE_LEVELS
        and isinstance(value.supported, bool)
        and bool(value.evaluator)
    )


def _valid_evaluation(value: object) -> Evaluation | None:
    if not isinstance(value, Evaluation):
        return None
    return value if _evaluation_fields_valid(value) else None


_Outcome = tuple[str, LifecycleDecision, tuple[Evaluation, ...]]


def _optional_tuple(values: Sequence[object] | None) -> tuple[object, ...] | None:
    return None if values is None else tuple(values)


def _resolved_vault(vault: Path | None) -> Path | None:
    return Path(vault).resolve(strict=True) if vault is not None else None


def _block_extraction(
    block: object, extraction: Mapping[str, object]
) -> Mapping[str, object]:
    """The per-block extraction payload when the payload carries one, else all of it."""
    block_id = getattr(block, "block_id", None)
    scoped = extraction[block_id] if block_id in extraction else None
    return scoped if isinstance(scoped, Mapping) else extraction


def _fully_supported(evaluations: Sequence[Evaluation]) -> bool:
    return all(item.supported and item.confidence == "high" for item in evaluations)


def _semantic_class(evaluations: Sequence[Evaluation]) -> str:
    """Two evaluations must agree, be supported, and be high-confidence."""
    agreed = (
        len(evaluations) == 2
        and evaluations[0].label == evaluations[1].label
        and _fully_supported(evaluations)
    )
    return evaluations[0].label if agreed else "unresolved"


_CALIBRATED_SEMANTIC = MappingProxyType(
    {
        "compatible": ("keep-both", "calibrated semantic compatibility"),
        "refinement": ("refine", "calibrated semantic refinement"),
    }
)


def _calibrated_decision(
    classification: str, evaluations: Sequence[Evaluation], benchmark_gate: bool
) -> LifecycleDecision | None:
    """Under the benchmark gate a calibrated semantic class decides directly."""
    if not evaluations or not benchmark_gate:
        return None
    calibrated = _CALIBRATED_SEMANTIC.get(classification)
    if calibrated is None:
        return None
    return LifecycleDecision(calibrated[0], reason=calibrated[1])


def _candidate_decision(
    claim: NormalizedClaim,
    existing: IndexedClaim,
    classification: str,
    evaluations: Sequence[Evaluation],
    benchmark_gate: bool,
) -> LifecycleDecision:
    calibrated = _calibrated_decision(classification, evaluations, benchmark_gate)
    if calibrated is not None:
        return calibrated
    return apply_policy(
        claim,
        existing,
        evaluations,
        deterministic=classification if not evaluations else None,
    )


def _actionable_recommendations(outcomes: Sequence[_Outcome]) -> set[str]:
    return {
        item[1].recommendation
        for item in outcomes
        if item[1].recommendation != "keep-both"
    }


def _sole(values: set[str]) -> str | None:
    return next(iter(values)) if len(values) == 1 else None


def _conflicting_outcome(outcomes: Sequence[_Outcome]) -> _Outcome:
    return (
        "unresolved",
        LifecycleDecision("quarantine", reason="candidate recommendations conflict"),
        tuple(evaluation for item in outcomes for evaluation in item[2]),
    )


def _quarantined_outcome(outcomes: Sequence[_Outcome]) -> _Outcome:
    return next(item for item in outcomes if item[1].recommendation == "quarantine")


def _superseding_outcome(outcomes: Sequence[_Outcome]) -> _Outcome:
    mutations = {
        mutation
        for item in outcomes
        if item[1].recommendation == "supersede"
        for mutation in item[1].mutations
    }
    return (
        "contradiction",
        LifecycleDecision(
            "supersede",
            tuple(sorted(mutations)),
            "all authoritative overlapping conflicts",
        ),
        (),
    )


def _refining_outcome(outcomes: Sequence[_Outcome]) -> _Outcome:
    return ("refinement", LifecycleDecision("refine", reason="all refinements agree"), ())


_ACTIONABLE_REDUCERS = MappingProxyType(
    {
        "quarantine": _quarantined_outcome,
        "supersede": _superseding_outcome,
        "refine": _refining_outcome,
    }
)


def _reduce_candidate_outcomes(outcomes: Sequence[_Outcome]) -> _Outcome:
    """Conflicting recommendations quarantine; one actionable recommendation decides."""
    actionable = _actionable_recommendations(outcomes)
    if len(actionable) > 1:
        return _conflicting_outcome(outcomes)
    reducer = _ACTIONABLE_REDUCERS.get(_sole(actionable))
    if reducer is None:
        return outcomes[0]
    return reducer(outcomes)


def _reduce_outcomes(
    outcomes: Sequence[_Outcome], retrieval_context: Sequence[Mapping[str, object]]
) -> _Outcome:
    """One verdict for the whole candidate set."""
    if retrieval_context:
        return (
            "unresolved",
            LifecycleDecision(
                "quarantine",
                reason="retrieval-only context has no verified claim ledger",
            ),
            (),
        )
    if not outcomes:
        return ("no-candidate", LifecycleDecision("keep-both", reason="no candidate"), ())
    return _reduce_candidate_outcomes(outcomes)


def _mutates_markdown(decision: LifecycleDecision) -> bool:
    return decision.recommendation == "quarantine" or bool(decision.mutations)


def _assessment_evidence(
    candidates: Sequence[IndexedClaim],
    retrieval_context: Sequence[Mapping[str, object]],
) -> tuple[dict[str, object], ...]:
    ledger = tuple(
        {
            "page": item.page,
            "claim_id": item.claim.record["id"],
            "evidence": item.claim.record["evidence"],
        }
        for item in candidates
    )
    retrieved = tuple(
        {
            "page": str(item.get("path", "")),
            "title": str(item.get("title", "")),
            "snippet": str(item.get("snippet", item.get("summary", ""))),
            "retrieval_only": True,
        }
        for item in retrieval_context
    )
    return ledger + retrieved


def _paired_evaluations(
    evaluators: Sequence[Callable[..., object]],
    claim: NormalizedClaim,
    existing: IndexedClaim,
) -> tuple[Evaluation, ...]:
    """A primary evaluation and a blind critique, dropping either when invalid."""
    first = _valid_evaluation(
        evaluators[0](claim, existing, critique=False, prior_label=None)
    )
    if first is None:
        return ()
    second_evaluator = evaluators[1] if len(evaluators) > 1 else evaluators[0]
    second = _valid_evaluation(
        second_evaluator(claim, existing, critique=True, prior_label=first.label)
    )
    return (first,) if second is None else (first, second)


def _critique_order(
    descriptors: Sequence[object], first_descriptor: object
) -> list[object]:
    """Every other provider first, so the critique is blind wherever it can be."""
    return [
        item for item in descriptors if item.identity != first_descriptor.identity
    ] + [first_descriptor]


def _stage_prompt(
    claim: NormalizedClaim, existing: IndexedClaim, prior_label: str | None
) -> str:
    payload: dict[str, object] = {
        "new_claim": claim.record,
        "existing_claim": existing.claim.record,
        "existing_page": existing.page,
    }
    if prior_label is not None:
        payload["label_to_critique"] = prior_label
    return canonical_json_bytes(payload).decode("utf-8")


def _stage_system(prior_label: str | None) -> str:
    opening = (
        "Blindly critique the supplied label using only the two claims and their literal evidence. "
        if prior_label is not None
        else "Classify the two claims using only their literal evidence. "
    )
    return opening + "Do not propose or perform lifecycle mutations."


def _evaluation_output_shape(value: object) -> bool:
    return isinstance(value, dict) and set(value) == {"label", "confidence", "supported"}


def _well_formed_evaluation(value: object) -> bool:
    return (
        _evaluation_output_shape(value)
        and value["label"] in SEMANTIC_LABELS
        and value["confidence"] in _CONFIDENCE_LEVELS
        and isinstance(value["supported"], bool)
    )


def _validated_evaluation_output(text: str) -> Mapping[str, object]:
    encoded = text.encode("utf-8", errors="strict")
    if len(encoded) > MAX_SEMANTIC_OUTPUT_BYTES:
        raise ValueError("output_too_large")
    value = json.loads(encoded)
    if not _well_formed_evaluation(value):
        raise ValueError("malformed_output")
    return value


def _parsed_evaluation(
    text: str,
    descriptor: object,
    canonical: object,
    stage: str,
    lineage: list[dict[str, object]],
) -> Evaluation | None:
    """One provider answer, recording either the refusal or the acceptance."""
    try:
        value = _validated_evaluation_output(text)
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        failure = "output_too_large" if str(exc) == "output_too_large" else "malformed_output"
        lineage.append(
            {"descriptor": canonical, "stage": f"{stage}.parse", "failure": failure}
        )
        return None
    lineage.append({"descriptor": canonical, "stage": f"{stage}.parse", "failure": None})
    return Evaluation(
        value["label"], value["confidence"], value["supported"], descriptor.identity
    )


def _commit_assessment(
    claim: NormalizedClaim, decision: LifecycleDecision
) -> ClaimAssessment:
    return ClaimAssessment(
        claim,
        "unresolved",
        decision.recommendation,
        (),
        {"interval": claim.record["validity"], "status": "verified"},
        decision.mutations,
        None,
    )


def _commit_operation_id(path: str | None, decision: LifecycleDecision) -> str:
    return "contradiction:" + sha256_bytes(
        canonical_json_bytes(
            {
                "candidate": path or "none",
                "mutations": [item.canonical() for item in decision.mutations],
            }
        )
    )


def _is_regular_directory(path: Path, metadata: os.stat_result) -> bool:
    return (
        not path.is_symlink()
        and not (getattr(metadata, "st_file_attributes", 0) & 0x400)
        and stat.S_ISDIR(metadata.st_mode)
    )


def _ensured_directory(path: Path) -> Path:
    """Create the directory when it is absent; refuse anything but a real directory."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        os.mkdir(path, 0o700)
        metadata = path.lstat()
    if not _is_regular_directory(path, metadata):
        raise PermissionError("candidate parent must be a regular directory")
    return path


def _grouped_targets(
    mutations: Sequence[LifecycleTarget],
) -> dict[str, dict[str, LifecycleTarget]]:
    """One target per page and claim id; two different targets for one id conflict."""
    grouped: dict[str, dict[str, LifecycleTarget]] = {}
    for target in mutations:
        claims = grouped.setdefault(target.page, {})
        if claims.setdefault(target.claim_id, target) != target:
            raise StaleLifecycleTarget(
                "lifecycle target identity conflicts within one decision"
            )
    return grouped


def _require_unchanged_claim(
    record: Mapping[str, object],
    expected: LifecycleTarget,
    path: str,
    claim_id: str,
) -> None:
    evidence = record["evidence"]
    unchanged = (
        record["fingerprint"] == expected.fingerprint
        and sha256_bytes(canonical_json_bytes(record)) == expected.record_hash
        and evidence["sha256"] == expected.evidence_hash
    )
    if not unchanged:
        raise StaleLifecycleTarget(
            f"lifecycle target identity changed: {path}#{claim_id}"
        )


def _supersede_ledger_claims(
    ledger: Mapping[str, object],
    targets: Mapping[str, LifecycleTarget],
    path: str,
) -> None:
    """Mark every named claim superseded in place; a claim that is gone is stale."""
    found = set()
    for record in ledger["claims"]:
        claim_id = str(record["id"])
        expected = targets.get(claim_id)
        if expected is not None:
            _require_unchanged_claim(record, expected, path, claim_id)
            record["lifecycle"] = "superseded"
            found.add(claim_id)
    if found != set(targets):
        raise StaleLifecycleTarget("lifecycle target claim identity is missing")


def _page_after(after: bytes, ledger: Mapping[str, object], source_page: str) -> bytes:
    if not page_is_superseded(ledger["claims"]):
        return after
    return _mark_page_superseded(after, source_page)


class ContradictionPipeline:
    def __init__(
        self,
        *,
        claim_pipeline: ClaimPipeline | object | None = None,
        claim_index: ClaimIndex | None = None,
        evaluators: Sequence[Callable[..., object]] | None = None,
        vault: Path | None = None,
        coordinator: MarkdownCoordinator | None = None,
        source_page: str = "knowledge/notes/unknown.md",
        secondary_search: Callable[[str, int], Sequence[Mapping[str, object]]] | None = None,
        provider_descriptors: Sequence[object] | None = None,
        provider_probe: Callable[[object], bool] | None = None,
        provider_call: Callable[..., object] | None = None,
    ):
        self.claim_pipeline = claim_pipeline
        self.claim_index = claim_index
        self.evaluators = _optional_tuple(evaluators)
        self.vault = _resolved_vault(vault)
        self.coordinator = coordinator
        self.source_page = source_page
        self.secondary_search = (
            self._vault_secondary_search()
            if secondary_search is None
            else secondary_search
        )
        self.provider_descriptors = _optional_tuple(provider_descriptors)
        self.provider_probe = provider_probe or probe_candidate
        self.provider_call = provider_call or call_candidate

    def _vault_secondary_search(
        self,
    ) -> Callable[[str, int], Sequence[Mapping[str, object]]] | None:
        """Vault-scoped retrieval fallback, or none at all without a vault."""
        if self.vault is None:
            return None
        return lambda query, limit: default_secondary_search(self.vault, query, limit)

    def assess_raw(
        self,
        source: bytes,
        extraction: Mapping[str, object],
        *,
        benchmark_gate: bool = False,
    ) -> tuple[ClaimAssessment, ...]:
        if self.claim_pipeline is None:
            raise ValueError("assess_raw requires a ClaimPipeline")
        assessments: list[ClaimAssessment] = []
        for block in self.claim_pipeline.split_blocks(source):
            assessments.extend(
                self._assess_block(
                    block, _block_extraction(block, extraction), benchmark_gate
                )
            )
        return tuple(assessments)

    def _assess_block(
        self,
        block: object,
        block_extraction: Mapping[str, object],
        benchmark_gate: bool,
    ) -> list[ClaimAssessment]:
        assessments = []
        for raw_claim in self.claim_pipeline.extract(block, block_extraction):
            verified = self.claim_pipeline.verify_literal(raw_claim)
            normalized = self.claim_pipeline.normalize(verified)
            assessments.append(self.assess(normalized, benchmark_gate=benchmark_gate))
        return assessments

    def assess(
        self,
        claim: NormalizedClaim,
        *,
        candidates: Sequence[IndexedClaim] | None = None,
        benchmark_gate: bool = False,
        commit: bool = True,
    ) -> ClaimAssessment:
        if not isinstance(claim, NormalizedClaim):
            raise TypeError("claim must be normalized")
        claim_tree_manifest = self._claim_tree_manifest(commit)
        resolved = tuple(self._candidates_for(claim, candidates))
        retrieval_context = self._retrieval_context(claim, resolved)
        outcomes, evaluation_lineage = self._candidate_outcomes(
            claim, resolved, benchmark_gate
        )
        classification, decision, evaluations = _reduce_outcomes(
            outcomes, retrieval_context
        )
        candidate_path = self._commit_if_needed(
            claim, decision, commit=commit, claim_tree_manifest=claim_tree_manifest
        )
        return ClaimAssessment(
            claim,
            classification,
            decision.recommendation,
            _assessment_evidence(resolved, retrieval_context),
            {"interval": claim.record["validity"], "status": "verified"},
            decision.mutations,
            candidate_path,
            evaluations,
            tuple(evaluation_lineage),
        )

    def _claim_tree_manifest(self, commit: bool) -> Mapping[str, object] | None:
        if not commit or self.coordinator is None or self.vault is None:
            return None
        return snapshot_claim_tree(self.vault)

    def _candidates_for(
        self, claim: NormalizedClaim, candidates: Sequence[IndexedClaim] | None
    ) -> Sequence[IndexedClaim]:
        if candidates is not None:
            return candidates
        if self.claim_index is None:
            return ()
        return self.claim_index.candidates(claim)

    def _retrieval_context(
        self, claim: NormalizedClaim, candidates: Sequence[IndexedClaim]
    ) -> tuple[Mapping[str, object], ...]:
        """Bounded fallback context when the verified ledger offers no candidate."""
        if candidates or self.secondary_search is None:
            return ()
        return tuple(self.secondary_search(str(claim.record["text"]), 5))[:5]

    def _candidate_outcomes(
        self,
        claim: NormalizedClaim,
        candidates: Sequence[IndexedClaim],
        benchmark_gate: bool,
    ) -> tuple[list[_Outcome], list[dict[str, object]]]:
        outcomes: list[_Outcome] = []
        evaluation_lineage: list[dict[str, object]] = []
        for existing in candidates:
            classification, evaluations, lineage = self._classify_candidate(
                claim, existing
            )
            evaluation_lineage.extend(lineage)
            decision = _candidate_decision(
                claim, existing, classification, evaluations, benchmark_gate
            )
            outcomes.append((classification, decision, evaluations))
        return outcomes, evaluation_lineage

    def _classify_candidate(
        self, claim: NormalizedClaim, existing: IndexedClaim
    ) -> tuple[str, tuple[Evaluation, ...], tuple[dict[str, object], ...]]:
        """The deterministic class where one exists, else two agreeing evaluations."""
        classification = deterministic_class(claim, existing)
        if classification is not None:
            return classification, (), ()
        evaluations, lineage = self._evaluate_semantic(claim, existing)
        return _semantic_class(evaluations), evaluations, lineage

    def _commit_if_needed(
        self,
        claim: NormalizedClaim,
        decision: LifecycleDecision,
        *,
        commit: bool,
        claim_tree_manifest: Mapping[str, object] | None,
    ) -> str | None:
        if not commit or self.coordinator is None:
            return None
        if not _mutates_markdown(decision):
            return None
        return self._commit(claim, decision, claim_tree_manifest=claim_tree_manifest)

    def _evaluate_semantic(
        self, claim: NormalizedClaim, existing: IndexedClaim
    ) -> tuple[tuple[Evaluation, ...], tuple[dict[str, object], ...]]:
        if self.evaluators is None:
            return self._evaluate_with_providers(claim, existing)
        if not self.evaluators:
            return (), ()
        return _paired_evaluations(self.evaluators, claim, existing), ()

    def _evaluate_with_providers(
        self, claim: NormalizedClaim, existing: IndexedClaim
    ) -> tuple[tuple[Evaluation, ...], tuple[dict[str, object], ...]]:
        descriptors = self._descriptors()
        first, first_descriptor, first_lineage = self._provider_stage(
            "primary", descriptors, claim, existing, prior_label=None
        )
        if first is None or first_descriptor is None:
            return (), tuple(first_lineage)
        second, _second_descriptor, second_lineage = self._provider_stage(
            "critique",
            _critique_order(descriptors, first_descriptor),
            claim,
            existing,
            prior_label=first.label,
        )
        pair = (first,) if second is None else (first, second)
        return pair, tuple(first_lineage + second_lineage)

    def _descriptors(self) -> Sequence[object]:
        return self.provider_descriptors or tuple(
            provider_candidates(
                os.environ.get("MEMORY_LLM_PROVIDER", ""), max_tokens=800
            )
        )

    def _provider_stage(
        self,
        stage: str,
        descriptors: Sequence[object],
        claim: NormalizedClaim,
        existing: IndexedClaim,
        *,
        prior_label: str | None,
    ) -> tuple[Evaluation | None, object | None, list[dict[str, object]]]:
        lineage: list[dict[str, object]] = []
        prompt = _stage_prompt(claim, existing, prior_label)
        system = _stage_system(prior_label)
        for descriptor in descriptors:
            evaluation = self._try_descriptor(descriptor, stage, prompt, system, lineage)
            if evaluation is not None:
                return evaluation, descriptor, lineage
        return None, None, lineage

    def _try_descriptor(
        self,
        descriptor: object,
        stage: str,
        prompt: str,
        system: str,
        lineage: list[dict[str, object]],
    ) -> Evaluation | None:
        """One provider attempt; every refusal is appended to the lineage."""
        canonical = descriptor.canonical()
        if not self.provider_probe(descriptor):
            lineage.append(
                {"descriptor": canonical, "stage": f"{stage}.probe", "failure": "unavailable"}
            )
            return None
        result = self.provider_call(
            descriptor,
            prompt,
            system,
            max_tokens=800,
            schema=EVALUATION_SCHEMA,
            available=True,
        )
        if result.text is None:
            lineage.append(
                {
                    "descriptor": canonical,
                    "stage": f"{stage}.call",
                    "failure": result.failure_class or "empty_response",
                }
            )
            return None
        return _parsed_evaluation(result.text, descriptor, canonical, stage, lineage)

    def _commit(
        self,
        claim: NormalizedClaim,
        decision: LifecycleDecision,
        *,
        claim_tree_manifest: Mapping[str, object] | None = None,
    ) -> str | None:
        self._require_writer()
        changes, preconditions, candidate_paths = self.plan_changes(
            (_commit_assessment(claim, decision),)
        )
        if claim_tree_manifest is not None:
            preconditions["claim_tree_manifest"] = dict(claim_tree_manifest)
        path = candidate_paths[0] if candidate_paths else None
        self._write_candidate(changes, preconditions, path, decision)
        return path

    def _require_writer(self) -> None:
        if self.vault is None or self.coordinator is None:
            raise ValueError("candidate writes require a vault and coordinator")

    def _write_candidate(
        self,
        changes: Sequence[MarkdownChange],
        preconditions: dict[str, object],
        path: str | None,
        decision: LifecycleDecision,
    ) -> None:
        operation_id = _commit_operation_id(path, decision)
        with self.coordinator.writer_gate():
            self._prepare_and_apply(changes, preconditions, path, operation_id)
        if self.claim_index is not None and decision.mutations:
            self.claim_index.rebuild()

    def _prepare_and_apply(
        self,
        changes: Sequence[MarkdownChange],
        preconditions: dict[str, object],
        path: str | None,
        operation_id: str,
    ) -> None:
        if path is not None:
            self.ensure_candidate_parent()
        transaction = self.coordinator.prepare(
            changes,
            operation_id=operation_id,
            preconditions=preconditions,
            content_guard="model_output",
        )
        self.coordinator.apply(transaction.id)

    def plan_changes(
        self, assessments: Sequence[ClaimAssessment]
    ) -> tuple[list[MarkdownChange], dict[str, object], tuple[str, ...]]:
        if self.vault is None:
            raise ValueError("mutation planning requires a vault")
        changes: list[MarkdownChange] = []
        candidate_paths = []
        mutations = set()
        for assessment in sorted(
            assessments, key=lambda item: (str(item.claim.record["id"]), str(item.claim.record["fingerprint"]))
        ):
            mutations.update(assessment.lifecycle_mutations)
            if assessment.recommendation != "quarantine":
                continue
            claim = assessment.claim
            quarantined = NormalizedClaim({**claim.record, "lifecycle": "quarantined"})
            validate_claim_record(quarantined.record)
            candidate = {
                "schema_version": "claim-candidate/v1",
                "status": "quarantined",
                "reason": "contradiction assessment requires manual review",
                "claim": quarantined.record,
                "source_page": self.source_page,
                "created_at": claim.record["observed_at"],
            }
            validate_schema(candidate, CANDIDATE_SCHEMA)
            identity = sha256_bytes(
                canonical_json_bytes(
                    {"id": claim.record["id"], "evidence": claim.record["evidence"]}
                )
            )[:20]
            path = f"knowledge/inbox/claims/{claim.record['fingerprint']}-{identity}.md"
            content = (
                "---\ntype: claim-candidate\nstatus: quarantined\n---\n"
                f"# Quarantined claim {claim.record['id']}\n\n"
                "```json\n" + canonical_json_bytes(candidate).decode("utf-8") + "\n```\n"
            ).encode("utf-8")
            changes.append(MarkdownChange.create(path, content))
            candidate_paths.append(path)
        lifecycle_changes, preconditions = self._lifecycle_changes(sorted(mutations))
        changes.extend(lifecycle_changes)
        return changes, preconditions, tuple(candidate_paths)

    def ensure_candidate_parent(self) -> Path:
        self._require_candidate_parent_ownership()
        current = self.vault
        for part in ("knowledge", "inbox", "claims"):
            current = _ensured_directory(current / part)
        return current

    def _require_candidate_parent_ownership(self) -> None:
        if self.vault is None or self.coordinator is None:
            raise ValueError("candidate parent creation requires a vault and coordinator")
        if not self.coordinator.writer_gate_held():
            raise RuntimeError("candidate parent creation requires writer ownership")

    def _lifecycle_changes(
        self, mutations: Sequence[LifecycleTarget]
    ) -> tuple[list[MarkdownChange], dict[str, object]]:
        grouped = _grouped_targets(mutations)
        changes = []
        preconditions: dict[str, object] = {}
        if mutations:
            preconditions["claim_targets"] = [
                item.canonical() for item in sorted(mutations)
            ]
        for path, targets in grouped.items():
            changes.append(self._lifecycle_change(path, targets, preconditions))
        return changes, preconditions

    def _lifecycle_change(
        self,
        path: str,
        targets: Mapping[str, LifecycleTarget],
        preconditions: dict[str, object],
    ) -> MarkdownChange:
        """Supersede every named claim on one page, refusing any drifted identity."""
        raw = read_stable_bytes(
            self.vault / path, MAX_CLAIM_PAGE_BYTES, label="claim lifecycle page"
        )
        preconditions[path] = sha256_bytes(raw)
        match = _CLAIMS_RE.search(raw)
        if match is None:
            raise ValueError("lifecycle target has no canonical claim ledger")
        ledger = json.loads(match[2])
        _supersede_ledger_claims(ledger, targets, path)
        encoded = canonical_json_bytes(ledger)
        after = raw[: match.start(2)] + encoded + raw[match.end(2) :]
        return MarkdownChange.replace(
            path,
            _page_after(after, ledger, self.source_page),
            max_before_bytes=MAX_CLAIM_PAGE_BYTES,
        )


def _frontmatter_newline(content: bytes) -> bytes:
    if content.startswith(b"---\r\n"):
        return b"\r\n"
    if content.startswith(b"---\n"):
        return b"\n"
    raise ValueError("lifecycle target has no canonical frontmatter")


def _frontmatter_end(content: bytes, delimiter: bytes, start: int) -> int:
    end = content.find(delimiter, start)
    if end < 0 or end > 64 * 1024:
        raise ValueError("lifecycle target has no bounded canonical frontmatter")
    return end


def _require_uniform_newlines(frontmatter: str, newline: bytes) -> None:
    mixed = (
        "\n" in frontmatter.replace("\r\n", "")
        if newline == b"\r\n"
        else "\r" in frontmatter
    )
    if mixed:
        raise ValueError("lifecycle target frontmatter has mixed line endings")


def _rebuilt_frontmatter(frontmatter: str, newline: bytes, slug: str) -> bytes:
    separator = newline.decode("ascii")
    lines = [
        line
        for line in frontmatter.split(separator)
        if not line.startswith(("status:", "superseded_by:"))
    ]
    lines.extend(("status: superseded", f"superseded_by: [[{slug}]]"))
    return (
        b"---"
        + newline
        + separator.join(lines).encode("utf-8")
        + newline
        + b"---"
        + newline
    )


def _mark_page_superseded(content: bytes, source_page: str) -> bytes:
    newline = _frontmatter_newline(content)
    delimiter = newline + b"---" + newline
    end = _frontmatter_end(content, delimiter, 3 + len(newline))
    frontmatter = content[3 + len(newline) : end].decode("utf-8", errors="strict")
    _require_uniform_newlines(frontmatter, newline)
    body = content[end + len(delimiter) :]
    rebuilt = _rebuilt_frontmatter(frontmatter, newline, Path(source_page).stem)
    return rebuilt + body


def _secondary_search_hit(
    page: Path, root: Path, terms: Sequence[str]
) -> dict[str, object] | None:
    """One scored result for a page that contains every term, else nothing."""
    from search_memory import (
        MAX_PAGE_BYTES,
        _extract_title_and_summary,
        _strip_frontmatter,
    )

    try:
        content = read_stable_bytes(
            page, MAX_PAGE_BYTES, label="secondary search page"
        ).decode("utf-8", errors="ignore")
    except (OSError, ValueError):
        return None
    searchable = _strip_frontmatter(content).casefold()
    if not all(term in searchable for term in terms):
        return None
    title, summary = _extract_title_and_summary(content, page.stem)
    return {
        "path": page.relative_to(root).as_posix(),
        "title": title,
        "summary": summary[:120],
        "score": sum(searchable.count(term) for term in terms),
        "project": "",
        "timestamp": "",
    }


def _scan_secondary_pages(
    root: Path, query: str, bounded_limit: int
) -> Sequence[Mapping[str, object]]:
    """Term-conjunction scan over a vault that is not the running installation."""
    from search_memory import _collect_pages

    terms = tuple(dict.fromkeys(re.findall(r"\w+", query.casefold())))
    if not terms:
        return ()
    pages = _collect_pages(
        "all", knowledge_dir=root / "knowledge" / "notes", root=root
    )
    results: list[dict[str, object]] = []
    for page in pages:
        hit = _secondary_search_hit(page, root, terms)
        if hit is not None:
            results.append(hit)
    results.sort(key=lambda item: (-int(item["score"]), str(item["path"])))
    return results[:bounded_limit]


def default_secondary_search(
    root: Path, query: str, limit: int
) -> Sequence[Mapping[str, object]]:
    from search_memory import ROOT as search_root
    from search_memory import search

    bounded_limit = max(0, min(limit, 5))
    if bounded_limit == 0:
        return ()
    root = root.resolve()
    if root == search_root.resolve():
        return search(query, limit=bounded_limit)
    return _scan_secondary_pages(root, query, bounded_limit)


@dataclass
class _BenchmarkTally:
    """Per-case tallies gathered while the benchmark assesses every claim."""

    predicted_classes: list[str] = field(default_factory=list)
    gold_classes: list[str] = field(default_factory=list)
    predicted_lifecycle: list[str] = field(default_factory=list)
    gold_lifecycle: list[str] = field(default_factory=list)
    provenance: int = 0
    retrieved: int = 0
    retrievable: int = 0
    negative: int = 0
    false_superseded: int = 0


@dataclass(frozen=True)
class _PublicationOutcome:
    published: int
    published_errors: int
    quarantine_notes: int
    candidate_files: int


class _BenchmarkProvider:
    """Records which provider served each stage, without calling a real backend."""

    def __init__(self) -> None:
        self.calls: dict[str, list[str]] = {"primary": [], "critique": []}
        self.fallback_probes = 0

    def probe(self, descriptor: object) -> bool:
        available = descriptor.provider != "benchmark-unavailable"
        self.fallback_probes += int(not available)
        return available

    def call(
        self, descriptor: object, prompt: str, system: str, **_kwargs: object
    ) -> object:
        stage = "critique" if system.startswith("Blindly critique") else "primary"
        self.calls[stage].append(descriptor.identity)
        return SimpleNamespace(
            text='{"label":"compatible","confidence":"high","supported":true}',
            failure_class=None,
        )


_BENCHMARK_PROVIDERS = (
    "benchmark-unavailable",
    "benchmark-primary",
    "benchmark-critique",
)


def _benchmark_descriptors() -> tuple[ProviderDescriptor, ...]:
    return tuple(
        ProviderDescriptor(
            provider=name,
            model=f"{name}-model",
            capabilities=MappingProxyType(
                {"structured_output": "native", "max_tokens_enforced": True}
            ),
            inference_settings=MappingProxyType({"max_tokens": 800}),
            candidate_index=position,
            fallback_from=(),
        )
        for position, name in enumerate(_BENCHMARK_PROVIDERS)
    )


def _valid_benchmark_cases(cases: object, source_text: object) -> bool:
    return isinstance(cases, list) and bool(cases) and isinstance(source_text, str)


def _require_benchmark_corpus(
    corpus: Mapping[str, object]
) -> tuple[list[Mapping[str, object]], str]:
    if corpus.get("provider") != "fake":
        raise ValueError("frozen contradiction benchmark requires the fake provider")
    cases = corpus.get("cases")
    source_text = corpus.get("source")
    if not _valid_benchmark_cases(cases, source_text):
        raise ValueError("frozen contradiction benchmark has no cases")
    return cases, source_text


def _prepare_benchmark_vault(temporary: Path) -> tuple[Path, Path]:
    vault = temporary / "vault"
    state_root = temporary / "state"
    for relative in ("knowledge/daily", "knowledge/notes", "knowledge/projects"):
        (vault / relative).mkdir(parents=True)
    state_root.mkdir()
    return vault, state_root


def _benchmark_page_bytes(claim_record: Mapping[str, object]) -> bytes:
    ledger = {"schema_version": "claim-ledger/v1", "claims": [claim_record]}
    return (
        b"---\ntype: concept\n---\n# Benchmark\n\n## Claims\n```json\n"
        + canonical_json_bytes(ledger)
        + b"\n```\n"
    )


def _seed_benchmark_pages(
    coordinator: MarkdownCoordinator,
    source: bytes,
    cases: Sequence[Mapping[str, object]],
) -> None:
    """Publish the daily source and one existing-claim ledger page per case."""
    source_record = coordinator.prepare(
        [MarkdownChange.create("knowledge/daily/2026-01-01.md", source)],
        operation_id=f"benchmark-source:{sha256_bytes(source)}",
    )
    coordinator.apply(source_record.id)
    for case in cases:
        page_bytes = _benchmark_page_bytes(case["existing_claim"])
        page_record = coordinator.prepare(
            [MarkdownChange.create(case["existing_page"], page_bytes)],
            operation_id=f"benchmark-page:{sha256_bytes(page_bytes)}",
        )
        coordinator.apply(page_record.id)


def _normalized_case_claims(
    pipeline: ClaimPipeline, block: object, case: Mapping[str, object]
) -> list[NormalizedClaim]:
    raw_claims = pipeline.extract(
        block,
        {"schema_version": "claim-extraction/v1", "claims": [case["new_extraction"]]},
    )
    return [
        pipeline.normalize(pipeline.verify_literal(raw_claim))
        for raw_claim in raw_claims
    ]


def _extract_benchmark_claims(
    pipeline: ClaimPipeline,
    blocks: Mapping[str, object],
    cases: Sequence[Mapping[str, object]],
) -> tuple[dict[str, NormalizedClaim], int]:
    """The normalized claim per case id, and how many match the expected record."""
    extracted: dict[str, NormalizedClaim] = {}
    true_positive = 0
    for case in cases:
        for normalized in _normalized_case_claims(
            pipeline, blocks[case["block_id"]], case
        ):
            extracted[str(case["id"])] = normalized
            true_positive += int(
                canonical_json_bytes(normalized.record)
                == canonical_json_bytes(case["expected_new_claim"])
            )
    return extracted, true_positive


def _record_resolves(resolver: object, record: Mapping[str, object]) -> bool:
    validate_claim_record(record)
    evidence = record["evidence"]
    resolved = resolver.resolve(evidence["reference"])
    return (
        resolved.sha256 == evidence["sha256"]
        and resolved.bytes.decode("utf-8", errors="strict") == evidence["text"]
    )


def _provenance_valid(resolver: object, *records: Mapping[str, object]) -> bool:
    """Every record must validate and resolve to exactly the bytes it names."""
    try:
        outcomes = [_record_resolves(resolver, record) for record in records]
    except (TypeError, ValueError):
        return False
    return all(outcomes)


def _score_retrieval(
    tally: _BenchmarkTally, case: Mapping[str, object], result: ClaimAssessment
) -> None:
    if not case["retrievable"]:
        return
    tally.retrievable += 1
    tally.retrieved += int(
        case["existing_page"] in {item["page"] for item in result.evidence}
    )


def _score_negative_control(
    tally: _BenchmarkTally, case: Mapping[str, object], result: ClaimAssessment
) -> None:
    if not case["negative_control"]:
        return
    tally.negative += 1
    tally.false_superseded += int(result.recommendation == "supersede")


def _score_case(
    tally: _BenchmarkTally,
    case: Mapping[str, object],
    new: NormalizedClaim,
    result: ClaimAssessment,
    resolver: object,
) -> None:
    tally.predicted_classes.append(result.contradiction_class)
    tally.gold_classes.append(case["expected_class"])
    tally.predicted_lifecycle.append(result.recommendation)
    tally.gold_lifecycle.append(case["expected_lifecycle"])
    _score_retrieval(tally, case, result)
    valid = _provenance_valid(resolver, new.record, case["existing_claim"])
    tally.provenance += int(valid is bool(case["expected_provenance_valid"]))
    _score_negative_control(tally, case, result)


def _assess_benchmark_cases(
    vault: Path,
    index: ClaimIndex,
    provider: _BenchmarkProvider,
    resolver: object,
    cases: Sequence[Mapping[str, object]],
    extracted: Mapping[str, NormalizedClaim],
) -> tuple[list[tuple[Mapping[str, object], NormalizedClaim, ClaimAssessment]], _BenchmarkTally]:
    pipeline = ContradictionPipeline(
        claim_index=index,
        vault=vault,
        provider_descriptors=_benchmark_descriptors(),
        provider_probe=provider.probe,
        provider_call=provider.call,
    )
    tally = _BenchmarkTally()
    assessed: list[tuple[Mapping[str, object], NormalizedClaim, ClaimAssessment]] = []
    for case in cases:
        new = extracted[str(case["id"])]
        result = pipeline.assess(new, benchmark_gate=False, commit=False)
        assessed.append((case, new, result))
        _score_case(tally, case, new, result, resolver)
    return assessed, tally


def _case_publication(
    vault: Path, new: NormalizedClaim, result: ClaimAssessment, proposed: str
) -> tuple[list[MarkdownChange], list[str]]:
    if result.recommendation == "quarantine":
        candidate_pipeline = ContradictionPipeline(
            vault=vault, source_page=proposed, evaluators=()
        )
        changes, _preconditions, paths = candidate_pipeline.plan_changes((result,))
        return list(changes), list(paths)
    ledger = {"schema_version": "claim-ledger/v1", "claims": [new.record]}
    return (
        [
            MarkdownChange.create(
                proposed,
                b"---\ntype: concept\n---\n# Proposed\n\n## Claims\n```json\n"
                + canonical_json_bytes(ledger)
                + b"\n```\n",
                max_before_bytes=MAX_CLAIM_PAGE_BYTES,
            )
        ],
        [],
    )


def _publication_changes(
    vault: Path,
    assessed: Sequence[tuple[Mapping[str, object], NormalizedClaim, ClaimAssessment]],
    proposed_paths: Sequence[str],
) -> tuple[list[MarkdownChange], list[str]]:
    changes: list[MarkdownChange] = []
    candidate_paths: list[str] = []
    for (_case, new, result), proposed in zip(assessed, proposed_paths):
        page_changes, paths = _case_publication(vault, new, result, proposed)
        changes.extend(page_changes)
        candidate_paths.extend(paths)
    return changes, candidate_paths


def _publish_benchmark_changes(
    coordinator: MarkdownCoordinator,
    vault: Path,
    changes: Sequence[MarkdownChange],
    candidate_paths: Sequence[str],
    proposed_paths: Sequence[str],
) -> None:
    with coordinator.writer_gate():
        if candidate_paths:
            ContradictionPipeline(
                vault=vault, coordinator=coordinator
            ).ensure_candidate_parent()
        transaction = coordinator.prepare(
            sorted(changes, key=lambda item: item.path),
            operation_id="benchmark-publication:"
            + sha256_bytes(canonical_json_bytes(proposed_paths)),
            preconditions={item.path: "absent" for item in changes},
            content_guard="model_output",
        )
        coordinator.apply(transaction.id)


def _case_mismatch(case: Mapping[str, object], result: ClaimAssessment) -> bool:
    return (
        result.contradiction_class != case["expected_class"]
        or result.recommendation != case["expected_lifecycle"]
    )


def _require_absent_from_retrieval(
    index: ClaimIndex, new: NormalizedClaim, proposed: str
) -> None:
    if any(item.page == proposed for item in index.candidates(new)):
        raise ValueError("quarantined benchmark claim entered active retrieval")


def _require_quarantine_unpublished(
    index: ClaimIndex,
    new: NormalizedClaim,
    result: ClaimAssessment,
    proposed: str,
    exists: bool,
) -> None:
    """A quarantined claim must reach neither the vault nor active retrieval."""
    if result.recommendation != "quarantine":
        return
    if exists:
        raise ValueError("quarantined benchmark claim was published")
    _require_absent_from_retrieval(index, new, proposed)


def _verify_benchmark_publication(
    vault: Path,
    index: ClaimIndex,
    assessed: Sequence[tuple[Mapping[str, object], NormalizedClaim, ClaimAssessment]],
    proposed_paths: Sequence[str],
) -> tuple[int, int, int]:
    """(published, published errors, quarantine notes published), fail-closed."""
    published = published_errors = quarantine_notes = 0
    for (case, new, result), proposed in zip(assessed, proposed_paths):
        exists = (vault / proposed).exists()
        published += int(exists)
        published_errors += int(exists and _case_mismatch(case, result))
        quarantine_notes += int(exists and result.recommendation == "quarantine")
        _require_quarantine_unpublished(index, new, result, proposed, exists)
    return published, published_errors, quarantine_notes


def _publish_benchmark_assessments(
    vault: Path,
    state_root: Path,
    index: ClaimIndex,
    assessed: Sequence[tuple[Mapping[str, object], NormalizedClaim, ClaimAssessment]],
) -> _PublicationOutcome:
    coordinator = active_or_legacy_coordinator(vault, state_root)
    proposed_paths = [
        f"knowledge/notes/proposed-{case['id']}.md" for case, _new, _result in assessed
    ]
    changes, candidate_paths = _publication_changes(vault, assessed, proposed_paths)
    _publish_benchmark_changes(
        coordinator, vault, changes, candidate_paths, proposed_paths
    )
    index.rebuild()
    published, published_errors, quarantine_notes = _verify_benchmark_publication(
        vault, index, assessed, proposed_paths
    )
    return _PublicationOutcome(
        published,
        published_errors,
        quarantine_notes,
        len([path for path in candidate_paths if (vault / path).is_file()]),
    )


def _ratio(numerator: float, denominator: float, empty: float) -> float:
    return numerator / denominator if denominator else empty


def _extraction_f1(true_positive: int, extracted_count: int, total: int) -> float:
    precision = _ratio(true_positive, extracted_count, 0)
    recall = true_positive / total
    if not precision + recall:
        return 0
    return 2 * precision * recall / (precision + recall)


def _evaluators_independent(calls: Mapping[str, list[str]]) -> bool:
    return (
        bool(calls["primary"])
        and bool(calls["critique"])
        and set(calls["primary"]).isdisjoint(calls["critique"])
    )


def _benchmark_metrics(
    total: int,
    extracted_count: int,
    true_positive: int,
    tally: _BenchmarkTally,
    provider: _BenchmarkProvider,
    publication: _PublicationOutcome,
) -> BenchmarkMetrics:
    return BenchmarkMetrics(
        _extraction_f1(true_positive, extracted_count, total),
        _ratio(tally.retrieved, tally.retrievable, 1),
        _macro_f1(tally.gold_classes, tally.predicted_classes),
        _macro_f1(tally.gold_lifecycle, tally.predicted_lifecycle),
        tally.provenance / total,
        _ratio(publication.published_errors, publication.published, 0),
        _ratio(tally.false_superseded, tally.negative, 0),
        publication.published / total,
        len(provider.calls["primary"]),
        len(provider.calls["critique"]),
        provider.fallback_probes,
        _evaluators_independent(provider.calls),
        False,
        publication.candidate_files,
        publication.quarantine_notes,
    )


def _run_benchmark_in(
    temporary: Path, cases: Sequence[Mapping[str, object]], source_text: str
) -> BenchmarkMetrics:
    from evidence_resolver import EvidenceResolver

    vault, state_root = _prepare_benchmark_vault(temporary)
    source = source_text.encode("utf-8")
    _seed_benchmark_pages(
        active_or_legacy_coordinator(vault, state_root), source, cases
    )
    resolver = EvidenceResolver(vault, state_root=state_root)
    extraction_pipeline = ClaimPipeline(resolver)
    blocks = {
        block.block_id: block for block in extraction_pipeline.split_blocks(source)
    }
    extracted, true_positive = _extract_benchmark_claims(
        extraction_pipeline, blocks, cases
    )
    index = ClaimIndex(state_root, vault=vault)
    index.rebuild()
    provider = _BenchmarkProvider()
    assessed, tally = _assess_benchmark_cases(
        vault, index, provider, resolver, cases, extracted
    )
    publication = _publish_benchmark_assessments(vault, state_root, index, assessed)
    return _benchmark_metrics(
        len(cases), len(extracted), true_positive, tally, provider, publication
    )


def run_frozen_benchmark(corpus: Mapping[str, object]) -> BenchmarkMetrics:
    """Run extraction, provenance, retrieval, classification, and policy end to end."""
    cases, source_text = _require_benchmark_corpus(corpus)
    with tempfile.TemporaryDirectory(prefix="llm-wiki-contradiction-") as temporary:
        return _run_benchmark_in(Path(temporary), cases, source_text)


def _pair_count(pairs: Sequence[tuple[str, str]], index: int, label: str) -> int:
    return sum(1 for pair in pairs if pair[index] == label)


def _confusion_counts(
    label: str, pairs: Sequence[tuple[str, str]]
) -> tuple[int, int, int]:
    """(true positive, false positive, false negative) for one label."""
    true_positive = sum(1 for pair in pairs if pair == (label, label))
    return (
        true_positive,
        _pair_count(pairs, 1, label) - true_positive,
        _pair_count(pairs, 0, label) - true_positive,
    )


def _label_f1(label: str, pairs: Sequence[tuple[str, str]]) -> float:
    true_positive, false_positive, false_negative = _confusion_counts(label, pairs)
    denominator = 2 * true_positive + false_positive + false_negative
    return _ratio(2 * true_positive, denominator, 0)


def _macro_f1(gold: Sequence[str], predicted: Sequence[str]) -> float:
    labels = sorted(set(gold) | set(predicted))
    pairs = tuple(zip(gold, predicted))
    scores = [_label_f1(label, pairs) for label in labels]
    return _ratio(sum(scores), len(scores), 0)


def _require_query_text(claim: object) -> str:
    if not isinstance(claim, str) or not claim.strip():
        raise ValueError("claim must be a non-empty string")
    return claim


def _decoded_claim(claim: str) -> object:
    try:
        return json.loads(claim)
    except json.JSONDecodeError:
        return None


def _is_claim_document(decoded: object) -> bool:
    return isinstance(decoded, dict) and decoded.get("schema_version") == "claim/v1"


def _verified_claim(
    decoded: Mapping[str, object], root: Path, state_root: Path
) -> NormalizedClaim:
    """Normalize a JSON claim whose literal evidence resolves byte for byte."""
    validate_claim_record(decoded)
    from evidence_resolver import EvidenceResolver

    evidence = decoded["evidence"]
    resolved = EvidenceResolver(root, state_root=state_root).resolve(
        evidence["reference"]
    )
    matches = (
        resolved.sha256 == evidence["sha256"]
        and resolved.bytes.decode("utf-8", errors="strict") == evidence["text"]
    )
    if not matches:
        raise ValueError("claim evidence does not match resolved bytes")
    return NormalizedClaim(decoded)


def _assess_query_claim(
    normalized: NormalizedClaim, root: Path, state_root: Path
) -> ClaimAssessment:
    index = ClaimIndex(state_root, vault=root)
    pipeline = ContradictionPipeline(
        claim_index=index,
        secondary_search=lambda query, limit: default_secondary_search(
            root, query, limit
        ),
    )
    return pipeline.assess(normalized)


def _unsupported_evidence_view(
    canonical: dict[str, object]
) -> tuple[dict[str, object], list[str]]:
    """Plain text carries no literal evidence, so it can only be quarantined."""
    canonical["recommendation"] = "quarantine"
    canonical["lifecycle_mutations"] = []
    canonical["candidate_path"] = None
    return (
        {
            "status": "unsupported-evidence",
            "reason": "plain string input has no literal evidence reference",
        },
        ["quarantine"],
    )


def assess_text(claim: str) -> dict[str, object]:
    """Assess JSON claims or retrieve context for unsupported plain-text evidence."""
    text = _require_query_text(claim)
    from memory_state import ROOT, STATE_ROOT

    decoded = _decoded_claim(text)
    verified = _is_claim_document(decoded)
    normalized = (
        _verified_claim(decoded, ROOT, STATE_ROOT)
        if verified
        else _plain_text_query_claim(text.strip())
    )
    assessment = _assess_query_claim(normalized, ROOT, STATE_ROOT)
    canonical = assessment.canonical()
    validity, recommendations = (
        (assessment.validity, [assessment.recommendation])
        if verified
        else _unsupported_evidence_view(canonical)
    )
    return {
        "assessments": [canonical],
        "evidence": list(assessment.evidence),
        "validity": validity,
        "recommendations": recommendations,
    }


def _plain_text_query_claim(text: str) -> NormalizedClaim:
    relation = next(
        (item for item in sorted(FUNCTIONAL_RELATIONS | {"uses", "depends-on", "member-of"}, key=len, reverse=True) if f" {item} " in text),
        "equals",
    )
    if relation == "equals":
        subject, value = text, text
    else:
        subject, value = text.split(f" {relation} ", 1)
    semantic = {
        "subject": " ".join(subject.split()).casefold(),
        "relation": relation,
        "value": {"type": "string", "value": " ".join(value.split())},
        "qualifiers": [],
        "validity": {"from": None, "to": None},
    }
    digest = sha256_bytes(text.encode("utf-8"))
    return NormalizedClaim(
        {
            "schema_version": "claim/v1",
            "id": f"mcp-{digest[:16]}",
            "fingerprint": sha256_bytes(canonical_json_bytes(semantic)),
            "text": text,
            **semantic,
            "observed_at": "1970-01-01T00:00:00Z",
            "lifecycle": "active",
            "confidence": "low",
            "authority": "inferred",
            "evidence": {
                "reference": f"daily:1970-01-01 sha256:{'0' * 64} block:00:00:00 bytes:0-1",
                "sha256": digest,
                "text": text,
            },
            "links": [],
            "extractor_version": "mcp-text/v1",
        }
    )
