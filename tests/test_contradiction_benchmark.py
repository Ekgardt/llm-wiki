from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from reliable_memory import canonical_json_bytes, validate_schema

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "benchmark/contradiction-v1.json"
SCHEMA = ROOT / "benchmark/contradiction-v1.schema.json"


_CLAIM_KEYS = ("new_extraction", "expected_new_claim", "existing_claim")


def _validity_bounds(corpus: dict) -> list:
    """Every `from`/`to` of every claim of every case, in one flat list."""
    return [
        case[claim_key]["validity"][bound]
        for case in corpus["cases"]
        for claim_key in _CLAIM_KEYS
        for bound in ("from", "to")
    ]


def _category_floor(corpus: dict) -> int:
    counts = Counter(case["category"] for case in corpus["cases"])
    return min(counts[name] for name in corpus["categories"])


def test_frozen_corpus_is_closed_canonical_and_has_required_coverage():
    raw = CORPUS.read_bytes()
    corpus = json.loads(raw)
    validate_schema(corpus, SCHEMA)
    negative_controls = sum(case["negative_control"] for case in corpus["cases"])

    assert (
        canonical_json_bytes(corpus) + b"\n" == raw,
        {type(value) for value in _validity_bounds(corpus)},
        len(corpus["cases"]) >= 240,
        _category_floor(corpus) >= 40,
        negative_controls >= 200,
    ) == (True, {str, type(None)}, True, True, True)


def test_frozen_benchmark_meets_every_exact_gate():
    from contradiction_pipeline import run_frozen_benchmark

    metrics = run_frozen_benchmark(json.loads(CORPUS.read_text(encoding="utf-8"))).canonical()
    exact = {
        "extraction_f1": 1,
        "candidate_recall": 1,
        "class_macro_f1": 1,
        "lifecycle_macro_f1": 1,
        "provenance_correctness": 1,
        "quarantine_risk": 0,
        "semantic_primary_calls": 40,
        "semantic_critique_calls": 40,
        "semantic_fallback_probes": 80,
        "semantic_evaluators_independent": True,
        "semantic_benchmark_gate": False,
        "quarantine_candidates": 80,
        "quarantine_notes_published": 0,
    }

    assert {name: metrics[name] for name in exact} == exact
    assert (metrics["false_supersession"] <= 0.01, 0 <= metrics["quarantine_coverage"] <= 1) == (True, True)


def test_benchmark_executes_real_extraction_resolver_and_claim_index(monkeypatch):
    import claims
    import contradiction_pipeline
    import evidence_resolver

    calls = {"split": 0, "verify": 0, "rebuild": 0, "candidates": 0, "resolve": 0}
    original_split = claims.ClaimPipeline.split_blocks
    original_verify = claims.ClaimPipeline.verify_literal
    original_rebuild = claims.ClaimIndex.rebuild
    original_candidates = claims.ClaimIndex.candidates
    original_resolve = evidence_resolver.EvidenceResolver.resolve

    def split(self, source):
        calls["split"] += 1
        return original_split(self, source)

    def verify(self, item, reference=None):
        calls["verify"] += 1
        return original_verify(self, item, reference)

    def rebuild(self, sources=None):
        calls["rebuild"] += 1
        return original_rebuild(self, sources)

    def candidates(self, item):
        calls["candidates"] += 1
        return original_candidates(self, item)

    def resolve(self, reference):
        calls["resolve"] += 1
        return original_resolve(self, reference)

    monkeypatch.setattr(claims.ClaimPipeline, "split_blocks", split)
    monkeypatch.setattr(claims.ClaimPipeline, "verify_literal", verify)
    monkeypatch.setattr(claims.ClaimIndex, "rebuild", rebuild)
    monkeypatch.setattr(claims.ClaimIndex, "candidates", candidates)
    monkeypatch.setattr(evidence_resolver.EvidenceResolver, "resolve", resolve)

    contradiction_pipeline.run_frozen_benchmark(
        json.loads(CORPUS.read_text(encoding="utf-8"))
    )

    assert all(value > 0 for value in calls.values())
