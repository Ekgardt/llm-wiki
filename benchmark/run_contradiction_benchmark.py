"""Generate and execute the frozen deterministic contradiction benchmark."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from contradiction_pipeline import run_frozen_benchmark  # noqa: E402
from reliable_memory import (  # noqa: E402
    canonical_json_bytes,
    sha256_bytes,
    validate_schema,
)

SCHEMA = ROOT / "benchmark/contradiction-v1.schema.json"


def _claim(case_id: str, value: str, *, relation: str = "has-state", start: str | None = "2026-01-01", end: str | None = None, authority: str = "user") -> dict[str, object]:
    semantic = {"subject": f"subject-{case_id}", "relation": relation, "value": {"type": "string", "value": value}, "qualifiers": [], "validity": {"from": start, "to": end}}
    text = f"subject-{case_id} {relation} {value}"
    return {
        "schema_version": "claim/v1", "id": case_id, "fingerprint": sha256_bytes(canonical_json_bytes(semantic)), "text": text,
        **semantic, "observed_at": "2026-01-01T12:00:00Z", "lifecycle": "active", "confidence": "high", "authority": authority,
        "evidence": {"reference": f"daily:2026-01-01 sha256:{'a' * 64} block:12:00:00 bytes:0-1", "sha256": sha256_bytes(text.encode()), "text": text},
        "links": [], "extractor_version": "benchmark/v1",
    }


def build_corpus() -> dict[str, object]:
    cases = []
    categories = ["equality", "interval", "functional-relation", "authority-lifecycle", "keep-both-refine", "quarantine"]
    for category in categories:
        for index in range(40):
            case_id = f"{category}-{index:02d}"
            old = _claim(case_id, "blue", authority="web")
            new = _claim(case_id, "blue")
            expected_class, expected_lifecycle = "equivalent", "keep-both"
            negative = True
            if category == "interval":
                old = _claim(case_id, "blue", start="2026-01-01", end="2026-02-01")
                new = _claim(case_id, "red", start="2026-02-01")
                expected_class = "temporal-distinct"
            elif category == "functional-relation":
                new = _claim(case_id, "red", authority="user")
                expected_class, expected_lifecycle, negative = "contradiction", "supersede", False
            elif category == "authority-lifecycle":
                old = _claim(case_id, "blue", authority="user")
                new = _claim(case_id, "red", authority="inferred")
                expected_class, expected_lifecycle = "contradiction", "quarantine"
            elif category == "keep-both-refine":
                if index < 20:
                    old = _claim(case_id, "blue", relation="uses")
                    new = _claim(case_id, "red", relation="uses")
                    expected_class = "compatible"
                else:
                    old = _claim(case_id, "blue", start=None)
                    new = _claim(case_id, "blue", start="2026-02-01")
                    expected_class, expected_lifecycle = "refinement", "refine"
            elif category == "quarantine":
                old = _claim(case_id, "blue", relation="depends-on")
                new = _claim(case_id, "blue", relation="uses")
                expected_class, expected_lifecycle = "unresolved", "quarantine"
            cases.append({
                "id": case_id, "category": category, "new_claim": new, "existing_claim": old,
                "existing_page": f"knowledge/notes/{case_id}.md", "literal_evidence": {"new": new["text"], "existing": old["text"]},
                "retrievable": True, "expected_class": expected_class, "expected_lifecycle": expected_lifecycle,
                "expected_provenance_valid": True, "negative_control": negative,
            })
    return {
        "version": "contradiction-v1", "provider": "fake", "categories": categories,
        "relation_metadata": {"functional": ["equals", "has-state", "has-value", "located-at", "starts-at", "ends-at"], "non_functional": ["member-of", "uses", "depends-on"]},
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, default=ROOT / "benchmark/contradiction-v1.json")
    parser.add_argument("--generate", action="store_true")
    args = parser.parse_args()
    if args.generate:
        corpus = build_corpus()
        validate_schema(corpus, SCHEMA)
        args.corpus.write_bytes(canonical_json_bytes(corpus) + b"\n")
        return 0
    raw = args.corpus.read_bytes()
    corpus = json.loads(raw)
    validate_schema(corpus, SCHEMA)
    if canonical_json_bytes(corpus) + b"\n" != raw:
        raise ValueError("corpus is not restricted canonical JSON")
    metrics = run_frozen_benchmark(corpus)
    print(json.dumps(metrics.canonical(), sort_keys=True, indent=2))
    gates = (
        metrics.extraction_f1 == 1 and metrics.candidate_recall == 1
        and metrics.class_macro_f1 == 1 and metrics.lifecycle_macro_f1 == 1
        and metrics.provenance_correctness == 1 and metrics.quarantine_risk == 0
        and metrics.false_supersession <= 0.01
    )
    return 0 if gates else 1


if __name__ == "__main__":
    raise SystemExit(main())
