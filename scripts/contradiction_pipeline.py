"""Fail-closed contradiction assessment for evidence-verified atomic claims."""
from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from claims import (
    CANDIDATE_SCHEMA,
    ClaimIndex,
    ClaimPipeline,
    IndexedClaim,
    NormalizedClaim,
    is_substantive,
    validate_claim_record,
)
from llm_client import call_candidate, probe_candidate, provider_candidates
from markdown_transaction import MarkdownChange, MarkdownCoordinator
from reliable_memory import canonical_json_bytes, sha256_bytes, validate_schema

AUTHORITY = {"inferred": 0, "ai-derived": 1, "web": 2, "user": 3}
FUNCTIONAL_RELATIONS = frozenset(
    {"equals", "has-state", "has-value", "located-at", "starts-at", "ends-at"}
)
SEMANTIC_LABELS = frozenset({"contradiction", "compatible", "refinement"})
RECOMMENDATIONS = frozenset({"refine", "supersede", "keep-both", "quarantine"})
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


@dataclass(frozen=True)
class LifecycleDecision:
    recommendation: str
    mutations: tuple[tuple[str, str], ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class ClaimAssessment:
    claim: NormalizedClaim
    contradiction_class: str
    recommendation: str
    evidence: tuple[dict[str, object], ...]
    validity: dict[str, object]
    lifecycle_mutations: tuple[tuple[str, str], ...]
    candidate_path: str | None
    evaluations: tuple[Evaluation, ...] = ()

    def canonical(self) -> dict[str, object]:
        value = {
            "claim": self.claim.record,
            "contradiction_class": self.contradiction_class,
            "recommendation": self.recommendation,
            "evidence": list(self.evidence),
            "validity": self.validity,
            "lifecycle_mutations": [list(item) for item in self.lifecycle_mutations],
            "candidate_path": self.candidate_path,
            "evaluations": [asdict(item) for item in self.evaluations],
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


def deterministic_class(
    new_claim: NormalizedClaim, existing: IndexedClaim
) -> str | None:
    new = new_claim.record
    old = existing.claim.record
    if old.get("lifecycle") != "active":
        return "compatible"
    if new["fingerprint"] == old["fingerprint"]:
        return "equivalent"
    if new["subject"] != old["subject"]:
        return "unrelated"
    if new["relation"] != old["relation"]:
        return None
    if not intervals_overlap(new["validity"], old["validity"]):
        return "temporal-distinct"
    if _same_value(new, old):
        return "refinement"
    if new["relation"] in FUNCTIONAL_RELATIONS:
        return "contradiction"
    return "compatible"


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
    if record.get("lifecycle") != "active":
        return LifecycleDecision("quarantine", reason="new claim is not active")
    if record.get("confidence") == "low":
        return LifecycleDecision("quarantine", reason="low-confidence claim")
    if deterministic in {"equivalent", "unrelated", "temporal-distinct", "compatible"}:
        return LifecycleDecision("keep-both", reason=deterministic)
    if deterministic == "refinement":
        return LifecycleDecision("refine", reason="deterministic refinement")
    if deterministic == "contradiction":
        eligible = (
            existing.ledger_backed
            and intervals_overlap(record["validity"], old["validity"])
            and AUTHORITY[str(record["authority"])] >= AUTHORITY[str(old["authority"])]
        )
        if eligible:
            return LifecycleDecision(
                "supersede", ((existing.page, str(old["id"])),), "authoritative overlap"
            )
        return LifecycleDecision("quarantine", reason="supersession policy not satisfied")
    # Semantic supersession is intentionally disabled, including after calibration.
    return LifecycleDecision("quarantine", reason="semantic result requires review")


def page_is_superseded(records: Sequence[Mapping[str, object]]) -> bool:
    substantive = []
    for record in records:
        active_view = {**record, "lifecycle": "active"}
        if is_substantive(active_view):
            substantive.append(record)
    return bool(substantive) and all(item.get("lifecycle") == "superseded" for item in substantive)


def _valid_evaluation(value: object) -> Evaluation | None:
    if not isinstance(value, Evaluation):
        return None
    if (
        value.label not in SEMANTIC_LABELS
        or value.confidence not in {"high", "medium", "low"}
        or not isinstance(value.supported, bool)
        or not value.evaluator
    ):
        return None
    return value


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
    ):
        self.claim_pipeline = claim_pipeline
        self.claim_index = claim_index
        self.evaluators = None if evaluators is None else tuple(evaluators)
        self.vault = Path(vault).resolve(strict=True) if vault is not None else None
        self.coordinator = coordinator
        self.source_page = source_page

    def assess_raw(
        self,
        source: bytes,
        extraction: Mapping[str, object],
        *,
        benchmark_gate: bool = False,
    ) -> tuple[ClaimAssessment, ...]:
        if self.claim_pipeline is None:
            raise ValueError("assess_raw requires a ClaimPipeline")
        assessments = []
        for block in self.claim_pipeline.split_blocks(source):
            block_extraction = extraction
            block_id = getattr(block, "block_id", None)
            if block_id in extraction and isinstance(extraction[block_id], Mapping):
                block_extraction = extraction[block_id]
            for raw_claim in self.claim_pipeline.extract(block, block_extraction):
                verified = self.claim_pipeline.verify_literal(raw_claim)
                normalized = self.claim_pipeline.normalize(verified)
                assessments.append(
                    self.assess(normalized, benchmark_gate=benchmark_gate)
                )
        return tuple(assessments)

    def assess(
        self,
        claim: NormalizedClaim,
        *,
        candidates: Sequence[IndexedClaim] | None = None,
        benchmark_gate: bool = False,
    ) -> ClaimAssessment:
        if not isinstance(claim, NormalizedClaim):
            raise TypeError("claim must be normalized")
        if candidates is None:
            if self.claim_index is None:
                candidates = ()
            else:
                candidates = self.claim_index.candidates(claim)
        candidates = tuple(candidates)
        outcomes: list[tuple[str, LifecycleDecision, tuple[Evaluation, ...]]] = []
        for existing in candidates:
            classification = deterministic_class(claim, existing)
            evaluations: tuple[Evaluation, ...] = ()
            if classification is None:
                evaluations = self._evaluate_semantic(claim, existing)
                classification = (
                    evaluations[0].label
                    if len(evaluations) == 2
                    and evaluations[0].label == evaluations[1].label
                    and all(item.supported and item.confidence == "high" for item in evaluations)
                    else "unresolved"
                )
            if evaluations and benchmark_gate and classification == "compatible":
                decision = LifecycleDecision(
                    "keep-both", reason="calibrated semantic compatibility"
                )
            elif evaluations and benchmark_gate and classification == "refinement":
                decision = LifecycleDecision(
                    "refine", reason="calibrated semantic refinement"
                )
            else:
                decision = apply_policy(
                    claim,
                    existing,
                    evaluations,
                    deterministic=classification if not evaluations else None,
                )
            outcomes.append((classification, decision, evaluations))

        if not outcomes:
            classification = "no-candidate"
            decision = LifecycleDecision("keep-both", reason="no candidate")
            evaluations = ()
        else:
            priority = {"quarantine": 3, "supersede": 2, "refine": 1, "keep-both": 0}
            classification, decision, evaluations = max(
                outcomes, key=lambda item: priority[item[1].recommendation]
            )
        candidate_path = None
        if self.coordinator is not None and (
            decision.recommendation == "quarantine" or decision.mutations
        ):
            candidate_path = self._commit(claim, decision)
        evidence = tuple(
            {
                "page": item.page,
                "claim_id": item.claim.record["id"],
                "evidence": item.claim.record["evidence"],
            }
            for item in candidates
        )
        return ClaimAssessment(
            claim,
            classification,
            decision.recommendation,
            evidence,
            {"interval": claim.record["validity"], "status": "verified"},
            decision.mutations,
            candidate_path,
            evaluations,
        )

    def _evaluate_semantic(
        self, claim: NormalizedClaim, existing: IndexedClaim
    ) -> tuple[Evaluation, ...]:
        evaluators = (
            tuple(self._provider_evaluators())
            if self.evaluators is None
            else self.evaluators
        )
        if not evaluators:
            return ()
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

    def _provider_evaluators(self) -> Sequence[Callable[..., object]]:
        descriptors = [
            item
            for item in provider_candidates(os.environ.get("MEMORY_LLM_PROVIDER", ""), max_tokens=800)
            if probe_candidate(item)
        ]
        if not descriptors:
            return ()
        selected = descriptors[:2] if len(descriptors) > 1 else descriptors

        def make(descriptor):
            def evaluate(claim, existing, *, critique, prior_label=None):
                payload = {
                    "new_claim": claim.record,
                    "existing_claim": existing.claim.record,
                    "existing_page": existing.page,
                }
                if critique:
                    payload["label_to_critique"] = prior_label
                prompt = canonical_json_bytes(payload).decode("utf-8")
                system = (
                    "Blindly critique the supplied label using only the two claims and their literal evidence. "
                    if critique
                    else "Classify the two claims using only their literal evidence. "
                ) + "Do not propose or perform lifecycle mutations."
                result = call_candidate(
                    descriptor, prompt, system, max_tokens=800,
                    schema=EVALUATION_SCHEMA, available=True,
                )
                if result.text is None:
                    return None
                try:
                    value = json.loads(result.text)
                    validate_schema(value, EVALUATION_SCHEMA)
                    return Evaluation(
                        value["label"], value["confidence"], value["supported"],
                        descriptor.identity,
                    )
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    return None
            return evaluate

        return tuple(make(item) for item in selected)

    def _commit(
        self, claim: NormalizedClaim, decision: LifecycleDecision
    ) -> str | None:
        if self.vault is None or self.coordinator is None:
            raise ValueError("candidate writes require a vault and coordinator")
        path = None
        candidate = None
        changes = []
        if decision.recommendation == "quarantine":
            quarantined = NormalizedClaim({**claim.record, "lifecycle": "quarantined"})
            validate_claim_record(quarantined.record)
            candidate = {
                "schema_version": "claim-candidate/v1",
                "status": "quarantined",
                "reason": decision.reason or "manual review required",
                "claim": quarantined.record,
                "source_page": self.source_page,
                "created_at": claim.record["observed_at"],
            }
            validate_schema(candidate, CANDIDATE_SCHEMA)
            path = f"knowledge/inbox/claims/{claim.record['fingerprint']}.md"
            content = (
                "---\ntype: claim-candidate\nstatus: quarantined\n---\n"
                f"# Quarantined claim {claim.record['id']}\n\n"
                "```json\n" + canonical_json_bytes(candidate).decode("utf-8") + "\n```\n"
            ).encode("utf-8")
            changes.append(MarkdownChange.create(path, content))
        changes.extend(self._lifecycle_changes(decision.mutations))
        operation_id = "contradiction:" + sha256_bytes(
            canonical_json_bytes(
                {"candidate": candidate, "mutations": [list(item) for item in decision.mutations]}
            )
        )
        transaction = self.coordinator.prepare(changes, operation_id=operation_id)
        self.coordinator.apply(transaction.id)
        return path

    def _lifecycle_changes(
        self, mutations: Sequence[tuple[str, str]]
    ) -> list[MarkdownChange]:
        grouped: dict[str, set[str]] = {}
        for path, claim_id in mutations:
            grouped.setdefault(path, set()).add(claim_id)
        changes = []
        for path, ids in grouped.items():
            target = self.vault / path
            raw = target.read_bytes()
            match = _CLAIMS_RE.search(raw)
            if match is None:
                raise ValueError("lifecycle target has no canonical claim ledger")
            ledger = json.loads(match[2])
            found = set()
            for record in ledger["claims"]:
                if record["id"] in ids:
                    record["lifecycle"] = "superseded"
                    found.add(record["id"])
            if found != ids:
                raise ValueError("lifecycle target claim is missing")
            encoded = canonical_json_bytes(ledger)
            after = raw[: match.start(2)] + encoded + raw[match.end(2) :]
            changes.append(MarkdownChange.replace(path, after))
        return changes


def run_frozen_benchmark(corpus: Mapping[str, object]) -> BenchmarkMetrics:
    """Run the deterministic frozen corpus; fake provider metadata is mandatory."""
    if corpus.get("provider") != "fake":
        raise ValueError("frozen contradiction benchmark requires the fake provider")
    cases = corpus.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("frozen contradiction benchmark has no cases")
    extraction_matches = []
    predicted_classes = []
    gold_classes = []
    predicted_lifecycle = []
    gold_lifecycle = []
    provenance = 0
    retrieved = retrievable = 0
    negative = false_superseded = published = published_errors = quarantined = 0
    for case in cases:
        new = NormalizedClaim(case["new_claim"])
        existing = IndexedClaim(case["existing_page"], NormalizedClaim(case["existing_claim"]))
        result = ContradictionPipeline(evaluators=()).assess(
            new, candidates=[existing]
        )
        predicted_classes.append(result.contradiction_class)
        gold_classes.append(case["expected_class"])
        predicted_lifecycle.append(result.recommendation)
        gold_lifecycle.append(case["expected_lifecycle"])
        literal = case["literal_evidence"]
        extraction_matches.append(
            literal["new"] == new.record["text"]
            and literal["existing"] == existing.claim.record["text"]
        )
        if case["retrievable"]:
            retrievable += 1
            retrieved += int(
                existing.claim.record["subject"] == new.record["subject"]
                or existing.claim.record["relation"] == new.record["relation"]
            )
        try:
            validate_claim_record(new.record)
            validate_claim_record(existing.claim.record)
            provenance += int(case["expected_provenance_valid"] is True)
        except (TypeError, ValueError):
            provenance += int(case["expected_provenance_valid"] is False)
        if case["negative_control"]:
            negative += 1
            false_superseded += int(result.recommendation == "supersede")
        is_quarantined = result.recommendation == "quarantine"
        quarantined += int(is_quarantined)
        if not is_quarantined:
            published += 1
            published_errors += int(
                predicted_classes[-1] != gold_classes[-1]
                or predicted_lifecycle[-1] != gold_lifecycle[-1]
            )
    total = len(cases)
    return BenchmarkMetrics(
        sum(extraction_matches) / total,
        retrieved / retrievable if retrievable else 1,
        _macro_f1(gold_classes, predicted_classes),
        _macro_f1(gold_lifecycle, predicted_lifecycle),
        provenance / total,
        published_errors / published if published else 0,
        false_superseded / negative if negative else 0,
        published / total,
    )


def _macro_f1(gold: Sequence[str], predicted: Sequence[str]) -> float:
    labels = sorted(set(gold) | set(predicted))
    scores = []
    for label in labels:
        true_positive = sum(g == label and p == label for g, p in zip(gold, predicted))
        false_positive = sum(g != label and p == label for g, p in zip(gold, predicted))
        false_negative = sum(g == label and p != label for g, p in zip(gold, predicted))
        denominator = 2 * true_positive + false_positive + false_negative
        scores.append(2 * true_positive / denominator if denominator else 0)
    return sum(scores) / len(scores) if scores else 0


def assess_text(claim: str) -> dict[str, object]:
    """Return a fail-closed structured result for the MCP string compatibility API."""
    if not isinstance(claim, str) or not claim.strip():
        raise ValueError("claim must be a non-empty string")
    return {
        "assessments": [
            {
                "claim": claim.strip(),
                "contradiction_class": "unresolved",
                "recommendation": "quarantine",
            }
        ],
        "evidence": [],
        "validity": {"status": "unverified", "reason": "string input has no literal evidence reference"},
        "recommendations": ["quarantine"],
    }
