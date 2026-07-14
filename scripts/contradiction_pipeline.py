"""Fail-closed contradiction assessment for evidence-verified atomic claims."""
from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from bounded_io import read_stable_bytes
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
from llm_client import call_candidate, probe_candidate, provider_candidates
from markdown_transaction import MarkdownChange, MarkdownCoordinator
from reliable_memory import canonical_json_bytes, sha256_bytes, validate_schema

AUTHORITY = {"inferred": 0, "ai-derived": 1, "web": 2, "user": 3}
FUNCTIONAL_RELATIONS = frozenset(
    {"equals", "has-state", "has-value", "located-at", "starts-at", "ends-at"}
)
SEMANTIC_LABELS = frozenset({"contradiction", "compatible", "refinement"})
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
    evaluation_lineage: tuple[dict[str, object], ...] = ()

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
    if not _same_qualifier_scope(new, old):
        return None
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
        secondary_search: Callable[[str, int], Sequence[Mapping[str, object]]] | None = None,
    ):
        self.claim_pipeline = claim_pipeline
        self.claim_index = claim_index
        self.evaluators = None if evaluators is None else tuple(evaluators)
        self.vault = Path(vault).resolve(strict=True) if vault is not None else None
        self.coordinator = coordinator
        self.source_page = source_page
        self.secondary_search = secondary_search
        if self.secondary_search is None and self.vault is not None:
            self.secondary_search = lambda query, limit: default_secondary_search(
                self.vault, query, limit
            )

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
        commit: bool = True,
    ) -> ClaimAssessment:
        if not isinstance(claim, NormalizedClaim):
            raise TypeError("claim must be normalized")
        if candidates is None:
            if self.claim_index is None:
                candidates = ()
            else:
                candidates = self.claim_index.candidates(claim)
        candidates = tuple(candidates)
        retrieval_context: tuple[Mapping[str, object], ...] = ()
        if not candidates and self.secondary_search is not None:
            retrieval_context = tuple(self.secondary_search(str(claim.record["text"]), 5))[:5]
        outcomes: list[tuple[str, LifecycleDecision, tuple[Evaluation, ...]]] = []
        evaluation_lineage: list[dict[str, object]] = []
        for existing in candidates:
            classification = deterministic_class(claim, existing)
            evaluations: tuple[Evaluation, ...] = ()
            if classification is None:
                evaluations, lineage = self._evaluate_semantic(claim, existing)
                evaluation_lineage.extend(lineage)
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

        if retrieval_context:
            classification = "unresolved"
            decision = LifecycleDecision(
                "quarantine", reason="retrieval-only context has no verified claim ledger"
            )
            evaluations = ()
        elif not outcomes:
            classification = "no-candidate"
            decision = LifecycleDecision("keep-both", reason="no candidate")
            evaluations = ()
        else:
            actionable = {
                item[1].recommendation
                for item in outcomes
                if item[1].recommendation != "keep-both"
            }
            if len(actionable) > 1:
                classification = "unresolved"
                decision = LifecycleDecision(
                    "quarantine", reason="candidate recommendations conflict"
                )
                evaluations = tuple(
                    evaluation for item in outcomes for evaluation in item[2]
                )
            elif actionable == {"quarantine"}:
                quarantined = next(
                    item for item in outcomes if item[1].recommendation == "quarantine"
                )
                classification, decision, evaluations = quarantined
            elif actionable == {"supersede"}:
                superseding = [item for item in outcomes if item[1].recommendation == "supersede"]
                classification = "contradiction"
                decision = LifecycleDecision(
                    "supersede",
                    tuple(
                        sorted(
                            {
                                mutation
                                for item in superseding
                                for mutation in item[1].mutations
                            }
                        )
                    ),
                    "all authoritative overlapping conflicts",
                )
                evaluations = ()
            elif actionable == {"refine"}:
                classification = "refinement"
                decision = LifecycleDecision("refine", reason="all refinements agree")
                evaluations = ()
            else:
                classification, decision, evaluations = outcomes[0]
        candidate_path = None
        if commit and self.coordinator is not None and (
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
        ) + tuple(
            {
                "page": str(item.get("path", "")),
                "title": str(item.get("title", "")),
                "snippet": str(item.get("snippet", item.get("summary", ""))),
                "retrieval_only": True,
            }
            for item in retrieval_context
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
            tuple(evaluation_lineage),
        )

    def _evaluate_semantic(
        self, claim: NormalizedClaim, existing: IndexedClaim
    ) -> tuple[tuple[Evaluation, ...], tuple[dict[str, object], ...]]:
        if self.evaluators is None:
            return self._evaluate_with_providers(claim, existing)
        evaluators = self.evaluators
        if not evaluators:
            return (), ()
        first = _valid_evaluation(
            evaluators[0](claim, existing, critique=False, prior_label=None)
        )
        if first is None:
            return (), ()
        second_evaluator = evaluators[1] if len(evaluators) > 1 else evaluators[0]
        second = _valid_evaluation(
            second_evaluator(claim, existing, critique=True, prior_label=first.label)
        )
        return ((first,) if second is None else (first, second)), ()

    def _evaluate_with_providers(
        self, claim: NormalizedClaim, existing: IndexedClaim
    ) -> tuple[tuple[Evaluation, ...], tuple[dict[str, object], ...]]:
        descriptors = provider_candidates(
            os.environ.get("MEMORY_LLM_PROVIDER", ""), max_tokens=800
        )
        first, first_descriptor, first_lineage = self._provider_stage(
            "primary", descriptors, claim, existing, prior_label=None
        )
        if first is None or first_descriptor is None:
            return (), tuple(first_lineage)
        critique_order = [
            item for item in descriptors if item.identity != first_descriptor.identity
        ] + [first_descriptor]
        second, _second_descriptor, second_lineage = self._provider_stage(
            "critique", critique_order, claim, existing, prior_label=first.label
        )
        lineage = tuple(first_lineage + second_lineage)
        return ((first,) if second is None else (first, second)), lineage

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
        payload = {
            "new_claim": claim.record,
            "existing_claim": existing.claim.record,
            "existing_page": existing.page,
        }
        if prior_label is not None:
            payload["label_to_critique"] = prior_label
        prompt = canonical_json_bytes(payload).decode("utf-8")
        system = (
            "Blindly critique the supplied label using only the two claims and their literal evidence. "
            if prior_label is not None
            else "Classify the two claims using only their literal evidence. "
        ) + "Do not propose or perform lifecycle mutations."
        for descriptor in descriptors:
            canonical = descriptor.canonical()
            if not probe_candidate(descriptor):
                lineage.append(
                    {"descriptor": canonical, "stage": f"{stage}.probe", "failure": "unavailable"}
                )
                continue
            result = call_candidate(
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
                continue
            try:
                encoded = result.text.encode("utf-8", errors="strict")
                if len(encoded) > MAX_SEMANTIC_OUTPUT_BYTES:
                    raise ValueError("output_too_large")
                value = json.loads(encoded)
                if (
                    not isinstance(value, dict)
                    or set(value) != {"label", "confidence", "supported"}
                    or value["label"] not in SEMANTIC_LABELS
                    or value["confidence"] not in {"high", "medium", "low"}
                    or not isinstance(value["supported"], bool)
                ):
                    raise ValueError("malformed_output")
            except (TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
                failure = "output_too_large" if str(exc) == "output_too_large" else "malformed_output"
                lineage.append(
                    {"descriptor": canonical, "stage": f"{stage}.parse", "failure": failure}
                )
                continue
            lineage.append(
                {"descriptor": canonical, "stage": f"{stage}.parse", "failure": None}
            )
            return (
                Evaluation(
                    value["label"],
                    value["confidence"],
                    value["supported"],
                    descriptor.identity,
                ),
                descriptor,
                lineage,
            )
        return None, None, lineage

    def _commit(
        self, claim: NormalizedClaim, decision: LifecycleDecision
    ) -> str | None:
        if self.vault is None or self.coordinator is None:
            raise ValueError("candidate writes require a vault and coordinator")
        assessment = ClaimAssessment(
            claim,
            "unresolved",
            decision.recommendation,
            (),
            {"interval": claim.record["validity"], "status": "verified"},
            decision.mutations,
            None,
        )
        changes, preconditions, candidate_paths = self.plan_changes((assessment,))
        path = candidate_paths[0] if candidate_paths else None
        candidate_identity = path or "none"
        operation_id = "contradiction:" + sha256_bytes(
            canonical_json_bytes(
                {
                    "candidate": candidate_identity,
                    "mutations": [list(item) for item in decision.mutations],
                }
            )
        )
        with self.coordinator.writer_gate():
            if path is not None:
                self.ensure_candidate_parent()
            transaction = self.coordinator.prepare(
                changes, operation_id=operation_id, preconditions=preconditions
            )
            self.coordinator.apply(transaction.id)
        if self.claim_index is not None and decision.mutations:
            self.claim_index.rebuild()
        return path

    def plan_changes(
        self, assessments: Sequence[ClaimAssessment]
    ) -> tuple[list[MarkdownChange], dict[str, str], tuple[str, ...]]:
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
        if self.vault is None or self.coordinator is None:
            raise ValueError("candidate parent creation requires a vault and coordinator")
        if not self.coordinator.writer_gate_held():
            raise RuntimeError("candidate parent creation requires writer ownership")
        current = self.vault
        for part in ("knowledge", "inbox", "claims"):
            current = current / part
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                os.mkdir(current, 0o700)
                metadata = current.lstat()
            if (
                current.is_symlink()
                or getattr(metadata, "st_file_attributes", 0) & 0x400
                or not stat.S_ISDIR(metadata.st_mode)
            ):
                raise PermissionError("candidate parent must be a regular directory")
        return current

    def _lifecycle_changes(
        self, mutations: Sequence[tuple[str, str]]
    ) -> tuple[list[MarkdownChange], dict[str, str]]:
        grouped: dict[str, set[str]] = {}
        for path, claim_id in mutations:
            grouped.setdefault(path, set()).add(claim_id)
        changes = []
        preconditions = {}
        for path, ids in grouped.items():
            target = self.vault / path
            raw = read_stable_bytes(
                target, MAX_CLAIM_PAGE_BYTES, label="claim lifecycle page"
            )
            preconditions[path] = sha256_bytes(raw)
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
            if page_is_superseded(ledger["claims"]):
                after = _mark_page_superseded(after, self.source_page)
            changes.append(
                MarkdownChange.replace(
                    path, after, max_before_bytes=MAX_CLAIM_PAGE_BYTES
                )
            )
        return changes, preconditions


def _mark_page_superseded(content: bytes, source_page: str) -> bytes:
    text = content.decode("utf-8", errors="strict")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ValueError("lifecycle target has no canonical frontmatter")
    frontmatter, body = text[4:].split("\n---\n", 1)
    lines = [
        line
        for line in frontmatter.splitlines()
        if not line.startswith(("status:", "superseded_by:"))
    ]
    slug = Path(source_page).stem
    lines.extend(("status: superseded", f"superseded_by: [[{slug}]]"))
    return ("---\n" + "\n".join(lines) + "\n---\n" + body).encode("utf-8")


def default_secondary_search(
    root: Path, query: str, limit: int
) -> Sequence[Mapping[str, object]]:
    from search_memory import search

    return search(query, limit=max(0, min(limit, 5)))


def run_frozen_benchmark(corpus: Mapping[str, object]) -> BenchmarkMetrics:
    """Run extraction, provenance, retrieval, classification, and policy end to end."""
    if corpus.get("provider") != "fake":
        raise ValueError("frozen contradiction benchmark requires the fake provider")
    cases = corpus.get("cases")
    source_text = corpus.get("source")
    if not isinstance(cases, list) or not cases or not isinstance(source_text, str):
        raise ValueError("frozen contradiction benchmark has no cases")
    from evidence_resolver import EvidenceResolver

    with tempfile.TemporaryDirectory(prefix="llm-wiki-contradiction-") as temporary:
        vault = Path(temporary) / "vault"
        state_root = Path(temporary) / "state"
        for relative in (
            "knowledge/daily",
            "knowledge/notes",
            "knowledge/projects",
        ):
            (vault / relative).mkdir(parents=True)
        state_root.mkdir()
        source = source_text.encode("utf-8")
        (vault / "knowledge/daily/2026-01-01.md").write_bytes(source)
        for case in cases:
            ledger = {
                "schema_version": "claim-ledger/v1",
                "claims": [case["existing_claim"]],
            }
            (vault / case["existing_page"]).write_bytes(
                b"---\ntype: concept\n---\n# Benchmark\n\n## Claims\n```json\n"
                + canonical_json_bytes(ledger)
                + b"\n```\n"
            )

        resolver = EvidenceResolver(vault, state_root=state_root)
        extraction_pipeline = ClaimPipeline(resolver)
        blocks = {
            block.block_id: block for block in extraction_pipeline.split_blocks(source)
        }
        extracted: dict[str, NormalizedClaim] = {}
        true_positive = 0
        for case in cases:
            raw_claims = extraction_pipeline.extract(
                blocks[case["block_id"]],
                {
                    "schema_version": "claim-extraction/v1",
                    "claims": [case["new_extraction"]],
                },
            )
            for raw_claim in raw_claims:
                normalized = extraction_pipeline.normalize(
                    extraction_pipeline.verify_literal(raw_claim)
                )
                extracted[str(case["id"])] = normalized
                true_positive += int(
                    canonical_json_bytes(normalized.record)
                    == canonical_json_bytes(case["expected_new_claim"])
                )

        index = ClaimIndex(state_root, vault=vault)
        index.rebuild()
        assessment_pipeline = ContradictionPipeline(
            claim_index=index, evaluators=(), vault=vault
        )
        predicted_classes = []
        gold_classes = []
        predicted_lifecycle = []
        gold_lifecycle = []
        provenance = 0
        retrieved = retrievable = 0
        negative = false_superseded = published = published_errors = 0
        for case in cases:
            new = extracted[str(case["id"])]
            result = assessment_pipeline.assess(new, commit=False)
            predicted_classes.append(result.contradiction_class)
            gold_classes.append(case["expected_class"])
            predicted_lifecycle.append(result.recommendation)
            gold_lifecycle.append(case["expected_lifecycle"])
            if case["retrievable"]:
                retrievable += 1
                retrieved += int(
                    case["existing_page"]
                    in {item["page"] for item in result.evidence}
                )
            provenance_valid = True
            try:
                for record in (new.record, case["existing_claim"]):
                    validate_claim_record(record)
                    evidence = record["evidence"]
                    resolved = resolver.resolve(evidence["reference"])
                    provenance_valid = provenance_valid and (
                        resolved.sha256 == evidence["sha256"]
                        and resolved.bytes.decode("utf-8", errors="strict")
                        == evidence["text"]
                    )
            except (TypeError, ValueError):
                provenance_valid = False
            provenance += int(
                provenance_valid is bool(case["expected_provenance_valid"])
            )
            if case["negative_control"]:
                negative += 1
                false_superseded += int(result.recommendation == "supersede")
            is_quarantined = result.recommendation == "quarantine"
            if not is_quarantined:
                published += 1
                published_errors += int(
                    predicted_classes[-1] != gold_classes[-1]
                    or predicted_lifecycle[-1] != gold_lifecycle[-1]
                )
        total = len(cases)
        extraction_precision = true_positive / len(extracted) if extracted else 0
        extraction_recall = true_positive / total
        extraction_f1 = (
            2 * extraction_precision * extraction_recall
            / (extraction_precision + extraction_recall)
            if extraction_precision + extraction_recall
            else 0
        )
        return BenchmarkMetrics(
            extraction_f1,
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
    """Assess JSON claims or retrieve context for unsupported plain-text evidence."""
    if not isinstance(claim, str) or not claim.strip():
        raise ValueError("claim must be a non-empty string")
    from memory_state import ROOT, STATE_ROOT

    root = ROOT
    state_root = STATE_ROOT
    verified = False
    try:
        decoded = json.loads(claim)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, dict) and decoded.get("schema_version") == "claim/v1":
        validate_claim_record(decoded)
        from evidence_resolver import EvidenceResolver

        evidence = decoded["evidence"]
        resolved = EvidenceResolver(root, state_root=state_root).resolve(
            evidence["reference"]
        )
        if (
            resolved.sha256 != evidence["sha256"]
            or resolved.bytes.decode("utf-8", errors="strict") != evidence["text"]
        ):
            raise ValueError("claim evidence does not match resolved bytes")
        normalized = NormalizedClaim(decoded)
        verified = True
    else:
        normalized = _plain_text_query_claim(claim.strip())
    index = ClaimIndex(state_root, vault=root)
    pipeline = ContradictionPipeline(
        claim_index=index,
        secondary_search=lambda query, limit: default_secondary_search(
            root, query, limit
        ),
    )
    assessment = pipeline.assess(normalized)
    canonical = assessment.canonical()
    if not verified:
        canonical["recommendation"] = "quarantine"
        canonical["lifecycle_mutations"] = []
        canonical["candidate_path"] = None
        validity = {
            "status": "unsupported-evidence",
            "reason": "plain string input has no literal evidence reference",
        }
        recommendations = ["quarantine"]
    else:
        validity = assessment.validity
        recommendations = [assessment.recommendation]
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
