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


def test_frozen_corpus_is_closed_canonical_and_has_required_coverage():
    raw = CORPUS.read_bytes()
    corpus = json.loads(raw)
    validate_schema(corpus, SCHEMA)
    assert canonical_json_bytes(corpus) + b"\n" == raw
    assert len(corpus["cases"]) >= 240
    categories = Counter(case["category"] for case in corpus["cases"])
    assert all(categories[name] >= 40 for name in corpus["categories"])
    assert sum(case["negative_control"] for case in corpus["cases"]) >= 200


def test_frozen_benchmark_meets_every_exact_gate():
    from contradiction_pipeline import run_frozen_benchmark

    metrics = run_frozen_benchmark(json.loads(CORPUS.read_text(encoding="utf-8")))
    assert metrics.extraction_f1 == 1
    assert metrics.candidate_recall == 1
    assert metrics.class_macro_f1 == 1
    assert metrics.lifecycle_macro_f1 == 1
    assert metrics.provenance_correctness == 1
    assert metrics.quarantine_risk == 0
    assert metrics.false_supersession <= 0.01
    assert 0 <= metrics.quarantine_coverage <= 1
